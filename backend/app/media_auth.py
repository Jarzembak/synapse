"""Interactive, project-scoped browser authentication for URL media.

A normal browser popup cannot expose another site's HttpOnly cookies to
Synapse.  The optional Docker auth-browser instead runs an isolated WebDriver
session that the user controls through noVNC.  This module keeps the control
channel private, exports only cookies applicable to the submitted/final URL,
and destroys the temporary browser profile after capture.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlencode, urlsplit
from urllib.request import Request, urlopen

from .config import settings
from .security import redact_urls, validate_source_url
from .task_names import MEDIA_AUTH_LEASE_TASK
from .tasks import media


SESSION_FILE = ".browser-auth-session.json"
AUTH_CONTEXT_FILE = ".browser-auth.json"
COOKIE_FILE = "cookies.txt"
AUTH_CONTEXT_SCHEMA = 1
# Public compatibility name used by the API and tests.  The canonical value
# lives in a dependency-free module so worker startup does not import the
# browser-auth implementation (or its task helpers).
LEASE_TASK = MEDIA_AUTH_LEASE_TASK
_LOCK = threading.RLock()


class MediaAuthError(RuntimeError):
    """Base class for user-facing interactive-auth failures."""


class MediaAuthUnavailable(MediaAuthError):
    pass


class MediaAuthBusy(MediaAuthError):
    pass


class MediaAuthSessionMissing(MediaAuthError):
    pass


class MediaAuthSessionGone(MediaAuthSessionMissing):
    """The remote browser confirms that the session no longer exists."""


def cookies_path(project_slug: str) -> Path:
    return media.workdir(project_slug) / COOKIE_FILE


def auth_context_path(project_slug: str) -> Path:
    return media.workdir(project_slug) / AUTH_CONTEXT_FILE


def auth_session_path(project_slug: str) -> Path:
    return media.workdir(project_slug) / SESSION_FILE


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}.{threading.get_ident()}")
    try:
        tmp.write_bytes(payload)
        try:
            tmp.chmod(0o600)
        except OSError:
            pass
        tmp.replace(path)
        try:
            path.chmod(0o600)
        except OSError:
            pass
    finally:
        tmp.unlink(missing_ok=True)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    _atomic_write(
        path,
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"),
    )


def _source_digest(source_url: str) -> str:
    return hashlib.sha256(source_url.encode("utf-8")).hexdigest()


def _webdriver_base() -> str:
    value = settings.auth_browser_webdriver_url.strip().rstrip("/")
    if not value:
        raise MediaAuthUnavailable(
            "interactive login is not configured; start the auth-browser service"
        )
    return value


def _webdriver_request(
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    *,
    timeout: float = 30,
) -> Any:
    body = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(
        f"{_webdriver_base()}{path}",
        data=body,
        headers=headers,
        method=method,
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except HTTPError as exc:
        detail = ""
        error_code = ""
        try:
            value = json.loads(exc.read().decode("utf-8", errors="replace"))
            error = value.get("value", value)
            if isinstance(error, dict):
                error_code = str(error.get("error") or "")
                detail = str(error.get("message") or error.get("error") or "")
        except (ValueError, TypeError):
            pass
        if error_code.casefold() == "invalid session id":
            raise MediaAuthSessionGone(
                "the authentication browser session is already gone"
            ) from exc
        raise MediaAuthError(
            redact_urls(detail) if detail
            else f"authentication browser returned HTTP {exc.code}"
        ) from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise MediaAuthUnavailable(
            "authentication browser is unavailable; check the auth-browser container"
        ) from exc

    if not raw:
        return None
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except ValueError as exc:
        raise MediaAuthError("authentication browser returned invalid JSON") from exc
    value = decoded.get("value", decoded) if isinstance(decoded, dict) else decoded
    if isinstance(value, dict) and value.get("error"):
        raise MediaAuthError(
            redact_urls(str(
                value.get("message") or value.get("error") or "browser command failed"
            ))
        )
    return value


def _webdriver_ready() -> bool:
    value = _webdriver_request("GET", "/status", timeout=5)
    return bool(isinstance(value, dict) and value.get("ready"))


def _wait_webdriver_ready(timeout: float = 20) -> bool:
    """Allow the one-session container time to restart after secure teardown."""
    deadline = time.monotonic() + max(0, timeout)
    while True:
        try:
            if _webdriver_ready():
                return True
        except MediaAuthUnavailable:
            pass
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.25)


def _session_path(session_id: str, suffix: str = "") -> str:
    return f"/session/{quote(session_id, safe='')}{suffix}"


def _delete_remote_session(session_id: str) -> None:
    try:
        _webdriver_request("DELETE", _session_path(session_id), timeout=10)
    except MediaAuthSessionGone:
        # Idempotent cleanup is safe only when Selenium explicitly confirms the
        # session is absent. Connectivity and other failures must remain
        # retryable; otherwise a credential-bearing viewer could outlive its
        # local tracking record.
        return


def _load_live_session(project_slug: str) -> dict[str, Any]:
    path = auth_session_path(project_slug)
    session = _read_json(path)
    if not session:
        return {}
    expires = _parse_time(session.get("expires_at"))
    if expires is not None and expires <= _utc_now():
        session_id = str(session.get("session_id") or "")
        if session_id:
            _delete_remote_session(session_id)
        revoke_viewer_token(str(session.get("viewer_token") or ""))
        path.unlink(missing_ok=True)
        return {}
    return session


def _viewer_url(project_id: int, token: str) -> str:
    base = f"/api/projects/{project_id}/auth/browser/view/{token}/"
    websocket_path = (
        f"api/projects/{project_id}/auth/browser/view/{token}/websockify"
    )
    query = urlencode({
        "autoconnect": "1",
        "resize": "scale",
        "reconnect": "0",
        "path": websocket_path,
    })
    return f"{base}?{query}"


def viewer_session(project_id: int, project_slug: str, token: str) -> dict[str, Any]:
    """Resolve a live project-bound viewer capability without exposing IDs."""
    session = _load_live_session(project_slug)
    expected = str(session.get("viewer_token") or "")
    if (
        not session
        or int(session.get("project_id") or 0) != project_id
        or not expected
        or not hmac.compare_digest(expected, token)
    ):
        raise MediaAuthSessionMissing("authentication viewer is no longer available")
    return session


def revoke_viewer_token(token: str) -> None:
    if not token:
        return
    # Imported lazily to avoid coupling downloader workers to FastAPI/websocket
    # relay state. Revocation also remains safe during application shutdown.
    try:
        from .media_auth_view import revoke_viewer_token as revoke

        revoke(token)
    except (ImportError, RuntimeError):
        pass


def auth_status(
    project_id: int,
    project_slug: str,
    source_type: str,
) -> dict[str, Any]:
    """Return a redacted status object safe for the unauthenticated local UI."""
    session = _load_live_session(project_slug) if source_type == "url" else {}
    context = _read_json(auth_context_path(project_slug)) if source_type == "url" else {}
    cookie_file = cookies_path(project_slug)
    return {
        "available": bool(settings.auth_browser_webdriver_url.strip()),
        "applicable": source_type == "url",
        "active": bool(session),
        "browser_url": (
            _viewer_url(project_id, str(session.get("viewer_token") or ""))
            if session and session.get("viewer_token")
            else None
        ),
        "started_at": session.get("created_at") if session else None,
        "expires_at": session.get("expires_at") if session else None,
        "cookies_present": cookie_file.is_file() and cookie_file.stat().st_size > 0,
        "captured_at": context.get("captured_at"),
        "authenticated_host": context.get("authenticated_host"),
        "cookie_count": int(context.get("cookie_count") or 0),
        "project_id": project_id,
    }


def start_browser_auth(
    project_id: int,
    project_slug: str,
    source_url: str,
) -> dict[str, Any]:
    source_url = validate_source_url(
        source_url, allow_private=settings.allow_private_urls
    )
    with _LOCK:
        existing = _load_live_session(project_slug)
        if existing:
            try:
                _browser_current_url(str(existing.get("session_id") or ""))
                return auth_status(project_id, project_slug, "url")
            except MediaAuthError:
                # Do not discard the only handle to a browser that did not
                # explicitly confirm its own destruction.
                raise MediaAuthError(
                    "the previous authentication browser could not be verified; "
                    "cancel it before starting another"
                )
        if not _wait_webdriver_ready():
            raise MediaAuthBusy(
                "the authentication browser is still restarting; try again shortly"
            )

        value = _webdriver_request(
            "POST",
            "/session",
            {
                "capabilities": {
                    "alwaysMatch": {
                        "browserName": "chrome",
                        "acceptInsecureCerts": False,
                        "goog:chromeOptions": {
                            "args": [
                                "--disable-background-networking",
                                "--disable-component-update",
                                "--disable-default-apps",
                                "--disable-dev-shm-usage",
                                "--disable-features=Translate",
                                "--disable-extensions",
                                "--disable-quic",
                                "--disable-sync",
                                "--force-webrtc-ip-handling-policy="
                                "disable_non_proxied_udp",
                                "--host-resolver-rules="
                                "MAP api ~NOTFOUND,MAP redis ~NOTFOUND,"
                                "MAP worker ~NOTFOUND,MAP paper-worker ~NOTFOUND,"
                                "MAP beat ~NOTFOUND,MAP ollama ~NOTFOUND,"
                                "MAP frontend ~NOTFOUND,MAP auth-browser ~NOTFOUND,"
                                "MAP host.docker.internal ~NOTFOUND,"
                                "MAP gateway.docker.internal ~NOTFOUND,"
                                "MAP metadata.google.internal ~NOTFOUND",
                                "--no-first-run",
                                "--proxy-bypass-list=<-loopback>",
                                "--proxy-server=http://auth-egress:8899",
                                "--window-size=1440,900",
                            ],
                            "prefs": {
                                "credentials_enable_service": False,
                                "download.prompt_for_download": False,
                                "profile.default_content_setting_values.automatic_downloads": 2,
                                "profile.password_manager_enabled": False,
                            },
                        },
                    },
                },
            },
            timeout=30,
        )
        if not isinstance(value, dict):
            raise MediaAuthError("authentication browser did not create a session")
        session_id = str(value.get("sessionId") or "")
        if not session_id:
            raise MediaAuthError("authentication browser omitted its session ID")

        now = _utc_now()
        expires = now + timedelta(
            minutes=max(5, int(settings.auth_browser_session_minutes))
        )
        viewer_token = secrets.token_urlsafe(32)
        _write_json(
            auth_session_path(project_slug),
            {
                "schema": 1,
                "project_id": project_id,
                "session_id": session_id,
                "viewer_token": viewer_token,
                "source_digest": _source_digest(source_url),
                "created_at": _iso(now),
                "expires_at": _iso(expires),
            },
        )
        try:
            _webdriver_request(
                "POST",
                _session_path(session_id, "/timeouts"),
                {"pageLoad": 60_000, "script": 10_000, "implicit": 0},
                timeout=10,
            )
            _webdriver_request(
                "POST",
                _session_path(session_id, "/url"),
                {"url": source_url},
                timeout=75,
            )
        except Exception as exc:
            try:
                _delete_remote_session(session_id)
            except MediaAuthError as cleanup_exc:
                raise MediaAuthError(
                    "the sign-in page could not open and browser cleanup is "
                    "pending; cancel source access to retry cleanup"
                ) from cleanup_exc
            revoke_viewer_token(viewer_token)
            auth_session_path(project_slug).unlink(missing_ok=True)
            raise
        return auth_status(project_id, project_slug, "url")


def _related_hosts(first_url: str, second_url: str) -> bool:
    first = (urlsplit(first_url).hostname or "").lower().rstrip(".")
    second = (urlsplit(second_url).hostname or "").lower().rstrip(".")
    if not first or not second:
        return False
    return (
        first == second
        or first.endswith(f".{second}")
        or second.endswith(f".{first}")
    )


_AUTH_PATH_MARKERS = {
    "account",
    "auth",
    "authorize",
    "create-account",
    "login",
    "log-in",
    "oauth",
    "register",
    "registration",
    "session",
    "sessions",
    "signup",
    "sign-up",
    "signin",
    "sign-in",
    "sso",
}


def _looks_like_noncontent_landing(source_url: str, final_url: str) -> bool:
    source = urlsplit(source_url)
    final = urlsplit(final_url)
    source_path = source.path or "/"
    final_path = final.path or "/"
    decoded_path = final_path
    # Authentication routers commonly hide markers behind percent encoding
    # or filename suffixes such as /login.php and /session/new.
    for _ in range(2):
        expanded = unquote(decoded_path)
        if expanded == decoded_path:
            break
        decoded_path = expanded
    normalized_segments = [
        segment.casefold().replace("_", "-")
        for segment in decoded_path.split("/")
        if segment
    ]
    path_tokens = {
        token
        for segment in normalized_segments
        for token in re.split(r"[.]", segment)
        if token
    }
    if path_tokens & _AUTH_PATH_MARKERS:
        return True
    if source_path not in {"", "/"} and decoded_path in {"", "/"}:
        return True
    source_host = (source.hostname or "").casefold()
    if source_host.endswith(".zoom.us") or source_host == "zoom.us":
        return not decoded_path.casefold().startswith(("/rec/play/", "/rec/share/"))
    return False


def _zoom_recording_url(value: str) -> bool:
    parsed = urlsplit(value)
    host = (parsed.hostname or "").casefold()
    return (
        (host == "zoom.us" or host.endswith(".zoom.us"))
        and parsed.path.casefold().startswith(("/rec/play/", "/rec/share/"))
    )


def _browser_has_playable_media(session_id: str) -> bool:
    value = _webdriver_request(
        "POST",
        _session_path(session_id, "/execute/sync"),
        {
            "script": (
                "const media = document.querySelector('video, audio');"
                "return Boolean(media && (media.currentSrc || media.src || "
                "media.readyState > 0));"
            ),
            "args": [],
        },
        timeout=10,
    )
    return value is True


def _browser_current_url(session_id: str) -> str:
    value = _webdriver_request(
        "GET", _session_path(session_id, "/url"), timeout=10
    )
    if not isinstance(value, str):
        raise MediaAuthError("authentication browser did not report its current URL")
    return validate_source_url(
        value, allow_private=settings.allow_private_urls
    )


def _browser_user_agent(session_id: str) -> str:
    value = _webdriver_request(
        "POST",
        _session_path(session_id, "/execute/sync"),
        {"script": "return navigator.userAgent;", "args": []},
        timeout=10,
    )
    return value.strip() if isinstance(value, str) else ""


def _browser_cookies(
    session_id: str,
    source_url: str,
    final_url: str,
) -> list[dict[str, Any]]:
    urls = list(dict.fromkeys([source_url, final_url]))
    try:
        value = _webdriver_request(
            "POST",
            _session_path(session_id, "/goog/cdp/execute"),
            {"cmd": "Network.getCookies", "params": {"urls": urls}},
            timeout=15,
        )
        cookies = value.get("cookies") if isinstance(value, dict) else None
        if isinstance(cookies, list):
            return _cookies_for_urls(cookies, urls)
    except MediaAuthError:
        pass

    value = _webdriver_request(
        "GET", _session_path(session_id, "/cookie"), timeout=10
    )
    return _cookies_for_urls(value, urls) if isinstance(value, list) else []


def _cookies_for_urls(
    cookies: list[Any],
    urls: list[str],
) -> list[dict[str, Any]]:
    """Defense in depth: retain only cookies label-matching admitted URLs."""
    hosts = {
        (urlsplit(url).hostname or "").casefold().rstrip(".")
        for url in urls
    }
    result = []
    for item in cookies:
        if not isinstance(item, dict):
            continue
        domain = str(item.get("domain") or "").casefold().lstrip(".").rstrip(".")
        if not domain:
            continue
        if any(host == domain or host.endswith(f".{domain}") for host in hosts):
            result.append(item)
    return result


def _clean_cookie_field(value: Any) -> str:
    return str(value or "").replace("\t", "").replace("\r", "").replace("\n", "")


def _render_netscape_cookies(cookies: list[dict[str, Any]]) -> bytes:
    lines = [
        "# Netscape HTTP Cookie File",
        "# Generated by Synapse interactive login; treat this file like a password.",
    ]
    seen: set[tuple[str, str, str]] = set()
    for cookie in cookies:
        name = _clean_cookie_field(cookie.get("name"))
        domain = _clean_cookie_field(cookie.get("domain"))
        path = _clean_cookie_field(cookie.get("path") or "/") or "/"
        if not name or not domain:
            continue
        key = (domain, path, name)
        if key in seen:
            continue
        seen.add(key)
        include_subdomains = "TRUE" if domain.startswith(".") else "FALSE"
        rendered_domain = f"#HttpOnly_{domain}" if cookie.get("httpOnly") else domain
        secure = "TRUE" if cookie.get("secure") else "FALSE"
        expires = cookie.get("expires", cookie.get("expiry", 0))
        try:
            expires_int = max(0, int(float(expires or 0)))
        except (TypeError, ValueError, OverflowError):
            expires_int = 0
        lines.append(
            "\t".join(
                [
                    rendered_domain,
                    include_subdomains,
                    path,
                    secure,
                    str(expires_int),
                    name,
                    _clean_cookie_field(cookie.get("value")),
                ]
            )
        )
    return ("\n".join(lines) + "\n").encode("utf-8")


def complete_browser_auth(
    project_id: int,
    project_slug: str,
    source_url: str,
) -> dict[str, Any]:
    source_url = validate_source_url(
        source_url, allow_private=settings.allow_private_urls
    )
    with _LOCK:
        session = _load_live_session(project_slug)
        if not session:
            raise MediaAuthSessionMissing(
                "no interactive login is active for this project"
            )
        if session.get("source_digest") != _source_digest(source_url):
            raise MediaAuthError(
                "the project source changed after login started; cancel and start again"
            )
        session_id = str(session.get("session_id") or "")
        if not session_id:
            raise MediaAuthSessionMissing("authentication browser session is invalid")

        before_reload = _browser_current_url(session_id)
        zoom_token_candidate = (
            before_reload
            if _related_hosts(source_url, before_reload)
            and _zoom_recording_url(before_reload)
            and not _looks_like_noncontent_landing(source_url, before_reload)
            else ""
        )

        # Always revisit the submitted source after the user declares the login
        # complete. This lets the source consume an IdP session and prevents a
        # same-site sign-in/home tab from being mistaken for downloadable media.
        _webdriver_request(
            "POST",
            _session_path(session_id, "/url"),
            {"url": source_url},
            timeout=75,
        )
        current_url = _browser_current_url(session_id)
        invalid_landing = (
            not _related_hosts(source_url, current_url)
            or _looks_like_noncontent_landing(source_url, current_url)
        )
        if invalid_landing and zoom_token_candidate:
            # Zoom on-demand registration may grant access only through an
            # emailed /rec/play token. Preserve that current URL only after a
            # provider-aware check confirms it renders playable media.
            _webdriver_request(
                "POST",
                _session_path(session_id, "/url"),
                {"url": zoom_token_candidate},
                timeout=75,
            )
            candidate_url = _browser_current_url(session_id)
            if (
                _related_hosts(source_url, candidate_url)
                and _zoom_recording_url(candidate_url)
                and _browser_has_playable_media(session_id)
            ):
                current_url = candidate_url
                invalid_landing = False
        if invalid_landing:
            raise MediaAuthError(
                "the submitted source has not returned to its content "
                "page; finish sign-in or registration in the browser and try again"
            )

        cookies = _browser_cookies(session_id, source_url, current_url)
        user_agent = _browser_user_agent(session_id)
        now = _utc_now()
        context = {
            "schema": AUTH_CONTEXT_SCHEMA,
            "source_digest": _source_digest(source_url),
            "final_url": current_url,
            "referer": source_url,
            "user_agent": user_agent,
            "authenticated_host": urlsplit(current_url).hostname or "",
            "cookie_count": len(cookies),
            "captured_at": _iso(now),
        }
        _atomic_write(cookies_path(project_slug), _render_netscape_cookies(cookies))
        _write_json(auth_context_path(project_slug), context)
        _delete_remote_session(session_id)
        revoke_viewer_token(str(session.get("viewer_token") or ""))
        auth_session_path(project_slug).unlink(missing_ok=True)
        return auth_status(project_id, project_slug, "url")


def cancel_browser_auth(project_slug: str) -> None:
    with _LOCK:
        session = _read_json(auth_session_path(project_slug))
        session_id = str(session.get("session_id") or "")
        if session_id:
            _delete_remote_session(session_id)
        revoke_viewer_token(str(session.get("viewer_token") or ""))
        auth_session_path(project_slug).unlink(missing_ok=True)


def clear_saved_auth(project_slug: str, *, remove_cookies: bool = True) -> None:
    cancel_browser_auth(project_slug)
    auth_context_path(project_slug).unlink(missing_ok=True)
    if remove_cookies:
        cookies_path(project_slug).unlink(missing_ok=True)


def browser_auth_active(project_slug: str) -> bool:
    return bool(_load_live_session(project_slug))


def apply_media_egress(
    options: dict[str, Any],
    source_url: str,
    *,
    credentialed: bool = False,
) -> str:
    """Validate a media URL and route it through the public-only proxy.

    Compose supplies the proxy for every yt-dlp request. When saved cookies,
    headers, or a signed final URL would be attached, absence of that boundary
    is an error: credentials must never be sent through a DNS-rebindable direct
    connection. ``ALLOW_PRIVATE_URLS`` is the user's explicit opt-out for local
    development and intentionally disables this public-only restriction.
    """
    source_url = validate_source_url(
        source_url, allow_private=settings.allow_private_urls
    )
    if settings.allow_private_urls:
        return source_url

    proxy_url = settings.media_egress_proxy_url.strip()
    if not proxy_url:
        kind = "authenticated media downloads" if credentialed else "URL media retrieval"
        raise MediaAuthError(
            f"{kind} require MEDIA_EGRESS_PROXY_URL; use the Docker Compose "
            "stack or configure the guarded proxy"
        )

    try:
        parsed = urlsplit(proxy_url)
        proxy_port = parsed.port
    except ValueError as exc:
        raise MediaAuthError("MEDIA_EGRESS_PROXY_URL is malformed") from exc
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or proxy_port is None
    ):
        raise MediaAuthError("MEDIA_EGRESS_PROXY_URL is malformed")
    options["proxy"] = proxy_url
    return source_url


def apply_download_auth(
    options: dict[str, Any],
    project_slug: str,
    source_url: str,
) -> str:
    """Apply a valid project cookie jar/UA and return the authorized URL."""
    cookie_file = cookies_path(project_slug)
    context = _read_json(auth_context_path(project_slug))
    context_valid = (
        context.get("schema") == AUTH_CONTEXT_SCHEMA
        and context.get("source_digest") == _source_digest(source_url)
    )
    final_url = context.get("final_url") if context_valid else None
    if isinstance(final_url, str) and final_url:
        try:
            final_url = validate_source_url(
                final_url, allow_private=settings.allow_private_urls
            )
        except ValueError:
            final_url = None
    else:
        final_url = None

    cookie_present = cookie_file.is_file() and cookie_file.stat().st_size > 0
    effective_url = final_url or source_url
    apply_media_egress(
        options,
        effective_url,
        credentialed=cookie_present or final_url is not None,
    )
    if cookie_present:
        options["cookiefile"] = str(cookie_file)

    if final_url is None:
        return effective_url

    headers = dict(options.get("http_headers") or {})
    user_agent = context.get("user_agent")
    referer = context.get("referer")
    if isinstance(user_agent, str) and user_agent.strip():
        headers["User-Agent"] = user_agent.strip()
    if isinstance(referer, str) and referer.strip():
        headers["Referer"] = validate_source_url(
            referer, allow_private=settings.allow_private_urls
        )
    if headers:
        options["http_headers"] = headers
    return effective_url
