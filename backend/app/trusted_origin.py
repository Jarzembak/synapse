"""Trusted frontend/origin boundary for authenticated-media browser controls.

The API is not published by the production Compose stack.  Requests reach it
through the frontend proxy, which overwrites ``X-Synapse-Public-Origin`` with
the configured canonical Synapse origin.  These helpers deliberately never
derive trust from the client-controlled Host or X-Forwarded-Host headers.
"""
from __future__ import annotations

import hmac
from urllib.parse import urlsplit

from fastapi import HTTPException, Request, WebSocket
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from .config import settings


TRUSTED_ORIGIN_HEADER = "x-synapse-public-origin"
TRUSTED_REQUEST_HOST_HEADER = "x-synapse-request-host"
_HEALTH_PATH = "/api/health"


def normalize_origin(value: str) -> str:
    """Return a browser-style HTTP(S) origin or reject non-origin URLs."""
    raw = value.strip()
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("invalid origin") from exc
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError("expected an HTTP(S) origin without a path")

    try:
        hostname = parsed.hostname.encode("idna").decode("ascii").casefold()
    except UnicodeError as exc:
        raise ValueError("invalid origin hostname") from exc
    if ":" in hostname:
        hostname = f"[{hostname}]"

    scheme = parsed.scheme.casefold()
    if port is not None and not (
        (scheme == "http" and port == 80)
        or (scheme == "https" and port == 443)
    ):
        hostname = f"{hostname}:{port}"
    return f"{scheme}://{hostname}"


def configured_public_origin() -> str:
    try:
        return normalize_origin(settings.synapse_public_origin)
    except ValueError as exc:
        raise RuntimeError(
            "SYNAPSE_PUBLIC_ORIGIN must be one canonical HTTP(S) origin"
        ) from exc


def validate_public_origin() -> None:
    """Fail startup rather than silently disabling the browser trust boundary."""
    configured_public_origin()


def _origin_matches(value: str | None, expected: str) -> bool:
    if not value:
        return False
    try:
        candidate = normalize_origin(value)
    except ValueError:
        return False
    return hmac.compare_digest(candidate, expected)


def _authority_matches(value: str | None, expected_origin: str) -> bool:
    """Compare a proxy-attested request authority to the configured origin.

    The fixed public-origin header proves that a request traversed the bundled
    frontend, but on its own it cannot distinguish an attacker-controlled Host
    accepted by that frontend.  nginx/Vite therefore overwrite a second header
    with the incoming request authority.  Reconstructing an origin with the
    configured scheme gives default ports and IPv6 the same normalization as
    ``normalize_origin``.
    """
    if not value:
        return False
    expected = urlsplit(expected_origin)
    try:
        candidate = normalize_origin(f"{expected.scheme}://{value.strip()}")
    except ValueError:
        return False
    return hmac.compare_digest(
        urlsplit(candidate).netloc,
        expected.netloc,
    )


def _trusted_proxy_headers(headers: Headers) -> str | None:
    expected = configured_public_origin()
    if not _origin_matches(headers.get(TRUSTED_ORIGIN_HEADER), expected):
        return None
    if not _authority_matches(
        headers.get(TRUSTED_REQUEST_HOST_HEADER),
        expected,
    ):
        return None
    return expected


class TrustedFrontendMiddleware:
    """Reject direct or rebound HTTP access to every data-bearing API route."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        path = str(scope.get("path") or "")
        protected = path == "/api" or path.startswith("/api/")
        if (
            scope["type"] == "http"
            and protected
            and path != _HEALTH_PATH
            and _trusted_proxy_headers(Headers(scope=scope)) is None
        ):
            response = JSONResponse(
                {"detail": "request did not pass through the trusted frontend"},
                status_code=403,
                headers={"Cache-Control": "no-store"},
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


def require_trusted_frontend(
    request: Request,
    *,
    state_changing: bool = False,
) -> str:
    """Require the frontend proxy and, for mutations, its browser Origin.

    Read-only requests may omit ``Origin`` because browsers commonly do so for
    same-origin GETs.  If they send it, it must still match.  Browser mutations
    must always carry the exact configured origin.
    """
    expected = _trusted_proxy_headers(request.headers)
    if expected is None:
        raise HTTPException(403, "request did not pass through the trusted frontend")

    browser_origin = request.headers.get("origin")
    if state_changing and not browser_origin:
        raise HTTPException(403, "browser Origin header is required")
    if browser_origin and not _origin_matches(browser_origin, expected):
        raise HTTPException(403, "browser origin rejected")
    return expected


def trusted_viewer_websocket(websocket: WebSocket) -> bool:
    """Validate a viewer socket without consulting Host/forwarded-host."""
    expected = _trusted_proxy_headers(websocket.headers)
    return bool(
        expected
        and _origin_matches(websocket.headers.get("origin"), expected)
    )
