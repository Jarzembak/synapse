"""Session-scoped HTTP/WebSocket relay for the private noVNC sidecar.

The Selenium container's viewer is never published on a host port.  A
high-entropy capability stored with the project session gates every noVNC
asset and WebSocket connection, and WebSocket origins must match the Synapse
origin exactly.  Revocation actively closes attached relays.
"""
from __future__ import annotations

import asyncio
import threading
from collections import defaultdict

import httpx
import websockets
from fastapi import APIRouter, HTTPException, Request, WebSocket
from fastapi.responses import Response

from . import media_auth
from .config import settings
from .db import get_session
from .models import Project
from .trusted_origin import require_trusted_frontend, trusted_viewer_websocket


router = APIRouter(prefix="/api/projects", tags=["projects"])
_VIEWERS: dict[str, set[tuple[WebSocket, asyncio.AbstractEventLoop]]] = defaultdict(set)
_VIEWERS_LOCK = threading.RLock()
_MAX_ASSET_BYTES = 16 * 1024 * 1024


def _viewer_project(project_id: int, token: str) -> Project:
    with get_session() as session:
        project = session.get(Project, project_id)
        if (
            project is None
            or project.deleting
            or project.source_type != "url"
        ):
            raise HTTPException(404, "authentication viewer is no longer available")
        try:
            media_auth.viewer_session(project.id, project.slug, token)
        except media_auth.MediaAuthError as exc:
            raise HTTPException(404, str(exc)) from exc
        return project


def _safe_asset_path(value: str) -> str:
    parts = value.replace("\\", "/").split("/")
    if any(part in {".", ".."} or "\x00" in part for part in parts):
        raise HTTPException(400, "invalid authentication-viewer asset path")
    return "/".join(part for part in parts if part)


def revoke_viewer_token(token: str) -> None:
    """Revoke a capability and close every attached relay, from any thread."""
    if not token:
        return
    with _VIEWERS_LOCK:
        viewers = list(_VIEWERS.pop(token, set()))
    for websocket, loop in viewers:
        try:
            asyncio.run_coroutine_threadsafe(
                websocket.close(code=1008, reason="authentication session ended"),
                loop,
            )
        except RuntimeError:
            pass


@router.get(
    "/{project_id}/auth/browser/view/{token}/{asset_path:path}",
    include_in_schema=False,
)
async def browser_view_asset(
    project_id: int,
    token: str,
    asset_path: str,
    request: Request,
):
    require_trusted_frontend(request)
    _viewer_project(project_id, token)
    safe_path = _safe_asset_path(asset_path)
    base = settings.auth_browser_view_url.strip().rstrip("/")
    if not base:
        raise HTTPException(503, "authentication viewer is not configured")
    upstream_url = f"{base}/{safe_path}" if safe_path else f"{base}/"
    try:
        async with httpx.AsyncClient(
            follow_redirects=False,
            timeout=httpx.Timeout(15, connect=5),
            trust_env=False,
        ) as client:
            upstream = await client.get(
                upstream_url,
                params=list(request.query_params.multi_items()),
                headers={"Accept": request.headers.get("accept", "*/*")},
            )
    except httpx.HTTPError as exc:
        raise HTTPException(
            503, "authentication viewer is restarting; try reopening it"
        ) from exc
    if len(upstream.content) > _MAX_ASSET_BYTES:
        raise HTTPException(502, "authentication viewer returned an oversized asset")
    headers = {
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
    }
    for name in ("content-type", "etag", "last-modified"):
        if name in upstream.headers:
            headers[name] = upstream.headers[name]
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=headers,
    )


async def _client_to_upstream(websocket: WebSocket, upstream) -> None:
    while True:
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            return
        payload = message.get("bytes")
        if payload is None:
            payload = message.get("text")
        if payload is not None:
            await upstream.send(payload)


async def _upstream_to_client(websocket: WebSocket, upstream) -> None:
    async for payload in upstream:
        if isinstance(payload, bytes):
            await websocket.send_bytes(payload)
        else:
            await websocket.send_text(payload)


async def _close_viewer_at_expiry(
    websocket: WebSocket,
    expires_at: object,
) -> None:
    """Close an already-attached viewer at its immutable session deadline."""
    expires = media_auth._parse_time(expires_at)
    if expires is None:
        await websocket.close(code=1008, reason="viewer session expired")
        return
    delay = max(0.0, (expires - media_auth._utc_now()).total_seconds())
    if delay:
        await asyncio.sleep(delay)
    await websocket.close(code=1008, reason="viewer session expired")


@router.websocket(
    "/{project_id}/auth/browser/view/{token}/websockify",
)
async def browser_view_socket(
    websocket: WebSocket,
    project_id: int,
    token: str,
):
    if not trusted_viewer_websocket(websocket):
        await websocket.close(code=1008, reason="viewer origin rejected")
        return
    try:
        project = _viewer_project(project_id, token)
        viewer_session = media_auth.viewer_session(project.id, project.slug, token)
    except HTTPException:
        await websocket.close(code=1008, reason="viewer session expired")
        return
    except media_auth.MediaAuthError:
        await websocket.close(code=1008, reason="viewer session expired")
        return

    base = settings.auth_browser_view_url.strip().rstrip("/")
    upstream_url = (
        ("wss://" if base.startswith("https://") else "ws://")
        + base.split("://", 1)[-1]
        + "/websockify"
    )
    loop = asyncio.get_running_loop()
    await websocket.accept()
    with _VIEWERS_LOCK:
        _VIEWERS[token].add((websocket, loop))
    try:
        async with websockets.connect(
            upstream_url,
            origin=base,
            compression=None,
            max_size=None,
            open_timeout=10,
            proxy=None,
        ) as upstream:
            tasks = {
                asyncio.create_task(_client_to_upstream(websocket, upstream)),
                asyncio.create_task(_upstream_to_client(websocket, upstream)),
                asyncio.create_task(
                    _close_viewer_at_expiry(
                        websocket, viewer_session.get("expires_at")
                    )
                ),
            }
            done, pending = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*done, *pending, return_exceptions=True)
    except Exception:
        # The one-session Selenium container intentionally restarts after
        # teardown, which closes the upstream socket during normal revocation.
        pass
    finally:
        with _VIEWERS_LOCK:
            viewers = _VIEWERS.get(token)
            if viewers is not None:
                viewers.discard((websocket, loop))
                if not viewers:
                    _VIEWERS.pop(token, None)
        try:
            await websocket.close()
        except RuntimeError:
            pass
