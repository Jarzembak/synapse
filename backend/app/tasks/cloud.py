"""Cloud sync via rclone.

One integration, five providers: S3-compatible (AWS/MinIO/B2/Wasabi), WebDAV
(Nextcloud/ownCloud), Google Drive, Dropbox, OneDrive. Settings (Settings →
Advanced → Cloud storage) are stored in the Settings table:

    cloud.provider     "s3" | "webdav" | "drive" | "dropbox" | "onedrive"
    cloud.config       provider-specific dict (see FIELDS below)
    cloud.remote_base  path prefix inside the remote (default "synapse")
    cloud.auto         bool — upload each artifact right after it's written
    cloud.last_sync    result of the most recent sync (status endpoint)

For the OAuth providers the user pastes the token JSON produced by running
`rclone authorize "drive"` (etc.) on any machine with a browser.
"""
from __future__ import annotations

import json
import logging
import subprocess
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("synapse.cloud")

from ..config import settings
from ..db import get_session
from ..models import Job
from ..settings_store import get_setting, set_setting
from .celery_app import celery
from .common import set_job

REMOTE = "synapse"

# field name -> is_secret; drives both config generation and UI masking
FIELDS: dict[str, dict[str, bool]] = {
    "s3": {"endpoint": False, "region": False, "bucket": False,
           "access_key_id": False, "secret_access_key": True},
    "webdav": {"url": False, "vendor": False, "user": False, "password": True},
    "drive": {"token": True, "root_folder_id": False},
    "dropbox": {"token": True},
    "onedrive": {"token": True, "drive_id": False, "drive_type": False},
}


def obscure(value: str) -> str:
    """rclone stores WebDAV passwords in its own obscured form."""
    proc = subprocess.run(["rclone", "obscure", value], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"rclone obscure failed: {proc.stderr}")
    return proc.stdout.strip()


def build_config(provider: str, cfg: dict) -> str:
    """Render an rclone.conf section for the configured provider."""
    if provider == "s3":
        lines = [f"[{REMOTE}]", "type = s3", "provider = Other",
                 f"access_key_id = {cfg.get('access_key_id', '')}",
                 f"secret_access_key = {cfg.get('secret_access_key', '')}",
                 f"endpoint = {cfg.get('endpoint', '')}"]
        if cfg.get("region"):
            lines.append(f"region = {cfg['region']}")
    elif provider == "webdav":
        lines = [f"[{REMOTE}]", "type = webdav",
                 f"url = {cfg.get('url', '')}",
                 f"vendor = {cfg.get('vendor') or 'nextcloud'}",
                 f"user = {cfg.get('user', '')}",
                 f"pass = {cfg.get('_obscured_password', '')}"]
    elif provider in ("drive", "dropbox", "onedrive"):
        lines = [f"[{REMOTE}]", f"type = {provider}",
                 f"token = {cfg.get('token', '')}"]
        if provider == "drive":
            lines.append("scope = drive")
            if cfg.get("root_folder_id"):
                lines.append(f"root_folder_id = {cfg['root_folder_id']}")
        if provider == "onedrive":
            if cfg.get("drive_id"):
                lines.append(f"drive_id = {cfg['drive_id']}")
            lines.append(f"drive_type = {cfg.get('drive_type') or 'personal'}")
    else:
        raise ValueError(f"unknown cloud provider {provider!r}")
    return "\n".join(lines) + "\n"


def _conf_path() -> Path:
    provider = get_setting("cloud.provider")
    cfg = get_setting("cloud.config") or {}
    if not provider:
        raise RuntimeError("cloud storage is not configured (Settings → Advanced)")
    if provider == "webdav" and cfg.get("password"):
        cfg["_obscured_password"] = obscure(cfg["password"])
    path = settings.db_path.parent / "rclone.conf"
    path.write_text(build_config(provider, cfg), encoding="utf-8")
    path.chmod(0o600)
    return path


def _dest(sub: str) -> str:
    base = (get_setting("cloud.remote_base") or "synapse").strip("/")
    remote_path = f"{base}/{sub}".strip("/")
    provider = get_setting("cloud.provider")
    if provider == "s3":
        bucket = (get_setting("cloud.config") or {}).get("bucket", "")
        remote_path = f"{bucket}/{remote_path}"
    return f"{REMOTE}:{remote_path}"


def _rclone(args: list[str]) -> None:
    conf = _conf_path()
    proc = subprocess.run(
        ["rclone", "--config", str(conf), "--log-level", "ERROR"] + args,
        capture_output=True, text=True, timeout=3600,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"rclone failed: {proc.stderr[-1500:]}")


def _record(status: str, detail: str) -> None:
    set_setting("cloud.last_sync", {
        "status": status, "detail": detail,
        "at": datetime.now(timezone.utc).isoformat(),
    })


@celery.task(name="cloud_sync_paths")
def sync_paths(paths: list[str]):
    """Upload individual artifact files (library-relative or 'media:' paths)."""
    try:
        for p in paths:
            if p.startswith("media:"):
                rel = p.removeprefix("media:")
                src = settings.media_dir / rel
                dest = _dest(f"media/{rel}")
            else:
                src = settings.library_dir / p
                dest = _dest(f"library/{p}")
            if src.exists():
                _rclone(["copyto", str(src), dest])
        _record("ok", f"synced {len(paths)} file(s)")
        log.info("cloud sync: uploaded %d file(s)", len(paths))
    except Exception as e:
        _record("error", str(e)[:500])
        log.error("cloud sync failed for %s: %s", paths, e)
        raise


@celery.task(name="cloud_sync_all")
def sync_all(job_id: int):
    """Full backfill: entire library + archived source media."""
    with get_session() as session:
        set_job(session, job_id, status="running", progress="uploading library")
    try:
        _rclone(["copy", str(settings.library_dir), _dest("library")])
        with get_session() as session:
            set_job(session, job_id, status="running", progress="uploading archived media")
        # media dir holds working files too — only the archived downloads sync
        _rclone(["copy", str(settings.media_dir), _dest("media"),
                 "--include", "/*/source_video.*", "--include", "/*/source_audio.*"])
        _record("ok", "full sync complete")
        with get_session() as session:
            set_job(session, job_id, status="done", progress="complete")
    except Exception as e:
        _record("error", str(e)[:500])
        with get_session() as session:
            set_job(session, job_id, status="error", error=str(e)[:2000])
        raise
