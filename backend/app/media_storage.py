"""Safe cloud-primary lifecycle for durable audio and video payloads.

The existing cloud feature is a best-effort mirror.  This module adds the
state required for an authoritative remote copy without weakening the default
local-retention behavior:

* project policy defaults to ``keep_local``;
* only explicitly eligible, non-restricted media is inventoried;
* a remote object is not marked verified until a complete readback matches its
  local SHA-256;
* eviction is recoverably staged and is impossible before durable verification;
* a cloud-only object is restored and verified before playback or processing.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath

from sqlmodel import Session, select, text

from . import library
from .config import settings
from .db import get_session
from .models import (
    Artifact,
    Job,
    MediaLease,
    MediaObject,
    MediaStorageTarget,
    Project,
    ProjectMediaPolicy,
    utcnow,
)
from .settings_store import get_setting

KEEP_LOCAL = "keep_local"
CLOUD_PRIMARY = "cloud_primary"
POLICY_MODES = {KEEP_LOCAL, CLOUD_PRIMARY}

LOCAL = "local"
UPLOADING = "uploading"
VERIFIED = "verified"
EVICTING = "evicting"
CLOUD_ONLY = "cloud_only"
RESTORING = "restoring"
PURGING = "purging"
ERROR = "error"

TRANSIENT_STATES = {UPLOADING, EVICTING, RESTORING, PURGING}

ELIGIBLE_ARTIFACT_TYPES = {
    "source_audio",
    "source_video",
    "podcast_audio",
    "trimmed_audio",
    "paper_part_audio",
}
ACTION_TASKS = {
    "sync": "media_storage_sync",
    "evict": "media_storage_evict",
    "restore": "media_storage_restore",
    "purge": "media_storage_purge",
}


class MediaStorageError(RuntimeError):
    pass


class MediaStorageBusy(MediaStorageError):
    pass


def active_media_transition(
    session: Session, project_id: int
) -> MediaObject | None:
    """Return a durable in-progress media mutation for a project, if any."""
    return session.exec(select(MediaObject).where(
        MediaObject.project_id == project_id,
        MediaObject.state.in_(tuple(TRANSIENT_STATES)),
    )).first()


def _assert_no_media_transition(
    session: Session, project_id: int, *, action: str
) -> None:
    if active_media_transition(session, project_id):
        raise MediaStorageBusy(
            "wait for interrupted or active media storage state recovery "
            f"before {action}"
        )


def assert_no_transient_media(
    session: Session, *, action: str, project_id: int | None = None
) -> None:
    query = select(MediaObject.id).where(
        MediaObject.state.in_(tuple(TRANSIENT_STATES))
    )
    if project_id is not None:
        query = query.where(MediaObject.project_id == project_id)
    if session.exec(query).first():
        scope = "project " if project_id is not None else ""
        raise MediaStorageBusy(
            f"wait for {scope}media storage recovery before {action}")


def assert_media_storage_idle(
    session: Session, project_id: int, *, action: str
) -> None:
    active_job = session.exec(select(Job.id).where(
        Job.project_id == project_id,
        Job.task.in_(tuple(ACTION_TASKS.values())),
        Job.status.in_(("queued", "running")),
    )).first()
    if active_job:
        raise MediaStorageBusy(
            f"wait for the active media storage action before {action}")
    assert_no_transient_media(
        session, project_id=project_id, action=action)


def assert_no_live_media_leases(
    session: Session, project_id: int, *, action: str
) -> None:
    now = utcnow()
    expired = session.exec(
        select(MediaLease)
        .join(MediaObject, MediaObject.id == MediaLease.media_object_id)
        .where(
            MediaObject.project_id == project_id,
            MediaLease.expires_at <= now,
        )
    ).all()
    for lease in expired:
        session.delete(lease)
    session.flush()
    live = session.exec(
        select(MediaLease.id)
        .join(MediaObject, MediaObject.id == MediaLease.media_object_id)
        .where(
            MediaObject.project_id == project_id,
            MediaLease.expires_at > now,
        )
    ).first()
    if live:
        raise MediaStorageBusy(
            "media is currently being played or downloaded; retry "
            f"{action} after playback completes")


def delete_artifact_with_media(
    session: Session,
    artifact: Artifact,
    *,
    remote_reference_policy: str,
) -> None:
    """Delete dependent leases/media before an Artifact foreign key.

    ``block`` is the normal policy and prevents untracked cloud objects.
    ``forget`` is reserved for a caller that has already deleted each exact
    remote object under the cloud mutation lock.
    """
    if remote_reference_policy not in {"block", "forget"}:
        raise ValueError("remote_reference_policy must be block or forget")
    rows = session.exec(select(MediaObject).where(
        MediaObject.artifact_id == artifact.id
    )).all()
    if any(row.state in TRANSIENT_STATES for row in rows):
        raise MediaStorageBusy(
            "wait for media storage recovery before deleting this artifact")
    if remote_reference_policy == "block" and any(row.remote_key for row in rows):
        raise MediaStorageBusy(
            "restore and purge this artifact's remote media before deleting it")
    if any(_live_lease(session, row.id) for row in rows if row.id is not None):
        raise MediaStorageBusy(
            "media is currently being played or downloaded; retry deletion "
            "after playback completes")
    row_ids = [row.id for row in rows if row.id is not None]
    if row_ids:
        for lease in session.exec(select(MediaLease).where(
            MediaLease.media_object_id.in_(row_ids)
        )).all():
            session.delete(lease)
        session.flush()
    for row in rows:
        session.delete(row)
    session.flush()
    library.delete_search_chunks(session, artifact.id)
    session.exec(text(
        "DELETE FROM artifact_fts WHERE artifact_id=:id"
    ).bindparams(id=artifact.id))
    session.exec(text(
        "DELETE FROM artifacttag WHERE artifact_id=:id"
    ).bindparams(id=artifact.id))
    session.delete(artifact)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(4 * 1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _safe_unlink(path: Path) -> None:
    """Best-effort staging cleanup; durable state is already authoritative."""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        # Startup recovery removes unowned files from validated staging roots.
        pass


def _safe_relative(value: str) -> tuple[Path, str]:
    media_root = value.startswith("media:")
    raw = value.removeprefix("media:") if media_root else value
    if not raw or "\\" in raw or "\x00" in raw:
        raise MediaStorageError("unsafe media path")
    pure = PurePosixPath(raw)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise MediaStorageError("unsafe media path")
    root = settings.media_dir if media_root else settings.library_dir
    candidate = (root / Path(*pure.parts)).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise MediaStorageError("media path escapes its storage root") from exc
    return candidate, ("media:" if media_root else "") + pure.as_posix()


def resolve_local_path(value: str) -> Path:
    return _safe_relative(value)[0]


def _target_identity(provider: str, cfg: dict, remote_base: str) -> str:
    """Fingerprint the destination, excluding credentials that may rotate."""
    if provider != "s3":
        raise MediaStorageError(
            "cloud-primary media currently requires an S3-compatible provider; "
            "Drive, Dropbox, OneDrive, and WebDAV remain mirror-only"
        )
    stable = {
        "provider": provider,
        "remote_base": remote_base.strip("/") or "synapse",
        "endpoint": str(cfg.get("endpoint") or "").rstrip("/"),
        "region": str(cfg.get("region") or ""),
        "bucket": str(cfg.get("bucket") or ""),
    }
    if not stable["bucket"]:
        raise MediaStorageError(
            "configure an S3-compatible bucket before enabling cloud-primary media"
        )
    return hashlib.sha256(
        json.dumps(stable, sort_keys=True).encode("utf-8")
    ).hexdigest()


def assert_target_change_allowed(
    provider: str, cfg: dict, remote_base: str
) -> None:
    """Reject destination changes while verified remote objects depend on it."""
    with get_session() as session:
        cloud_primary_policy = session.exec(select(
            ProjectMediaPolicy.project_id
        ).where(ProjectMediaPolicy.mode == CLOUD_PRIMARY)).first()
        target_ids = set(session.exec(select(MediaObject.storage_target_id).where(
            MediaObject.remote_key != "",
            MediaObject.storage_target_id != None,  # noqa: E711
        )).all())
        target_ids.update(session.exec(
            select(ProjectMediaPolicy.storage_target_id).where(
                ProjectMediaPolicy.storage_target_id != None,  # noqa: E711
            )
        ).all())
        targets = session.exec(select(MediaStorageTarget).where(
            MediaStorageTarget.id.in_(target_ids)
        )).all() if target_ids else []
    try:
        proposed = _target_identity(
            provider, cfg, remote_base.strip("/") or "synapse")
    except MediaStorageError as exc:
        if target_ids or cloud_primary_policy:
            raise MediaStorageError(
                "cloud target settings must remain S3-compatible while "
                "cloud-primary projects or remote media references exist"
            ) from exc
        return
    if not target_ids:
        return
    if any(target.identity_hash != proposed for target in targets):
        raise MediaStorageError(
            "cloud target settings are locked while verified media objects "
            "depend on the current destination; credential rotation is allowed, "
            "but provider, endpoint, region, bucket, and remote base must remain "
            "unchanged"
        )


def apply_target_change_in_transaction(
    session: Session,
    provider: str,
    cfg: dict,
    remote_base: str,
) -> int:
    """Validate and rebind media policies inside the settings write transaction."""
    policies = session.exec(select(ProjectMediaPolicy).where(
        ProjectMediaPolicy.mode == CLOUD_PRIMARY
    )).all()
    target_ids = set(session.exec(select(MediaObject.storage_target_id).where(
        MediaObject.remote_key != "",
        MediaObject.storage_target_id != None,  # noqa: E711
    )).all())
    target_ids.update(session.exec(
        select(ProjectMediaPolicy.storage_target_id).where(
            ProjectMediaPolicy.storage_target_id != None,  # noqa: E711
        )
    ).all())
    try:
        identity = _target_identity(
            provider, cfg, remote_base.strip("/") or "synapse")
    except MediaStorageError as exc:
        if target_ids or policies:
            raise MediaStorageError(
                "cloud target settings must remain S3-compatible while "
                "cloud-primary projects or remote media references exist"
            ) from exc
        return 0
    if target_ids:
        targets = session.exec(select(MediaStorageTarget).where(
            MediaStorageTarget.id.in_(target_ids)
        )).all()
        if any(target.identity_hash != identity for target in targets):
            raise MediaStorageError(
                "cloud target settings are locked while verified media objects "
                "depend on the current destination; credential rotation is "
                "allowed, but provider, endpoint, region, bucket, and remote "
                "base must remain unchanged"
            )
    project_ids = [policy.project_id for policy in policies]
    if project_ids:
        active_job = session.exec(select(Job.id).where(
            Job.project_id.in_(project_ids),
            Job.task.in_(tuple(ACTION_TASKS.values())),
            Job.status.in_(("queued", "running")),
        )).first()
        transient = session.exec(select(MediaObject.id).where(
            MediaObject.project_id.in_(project_ids),
            MediaObject.state.in_(tuple(TRANSIENT_STATES)),
        )).first()
        if active_job or transient:
            raise MediaStorageBusy(
                "wait for active or interrupted media storage work before "
                "changing the cloud target")
    fingerprint = hashlib.sha256(json.dumps(
        {"provider": provider, "config": cfg,
         "remote_base": remote_base.strip("/") or "synapse"},
        sort_keys=True,
    ).encode("utf-8")).hexdigest()
    target = session.exec(select(MediaStorageTarget).where(
        MediaStorageTarget.identity_hash == identity
    )).first()
    if not target:
        target = MediaStorageTarget(
            identity_hash=identity,
            provider=provider,
            remote_base=remote_base.strip("/") or "synapse",
            config_fingerprint=fingerprint,
        )
    else:
        target.config_fingerprint = fingerprint
        target.updated = utcnow()
    session.add(target)
    session.flush()
    rebound = 0
    for policy in policies:
        dependent = session.exec(select(MediaObject.id).where(
            MediaObject.project_id == policy.project_id,
            MediaObject.remote_key != "",
        )).first()
        if dependent or policy.storage_target_id == target.id:
            continue
        policy.storage_target_id = target.id
        policy.updated = utcnow()
        session.add(policy)
        rebound += 1
    return rebound


def _current_target_values() -> tuple[str, dict, str, str, str]:
    provider = str(get_setting("cloud.provider") or "")
    cfg = get_setting("cloud.config") or {}
    remote_base = str(get_setting("cloud.remote_base") or "synapse").strip("/") or "synapse"
    identity = _target_identity(provider, cfg, remote_base)
    config_fingerprint = hashlib.sha256(json.dumps(
        {"provider": provider, "config": cfg, "remote_base": remote_base},
        sort_keys=True,
    ).encode("utf-8")).hexdigest()
    return provider, cfg, remote_base, identity, config_fingerprint


def ensure_current_target(session: Session) -> MediaStorageTarget:
    provider, _cfg, remote_base, identity, fingerprint = _current_target_values()
    target = session.exec(select(MediaStorageTarget).where(
        MediaStorageTarget.identity_hash == identity
    )).first()
    if not target:
        target = MediaStorageTarget(
            identity_hash=identity,
            provider=provider,
            remote_base=remote_base,
            config_fingerprint=fingerprint,
        )
    else:
        target.config_fingerprint = fingerprint
        target.updated = utcnow()
    session.add(target)
    session.flush()
    return target


def mark_legacy_cleanup_required(
    session: Session, project_ids: set[int] | None = None,
) -> int:
    """Fence S3 legacy-mirror uploads until their exact paths are purged.

    A persisted keep-local policy means the project has participated in the
    cloud-primary lifecycle before.  Recording the target *before* a legacy
    upload makes project deletion conservative even if the process exits after
    the remote mutation.
    """
    if get_setting("cloud.provider") != "s3":
        return 0
    query = select(ProjectMediaPolicy).where(
        ProjectMediaPolicy.mode == KEEP_LOCAL)
    if project_ids is not None:
        if not project_ids:
            return 0
        query = query.where(
            ProjectMediaPolicy.project_id.in_(project_ids))
    policies = session.exec(query).all()
    if not policies:
        return 0
    target = ensure_current_target(session)
    marked = 0
    for policy in policies:
        project = session.get(Project, policy.project_id)
        if not project or project.deleting:
            continue
        if policy.storage_target_id is not None:
            existing = session.get(
                MediaStorageTarget, policy.storage_target_id)
            if not existing:
                raise MediaStorageError(
                    "legacy media cleanup target is missing")
            _assert_target_current(existing)
            continue
        policy.storage_target_id = target.id
        policy.updated = utcnow()
        session.add(policy)
        marked += 1
    session.flush()
    return marked


def _assert_target_current(target: MediaStorageTarget) -> None:
    _provider, _cfg, _base, identity, _fingerprint = _current_target_values()
    if target.identity_hash != identity:
        raise MediaStorageError(
            "the configured S3 destination does not match this media object's "
            "storage target; restore or migrate it before changing destinations"
        )


def get_policy(session: Session, project_id: int) -> ProjectMediaPolicy:
    policy = session.get(ProjectMediaPolicy, project_id)
    if policy:
        return policy
    # An unsaved default is intentional: reading a project does not mutate it.
    return ProjectMediaPolicy(project_id=project_id, mode=KEEP_LOCAL)


def set_policy(session: Session, project_id: int, mode: str) -> ProjectMediaPolicy:
    if mode not in POLICY_MODES:
        raise MediaStorageError(
            f"unknown media policy {mode!r}; choose keep_local or cloud_primary")
    # Serialize policy changes with action enqueue and durable transfer-state
    # publication. A running purge may have released SQLite while deleting the
    # remote object, but its Job remains running and keeps this transition
    # fenced until the row is finalized.
    session.rollback()
    session.exec(text("BEGIN IMMEDIATE"))
    project = session.get(Project, project_id)
    if not project:
        session.rollback()
        raise LookupError("project not found")
    if project.deleting:
        session.rollback()
        raise MediaStorageBusy("project is being deleted")
    active_job = session.exec(select(Job.id).where(
        Job.project_id == project_id,
        Job.task.in_(tuple(ACTION_TASKS.values())),
        Job.status.in_(("queued", "running")),
    )).first()
    if active_job:
        session.rollback()
        raise MediaStorageBusy(
            "wait for the active media storage action before changing policy")
    try:
        _assert_no_media_transition(
            session, project_id, action="changing media storage policy")
    except MediaStorageBusy:
        session.rollback()
        raise
    policy = session.get(ProjectMediaPolicy, project_id) or ProjectMediaPolicy(
        project_id=project_id)
    if mode == CLOUD_PRIMARY:
        target = ensure_current_target(session)
        policy.storage_target_id = target.id
    # Preserve an existing target when switching back.  Cloud-only objects need
    # that identity until the explicit restore action has completed.
    policy.mode = mode
    policy.updated = utcnow()
    session.add(policy)
    session.commit()
    session.refresh(policy)
    return policy


def rebind_unreferenced_cloud_primary_policies() -> int:
    """Bind zero-reference cloud-primary projects to the current target.

    Destination identity changes remain prohibited while any remote reference
    depends on the old target. Projects that have not published remote media
    can safely follow the newly configured destination without requiring a
    redundant keep-local/cloud-primary toggle.
    """
    with get_session() as session:
        session.exec(text("BEGIN IMMEDIATE"))
        policies = session.exec(select(ProjectMediaPolicy).where(
            ProjectMediaPolicy.mode == CLOUD_PRIMARY
        )).all()
        if not policies:
            session.rollback()
            return 0
        target = ensure_current_target(session)
        rebound = 0
        for policy in policies:
            dependent = session.exec(select(MediaObject.id).where(
                MediaObject.project_id == policy.project_id,
                MediaObject.remote_key != "",
            )).first()
            if dependent or policy.storage_target_id == target.id:
                continue
            if active_media_transition(session, policy.project_id):
                session.rollback()
                raise MediaStorageBusy(
                    "cloud target cannot change while media storage state "
                    "recovery is pending"
                )
            policy.storage_target_id = target.id
            policy.updated = utcnow()
            session.add(policy)
            rebound += 1
        session.commit()
        return rebound


def _eligible_artifacts(
    session: Session, project_id: int
) -> tuple[list[Artifact], list[Artifact]]:
    eligible: list[Artifact] = []
    excluded: list[Artifact] = []
    artifacts = session.exec(select(Artifact).where(
        Artifact.project_id == project_id,
        Artifact.type.in_(ELIGIBLE_ARTIFACT_TYPES),
    )).all()
    for artifact in artifacts:
        if not artifact.media_path:
            continue
        if library.artifact_is_cloud_excluded(session, artifact):
            excluded.append(artifact)
        else:
            eligible.append(artifact)
    return eligible, excluded


def _upsert_object(
    session: Session,
    *,
    project_id: int,
    role: str,
    local_path: str,
    artifact_id: int | None,
) -> MediaObject:
    _path, normalized = _safe_relative(local_path)
    row = None
    if artifact_id is not None:
        row = session.exec(select(MediaObject).where(
            MediaObject.artifact_id == artifact_id
        )).first()
    if row is None:
        row = session.exec(select(MediaObject).where(
            MediaObject.project_id == project_id,
            MediaObject.role == role,
            MediaObject.local_path == normalized,
        )).first()
    if row is None:
        row = MediaObject(
            project_id=project_id,
            artifact_id=artifact_id,
            role=role,
            local_path=normalized,
        )
    elif row.local_path != normalized:
        if row.remote_key:
            # Merely viewing status must never orphan a remote object. Keep the
            # old locator/reference fenced until the user restores and purges
            # it; a later inventory can adopt the regenerated path only after
            # the reference has been cleared.
            if row.state not in TRANSIENT_STATES:
                row.state = ERROR
            row.last_error = (
                "the artifact now points to different media; the prior remote "
                "reference must be restored/purged before tracking the new file")
        else:
            # A regenerated artifact now points at a different local payload.
            row.local_path = normalized
            row.sha256 = ""
            row.remote_sha256 = ""
            row.remote_size_bytes = 0
            row.verified_at = None
            row.state = LOCAL
    row.artifact_id = artifact_id
    row.updated = utcnow()
    path = resolve_local_path(row.local_path)
    if path.is_file() and not path.is_symlink():
        row.size_bytes = path.stat().st_size
        if row.state == CLOUD_ONLY:
            local_hash = _sha256(path)
            if row.remote_sha256 and local_hash == row.remote_sha256:
                row.sha256 = local_hash
                row.state = VERIFIED
                row.last_error = ""
            else:
                # Preserve the verified recovery reference.  An unexpected
                # local file may be valuable and must not be overwritten or
                # treated as the verified remote bytes without an explicit
                # conflict decision.
                row.state = ERROR
                row.sha256 = local_hash
                row.last_error = (
                    "an unexpected local file conflicts with the verified "
                    "cloud copy; neither copy was removed")
    elif row.state == VERIFIED:
        # Missing is not synonymous with intentionally evicted.  Only the
        # recoverable eviction transaction may establish CLOUD_ONLY.
        row.state = ERROR
        row.last_error = (
            "a verified local copy is missing without a completed eviction; "
            "run restore before using this media"
        )
    session.add(row)
    session.flush()
    return row


def _media_object_is_eligible(session: Session, row: MediaObject) -> bool:
    if row.artifact_id is not None:
        artifact = session.get(Artifact, row.artifact_id)
        if (not artifact or artifact.project_id != row.project_id
                or artifact.type not in ELIGIBLE_ARTIFACT_TYPES
                or not artifact.media_path
                or library.artifact_is_cloud_excluded(session, artifact)):
            return False
        try:
            _path, normalized = _safe_relative(artifact.media_path)
        except MediaStorageError:
            return False
        return row.local_path == normalized
    project = session.get(Project, row.project_id)
    if (not project or project.source_type != "upload"
            or row.role != "original_upload"
            or library.project_is_restricted(session, row.project_id)):
        return False
    try:
        path = resolve_local_path(row.local_path)
        project_dir = (settings.media_dir / project.slug).resolve()
        path.relative_to(project_dir)
    except (MediaStorageError, ValueError):
        return False
    return path.name.startswith("uploaded.")


def inventory_project(session: Session, project_id: int) -> dict:
    project = session.get(Project, project_id)
    if not project:
        raise LookupError("project not found")
    eligible, excluded = _eligible_artifacts(session, project_id)
    rows: list[MediaObject] = []
    for artifact in eligible:
        rows.append(_upsert_object(
            session,
            project_id=project_id,
            role=artifact.type,
            local_path=artifact.media_path,
            artifact_id=artifact.id,
        ))

    # Browser uploads are durable originals but have no Artifact row.  Only
    # the exact uploaded.* payload is eligible; cookies/auth JSON and temporary
    # files sharing the work directory are deliberately ignored.
    if project.source_type == "upload" and not library.project_is_restricted(
            session, project_id):
        project_dir = settings.media_dir / project.slug
        if project_dir.is_dir() and not project_dir.is_symlink():
            for upload in sorted(project_dir.glob("uploaded.*")):
                if upload.is_file() and not upload.is_symlink():
                    rows.append(_upsert_object(
                        session,
                        project_id=project_id,
                        role="original_upload",
                        local_path=f"media:{project.slug}/{upload.name}",
                        artifact_id=None,
                    ))
    # Reconcile rows that were once eligible but have since become restricted,
    # cloud-excluded, removed, or repointed. Remote references remain visible
    # and recoverable, but can no longer authorize sync or eviction.
    all_rows = session.exec(select(MediaObject).where(
        MediaObject.project_id == project_id
    )).all()
    for row in all_rows:
        if _media_object_is_eligible(session, row):
            continue
        if row.remote_key:
            if row.state not in TRANSIENT_STATES:
                row.state = ERROR
            if not row.last_error:
                row.last_error = (
                    "media is no longer cloud-eligible; restore and purge its "
                    "remote reference before removing local tracking")
            row.updated = utcnow()
            session.add(row)
            continue
        if row.state in TRANSIENT_STATES:
            continue
        still_owned = (
            session.get(Artifact, row.artifact_id) is not None
            if row.artifact_id is not None
            else session.get(Project, row.project_id) is not None
        )
        if still_owned:
            row.state = ERROR
            row.last_error = (
                "media is no longer cloud-eligible and remains local-only")
            row.updated = utcnow()
            session.add(row)
            continue
        if row.id is not None and _live_lease(session, row.id):
            row.state = ERROR
            row.last_error = (
                "media is no longer cloud-eligible but remains tracked until "
                "active playback completes")
            row.updated = utcnow()
            session.add(row)
            continue
        lease_ids = session.exec(select(MediaLease).where(
            MediaLease.media_object_id == row.id
        )).all()
        for lease in lease_ids:
            session.delete(lease)
        session.delete(row)
    session.commit()
    return {"objects": rows, "excluded": excluded}


def project_status(session: Session, project_id: int) -> dict:
    inventory = inventory_project(session, project_id)
    rows: list[MediaObject] = inventory["objects"]
    # Refresh rows after inventory's commit.
    rows = session.exec(select(MediaObject).where(
        MediaObject.project_id == project_id
    ).order_by(MediaObject.id)).all()
    policy = get_policy(session, project_id)
    target = (
        session.get(MediaStorageTarget, policy.storage_target_id)
        if policy.storage_target_id else None
    )
    local_objects = 0
    local_bytes = 0
    serialized = []
    eligibility: dict[int, bool] = {}
    for row in rows:
        path = resolve_local_path(row.local_path)
        present = path.is_file() and not path.is_symlink()
        eligible = _media_object_is_eligible(session, row)
        if row.id is not None:
            eligibility[row.id] = eligible
        local_objects += int(present)
        local_bytes += row.size_bytes if present else 0
        serialized.append({
            "id": row.id,
            "artifact_id": row.artifact_id,
            "role": row.role,
            "state": row.state,
            "size_bytes": row.size_bytes,
            "local_present": present,
            "verified_at": row.verified_at,
            "last_error": row.last_error,
            "eligible": eligible,
        })
    eligible_rows = [
        row for row in rows if row.id is not None and eligibility.get(row.id, False)
    ]
    excluded_artifact_ids = {
        artifact.id for artifact in inventory["excluded"] if artifact.id is not None
    }
    tracked_excluded = sum(
        not eligibility.get(row.id, False)
        and row.artifact_id not in excluded_artifact_ids
        for row in rows if row.id is not None
    )
    summary = {
        "eligible_objects": len(eligible_rows),
        "total_bytes": sum(row.size_bytes for row in eligible_rows),
        "local_objects": local_objects,
        "local_bytes": local_bytes,
        "verified_objects": sum(row.state == VERIFIED for row in rows),
        "cloud_only_objects": sum(row.state == CLOUD_ONLY for row in rows),
        "remote_objects": sum(bool(row.remote_key) for row in rows),
        "restorable_objects": sum(
            bool(row.remote_key and row.remote_sha256)
            and row.state in {CLOUD_ONLY, VERIFIED, ERROR}
            and not resolve_local_path(row.local_path).is_file()
            for row in rows
        ),
        "pending_objects": sum(
            row.state in TRANSIENT_STATES
            or (policy.mode == CLOUD_PRIMARY and row.state == LOCAL)
            for row in eligible_rows),
        "error_objects": sum(row.state == ERROR for row in rows),
        "excluded_objects": len(excluded_artifact_ids) + tracked_excluded,
    }
    return {
        "policy": {
            "mode": policy.mode,
            "storage_target_id": policy.storage_target_id,
        },
        "target": ({
            "id": target.id,
            "provider": target.provider,
            "remote_base": target.remote_base,
        } if target else None),
        "summary": summary,
        "objects": serialized,
    }


def _remote_destination(remote_key: str) -> str:
    from .tasks import cloud

    _validate_remote_key(remote_key)
    return cloud._dest(f"media-objects/{remote_key}")


def _rclone_copyto(source: str, destination: str) -> None:
    from .tasks import cloud

    cloud._rclone(["copyto", source, destination])


_REMOTE_KEY_RE = re.compile(
    r"^sha256/(?P<prefix>[0-9a-f]{2})/"
    r"(?P<digest>[0-9a-f]{64})(?P<suffix>\.[a-z0-9]{1,16})?$"
)


def _validate_remote_key(remote_key: str) -> re.Match[str]:
    match = _REMOTE_KEY_RE.fullmatch(remote_key or "")
    if not match or match.group("prefix") != match.group("digest")[:2]:
        raise MediaStorageError(
            "stored media remote key is invalid; no remote mutation was attempted")
    return match


def _content_remote_key(digest: str, suffix: str) -> str:
    normalized_suffix = suffix.lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,16}", normalized_suffix):
        normalized_suffix = ""
    return f"sha256/{digest[:2]}/{digest}{normalized_suffix}"


def _stream_remote_sha256(remote_key: str) -> tuple[int, str]:
    """Read an exact remote object through SHA-256 without a local full copy."""
    from .tasks import cloud

    remote = _remote_destination(remote_key)
    conf = cloud._conf_path()
    size = 0
    digest = hashlib.sha256()
    with tempfile.TemporaryFile() as stderr:
        proc = subprocess.Popen(
            [
                "rclone", "--config", str(conf), "--log-level", "ERROR",
                "cat", remote,
            ],
            stdout=subprocess.PIPE,
            stderr=stderr,
        )
        try:
            if proc.stdout is None:
                raise MediaStorageError("rclone did not expose remote media bytes")
            while block := proc.stdout.read(4 * 1024 * 1024):
                size += len(block)
                digest.update(block)
            return_code = proc.wait(timeout=3600)
        except Exception:
            proc.kill()
            proc.wait()
            raise
        if return_code != 0:
            stderr.seek(0, os.SEEK_END)
            length = stderr.tell()
            stderr.seek(max(0, length - 1500))
            detail = stderr.read().decode("utf-8", errors="replace")
            raise MediaStorageError(f"rclone remote read failed: {detail}")
    return size, digest.hexdigest()


def _ensure_upload_staging_capacity(path: Path, source_size: int) -> None:
    usage = shutil.disk_usage(path)
    reserve = max(256 * 1024 * 1024, min(5 * 1024 * 1024 * 1024,
                                         usage.total // 20))
    required = source_size + reserve
    if usage.free < required:
        raise MediaStorageError(
            "insufficient free disk space to create the immutable upload "
            f"snapshot ({required} bytes required, {usage.free} available)"
        )


def _transfer_staging_spec(
    row: MediaObject, kind: str, token: str
) -> str:
    prefix = "media:" if row.local_path.startswith("media:") else ""
    suffix = resolve_local_path(row.local_path).suffix
    return f"{prefix}.staging/media-{kind}/{row.id}-{token}{suffix}"


def _mark_transfer_error(
    media_object_id: int,
    token: str,
    exc: Exception,
    *,
    fallback_state: str = ERROR,
) -> None:
    with get_session() as session:
        session.exec(text("BEGIN IMMEDIATE"))
        row = session.get(MediaObject, media_object_id)
        if row and row.eviction_token == token:
            row.state = fallback_state
            row.eviction_token = ""
            row.staging_path = ""
            row.last_error = str(exc)[:1000]
            row.updated = utcnow()
            session.add(row)
        session.commit()


def _object_may_transfer(session: Session, row: MediaObject) -> None:
    if not _media_object_is_eligible(session, row):
        raise MediaStorageError(
            "media is no longer eligible for cloud storage; restore and purge "
            "any existing remote reference")


def sync_media_object(media_object_id: int) -> bool:
    """Upload and read back one object; preserve the local file on every error."""
    with get_session() as session:
        pending_cleanup = session.exec(select(MediaObject.id).where(
            MediaObject.id == media_object_id,
            MediaObject.state == ERROR,
            MediaObject.remote_key != "",
            MediaObject.verified_at == None,  # noqa: E711
        )).first()
    if pending_cleanup:
        _discard_unverified_remote_reference(media_object_id)

    with get_session() as session:
        row = session.get(MediaObject, media_object_id)
        if not row:
            raise MediaStorageError("media object not found")
        policy = get_policy(session, row.project_id)
        if policy.mode != CLOUD_PRIMARY or not policy.storage_target_id:
            raise MediaStorageError("project is not configured for cloud-primary media")
        target = session.get(MediaStorageTarget, policy.storage_target_id)
        if not target:
            raise MediaStorageError("media storage target is missing")
        _assert_target_current(target)
        _object_may_transfer(session, row)
        source = resolve_local_path(row.local_path)
        if row.state == CLOUD_ONLY and not source.exists():
            return False
        if row.state in TRANSIENT_STATES:
            raise MediaStorageBusy(
                "media has an active or interrupted storage transition")
        if not source.is_file() or source.is_symlink():
            raise MediaStorageError("local media file is missing or unsafe")
        source_size = source.stat().st_size
        local_spec = row.local_path
        target_id = target.id
        previous_state = row.state
        previous_remote_key = row.remote_key
        previous_remote_hash = row.remote_sha256
        previous_verified_at = row.verified_at

    stage_root = (
        settings.media_dir if local_spec.startswith("media:")
        else settings.library_dir
    ) / ".staging" / "media-uploads"
    stage_root.mkdir(parents=True, exist_ok=True)
    _ensure_upload_staging_capacity(stage_root, source_size)
    token = uuid.uuid4().hex
    with get_session() as session:
        session.exec(text("BEGIN IMMEDIATE"))
        row = session.get(MediaObject, media_object_id)
        policy = session.get(ProjectMediaPolicy, row.project_id)
        if (not policy or policy.mode != CLOUD_PRIMARY
                or policy.storage_target_id != target_id):
            session.rollback()
            raise MediaStorageBusy(
                "cloud-primary target changed before media upload")
        if row.state in TRANSIENT_STATES:
            session.rollback()
            raise MediaStorageBusy(
                "media has an active or interrupted storage transition")
        _object_may_transfer(session, row)
        row.state = UPLOADING
        row.eviction_token = token
        row.staging_path = _transfer_staging_spec(row, "uploads", token)
        row.last_error = ""
        row.updated = utcnow()
        session.add(row)
        session.commit()
        staging_spec = row.staging_path

    staged = resolve_local_path(staging_spec)
    staged.parent.mkdir(parents=True, exist_ok=True)
    pending_published = False
    try:
        shutil.copy2(resolve_local_path(local_spec), staged)
        size = staged.stat().st_size
        digest = _sha256(staged)
        if (previous_verified_at and previous_remote_key
                and previous_remote_hash != digest):
            raise MediaStorageError(
                "local media conflicts with its prior verified cloud copy; "
                "restore or purge the prior reference before syncing new bytes")
        remote_key = _content_remote_key(
            digest, resolve_local_path(local_spec).suffix)
        from .tasks import cloud

        # Serialize effective credentials, the remote mutation, full readback,
        # and durable verification publication.
        with cloud._remote_lock():
            with get_session() as session:
                session.exec(text("BEGIN IMMEDIATE"))
                row = session.get(MediaObject, media_object_id)
                if (not row or row.state != UPLOADING
                        or row.eviction_token != token):
                    session.rollback()
                    raise MediaStorageBusy("media upload state changed")
                policy = session.get(ProjectMediaPolicy, row.project_id)
                if (not policy or policy.mode != CLOUD_PRIMARY
                        or policy.storage_target_id != target_id):
                    session.rollback()
                    raise MediaStorageBusy(
                        "cloud-primary target changed during media upload")
                locked_target = session.get(MediaStorageTarget, target_id)
                if not locked_target:
                    session.rollback()
                    raise MediaStorageError("media storage target is missing")
                _assert_target_current(locked_target)
                _object_may_transfer(session, row)
                # Publish ownership of the prospective content-addressed key
                # before the first remote mutation. A crash can therefore be
                # reconciled without leaving an untracked remote object.
                row.storage_target_id = target_id
                row.remote_key = remote_key
                row.size_bytes = size
                row.sha256 = digest
                row.remote_size_bytes = size
                row.remote_sha256 = digest
                row.verified_at = None
                row.updated = utcnow()
                session.add(row)
                session.commit()
                pending_published = True

            remote = _remote_destination(remote_key)
            _rclone_copyto(str(staged), remote)
            remote_size, remote_digest = _stream_remote_sha256(remote_key)
            if remote_size != size or remote_digest != digest:
                raise MediaStorageError(
                    "remote readback did not match the local media SHA-256")

            current = resolve_local_path(local_spec)
            if (not current.is_file() or current.is_symlink()
                    or current.stat().st_size != size
                    or _sha256(current) != digest):
                raise MediaStorageError(
                    "local media changed during upload; it was not marked verified")

            with get_session() as session:
                session.exec(text("BEGIN IMMEDIATE"))
                row = session.get(MediaObject, media_object_id)
                if (not row or row.state != UPLOADING
                        or row.eviction_token != token
                        or row.storage_target_id != target_id
                        or row.remote_key != remote_key):
                    session.rollback()
                    raise MediaStorageBusy("media upload state changed")
                policy = session.get(ProjectMediaPolicy, row.project_id)
                if (not policy or policy.mode != CLOUD_PRIMARY
                        or policy.storage_target_id != target_id):
                    session.rollback()
                    raise MediaStorageBusy(
                        "cloud-primary policy changed during media upload")
                locked_target = session.get(MediaStorageTarget, target_id)
                if not locked_target:
                    session.rollback()
                    raise MediaStorageError("media storage target is missing")
                _assert_target_current(locked_target)
                _object_may_transfer(session, row)
                row.verified_at = utcnow()
                row.state = VERIFIED
                row.eviction_token = ""
                row.staging_path = ""
                row.last_error = ""
                row.updated = utcnow()
                session.add(row)
                session.commit()
        _safe_unlink(staged)
        return True
    except Exception as exc:
        cleanup_error: Exception | None = None
        if pending_published:
            try:
                from .tasks import cloud

                with cloud._remote_lock():
                    with get_session() as session:
                        session.exec(text("BEGIN IMMEDIATE"))
                        row = session.get(MediaObject, media_object_id)
                        same_pending = bool(
                            row and row.state == UPLOADING
                            and row.eviction_token == token
                            and row.storage_target_id == target_id
                            and row.remote_key == remote_key
                            and row.verified_at is None
                        )
                        other_reference = (
                            session.exec(select(MediaObject.id).where(
                                MediaObject.id != media_object_id,
                                MediaObject.storage_target_id == target_id,
                                MediaObject.remote_key == remote_key,
                            )).first()
                            if same_pending else True
                        )
                        session.rollback()
                    if same_pending and not other_reference:
                        _delete_remote_key(remote_key)
                    with get_session() as session:
                        session.exec(text("BEGIN IMMEDIATE"))
                        row = session.get(MediaObject, media_object_id)
                        if (row and row.state == UPLOADING
                                and row.eviction_token == token
                                and row.storage_target_id == target_id
                                and row.remote_key == remote_key
                                and row.verified_at is None):
                            _clear_remote_reference(row)
                            row.state = ERROR
                            row.last_error = str(exc)[:1000]
                            session.add(row)
                        session.commit()
            except Exception as cleanup_exc:
                cleanup_error = cleanup_exc
        _safe_unlink(staged)
        with get_session() as session:
            session.exec(text("BEGIN IMMEDIATE"))
            row = session.get(MediaObject, media_object_id)
            if row and row.state == UPLOADING and row.eviction_token == token:
                if not pending_published:
                    row.state = (
                        previous_state
                        if previous_state not in TRANSIENT_STATES else ERROR
                    )
                else:
                    row.state = ERROR
                row.eviction_token = ""
                row.staging_path = ""
                row.last_error = (
                    f"{exc}; pending remote cleanup failed: {cleanup_error}"
                    if cleanup_error else str(exc)
                )[:1000]
                row.updated = utcnow()
                session.add(row)
            session.commit()
        raise


def sync_project_media(project_id: int) -> dict:
    with get_session() as session:
        inventory_project(session, project_id)
        policy = get_policy(session, project_id)
        if policy.mode != CLOUD_PRIMARY:
            raise MediaStorageError("enable cloud-primary media before syncing")
        rows = session.exec(select(MediaObject).where(
            MediaObject.project_id == project_id
        ).order_by(MediaObject.id)).all()
        ids = [
            row.id for row in rows
            if row.id is not None and _media_object_is_eligible(session, row)
        ]
        skipped = len(rows) - len(ids)
    uploaded = 0
    for media_object_id in ids:
        with get_session() as session:
            row = session.get(MediaObject, media_object_id)
            if not row or not _media_object_is_eligible(session, row):
                skipped += 1
                continue
            path = resolve_local_path(row.local_path)
            if row.state == CLOUD_ONLY and not path.exists():
                skipped += 1
                continue
        uploaded += int(sync_media_object(media_object_id))
    return {"verified": uploaded, "skipped": skipped}


def _restore_failure(
    media_object_id: int, previous_state: str, token: str, exc: Exception
) -> None:
    with get_session() as session:
        session.exec(text("BEGIN IMMEDIATE"))
        row = session.get(MediaObject, media_object_id)
        if (row and row.state == RESTORING
                and row.eviction_token == token):
            row.state = CLOUD_ONLY if previous_state == CLOUD_ONLY else ERROR
            row.eviction_token = ""
            row.staging_path = ""
            row.last_error = str(exc)[:1000]
            row.updated = utcnow()
            session.add(row)
        session.commit()


def restore_media_object(media_object_id: int) -> Path:
    with get_session() as session:
        row = session.get(MediaObject, media_object_id)
        if not row:
            raise MediaStorageError("media object not found")
        destination = resolve_local_path(row.local_path)
        if destination.is_file() and not destination.is_symlink():
            if row.remote_sha256 and row.state in {CLOUD_ONLY, ERROR}:
                local_hash = _sha256(destination)
                if (destination.stat().st_size == row.remote_size_bytes
                        and local_hash == row.remote_sha256):
                    # ERROR may mean a purge deleted the remote object before a
                    # process crash. Matching local bytes alone must not
                    # resurrect remote verification or authorize eviction.
                    if row.state == CLOUD_ONLY:
                        row.state = VERIFIED
                        row.sha256 = local_hash
                        row.last_error = ""
                        row.updated = utcnow()
                        session.add(row)
                        session.commit()
                    return destination
                raise MediaStorageError(
                    "a local file conflicts with the verified cloud copy; "
                    "restore will not overwrite either copy")
            return destination
        if (not row.remote_key or not row.remote_sha256
                or row.state not in {CLOUD_ONLY, VERIFIED, ERROR}):
            raise MediaStorageError("media has no durably verified remote copy")
        target = session.get(MediaStorageTarget, row.storage_target_id)
        if not target:
            raise MediaStorageError("media storage target is missing")
        _assert_target_current(target)
        previous_state = row.state
        remote_key = row.remote_key
        target_id = row.storage_target_id
        expected_hash = row.remote_sha256
        expected_size = row.remote_size_bytes
        token = uuid.uuid4().hex
        row.state = RESTORING
        row.eviction_token = token
        row.staging_path = _transfer_staging_spec(row, "restores", token)
        row.last_error = ""
        row.updated = utcnow()
        session.add(row)
        session.commit()
        staging_spec = row.staging_path

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = resolve_local_path(staging_spec)
    temporary.parent.mkdir(parents=True, exist_ok=True)
    try:
        from .tasks import cloud

        with cloud._remote_lock():
            with get_session() as session:
                locked_target = session.get(MediaStorageTarget, target_id)
                if not locked_target:
                    raise MediaStorageError("media storage target is missing")
                _assert_target_current(locked_target)
            _rclone_copyto(_remote_destination(remote_key), str(temporary))
            with get_session() as session:
                locked_target = session.get(MediaStorageTarget, target_id)
                if not locked_target:
                    raise MediaStorageError("media storage target is missing")
                _assert_target_current(locked_target)
        if (temporary.stat().st_size != expected_size
                or _sha256(temporary) != expected_hash):
            raise MediaStorageError(
                "restored media did not match its verified SHA-256")
        os.replace(temporary, destination)
        with get_session() as session:
            session.exec(text("BEGIN IMMEDIATE"))
            row = session.get(MediaObject, media_object_id)
            if (not row or row.state != RESTORING
                    or row.eviction_token != token):
                session.rollback()
                raise MediaStorageBusy("media restore state changed")
            row.state = VERIFIED
            row.size_bytes = expected_size
            row.sha256 = expected_hash
            row.verified_at = row.verified_at or utcnow()
            row.eviction_token = ""
            row.staging_path = ""
            row.last_error = ""
            row.updated = utcnow()
            session.add(row)
            session.commit()
        return destination
    except Exception as exc:
        _safe_unlink(temporary)
        _restore_failure(media_object_id, previous_state, token, exc)
        raise


def restore_project_media(project_id: int) -> dict:
    with get_session() as session:
        inventory_project(session, project_id)
        rows = session.exec(select(MediaObject).where(
            MediaObject.project_id == project_id
        )).all()
        ids = [
            row.id for row in rows
            if (
                row.id is not None
                and row.remote_key
                and row.remote_sha256
                and row.state in {CLOUD_ONLY, VERIFIED, ERROR}
                and not resolve_local_path(row.local_path).is_file()
            )
        ]
    restored = 0
    for media_object_id in ids:
        restore_media_object(media_object_id)
        restored += 1
    return {"restored": restored}


def _delete_remote_key(remote_key: str) -> None:
    """Idempotently remove one exact content-addressed object."""
    _validate_remote_key(remote_key)
    _deletefile_idempotent(_remote_destination(remote_key))


def _deletefile_idempotent(destination: str) -> None:
    from .tasks import cloud

    try:
        cloud._rclone(["deletefile", destination])
    except RuntimeError as exc:
        detail = str(exc).lower()
        if any(marker in detail for marker in (
            "not found", "object not found", "does not exist",
            "directory not found", "404",
        )):
            return
        raise


def _legacy_remote_destination(local_path: str) -> str:
    from .tasks import cloud

    _path, normalized = _safe_relative(local_path)
    if normalized.startswith("media:"):
        return cloud._dest(f"media/{normalized.removeprefix('media:')}")
    return cloud._dest(f"library/{normalized}")


def _discard_unverified_remote_reference(media_object_id: int) -> None:
    """Retry cleanup for a crash-interrupted, never-verified upload."""
    from .tasks import cloud

    with cloud._remote_lock():
        with get_session() as session:
            session.exec(text("BEGIN IMMEDIATE"))
            row = session.get(MediaObject, media_object_id)
            if (not row or not row.remote_key or row.verified_at is not None):
                session.rollback()
                return
            target_id = row.storage_target_id
            remote_key = row.remote_key
            target = session.get(MediaStorageTarget, target_id)
            if not target:
                session.rollback()
                raise MediaStorageError("media storage target is missing")
            _assert_target_current(target)
            other_reference = session.exec(select(MediaObject.id).where(
                MediaObject.id != media_object_id,
                MediaObject.storage_target_id == target_id,
                MediaObject.remote_key == remote_key,
            )).first()
            session.rollback()
        if not other_reference:
            _delete_remote_key(remote_key)
        with get_session() as session:
            session.exec(text("BEGIN IMMEDIATE"))
            row = session.get(MediaObject, media_object_id)
            if (row and row.storage_target_id == target_id
                    and row.remote_key == remote_key
                    and row.verified_at is None):
                _clear_remote_reference(row)
                row.state = ERROR
                row.last_error = (
                    "interrupted unverified upload was removed; retry sync")
                session.add(row)
            session.commit()


def _clear_remote_reference(row: MediaObject) -> None:
    row.storage_target_id = None
    row.remote_key = ""
    row.remote_size_bytes = 0
    row.remote_sha256 = ""
    row.verified_at = None
    row.state = LOCAL
    row.last_error = ""
    row.eviction_token = ""
    row.staging_path = ""
    row.updated = utcnow()


def purge_project_remote_media(
    project_id: int, *, job_id: int | None = None
) -> dict:
    """Remove this keep-local project's remote references without data loss.

    Cloud-only bytes are restored first. Content-addressed objects shared by a
    different MediaObject are retained physically while this project's
    reference is cleared.
    """
    with get_session() as session:
        policy = get_policy(session, project_id)
        if policy.mode != KEEP_LOCAL:
            raise MediaStorageError(
                "switch the project to keep-local before purging remote media")
    restore_project_media(project_id)
    with get_session() as session:
        policy = get_policy(session, project_id)
        cleanup_target_id = policy.storage_target_id
        rows = session.exec(select(MediaObject).where(
            MediaObject.project_id == project_id,
        ).order_by(MediaObject.id)).all()
        ids = [
            row.id for row in rows
            if row.id is not None and (
                bool(row.remote_key) or cleanup_target_id is not None)
        ]

    deleted = shared = cleared = legacy_deleted = 0
    from .tasks import cloud

    for media_object_id in ids:
        with get_session() as session:
            row = session.get(MediaObject, media_object_id)
            policy = get_policy(session, project_id)
            if not row:
                continue
            local = resolve_local_path(row.local_path)
            if not local.is_file() or local.is_symlink():
                raise MediaStorageError(
                    "remote purge requires an intact local copy")
            remote_key = row.remote_key
            if remote_key and (
                local.stat().st_size != row.remote_size_bytes
                or _sha256(local) != row.remote_sha256
            ):
                raise MediaStorageError(
                    "remote purge requires an intact verified local copy")
            target_id = row.storage_target_id or policy.storage_target_id
            if target_id is None:
                continue
            local_path = row.local_path
            remote_key = row.remote_key
            if remote_key:
                _validate_remote_key(remote_key)

        with cloud._remote_lock():
            try:
                with get_session() as session:
                    session.exec(text("BEGIN IMMEDIATE"))
                    policy = session.get(ProjectMediaPolicy, project_id)
                    row = session.get(MediaObject, media_object_id)
                    if not policy or policy.mode != KEEP_LOCAL:
                        session.rollback()
                        raise MediaStorageBusy(
                            "cloud-primary policy canceled remote media purge")
                    if _active_project_job(session, project_id, job_id):
                        session.rollback()
                        raise MediaStorageBusy(
                            "wait for all other project jobs before purging "
                            "remote media")
                    if job_id is not None:
                        purge_job = session.get(Job, job_id)
                        if not purge_job or purge_job.status != "running":
                            session.rollback()
                            raise MediaStorageBusy(
                                "remote media purge job was canceled")
                    if (not row or row.local_path != local_path
                            or row.remote_key != remote_key
                            or (row.storage_target_id or
                                policy.storage_target_id) != target_id):
                        session.rollback()
                        continue
                    if row.state in TRANSIENT_STATES:
                        session.rollback()
                        raise MediaStorageBusy(
                            "media has an active or interrupted storage "
                            "transition")
                    target = session.get(MediaStorageTarget, target_id)
                    if not target:
                        session.rollback()
                        raise MediaStorageError(
                            "media storage target is missing")
                    _assert_target_current(target)
                    other_reference = (
                        session.exec(select(MediaObject.id).where(
                            MediaObject.id != media_object_id,
                            MediaObject.storage_target_id == target_id,
                            MediaObject.remote_key == remote_key,
                        )).first()
                        if remote_key else None
                    )
                    legacy_destination = _legacy_remote_destination(
                        row.local_path)
                    # PURGING is committed before the network mutation. If the
                    # process dies after deletefile, recovery converts it to
                    # ERROR while preserving metadata; eviction only accepts
                    # VERIFIED and can never trust that stale reference.
                    row.storage_target_id = target_id
                    row.state = PURGING
                    row.last_error = ""
                    row.updated = utcnow()
                    session.add(row)
                    session.commit()

                if remote_key and not other_reference:
                    _delete_remote_key(remote_key)
                    deleted += 1
                elif remote_key:
                    shared += 1
                # Legacy mirror keys are exact local paths. Eligible media
                # locators are project-owned, so this idempotent deletion also
                # covers projects that never completed a content-addressed
                # cloud-primary upload.
                _deletefile_idempotent(legacy_destination)
                legacy_deleted += 1

                with get_session() as session:
                    session.exec(text("BEGIN IMMEDIATE"))
                    row = session.get(MediaObject, media_object_id)
                    if (row and row.state == PURGING
                            and row.storage_target_id == target_id
                            and row.remote_key == remote_key):
                        local = resolve_local_path(row.local_path)
                        if not local.is_file() or local.is_symlink():
                            raise MediaStorageError(
                                "local media changed before purge completion")
                        if remote_key and (
                            local.stat().st_size != row.remote_size_bytes
                            or _sha256(local) != row.remote_sha256
                        ):
                            raise MediaStorageError(
                                "local media changed before purge completion")
                        # Clear after a successful idempotent delete even if a
                        # direct/internal caller raced a policy change. Leaving
                        # stale verification metadata is never safe.
                        _clear_remote_reference(row)
                        session.add(row)
                        cleared += 1
                    session.commit()
            except Exception as exc:
                with get_session() as session:
                    session.exec(text("BEGIN IMMEDIATE"))
                    row = session.get(MediaObject, media_object_id)
                    if (row and row.state == PURGING
                            and row.storage_target_id == target_id
                            and row.remote_key == remote_key):
                        row.state = ERROR
                        row.last_error = (
                            "remote purge was interrupted or failed; the "
                            "remote reference must be purged or resynchronized "
                            f"before eviction: {exc}"
                        )[:1000]
                        row.updated = utcnow()
                        session.add(row)
                    session.commit()
                raise

    # One final exact-path sweep closes the interval between per-object
    # deletes. Legacy sync records this policy target before publishing and
    # takes the same remote lock, so no legacy upload can appear between this
    # sweep and clearing the durable cleanup fence.
    with cloud._remote_lock():
        with get_session() as session:
            session.exec(text("BEGIN IMMEDIATE"))
            policy = session.get(ProjectMediaPolicy, project_id)
            if not policy or policy.mode != KEEP_LOCAL:
                session.rollback()
                raise MediaStorageBusy(
                    "cloud-primary policy canceled remote media purge")
            target_id = policy.storage_target_id
            if target_id is not None:
                target = session.get(MediaStorageTarget, target_id)
                if not target:
                    session.rollback()
                    raise MediaStorageError(
                        "media storage cleanup target is missing")
                _assert_target_current(target)
            remaining = session.exec(select(MediaObject).where(
                MediaObject.project_id == project_id,
            )).all()
            if any(row.remote_key for row in remaining):
                session.rollback()
                raise MediaStorageBusy(
                    "remote media references changed during purge; retry")
            if any(row.state in TRANSIENT_STATES for row in remaining):
                session.rollback()
                raise MediaStorageBusy(
                    "media storage state changed during purge; retry")
            final_paths = [row.local_path for row in remaining]
            session.commit()

        for local_path in final_paths:
            _deletefile_idempotent(_legacy_remote_destination(local_path))

        with get_session() as session:
            session.exec(text("BEGIN IMMEDIATE"))
            policy = session.get(ProjectMediaPolicy, project_id)
            if not policy or policy.mode != KEEP_LOCAL:
                session.rollback()
                raise MediaStorageBusy(
                    "cloud-primary policy canceled remote media purge")
            if policy.storage_target_id != target_id:
                session.rollback()
                raise MediaStorageBusy(
                    "legacy media cleanup target changed during purge; retry")
            remaining = session.exec(select(MediaObject.id).where(
                MediaObject.project_id == project_id,
                MediaObject.remote_key != "",
            )).first()
            transient = session.exec(select(MediaObject.id).where(
                MediaObject.project_id == project_id,
                MediaObject.state.in_(tuple(TRANSIENT_STATES)),
            )).first()
            if remaining or transient:
                session.rollback()
                raise MediaStorageBusy(
                    "media storage state changed during purge; retry")
            policy.storage_target_id = None
            policy.updated = utcnow()
            session.add(policy)
            session.commit()
    return {
        "cleared": cleared,
        "remote_objects_deleted": deleted,
        "shared_objects_retained": shared,
        "legacy_objects_deleted": legacy_deleted,
        "legacy_objects_retained": 0,
    }


def ensure_project_original_upload_local(project_id: int) -> Path:
    """Restore and return the immutable browser-upload source for a rerun."""
    with get_session() as session:
        inventory_project(session, project_id)
        row = session.exec(select(MediaObject).where(
            MediaObject.project_id == project_id,
            MediaObject.role == "original_upload",
        ).order_by(MediaObject.id)).first()
        if not row:
            raise FileNotFoundError("uploaded source is missing")
        path = resolve_local_path(row.local_path)
        if path.is_file() and not path.is_symlink():
            return path
        media_object_id = row.id
    return restore_media_object(media_object_id)


def _staging_spec(row: MediaObject, token: str) -> str:
    prefix = "media:" if row.local_path.startswith("media:") else ""
    suffix = resolve_local_path(row.local_path).suffix
    return f"{prefix}.staging/media-evictions/{row.id}-{token}{suffix}"


def _active_project_job(session: Session, project_id: int, job_id: int | None) -> Job | None:
    query = select(Job).where(
        Job.project_id == project_id,
        Job.status.in_(("queued", "running")),
    )
    if job_id is not None:
        query = query.where(Job.id != job_id)
    return session.exec(query).first()


def _live_lease(session: Session, media_object_id: int) -> MediaLease | None:
    now = utcnow()
    expired = session.exec(select(MediaLease).where(
        MediaLease.media_object_id == media_object_id,
        MediaLease.expires_at <= now,
    )).all()
    for lease in expired:
        session.delete(lease)
    session.flush()
    return session.exec(select(MediaLease).where(
        MediaLease.media_object_id == media_object_id,
        MediaLease.expires_at > now,
    )).first()


def evict_media_object(media_object_id: int, *, job_id: int | None = None) -> bool:
    with get_session() as session:
        row = session.get(MediaObject, media_object_id)
        if not row:
            raise MediaStorageError("media object not found")
        policy = get_policy(session, row.project_id)
        if policy.mode != CLOUD_PRIMARY:
            raise MediaStorageError("keep-local policy forbids media eviction")
        if row.state != VERIFIED or not row.verified_at:
            raise MediaStorageError("media is not durably verified and cannot be evicted")
        _object_may_transfer(session, row)
        target = session.get(MediaStorageTarget, row.storage_target_id)
        if not target:
            raise MediaStorageError("verified media target is missing")
        _assert_target_current(target)
        _validate_remote_key(row.remote_key)
        source = resolve_local_path(row.local_path)
        if not source.exists():
            raise MediaStorageError(
                "local media is already missing without a completed eviction")
        if source.is_symlink() or not source.is_file():
            raise MediaStorageError("local media path is unsafe")
        project_id = row.project_id

    token = uuid.uuid4().hex
    remote_validated = False
    from .tasks import cloud

    with cloud._remote_lock():
        with get_session() as session:
            session.exec(text("BEGIN IMMEDIATE"))
            row = session.get(MediaObject, media_object_id)
            policy = session.get(ProjectMediaPolicy, project_id)
            if not policy or policy.mode != CLOUD_PRIMARY:
                session.rollback()
                raise MediaStorageBusy("keep-local policy canceled media eviction")
            if _active_project_job(session, project_id, job_id):
                session.rollback()
                raise MediaStorageBusy(
                    "wait for all other project jobs to finish before freeing media")
            if _live_lease(session, media_object_id):
                session.rollback()
                raise MediaStorageBusy(
                    "media is currently being played or downloaded")
            if job_id is not None:
                eviction_job = session.get(Job, job_id)
                if not eviction_job or eviction_job.status != "running":
                    session.rollback()
                    raise MediaStorageBusy("media eviction job was canceled")
            if row.state != VERIFIED or not row.remote_sha256 or not row.verified_at:
                session.rollback()
                raise MediaStorageError(
                    "media verification changed before eviction")
            _object_may_transfer(session, row)
            target = session.get(MediaStorageTarget, row.storage_target_id)
            if not target:
                session.rollback()
                raise MediaStorageError("verified media target is missing")
            _assert_target_current(target)
            _validate_remote_key(row.remote_key)
            row.state = EVICTING
            row.eviction_token = token
            row.staging_path = _staging_spec(row, token)
            row.updated = utcnow()
            session.add(row)
            session.commit()
            staging_spec = row.staging_path
            local_spec = row.local_path
            expected_hash = row.remote_sha256
            expected_size = row.remote_size_bytes
            remote_key = row.remote_key
            target_id = row.storage_target_id

        source = resolve_local_path(local_spec)
        staged = resolve_local_path(staging_spec)
        staged.parent.mkdir(parents=True, exist_ok=True)
        try:
            # This is deliberately immediately before the local rename, under
            # the same cross-process lock used by target settings changes.
            remote_size, remote_hash = _stream_remote_sha256(remote_key)
            if remote_size != expected_size or remote_hash != expected_hash:
                raise MediaStorageError(
                    "remote media no longer matches its verified SHA-256; "
                    "the local copy was retained")
            remote_validated = True
            os.replace(source, staged)
            # Hash after the atomic rename so no different local bytes can be
            # deleted under verification belonging to an earlier file.
            if (staged.stat().st_size != expected_size
                    or _sha256(staged) != expected_hash):
                os.replace(staged, source)
                raise MediaStorageError(
                    "local media changed after verification; eviction was canceled")
            with get_session() as session:
                session.exec(text("BEGIN IMMEDIATE"))
                row = session.get(MediaObject, media_object_id)
                if (not row or row.state != EVICTING
                        or row.eviction_token != token
                        or row.storage_target_id != target_id
                        or row.remote_key != remote_key):
                    raise MediaStorageError(
                        "eviction state changed unexpectedly")
                policy = session.get(ProjectMediaPolicy, project_id)
                if not policy or policy.mode != CLOUD_PRIMARY:
                    raise MediaStorageBusy(
                        "keep-local policy canceled media eviction")
                if job_id is not None:
                    eviction_job = session.get(Job, job_id)
                    if not eviction_job or eviction_job.status != "running":
                        raise MediaStorageBusy("media eviction job was canceled")
                if _live_lease(session, media_object_id):
                    raise MediaStorageBusy(
                        "media began playback before eviction completed")
                _object_may_transfer(session, row)
                target = session.get(MediaStorageTarget, target_id)
                if not target:
                    raise MediaStorageError("verified media target is missing")
                _assert_target_current(target)
                row.state = CLOUD_ONLY
                row.updated = utcnow()
                session.add(row)
                session.commit()
            _safe_unlink(staged)
            with get_session() as session:
                row = session.get(MediaObject, media_object_id)
                if row and row.state == CLOUD_ONLY and row.eviction_token == token:
                    row.eviction_token = ""
                    row.staging_path = ""
                    row.updated = utcnow()
                    session.add(row)
                    session.commit()
            return True
        except Exception as exc:
            if staged.exists() and not source.exists():
                source.parent.mkdir(parents=True, exist_ok=True)
                os.replace(staged, source)
            with get_session() as session:
                session.exec(text("BEGIN IMMEDIATE"))
                row = session.get(MediaObject, media_object_id)
                if row and row.state == EVICTING and row.eviction_token == token:
                    row.state = (
                        VERIFIED if source.is_file() and remote_validated else ERROR
                    )
                    if not remote_validated:
                        row.verified_at = None
                    row.eviction_token = ""
                    row.staging_path = ""
                    row.last_error = str(exc)[:1000]
                    row.updated = utcnow()
                    session.add(row)
                session.commit()
            raise


def evict_project_media(project_id: int, *, job_id: int | None = None) -> dict:
    with get_session() as session:
        policy = get_policy(session, project_id)
        if policy.mode != CLOUD_PRIMARY:
            raise MediaStorageError("keep-local policy forbids media eviction")
        rows = session.exec(select(MediaObject).where(
            MediaObject.project_id == project_id,
            MediaObject.state == VERIFIED,
        ).order_by(MediaObject.id)).all()
        ids = [row.id for row in rows if row.id is not None]
    evicted = 0
    for media_object_id in ids:
        evicted += int(evict_media_object(media_object_id, job_id=job_id))
    return {"evicted": evicted}


def recover_interrupted_media_storage() -> dict:
    """Repair transitions not owned by a genuinely live media action.

    The API and worker can restart independently. A status-process restart must
    not rewrite a transfer that is still running in another process, so live
    Job rows own their project's transient rows and staging paths.
    """
    restored = removed = reset = skipped = 0
    with get_session() as session:
        session.exec(text("BEGIN IMMEDIATE"))
        live_projects = {
            project_id for project_id in session.exec(select(Job.project_id).where(
                Job.project_id != None,  # noqa: E711
                Job.task.in_(tuple(ACTION_TASKS.values())),
                Job.status.in_(("queued", "running")),
            )).all()
            if project_id is not None
        }
        rows = session.exec(select(MediaObject)).all()
        owned_staging: set[Path] = set()
        for row in rows:
            if row.project_id in live_projects:
                if row.staging_path:
                    owned_staging.add(
                        resolve_local_path(row.staging_path).resolve())
                if row.state in TRANSIENT_STATES:
                    skipped += 1
                continue
            local = resolve_local_path(row.local_path)
            staged = (
                resolve_local_path(row.staging_path) if row.staging_path else None)
            if row.state == EVICTING:
                if staged and staged.exists() and not local.exists():
                    local.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(staged, local)
                    restored += 1
                row.state = VERIFIED if local.is_file() else ERROR
                row.last_error = (
                    "" if local.is_file()
                    else "interrupted eviction could not restore its local file")
                row.eviction_token = ""
                row.staging_path = ""
                reset += 1
            elif row.state == CLOUD_ONLY and staged:
                if staged.exists():
                    staged.unlink(missing_ok=True)
                    removed += 1
                row.eviction_token = ""
                row.staging_path = ""
                reset += 1
            elif row.state == UPLOADING:
                if staged and staged.exists():
                    staged.unlink(missing_ok=True)
                    removed += 1
                # Preserve a published pending target/key for exact cleanup on
                # sync/purge retry, but never treat it as verified.
                row.state = ERROR
                row.last_error = (
                    "media upload was interrupted; retry sync to reconcile its "
                    "pending remote reference")
                row.eviction_token = ""
                row.staging_path = ""
                reset += 1
            elif row.state == RESTORING:
                if staged and staged.exists():
                    staged.unlink(missing_ok=True)
                    removed += 1
                if (local.is_file() and not local.is_symlink()
                        and row.remote_sha256
                        and local.stat().st_size == row.remote_size_bytes
                        and _sha256(local) == row.remote_sha256):
                    row.state = VERIFIED
                    row.sha256 = row.remote_sha256
                else:
                    row.state = (
                        CLOUD_ONLY
                        if row.remote_key and row.remote_sha256 and row.verified_at
                        else ERROR
                    )
                row.last_error = "media restore was interrupted by a restart"
                row.eviction_token = ""
                row.staging_path = ""
                reset += 1
            elif row.state == PURGING:
                row.state = ERROR
                row.last_error = (
                    "remote purge was interrupted after durable intent; retry "
                    "purge or sync before this media can be evicted")
                row.eviction_token = ""
                row.staging_path = ""
                reset += 1
            session.add(row)

        # Clean only validated staging roots while SQLite's writer lease keeps
        # a new operation from publishing a path between ownership discovery
        # and cleanup.
        for root in (settings.media_dir, settings.library_dir):
            for name in (
                "media-uploads", "media-restores", "media-evictions",
            ):
                stage_root = (root / ".staging" / name).resolve()
                if not stage_root.is_dir() or stage_root.is_symlink():
                    continue
                try:
                    stage_root.relative_to(root.resolve())
                except ValueError:
                    continue
                for candidate in stage_root.rglob("*"):
                    if (candidate.is_symlink() or not candidate.is_file()
                            or candidate.resolve() in owned_staging):
                        continue
                    candidate.unlink(missing_ok=True)
                    removed += 1
        session.commit()
    return {
        "restored": restored,
        "removed": removed,
        "reset": reset,
        "skipped_live": skipped,
    }


def acquire_lease(
    session: Session,
    media_object_id: int,
    *,
    owner: str,
    minutes: int = 240,
) -> MediaLease:
    lease = MediaLease(
        media_object_id=media_object_id,
        token=uuid.uuid4().hex,
        owner=owner,
        expires_at=utcnow() + timedelta(minutes=max(1, minutes)),
    )
    session.add(lease)
    session.commit()
    session.refresh(lease)
    return lease


def release_lease(lease_id: int) -> None:
    with get_session() as session:
        lease = session.get(MediaLease, lease_id)
        if lease:
            session.delete(lease)
            session.commit()


def ensure_artifact_media_local(artifact_id: int) -> tuple[Path, int | None]:
    """Return a local artifact payload, restoring verified cloud media first."""
    with get_session() as session:
        artifact = session.get(Artifact, artifact_id)
        if not artifact or not artifact.media_path:
            raise FileNotFoundError("artifact media is missing")
        path = library.resolve_media_path(artifact.media_path)
        inventory_project(session, artifact.project_id)
        row = session.exec(select(MediaObject).where(
            MediaObject.artifact_id == artifact_id
        )).first()
        if path.is_file():
            if (row and row.state == ERROR and row.remote_sha256
                    and (path.stat().st_size != row.remote_size_bytes
                         or _sha256(path) != row.remote_sha256)):
                raise MediaStorageError(
                    "local media conflicts with its verified cloud copy")
            return path, row.id if row else None
        if not row:
            raise FileNotFoundError("artifact media is missing")
        media_object_id = row.id
    return restore_media_object(media_object_id), media_object_id


def prepare_artifact_playback(artifact_id: int) -> tuple[Path, int | None]:
    # Establish the lease under SQLite's writer lock before checking/restoring
    # the payload.  Eviction performs its lease check under the same lock, so
    # neither side can pass its guard in the other's gap.
    with get_session() as session:
        artifact = session.get(Artifact, artifact_id)
        if not artifact or not artifact.media_path:
            raise FileNotFoundError("artifact media is missing")
        project = session.get(Project, artifact.project_id)
        if not project or project.deleting:
            raise MediaStorageBusy(
                "project deletion has started; new media playback is blocked")
        inventory_project(session, artifact.project_id)
        row = session.exec(select(MediaObject).where(
            MediaObject.artifact_id == artifact_id
        )).first()
        if not row:
            path = library.resolve_media_path(artifact.media_path)
            if not path.is_file():
                raise FileNotFoundError("artifact media is missing")
            return path, None
        media_object_id = row.id
    with get_session() as session:
        session.exec(text("BEGIN IMMEDIATE"))
        row = session.get(MediaObject, media_object_id)
        if not row or row.state == EVICTING:
            session.rollback()
            raise MediaStorageBusy("media is currently being evicted")
        project = session.get(Project, row.project_id)
        if not project or project.deleting:
            session.rollback()
            raise MediaStorageBusy(
                "project deletion has started; new media playback is blocked")
        lease = acquire_lease(
            session, media_object_id, owner=f"playback:artifact:{artifact_id}")
        lease_id = lease.id
    try:
        path, _media_object_id = ensure_artifact_media_local(artifact_id)
        return path, lease_id
    except Exception:
        release_lease(lease_id)
        raise
