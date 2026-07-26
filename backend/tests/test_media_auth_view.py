from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.websockets import WebSocket, WebSocketDisconnect

from app import media_auth, media_auth_view
from app.config import settings
from app.main import app
from app.trusted_origin import (
    TRUSTED_ORIGIN_HEADER,
    TRUSTED_REQUEST_HOST_HEADER,
    trusted_viewer_websocket,
)


def test_viewer_capability_is_project_bound_and_expires_with_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(settings, "media_dir", tmp_path)
    source = "https://example.com/watch"
    media_auth._write_json(
        media_auth.auth_session_path("bound-viewer"),
        {
            "schema": 1,
            "project_id": 91,
            "session_id": "driver-session",
            "viewer_token": "correct-capability",
            "source_digest": hashlib.sha256(source.encode()).hexdigest(),
            "created_at": "2099-01-01T00:00:00+00:00",
            "expires_at": "2099-01-01T00:30:00+00:00",
        },
    )

    assert media_auth.viewer_session(
        91, "bound-viewer", "correct-capability"
    )["session_id"] == "driver-session"
    with pytest.raises(media_auth.MediaAuthSessionMissing):
        media_auth.viewer_session(92, "bound-viewer", "correct-capability")
    with pytest.raises(media_auth.MediaAuthSessionMissing):
        media_auth.viewer_session(91, "bound-viewer", "wrong-capability")


def test_viewer_asset_proxy_is_token_gated_and_never_cached(
    monkeypatch: pytest.MonkeyPatch,
):
    seen: list[tuple[str, list[tuple[str, str]]]] = []

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, url, *, params, headers):
            seen.append((url, params))
            assert headers["Accept"] == "text/css"
            return httpx.Response(
                200,
                content=b"viewer-css",
                headers={"content-type": "text/css"},
            )

    monkeypatch.setattr(media_auth_view, "_viewer_project", lambda *_args: object())
    monkeypatch.setattr(media_auth_view.httpx, "AsyncClient", lambda **_kwargs: FakeClient())
    monkeypatch.setattr(
        settings, "auth_browser_view_url", "http://auth-bridge:7900"
    )

    with TestClient(app) as client:
        response = client.get(
            "/api/projects/91/auth/browser/view/capability/app/base.css",
            params={"v": "1"},
            headers={
                "Accept": "text/css",
                TRUSTED_ORIGIN_HEADER: "http://localhost:8080",
                TRUSTED_REQUEST_HOST_HEADER: "localhost:8080",
            },
        )

    assert response.status_code == 200
    assert response.content == b"viewer-css"
    assert response.headers["cache-control"] == "no-store"
    assert seen == [
        ("http://auth-bridge:7900/app/base.css", [("v", "1")])
    ]


def test_viewer_websocket_rejects_cross_origin_before_upstream_access():
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect(
                "/api/projects/91/auth/browser/view/capability/websockify",
                headers={
                    TRUSTED_ORIGIN_HEADER: "http://localhost:8080",
                    TRUSTED_REQUEST_HOST_HEADER: "localhost:8080",
                    "host": "synapse.example",
                    "origin": "https://attacker.example",
                },
            ):
                pass
    assert exc.value.code == 1008


def test_viewer_websocket_uses_configured_origin_not_host_headers(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        settings, "synapse_public_origin", "https://synapse.example/"
    )
    scope = {
        "type": "websocket",
        "asgi": {"version": "3.0"},
        "scheme": "wss",
        "path": "/",
        "query_string": b"",
        "headers": [
            (TRUSTED_ORIGIN_HEADER.encode(), b"https://synapse.example"),
            (TRUSTED_REQUEST_HOST_HEADER.encode(), b"synapse.example"),
            (b"origin", b"https://synapse.example"),
            (b"host", b"attacker.example"),
            (b"x-forwarded-host", b"attacker.example"),
        ],
        "client": ("127.0.0.1", 1234),
        "server": ("api", 8000),
        "subprotocols": [],
    }

    assert trusted_viewer_websocket(WebSocket(scope, None, None)) is True

    scope["headers"][2] = (b"origin", b"https://attacker.example")
    assert trusted_viewer_websocket(WebSocket(scope, None, None)) is False


def test_live_viewer_websocket_closes_at_session_expiry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    slug = "expiring-live-viewer"
    token = "expiring-capability"
    project_id = 93
    monkeypatch.setattr(settings, "media_dir", tmp_path)
    monkeypatch.setattr(settings, "synapse_public_origin", "http://localhost:8080")
    monkeypatch.setattr(
        settings, "auth_browser_view_url", "http://auth-bridge:7900"
    )
    media_auth._write_json(
        media_auth.auth_session_path(slug),
        {
            "schema": 1,
            "project_id": project_id,
            "session_id": "expiring-driver-session",
            "viewer_token": token,
            "source_digest": hashlib.sha256(b"https://example.com/watch").hexdigest(),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "expires_at": (
                # Leave enough time for TestClient's application startup and
                # WebSocket handshake under a loaded full-suite run.
                datetime.now(timezone.utc) + timedelta(seconds=1)
            ).isoformat(),
        },
    )
    monkeypatch.setattr(
        media_auth_view,
        "_viewer_project",
        lambda *_args: SimpleNamespace(id=project_id, slug=slug),
    )

    class FakeUpstream:
        async def send(self, _payload):
            return None

        def __aiter__(self):
            return self

        async def __anext__(self):
            await asyncio.Future()

    class FakeConnect:
        async def __aenter__(self):
            return FakeUpstream()

        async def __aexit__(self, *_args):
            return None

    monkeypatch.setattr(
        media_auth_view.websockets,
        "connect",
        lambda *_args, **_kwargs: FakeConnect(),
    )

    with TestClient(app) as client:
        with client.websocket_connect(
            f"/api/projects/{project_id}/auth/browser/view/{token}/websockify",
            headers={
                TRUSTED_ORIGIN_HEADER: "http://localhost:8080",
                TRUSTED_REQUEST_HOST_HEADER: "localhost:8080",
                "origin": "http://localhost:8080",
            },
        ) as socket:
            with pytest.raises(WebSocketDisconnect) as closed:
                socket.receive_bytes()
    assert closed.value.code == 1008


@pytest.mark.parametrize("value", ["../secret", "app/../../secret", "app\\..\\secret"])
def test_viewer_rejects_path_traversal(value: str):
    with pytest.raises(HTTPException):
        media_auth_view._safe_asset_path(value)
