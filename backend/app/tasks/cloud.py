"""Cloud sync via rclone.

One integration, five providers: S3-compatible (AWS/MinIO/B2/Wasabi), WebDAV
(Nextcloud/ownCloud), Google Drive, Dropbox, OneDrive. Settings (Settings →
Advanced → Cloud storage) are stored in the Settings table:

    cloud.provider     "s3" | "webdav" | "drive" | "dropbox" | "onedrive"
    cloud.config       provider-specific dict (see FIELDS below)
    cloud.remote_base  path prefix inside the remote (default "synapse")
    cloud.auto         bool — upload each artifact right after it's written
    cloud.mode         "push" (one-way, local → cloud) | "bisync" (two-way)
    cloud.bisync_state remote dest the bisync baseline was established against
    cloud.last_sync    result of the most recent sync (status endpoint)

Two-way mode syncs the *library* (the Markdown vault) in both directions with
`rclone bisync`; archived media stays push-only in both modes (large binaries,
no story for editing them remotely). After a two-way pass the SQLite index is
rebuilt from the vault so pulled/deleted documents show up in the app.

For the OAuth providers the user pastes the token JSON produced by running
`rclone authorize "drive"` (etc.) on any machine with a browser.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from celery import chain as celery_chain

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
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            tmp = Path(handle.name)
            handle.write(build_config(provider, cfg))
            handle.flush()
            os.fsync(handle.fileno())
        tmp.chmod(0o600)
        os.replace(tmp, path)
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)
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


def sync_mode() -> str:
    return get_setting("cloud.mode") or "push"


def _bisync_args(src: str, dest: str, workdir: str, resync: bool) -> list[str]:
    """Two-way sync flags, per rclone's own set-and-forget recommendation:
    resilient/recover keep an interrupted run restartable, max-lock lets a
    crashed run's lock expire, conflict-resolve keeps the newer side and
    renames the loser with a .conflict suffix instead of losing it. bisync's
    built-in --max-delete safety (default 50%) aborts a run that would mass-
    delete either side."""
    args = ["bisync", src, dest,
            "--workdir", workdir,
            "--resilient", "--recover", "--max-lock", "2m",
            "--conflict-resolve", "newer"]
    if resync:
        # Establish the baseline. --resync-mode newer matters: the default
        # (path1) would unconditionally overwrite differing cloud files with
        # the local copy — exactly wrong for "I've been editing the cloud copy
        # from another machine, now let's link them". conflict-resolve does
        # NOT apply during a resync, so the mode is the only protection.
        # (rclone falls back to local-wins on backends without modtimes.)
        args += ["--resync", "--resync-mode", "newer"]
    return args


def _bisync_fingerprint(dest: str) -> str:
    """A baseline is only valid against the exact remote it was built for.
    The dest string alone can't tell providers apart (the rclone remote is
    always named [synapse]; only s3 embeds the bucket), so fingerprint the
    whole effective remote identity — provider, its config (endpoints,
    accounts, tokens), and the destination path. Hashed so tokens never sit
    in a plaintext settings row."""
    raw = json.dumps({
        "dest": dest,
        "provider": get_setting("cloud.provider") or "",
        "config": get_setting("cloud.config") or {},
    }, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _sync_all_bisync(job_id: int) -> None:
    dest = _dest("library")
    workdir = settings.db_path.parent / "bisync-state"
    workdir.mkdir(parents=True, exist_ok=True)
    fingerprint = _bisync_fingerprint(dest)
    resync = get_setting("cloud.bisync_state") != fingerprint

    # bisync (unlike copy) requires both base directories to exist — a
    # never-pushed remote would otherwise fail the very first two-way run
    _rclone(["mkdir", dest])

    # Google Drive allows same-name duplicates, which confuse a two-way
    # baseline — fold them down BEFORE bisync looks at the remote.
    if get_setting("cloud.provider") == "drive":
        with get_session() as session:
            set_job(session, job_id, status="running", progress="de-duplicating remote")
        _rclone(["dedupe", "--dedupe-mode", "newest", dest])

    with get_session() as session:
        set_job(session, job_id, status="running",
                progress="two-way library sync" + (" (baseline)" if resync else ""))
    try:
        _rclone(_bisync_args(str(settings.library_dir), dest, str(workdir), resync))
    except RuntimeError as e:
        # bisync's lockout state ("Must run --resync to recover") means its
        # listings can't be trusted. Clear the marker so the next explicit
        # "Sync everything now" click re-baselines (newer side wins, nothing
        # deleted), and tell the operator that's what will happen.
        if "must run --resync" in str(e).lower():
            set_setting("cloud.bisync_state", None)
            raise RuntimeError(
                f"{e}\ntwo-way sync needs a fresh baseline — the next "
                "'Sync everything now' will re-establish it (the newer copy "
                "of each file wins during a baseline; nothing is deleted)")
        raise
    set_setting("cloud.bisync_state", fingerprint)

    # media stays one-way in both modes
    with get_session() as session:
        set_job(session, job_id, status="running", progress="uploading archived media")
    _rclone(["copy", str(settings.media_dir), _dest("media"),
             "--include", "/*/source_video.*", "--include", "/*/source_audio.*"])
    if get_setting("cloud.provider") == "drive":
        _rclone(["dedupe", "--dedupe-mode", "newest", _dest("media")])

    # a two-way pass may have pulled/changed/deleted vault files — rebuild the
    # SQLite index from the Markdown (prune rows whose files are gone), then
    # re-embed for semantic search (the rebuild recreates chunks without
    # embeddings, so skipping this would silently kill Hybrid search)
    with get_session() as session:
        set_job(session, job_id, status="running", progress="reindexing pulled changes")
        rebuild = Job(project_id=None, task="rebuild_library")
        session.add(rebuild)
        session.commit()
        session.refresh(rebuild)
        rebuild_id = rebuild.id
        search_id = None
        if get_setting("search.semantic_enabled", False):
            search_job = Job(project_id=None, task="rebuild_search")
            session.add(search_job)
            session.commit()
            session.refresh(search_job)
            search_id = search_job.id
    try:
        rebuild_sig = celery.signature(
            "rebuild_library", args=[rebuild_id, True], immutable=True)
        if search_id is not None:
            # chain: embeddings must rebuild AFTER the vault reindex finishes
            result = celery_chain(
                rebuild_sig,
                celery.signature("rebuild_search", args=[search_id], immutable=True),
            ).apply_async()
        else:
            result = rebuild_sig.apply_async()
        with get_session() as session:
            row = session.get(Job, rebuild_id)
            row.celery_id = getattr(getattr(result, "parent", None), "id", "") \
                or getattr(result, "id", "") or ""
            session.add(row)
            session.commit()
    except Exception:
        with get_session() as session:
            for orphan_id in filter(None, (rebuild_id, search_id)):
                row = session.get(Job, orphan_id)
                row.status = "error"
                row.error = "could not dispatch vault reindex after two-way sync"
                session.add(row)
            session.commit()
        log.warning("could not enqueue reindex after bisync", exc_info=True)


@celery.task(name="cloud_sync_paths")
def sync_paths(paths: list[str]):
    """Upload individual artifact files (library-relative or 'media:' paths)."""
    uploaded = 0
    skipped = 0
    seen: set[str] = set()
    try:
        for p in paths:
            # A retried/duplicated enqueue can contain the same sidecar or media
            # path more than once.  copyto targets a stable exact destination;
            # de-duplicating here avoids redundant uploads within this attempt.
            if p in seen:
                skipped += 1
                continue
            seen.add(p)
            if p.startswith("media:"):
                rel = p.removeprefix("media:")
                src = settings.media_dir / rel
                dest = _dest(f"media/{rel}")
            else:
                src = settings.library_dir / p
                dest = _dest(f"library/{p}")
            if not src.is_file():
                skipped += 1
                continue
            _rclone(["copyto", str(src), dest])
            uploaded += 1
        detail = f"uploaded {uploaded} file(s); skipped {skipped} file(s)"
        _record("ok", detail)
        log.info("cloud sync: %s", detail)
        return {"uploaded": uploaded, "skipped": skipped}
    except Exception as e:
        detail = (f"uploaded {uploaded} file(s); skipped {skipped} file(s); "
                  f"error: {e}")
        _record("error", detail[:500])
        log.error("cloud sync failed for %s: %s", paths, e)
        raise


@celery.task(name="cloud_sync_all")
def sync_all(job_id: int):
    """Full sync: two-way library bisync or one-way push, per cloud.mode;
    archived source media is pushed one-way either way."""
    if sync_mode() == "bisync":
        try:
            _sync_all_bisync(job_id)
            _record("ok", "two-way sync complete")
            with get_session() as session:
                set_job(session, job_id, status="done", progress="complete")
            return
        except Exception as e:
            _record("error", str(e)[:500])
            with get_session() as session:
                set_job(session, job_id, status="error", error=str(e)[:2000])
            raise
    with get_session() as session:
        set_job(session, job_id, status="running", progress="uploading library")
    try:
        _rclone(["copy", str(settings.library_dir), _dest("library")])
        with get_session() as session:
            set_job(session, job_id, status="running", progress="uploading archived media")
        # media dir holds working files too — only the archived downloads sync
        _rclone(["copy", str(settings.media_dir), _dest("media"),
                 "--include", "/*/source_video.*", "--include", "/*/source_audio.*"])
        # Google Drive allows same-name files in a folder, so an interrupted or
        # raced upload can leave duplicates. Fold them back to one (keep newest)
        # so a full sync always converges to a clean remote — self-healing.
        if get_setting("cloud.provider") == "drive":
            with get_session() as session:
                set_job(session, job_id, status="running", progress="de-duplicating remote")
            for sub in ("library", "media"):
                _rclone(["dedupe", "--dedupe-mode", "newest", _dest(sub)])
        _record("ok", "full sync complete")
        with get_session() as session:
            set_job(session, job_id, status="done", progress="complete")
    except Exception as e:
        _record("error", str(e)[:500])
        with get_session() as session:
            set_job(session, job_id, status="error", error=str(e)[:2000])
        raise
