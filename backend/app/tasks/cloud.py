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

import logging
import os
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from sqlmodel import Session, select, text

log = logging.getLogger("synapse.cloud")

from .. import library
from ..config import settings
from ..db import get_session
from ..models import Artifact, Job, RepositorySource
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


@contextmanager
def _remote_lock():
    """Serialize cloud mutations across API/worker processes."""
    path = settings.db_path.parent / ".cloud-remote.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _record(status: str, detail: str) -> None:
    set_setting("cloud.last_sync", {
        "status": status, "detail": detail,
        "at": datetime.now(timezone.utc).isoformat(),
    })


def _restricted_artifact_for_path(path: str, session: Session | None = None) -> bool:
    """Whether a library/media path is forbidden from cloud publication."""
    lookup = path.removeprefix("media:") if path.startswith("media:") else path
    if session is None:
        with get_session() as owned:
            return _restricted_artifact_for_path(path, owned)
    artifacts = session.exec(select(Artifact)).all()
    return any(
        library.artifact_is_cloud_excluded(session, artifact) and (
            artifact.path == lookup
            or artifact.media_path == path
            or artifact.media_path == lookup
        )
        for artifact in artifacts
    )


def _restricted_library_paths() -> list[str]:
    with get_session() as session:
        artifacts = [
            artifact for artifact in session.exec(select(Artifact)).all()
            if library.artifact_is_cloud_excluded(session, artifact)
        ]
    paths: set[str] = set()
    for artifact in artifacts:
        paths.add(artifact.path)
        paths.add(f".history/{artifact.path}.*")
        if artifact.media_path and not artifact.media_path.startswith("media:"):
            paths.add(artifact.media_path)
    return sorted(paths)


def _restricted_remote_paths() -> tuple[list[str], list[str]]:
    """Return remote-relative library/history and archived-media deletions."""
    with get_session() as session:
        artifacts = [
            artifact for artifact in session.exec(select(Artifact)).all()
            if library.artifact_is_cloud_excluded(session, artifact)
        ]
    library_paths: set[str] = set()
    media_paths: set[str] = set()
    for artifact in artifacts:
        artifact_path = _safe_relative_path(artifact.path)
        if artifact_path:
            library_paths.add(artifact_path)
            library_paths.add(f".history/{artifact_path}.*")
        if artifact.media_path:
            if artifact.media_path.startswith("media:"):
                media_path = _safe_relative_path(
                    artifact.media_path.removeprefix("media:"))
                if media_path:
                    media_paths.add(media_path)
            else:
                media_path = _safe_relative_path(artifact.media_path)
                if media_path:
                    library_paths.add(media_path)
    return sorted(library_paths), sorted(media_paths)


def _delete_remote_matches(subdir: str, paths: list[str]) -> None:
    for start in range(0, len(paths), 100):
        args = ["delete", _dest(subdir)]
        for path in paths[start:start + 100]:
            args.extend(["--include", f"/{path}"])
        _rclone(args)


def enqueue_pending_privacy_purges() -> int:
    """Re-dispatch the durable privacy outbox after process restarts."""
    if not get_setting("cloud.provider"):
        return 0
    with get_session() as session:
        source_ids = session.exec(select(RepositorySource.id).where(
            RepositorySource.cloud_purge_pending == True  # noqa: E712
        )).all()
    queued = 0
    for source_id in source_ids:
        try:
            queued += int(enqueue_privacy_purge(source_id))
        except Exception:
            log.exception("could not requeue cloud privacy purge for source %s", source_id)
    return queued


def enqueue_privacy_purge(source_id: int) -> bool:
    if not get_setting("cloud.provider"):
        return False
    celery.send_task("cloud_purge_restricted", args=[source_id])
    return True


def _safe_relative_path(value: str) -> str | None:
    if not value or "\\" in value or "\x00" in value:
        return None
    pure = PurePosixPath(value)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        return None
    return pure.as_posix()


def _copy_snapshot(source: Path, destination: Path) -> None:
    """Take an immutable byte copy while the privacy writer lock is held."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def _stage_public_path(path: str) -> tuple[Path, str] | None:
    """Snapshot one cloud-eligible file under the SQLite privacy writer lock."""
    media = path.startswith("media:")
    raw = path.removeprefix("media:") if media else path
    rel = _safe_relative_path(raw)
    if rel is None:
        return None
    root = settings.media_dir if media else settings.library_dir
    stage_base = root / ".staging" / "cloud-paths"
    stage_base.mkdir(parents=True, exist_ok=True)
    stage_dir = Path(tempfile.mkdtemp(prefix="upload-", dir=stage_base))
    staged = stage_dir / PurePosixPath(rel).name
    try:
        with get_session() as session:
            session.exec(text("BEGIN IMMEDIATE"))
            if _restricted_artifact_for_path(path, session):
                session.rollback()
                shutil.rmtree(stage_dir, ignore_errors=True)
                return None
            source = (root / Path(*PurePosixPath(rel).parts)).resolve()
            try:
                source.relative_to(root.resolve())
            except ValueError:
                session.rollback()
                shutil.rmtree(stage_dir, ignore_errors=True)
                return None
            if not source.is_file() or source.is_symlink():
                session.rollback()
                shutil.rmtree(stage_dir, ignore_errors=True)
                return None
            _copy_snapshot(source, staged)
            session.rollback()
        return staged, rel
    except Exception:
        shutil.rmtree(stage_dir, ignore_errors=True)
        raise


def _stage_public_library() -> Path:
    """Build a stable public-only byte snapshot before a network full sync.

    `BEGIN IMMEDIATE` serializes this short local snapshot with artifact
    publication. The network upload happens after releasing SQLite, so private
    files created during a long rclone run can never enter its source tree.
    """
    stage_base = settings.library_dir / ".staging" / "cloud-full"
    stage_base.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix="library-", dir=stage_base))
    try:
        with get_session() as session:
            session.exec(text("BEGIN IMMEDIATE"))
            public_artifacts = [
                artifact for artifact in session.exec(select(Artifact)).all()
                if not library.artifact_is_cloud_excluded(session, artifact)
            ]
            allowed_exact: set[str] = set()
            history_prefixes: list[str] = []
            for artifact in public_artifacts:
                allowed_exact.add(artifact.path)
                history_prefixes.append(f".history/{artifact.path}.")
                if artifact.media_path and not artifact.media_path.startswith("media:"):
                    allowed_exact.add(artifact.media_path)

            for rel in sorted(allowed_exact):
                safe_rel = _safe_relative_path(rel)
                if safe_rel is None:
                    continue
                source = settings.library_dir / Path(*PurePosixPath(safe_rel).parts)
                if source.is_symlink() or not source.is_file():
                    continue
                _copy_snapshot(source, stage / Path(*PurePosixPath(safe_rel).parts))
            history_root = settings.library_dir / ".history"
            if history_root.is_dir() and not history_root.is_symlink():
                for source in history_root.rglob("*"):
                    if source.is_symlink() or not source.is_file():
                        continue
                    rel = source.relative_to(settings.library_dir).as_posix()
                    if any(rel.startswith(prefix) for prefix in history_prefixes):
                        _copy_snapshot(source, stage / Path(*PurePosixPath(rel).parts))
            session.rollback()
        return stage
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise


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
            snapshot = _stage_public_path(p)
            if snapshot is None:
                skipped += 1
                log.info("cloud sync skipped missing, unsafe, or restricted path %s", p)
                continue
            src, rel = snapshot
            try:
                dest = _dest(f"media/{rel}" if p.startswith("media:") else f"library/{rel}")
                with _remote_lock():
                    if _restricted_artifact_for_path(p):
                        skipped += 1
                        continue
                    _rclone(["copyto", str(src), dest])
                    uploaded += 1
            finally:
                shutil.rmtree(src.parent, ignore_errors=True)
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
    """Full backfill: entire library + archived source media."""
    with get_session() as session:
        set_job(session, job_id, status="running", progress="uploading library")
    staged_library: Path | None = None
    remote_lock = None
    try:
        remote_lock = _remote_lock()
        remote_lock.__enter__()
        staged_library = _stage_public_library()
        # Preserve user-authored or remote-only notes; privacy removals use the
        # targeted durable purge task rather than destructive mirror semantics.
        _rclone(["copy", str(staged_library), _dest("library")])
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
    finally:
        if remote_lock is not None:
            remote_lock.__exit__(None, None, None)
        if staged_library:
            shutil.rmtree(staged_library, ignore_errors=True)


@celery.task(
    name="cloud_purge_restricted",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_jitter=True,
    max_retries=8,
)
def purge_restricted(source_id: int):
    """Remove every sticky-private artifact from the configured remote."""
    if not get_setting("cloud.provider"):
        raise RuntimeError(
            "cloud privacy purge is pending but cloud storage is not configured")
    try:
        library_paths, media_paths = _restricted_remote_paths()
        with _remote_lock():
            if library_paths:
                _delete_remote_matches("library", library_paths)
            if media_paths:
                _delete_remote_matches("media", media_paths)
        with get_session() as session:
            source = session.get(RepositorySource, source_id)
            if source:
                source.cloud_purge_pending = False
                session.add(source)
                session.commit()
        total = len(library_paths) + len(media_paths)
        _record("ok", f"privacy purge complete; removed matches for {total} path(s)")
        return {"purged": total}
    except Exception as exc:
        _record("error", f"privacy purge pending: {exc}"[:500])
        raise


@celery.task(name="cloud_privacy_purge_sweep")
def privacy_purge_sweep():
    """Periodically re-dispatch durable purge outbox entries.

    Autoretry covers a running task's transient failures; this sweep also
    repairs broker loss between committing the flag and dispatching the task.
    """
    return {"queued": enqueue_pending_privacy_purges()}
