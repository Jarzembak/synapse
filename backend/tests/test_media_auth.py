from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from unittest.mock import ANY
from urllib.parse import parse_qs, urljoin, urlsplit

import pytest
from fastapi.testclient import TestClient

from app import media_auth
from app.config import settings
from app.main import app
from app.security import redact_urls, validate_source_url
from app.trusted_origin import (
    TRUSTED_ORIGIN_HEADER,
    TRUSTED_REQUEST_HOST_HEADER,
)


def _source() -> str:
    return "https://sans.zoom.us/rec/share/example?token=secret"


def _session_payload(project_id: int, source: str) -> dict:
    return {
        "schema": 1,
        "project_id": project_id,
        "session_id": "webdriver-session",
        "viewer_token": "viewer-capability",
        "source_digest": hashlib.sha256(source.encode()).hexdigest(),
        "created_at": "2099-01-01T00:00:00+00:00",
        "expires_at": "2099-01-01T00:30:00+00:00",
    }


def test_viewer_url_resolves_websocket_path_from_the_origin_root():
    project_id = 41
    token = "viewer-capability"
    browser_path = media_auth._viewer_url(project_id, token)
    browser_url = urljoin("http://localhost:8080/", browser_path)
    websocket_path = parse_qs(urlsplit(browser_url).query)["path"][0]
    expected_path = (
        f"/api/projects/{project_id}/auth/browser/view/{token}/websockify"
    )

    assert websocket_path == expected_path

    resolved_websocket_url = urlsplit(urljoin(browser_url, websocket_path))
    assert resolved_websocket_url.scheme == "http"
    assert resolved_websocket_url.netloc == "localhost:8080"
    assert resolved_websocket_url.path == expected_path
    assert f"/view/{token}/api/projects/" not in resolved_websocket_url.path


def test_start_browser_auth_creates_isolated_session_and_redacts_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(settings, "media_dir", tmp_path)
    monkeypatch.setattr(settings, "auth_browser_webdriver_url", "http://driver:4444")
    calls: list[tuple[str, str, object]] = []

    def request(method, path, payload=None, **_kwargs):
        calls.append((method, path, payload))
        if path == "/status":
            return {"ready": True}
        if path == "/session":
            return {"sessionId": "session-123", "capabilities": {}}
        return None

    monkeypatch.setattr(media_auth, "_webdriver_request", request)

    status = media_auth.start_browser_auth(41, "zoom-fixture", _source())

    assert status["active"] is True
    assert status["browser_url"].startswith(
        "/api/projects/41/auth/browser/view/"
    )
    assert "viewer_token" not in status
    assert "session_id" not in status
    assert "source_url" not in status
    stored = json.loads(
        media_auth.auth_session_path("zoom-fixture").read_text(encoding="utf-8")
    )
    assert stored["session_id"] == "session-123"
    assert len(stored["viewer_token"]) >= 32
    assert "source_url" not in stored
    assert ("POST", "/session", ANY) in calls
    session_call = next(item for item in calls if item[1] == "/session")
    chrome_args = session_call[2]["capabilities"]["alwaysMatch"][
        "goog:chromeOptions"
    ]["args"]
    assert "--proxy-server=http://auth-egress:8899" in chrome_args
    assert "--proxy-bypass-list=<-loopback>" in chrome_args
    assert "--disable-quic" in chrome_args
    assert any("disable_non_proxied_udp" in arg for arg in chrome_args)
    navigation = next(item for item in calls if item[1].endswith("/url"))
    assert navigation[2] == {"url": _source()}


def test_complete_browser_auth_saves_target_cookies_final_url_and_user_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    source = _source()
    slug = "zoom-complete"
    monkeypatch.setattr(settings, "media_dir", tmp_path)
    monkeypatch.setattr(settings, "auth_browser_webdriver_url", "http://driver:4444")
    monkeypatch.setattr(
        settings, "media_egress_proxy_url", "http://auth-egress:8899"
    )
    media_auth._write_json(
        media_auth.auth_session_path(slug),
        _session_payload(42, source),
    )
    monkeypatch.setattr(
        media_auth,
        "_browser_current_url",
        lambda _session_id: "https://sans.zoom.us/rec/play/authorized?token=private",
    )
    monkeypatch.setattr(
        media_auth,
        "_browser_cookies",
        lambda *_args: [
            {
                "domain": ".zoom.us",
                "path": "/",
                "name": "zm_access",
                "value": "credential",
                "secure": True,
                "httpOnly": True,
                "expires": 4_102_444_800,
            },
        ],
    )
    monkeypatch.setattr(
        media_auth, "_browser_user_agent", lambda _session_id: "Fixture Browser"
    )
    monkeypatch.setattr(media_auth, "_webdriver_request", lambda *_args, **_kwargs: None)
    deleted: list[str] = []
    monkeypatch.setattr(
        media_auth, "_delete_remote_session", lambda session_id: deleted.append(session_id)
    )

    status = media_auth.complete_browser_auth(42, slug, source)

    assert status["active"] is False
    assert status["authenticated_host"] == "sans.zoom.us"
    assert status["cookie_count"] == 1
    assert deleted == ["webdriver-session"]
    cookie_text = media_auth.cookies_path(slug).read_text(encoding="utf-8")
    assert "#HttpOnly_.zoom.us\tTRUE\t/\tTRUE\t4102444800\tzm_access\tcredential" in cookie_text
    assert not media_auth.auth_session_path(slug).exists()

    options: dict = {}
    effective = media_auth.apply_download_auth(options, slug, source)
    assert effective == "https://sans.zoom.us/rec/play/authorized?token=private"
    assert options["proxy"] == "http://auth-egress:8899"
    assert options["cookiefile"] == str(media_auth.cookies_path(slug))
    assert options["http_headers"] == {
        "User-Agent": "Fixture Browser",
        "Referer": source,
    }


def test_authenticated_download_fails_closed_without_guarded_proxy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    slug = "proxy-required"
    source = "https://media.example.com/watch/1"
    monkeypatch.setattr(settings, "media_dir", tmp_path)
    monkeypatch.setattr(settings, "allow_private_urls", False)
    monkeypatch.setattr(settings, "media_egress_proxy_url", "")
    media_auth.cookies_path(slug).write_text(
        "# Netscape HTTP Cookie File\n.example.com\tTRUE\t/\tTRUE\t0\ta\tsecret\n",
        encoding="utf-8",
    )

    with pytest.raises(media_auth.MediaAuthError, match="MEDIA_EGRESS_PROXY_URL"):
        media_auth.apply_download_auth({}, slug, source)


def test_plain_url_retrieval_fails_closed_without_guarded_proxy(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(settings, "allow_private_urls", False)
    monkeypatch.setattr(settings, "media_egress_proxy_url", "")
    with pytest.raises(media_auth.MediaAuthError, match="MEDIA_EGRESS_PROXY_URL"):
        media_auth.apply_media_egress({}, "https://media.example.com/watch/1")


def test_download_auth_ignores_context_for_a_different_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(settings, "media_dir", tmp_path)
    monkeypatch.setattr(
        settings, "media_egress_proxy_url", "http://auth-egress:8899"
    )
    slug = "source-mismatch"
    old_source = "https://x.com/example/status/1"
    new_source = "https://x.com/example/status/2"
    media_auth._write_json(
        media_auth.auth_context_path(slug),
        {
            "schema": media_auth.AUTH_CONTEXT_SCHEMA,
            "source_digest": hashlib.sha256(old_source.encode()).hexdigest(),
            "final_url": "https://x.com/authorized",
            "user_agent": "Old browser",
        },
    )

    options: dict = {}
    assert media_auth.apply_download_auth(options, slug, new_source) == new_source
    assert "http_headers" not in options


def test_complete_refuses_to_capture_identity_provider_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    source = "https://course.example.com/lecture/1"
    slug = "unfinished-sso"
    monkeypatch.setattr(settings, "media_dir", tmp_path)
    monkeypatch.setattr(settings, "auth_browser_webdriver_url", "http://driver:4444")
    media_auth._write_json(
        media_auth.auth_session_path(slug),
        _session_payload(43, source),
    )
    monkeypatch.setattr(
        media_auth,
        "_browser_current_url",
        lambda _session_id: "https://accounts.example-idp.com/login",
    )
    monkeypatch.setattr(
        media_auth,
        "_webdriver_request",
        lambda *_args, **_kwargs: None,
    )

    with pytest.raises(media_auth.MediaAuthError, match="has not returned"):
        media_auth.complete_browser_auth(43, slug, source)

    assert media_auth.auth_session_path(slug).exists()
    assert not media_auth.cookies_path(slug).exists()


def test_complete_reloads_source_and_rejects_same_site_login_page(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    source = _source()
    slug = "same-site-login"
    monkeypatch.setattr(settings, "media_dir", tmp_path)
    media_auth._write_json(
        media_auth.auth_session_path(slug),
        _session_payload(44, source),
    )
    navigation: list[dict] = []
    monkeypatch.setattr(
        media_auth,
        "_webdriver_request",
        lambda method, path, payload=None, **_kwargs: (
            navigation.append(payload) if path.endswith("/url") else None
        ),
    )
    monkeypatch.setattr(
        media_auth,
        "_browser_current_url",
        lambda _session_id: "https://zoom.us/signin",
    )

    with pytest.raises(media_auth.MediaAuthError, match="sign-in"):
        media_auth.complete_browser_auth(44, slug, source)

    assert navigation == [{"url": source}]
    assert media_auth.auth_session_path(slug).exists()
    assert not media_auth.cookies_path(slug).exists()


def test_complete_preserves_verified_zoom_email_token_content_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    source = _source()
    slug = "zoom-email-token"
    candidate = "https://sans.zoom.us/rec/play/authorized?token=email-secret"
    monkeypatch.setattr(settings, "media_dir", tmp_path)
    media_auth._write_json(
        media_auth.auth_session_path(slug),
        _session_payload(46, source),
    )
    urls = iter(
        [
            candidate,
            "https://sans.zoom.us/rec/register/authorized",
            candidate,
        ]
    )
    monkeypatch.setattr(
        media_auth, "_browser_current_url", lambda _session_id: next(urls)
    )
    navigation: list[str] = []

    def request(_method, path, payload=None, **_kwargs):
        if path.endswith("/url"):
            navigation.append(payload["url"])
        if path.endswith("/execute/sync"):
            return True
        return None

    monkeypatch.setattr(media_auth, "_webdriver_request", request)
    monkeypatch.setattr(media_auth, "_browser_cookies", lambda *_args: [])
    monkeypatch.setattr(media_auth, "_browser_user_agent", lambda _session_id: "UA")
    monkeypatch.setattr(media_auth, "_delete_remote_session", lambda _session_id: None)

    result = media_auth.complete_browser_auth(46, slug, source)
    context = json.loads(
        media_auth.auth_context_path(slug).read_text(encoding="utf-8")
    )

    assert result["authenticated_host"] == "sans.zoom.us"
    assert context["final_url"] == candidate
    assert navigation == [source, candidate]


def test_cancel_retains_tracking_when_remote_cleanup_cannot_be_confirmed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    source = _source()
    slug = "cleanup-pending"
    monkeypatch.setattr(settings, "media_dir", tmp_path)
    path = media_auth.auth_session_path(slug)
    media_auth._write_json(path, _session_payload(45, source))
    monkeypatch.setattr(
        media_auth,
        "_delete_remote_session",
        lambda _session_id: (_ for _ in ()).throw(
            media_auth.MediaAuthUnavailable("driver unavailable")
        ),
    )

    with pytest.raises(media_auth.MediaAuthUnavailable, match="unavailable"):
        media_auth.cancel_browser_auth(slug)

    assert path.exists()
    assert json.loads(path.read_text(encoding="utf-8"))["session_id"] == (
        "webdriver-session"
    )


def test_cookie_renderer_rejects_control_characters_and_deduplicates():
    rendered = media_auth._render_netscape_cookies(
        [
            {
                "domain": ".example.com",
                "path": "/",
                "name": "session\nname",
                "value": "secret\r\nvalue",
                "secure": False,
            },
            {
                "domain": ".example.com",
                "path": "/",
                "name": "sessionname",
                "value": "duplicate",
            },
        ]
    ).decode()

    assert "\r" not in rendered
    assert rendered.count("\tsessionname\t") == 1
    assert "secretvalue" in rendered
    assert "duplicate" not in rendered


def test_cookie_capture_keeps_only_source_related_domains():
    cookies = media_auth._cookies_for_urls(
        [
            {"domain": ".zoom.us", "name": "allowed"},
            {"domain": "sans.zoom.us", "name": "also-allowed"},
            {"domain": ".example-idp.com", "name": "idp-secret"},
            {"domain": "evilzoom.us", "name": "suffix-trick"},
        ],
        [
            "https://sans.zoom.us/rec/share/example",
            "https://sans.zoom.us/rec/play/authorized",
        ],
    )
    assert [cookie["name"] for cookie in cookies] == [
        "allowed",
        "also-allowed",
    ]


def test_source_url_guard_rejects_compose_service_names():
    with pytest.raises(ValueError, match="private-network"):
        validate_source_url("http://api/api/projects")
    with pytest.raises(ValueError, match="private-network"):
        validate_source_url("https://redis/")
    for disguised_loopback in (
        "http://127.1/private",
        "http://0177.0.0.1/private",
        "http://0x7f.0.0.1/private",
        "http://2130706433/private",
    ):
        with pytest.raises(ValueError, match="private-network"):
            validate_source_url(disguised_loopback)
    assert validate_source_url("https://example.com/video") == "https://example.com/video"


@pytest.mark.parametrize(
    "landing",
    [
        "https://course.example.com/session/new",
        "https://course.example.com/login.php",
        "https://course.example.com/log%69n",
        "https://course.example.com/users/sign_in",
    ],
)
def test_generic_auth_landing_variants_are_not_accepted(landing: str):
    assert media_auth._looks_like_noncontent_landing(
        "https://course.example.com/lecture/1", landing
    )


def test_embedded_signed_urls_are_redacted_from_error_text():
    safe = redact_urls(
        "download failed for https://example.com/video?id=42&token=secret "
        "after redirect HTTPS://cdn.example.com/path#access-token"
    )
    assert "token=secret" not in safe
    assert "access-token" not in safe
    assert "https://example.com" in safe
    assert "https://cdn.example.com" in safe
    assert "/video" not in safe
    assert "/path" not in safe


def test_media_auth_routes_require_url_project_and_support_clear(
    monkeypatch: pytest.MonkeyPatch,
):
    with TestClient(app) as client:
        client.headers.update(
            {
                TRUSTED_ORIGIN_HEADER: "http://localhost:8080",
                TRUSTED_REQUEST_HOST_HEADER: "localhost:8080",
                "Origin": "http://localhost:8080",
                # These deliberately hostile values must not affect trust.
                "Host": "attacker.example",
                "X-Forwarded-Host": "attacker.example",
            }
        )
        suffix = uuid.uuid4().hex[:8]
        local = client.post(
            "/api/projects",
            json={
                "source": f"fixture-{suffix}.mp4",
                "source_type": "local",
                "title": f"Local {suffix}",
            },
        ).json()
        assert client.get(f"/api/projects/{local['id']}/auth").status_code == 409

        project = client.post(
            "/api/projects",
            json={
                "source": f"https://example.com/private-{suffix}",
                "source_type": "url",
                "title": f"URL {suffix}",
            },
        ).json()
        monkeypatch.setattr(settings, "auth_browser_webdriver_url", "http://driver:4444")
        monkeypatch.setattr(
            media_auth,
            "start_browser_auth",
            lambda project_id, slug, source: {
                **media_auth.auth_status(project_id, slug, "url"),
                "active": True,
                "browser_url": (
                    f"/api/projects/{project_id}/auth/browser/view/test-token/"
                ),
            },
        )

        started = client.post(f"/api/projects/{project['id']}/auth/browser")
        assert started.status_code == 200
        assert started.json()["browser_url"].startswith("/api/projects/")
        auth_status_response = client.get(f"/api/projects/{project['id']}/auth")
        assert auth_status_response.status_code == 200
        assert auth_status_response.headers["cache-control"] == "no-store"
        detail = client.get(f"/api/projects/{project['id']}").json()
        assert detail["media_auth_active"] is True
        assert detail["any_active"] is False
        assert all(
            job["task"] != media_auth.LEASE_TASK
            for job in client.get(
                "/api/jobs", params={"project_id": project["id"]}
            ).json()
        )

        monkeypatch.setattr(media_auth, "_delete_remote_session", lambda _session_id: None)
        media_auth._write_json(
            media_auth.auth_session_path(project["slug"]),
            _session_payload(project["id"], project["source"]),
        )
        blocked_step = client.post(f"/api/projects/{project['id']}/run/ingest")
        assert blocked_step.status_code == 409
        assert "finish or cancel" in blocked_step.json()["detail"]
        blocked_nonmedia_step = client.post(
            f"/api/projects/{project['id']}/run/summarize"
        )
        assert blocked_nonmedia_step.status_code == 409
        assert "finish or cancel" in blocked_nonmedia_step.json()["detail"]
        blocked_pipeline = client.post(
            f"/api/projects/{project['id']}/run_all",
            json={"profile": "full"},
        )
        assert blocked_pipeline.status_code == 409
        blocked_upload = client.post(
            f"/api/projects/{project['id']}/cookies",
            files={
                "file": (
                    "cookies.txt",
                    b"# Netscape HTTP Cookie File\n",
                    "text/plain",
                )
            },
        )
        assert blocked_upload.status_code == 409
        canceled = client.delete(f"/api/projects/{project['id']}/auth/browser")
        assert canceled.status_code == 200
        assert canceled.json()["active"] is False
        assert client.get(
            f"/api/projects/{project['id']}"
        ).json()["media_auth_active"] is False

        cookie_path = media_auth.cookies_path(project["slug"])
        cookie_path.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
        cleared = client.delete(f"/api/projects/{project['id']}/cookies")
        assert cleared.status_code == 200
        assert cleared.json()["cookies_present"] is False
        assert not cookie_path.exists()


def test_media_auth_api_requires_trusted_frontend_and_browser_origin(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        settings, "synapse_public_origin", "https://synapse.example"
    )
    with TestClient(app) as client:
        suffix = uuid.uuid4().hex[:8]
        trusted_proxy = {
            TRUSTED_ORIGIN_HEADER: "https://synapse.example",
            TRUSTED_REQUEST_HOST_HEADER: "synapse.example",
            "Host": "attacker.example",
            "X-Forwarded-Host": "attacker.example",
        }
        project = client.post(
            "/api/projects",
            json={
                "source": f"https://example.com/private-{suffix}",
                "source_type": "url",
                "title": f"URL {suffix}",
            },
            headers=trusted_proxy,
        ).json()
        status_path = f"/api/projects/{project['id']}/auth"
        start_path = f"{status_path}/browser"

        assert client.get(status_path).status_code == 403
        assert client.get(
            status_path,
            headers={
                TRUSTED_ORIGIN_HEADER: "https://synapse.example",
                "Origin": "https://attacker.example",
            },
        ).status_code == 403
        assert client.get(status_path, headers=trusted_proxy).status_code == 200

        # Mutations need both the proxy assertion and the exact browser Origin.
        assert client.post(start_path, headers=trusted_proxy).status_code == 403
        assert client.post(
            start_path,
            headers={
                **trusted_proxy,
                "Origin": "https://attacker.example",
            },
        ).status_code == 403
