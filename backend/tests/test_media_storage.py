from __future__ import annotations

import hashlib
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select, text

from app import library, media_storage
from app.db import get_session
from app.main import app
from app.models import (
    Artifact,
    Job,
    MediaLease,
    MediaObject,
    MediaStorageTarget,
    PaperSeries,
    PaperSeriesPart,
    Project,
    ProjectMediaPolicy,
)
from app.settings_store import get_setting, set_setting
from app.tasks import ingest as ingest_tasks
from app.tasks import media_storage as media_tasks
from app.trusted_origin import (
    TRUSTED_ORIGIN_HEADER,
    TRUSTED_REQUEST_HOST_HEADER,
)


@pytest.fixture(scope="module")
def client():
    with TestClient(
        app,
        headers={
            TRUSTED_ORIGIN_HEADER: "http://localhost:8080",
            TRUSTED_REQUEST_HOST_HEADER: "localhost:8080",
        },
    ) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def cloud_settings():
    keys = ("cloud.provider", "cloud.config", "cloud.remote_base", "cloud.auto")
    previous = {key: get_setting(key) for key in keys}
    set_setting("cloud.provider", "s3")
    set_setting("cloud.config", {
        "endpoint": "https://objects.example.test",
        "region": "test-1",
        "bucket": "synapse-media-test",
        "access_key_id": "test-key",
        "secret_access_key": "test-secret",
    })
    set_setting("cloud.remote_base", "synapse-tests")
    set_setting("cloud.auto", False)
    yield
    for key, value in previous.items():
        set_setting(key, value)


def _slug(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def _media_project(
    *,
    prefix: str = "media-storage",
    payload: bytes = b"durable audio bytes",
    source_type: str = "local",
) -> tuple[SimpleNamespace, SimpleNamespace, Path]:
    slug = _slug(prefix)
    with get_session() as session:
        project = Project(
            slug=slug,
            title=slug,
            source="sample.mp3",
            source_type=source_type,
        )
        session.add(project)
        session.commit()
        session.refresh(project)
        media_path = library.settings.media_dir / slug / "source.mp3"
        media_path.parent.mkdir(parents=True, exist_ok=True)
        media_path.write_bytes(payload)
        artifact = library.write_artifact(
            session,
            project_id=project.id,
            project_slug=slug,
            type="source_audio",
            title=f"Source audio — {slug}",
            body="durable source",
            media_rel=f"media:{slug}/source.mp3",
        )
        return (
            SimpleNamespace(id=project.id, slug=slug),
            SimpleNamespace(id=artifact.id),
            media_path,
        )


def _paper_series_media(
    *, prefix: str = "paper-series-media"
) -> tuple[SimpleNamespace, SimpleNamespace, SimpleNamespace, Path, MediaObject]:
    slug = _slug(prefix)
    with get_session() as session:
        project = Project(
            slug=slug,
            title=slug,
            source="paper.pdf",
            source_type="paper",
        )
        session.add(project)
        session.flush()
        series = PaperSeries(
            project_id=project.id,
            audience="generalist",
            title="Deletion safety",
        )
        session.add(series)
        session.flush()
        part = PaperSeriesPart(
            series_id=series.id,
            position=1,
            title="Part one",
        )
        session.add(part)
        session.flush()
        media_rel = f"projects/{slug}/part-one.mp3"
        media_path = library.settings.library_dir / media_rel
        media_path.parent.mkdir(parents=True, exist_ok=True)
        media_path.write_bytes(b"paper series audio")
        artifact = library.write_artifact(
            session,
            project_id=project.id,
            project_slug=slug,
            type="paper_part_audio",
            title="Part one audio",
            body="paper audio",
            media_rel=media_rel,
            paper_series_id=series.id,
            paper_part_id=part.id,
        )
        row = MediaObject(
            project_id=project.id,
            artifact_id=artifact.id,
            role="paper_part_audio",
            local_path=media_rel,
            state=media_storage.LOCAL,
            size_bytes=media_path.stat().st_size,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return (
            SimpleNamespace(id=project.id, slug=slug),
            SimpleNamespace(id=series.id),
            SimpleNamespace(id=artifact.id),
            media_path,
            row,
        )


def _enable(project_id: int) -> MediaObject:
    with get_session() as session:
        media_storage.set_policy(
            session, project_id, media_storage.CLOUD_PRIMARY)
        media_storage.inventory_project(session, project_id)
        return session.exec(select(MediaObject).where(
            MediaObject.project_id == project_id,
            MediaObject.role == "source_audio",
        )).one()


def _remote_copy(monkeypatch, *, corrupt_readback: bool = False):
    remote: dict[str, bytes] = {}

    def copyto(source: str, destination: str):
        if destination.startswith("synapse:"):
            remote[destination] = Path(source).read_bytes()
        else:
            payload = remote[source]
            Path(destination).parent.mkdir(parents=True, exist_ok=True)
            Path(destination).write_bytes(payload)

    def stream_hash(remote_key: str):
        payload = remote[media_storage._remote_destination(remote_key)]
        if corrupt_readback:
            payload += b"-corrupt"
        return len(payload), hashlib.sha256(payload).hexdigest()

    monkeypatch.setattr(media_storage, "_rclone_copyto", copyto)
    monkeypatch.setattr(media_storage, "_stream_remote_sha256", stream_hash)
    return remote


def test_policy_defaults_local_and_rejects_non_s3_authority(client):
    project, _artifact, _path = _media_project(prefix="policy")
    response = client.get(f"/api/projects/{project.id}/media-storage")
    assert response.status_code == 200
    assert response.json()["policy"] == {
        "mode": "keep_local", "storage_target_id": None}

    set_setting("cloud.provider", "drive")
    set_setting("cloud.config", {"token": "{\"access_token\":\"x\"}"})
    response = client.put(
        f"/api/projects/{project.id}/media-storage",
        json={"mode": "cloud_primary"},
    )
    assert response.status_code == 409
    assert "S3-compatible" in response.text
    with get_session() as session:
        assert session.get(ProjectMediaPolicy, project.id) is None


def test_inventory_includes_known_media_but_excludes_credentials_and_restricted(client):
    project, artifact, _path = _media_project(prefix="inventory")
    work = library.settings.media_dir / project.slug
    (work / "cookies.txt").write_text("secret", encoding="utf-8")
    (work / "auth-context.json").write_text("secret", encoding="utf-8")
    response = client.get(f"/api/projects/{project.id}/media-storage")
    assert response.status_code == 200
    assert [row["role"] for row in response.json()["objects"]] == ["source_audio"]

    with get_session() as session:
        stored = session.get(Artifact, artifact.id)
        stored.restricted = True
        session.add(stored)
        session.commit()
    response = client.get(f"/api/projects/{project.id}/media-storage")
    assert response.json()["summary"]["eligible_objects"] == 0
    assert response.json()["summary"]["excluded_objects"] == 1
    with get_session() as session:
        excluded_row = session.exec(select(MediaObject).where(
            MediaObject.project_id == project.id
        )).one()
        assert excluded_row.state == media_storage.ERROR
        assert excluded_row.remote_key == ""
    with get_session() as session:
        stored = session.get(Artifact, artifact.id)
        stored.restricted = False
        session.add(stored)
        session.commit()


def test_upload_readback_verify_evict_and_playback_restore(
    client, monkeypatch,
):
    payload = b"verified lifecycle payload"
    project, artifact, local = _media_project(
        prefix="lifecycle", payload=payload)
    row = _enable(project.id)
    remote = _remote_copy(monkeypatch)

    assert media_storage.sync_media_object(row.id) is True
    with get_session() as session:
        verified = session.get(MediaObject, row.id)
        assert verified.state == media_storage.VERIFIED
        assert verified.sha256 == verified.remote_sha256
        assert verified.remote_key
    assert local.read_bytes() == payload

    assert media_storage.evict_media_object(row.id) is True
    assert not local.exists()
    with get_session() as session:
        assert session.get(MediaObject, row.id).state == media_storage.CLOUD_ONLY

    response = client.get(f"/api/media/{artifact.id}")
    assert response.status_code == 200
    assert response.content == payload
    assert local.read_bytes() == payload
    with get_session() as session:
        restored = session.get(MediaObject, row.id)
        assert restored.state == media_storage.VERIFIED
        assert not session.exec(select(MediaLease).where(
            MediaLease.media_object_id == row.id
        )).first()
    assert remote


def test_restore_reestablishes_durable_verification_timestamp(monkeypatch):
    project, _artifact, local = _media_project(
        prefix="restore-verification", payload=b"verified remote payload")
    row = _enable(project.id)
    _remote_copy(monkeypatch)
    media_storage.sync_media_object(row.id)
    local.unlink()
    with get_session() as session:
        stored = session.get(MediaObject, row.id)
        stored.state = media_storage.ERROR
        stored.verified_at = None
        session.add(stored)
        session.commit()

    assert media_storage.restore_media_object(row.id) == local
    with get_session() as session:
        restored = session.get(MediaObject, row.id)
        assert restored.state == media_storage.VERIFIED
        assert restored.verified_at is not None


def test_failed_remote_readback_never_authorizes_eviction(monkeypatch):
    project, _artifact, local = _media_project(prefix="bad-readback")
    row = _enable(project.id)
    remote = _remote_copy(monkeypatch, corrupt_readback=True)
    monkeypatch.setattr(
        media_storage,
        "_deletefile_idempotent",
        lambda destination: remote.pop(destination, None),
    )

    with pytest.raises(media_storage.MediaStorageError, match="readback"):
        media_storage.sync_media_object(row.id)
    assert remote == {}
    assert local.exists()
    with get_session() as session:
        failed = session.get(MediaObject, row.id)
        assert failed.state == media_storage.ERROR
        assert failed.verified_at is None
        assert failed.remote_key == ""
    with pytest.raises(media_storage.MediaStorageError, match="not durably verified"):
        media_storage.evict_media_object(row.id)
    assert local.exists()


def test_unexpected_local_conflict_preserves_remote_recovery_reference(monkeypatch):
    project, _artifact, local = _media_project(
        prefix="local-conflict", payload=b"remote-original")
    row = _enable(project.id)
    remote = _remote_copy(monkeypatch)
    media_storage.sync_media_object(row.id)
    media_storage.evict_media_object(row.id)
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_bytes(b"unexpected-local")

    with get_session() as session:
        media_storage.inventory_project(session, project.id)
        conflict = session.get(MediaObject, row.id)
        remote_key = conflict.remote_key
        assert conflict.state == media_storage.ERROR
        assert conflict.remote_sha256
        assert remote_key
    with pytest.raises(media_storage.MediaStorageError, match="conflicts"):
        media_storage.restore_media_object(row.id)
    assert local.read_bytes() == b"unexpected-local"
    assert remote
    with get_session() as session:
        conflict = session.get(MediaObject, row.id)
        assert conflict.remote_key == remote_key
        assert conflict.remote_sha256


def test_interrupted_eviction_recovery_restores_staged_file(monkeypatch):
    project, _artifact, local = _media_project(
        prefix="eviction-recovery", payload=b"recover me")
    row = _enable(project.id)
    _remote_copy(monkeypatch)
    media_storage.sync_media_object(row.id)
    token = uuid.uuid4().hex
    with get_session() as session:
        stored = session.get(MediaObject, row.id)
        stored.state = media_storage.EVICTING
        stored.eviction_token = token
        stored.staging_path = media_storage._staging_spec(stored, token)
        staging = media_storage.resolve_local_path(stored.staging_path)
        staging.parent.mkdir(parents=True, exist_ok=True)
        local.replace(staging)
        session.add(stored)
        session.commit()

    result = media_storage.recover_interrupted_media_storage()
    assert result["restored"] >= 1
    assert local.read_bytes() == b"recover me"
    with get_session() as session:
        assert session.get(MediaObject, row.id).state == media_storage.VERIFIED


def test_playback_lease_blocks_eviction_until_released(monkeypatch):
    project, _artifact, local = _media_project(prefix="lease-fence")
    row = _enable(project.id)
    _remote_copy(monkeypatch)
    media_storage.sync_media_object(row.id)
    with get_session() as session:
        lease = media_storage.acquire_lease(
            session, row.id, owner="test-playback", minutes=5)
        lease_id = lease.id

    with pytest.raises(media_storage.MediaStorageBusy, match="played"):
        media_storage.evict_media_object(row.id)
    assert local.exists()
    media_storage.release_lease(lease_id)
    assert media_storage.evict_media_object(row.id) is True
    assert not local.exists()


def test_policy_change_during_eviction_is_blocked_and_restores_staged_bytes(
    monkeypatch,
):
    project, _artifact, local = _media_project(
        prefix="policy-race", payload=b"policy race")
    row = _enable(project.id)
    _remote_copy(monkeypatch)
    media_storage.sync_media_object(row.id)
    real_replace = media_storage.os.replace
    changed = False

    def replace_and_change_policy(source, destination):
        nonlocal changed
        real_replace(source, destination)
        if not changed and Path(source) == local:
            changed = True
            with get_session() as session:
                media_storage.set_policy(
                    session, project.id, media_storage.KEEP_LOCAL)

    monkeypatch.setattr(media_storage.os, "replace", replace_and_change_policy)
    with pytest.raises(media_storage.MediaStorageBusy, match="storage state recovery"):
        media_storage.evict_media_object(row.id)
    assert local.read_bytes() == b"policy race"
    with get_session() as session:
        stored = session.get(MediaObject, row.id)
        assert stored.state == media_storage.VERIFIED
        assert session.get(ProjectMediaPolicy, project.id).mode == \
            media_storage.CLOUD_PRIMARY


def test_canceled_before_pickup_evict_task_keeps_local(monkeypatch):
    project, _artifact, local = _media_project(prefix="cancel-evict")
    row = _enable(project.id)
    _remote_copy(monkeypatch)
    media_storage.sync_media_object(row.id)
    with get_session() as session:
        job = Job(
            project_id=project.id,
            task=media_storage.ACTION_TASKS["evict"],
            status="canceled",
        )
        session.add(job)
        session.commit()
        session.refresh(job)
        job_id = job.id

    assert media_tasks.evict_project.run(job_id, project.id) is None
    assert local.exists()
    with get_session() as session:
        assert session.get(MediaObject, row.id).state == media_storage.VERIFIED
        assert session.get(Job, job_id).status == "canceled"


def test_upload_rerun_restores_cloud_only_original(monkeypatch):
    slug = _slug("upload-rerun")
    payload = b"ID3 uploaded source"
    with get_session() as session:
        project = Project(
            slug=slug, title=slug, source="uploaded.mp3", source_type="upload")
        session.add(project)
        session.commit()
        session.refresh(project)
        uploaded = library.settings.media_dir / slug / "uploaded.mp3"
        uploaded.parent.mkdir(parents=True, exist_ok=True)
        uploaded.write_bytes(payload)
        project_id = project.id
        media_storage.set_policy(
            session, project.id, media_storage.CLOUD_PRIMARY)
        media_storage.inventory_project(session, project.id)
        original = session.exec(select(MediaObject).where(
            MediaObject.project_id == project.id,
            MediaObject.role == "original_upload",
        )).one()
        original_id = original.id
    _remote_copy(monkeypatch)
    media_storage.sync_media_object(original_id)
    media_storage.evict_media_object(original_id)
    assert not uploaded.exists()

    with get_session() as session:
        job = Job(project_id=project_id, task="ingest")
        session.add(job)
        session.commit()
        session.refresh(job)
        job_id = job.id
    monkeypatch.setattr(ingest_tasks, "auto_tag", lambda *_args: None)
    ingest_tasks.ingest.run(job_id, project_id)
    assert uploaded.read_bytes() == payload
    assert (library.settings.media_dir / slug / "source.mp3").read_bytes() == payload


def test_target_change_is_blocked_but_credential_rotation_is_allowed(
    client, monkeypatch,
):
    project, _artifact, _local = _media_project(prefix="target-fence")
    row = _enable(project.id)
    _remote_copy(monkeypatch)
    media_storage.sync_media_object(row.id)

    current = get_setting("cloud.config")
    rotated = {**current, "secret_access_key": "rotated-secret"}
    response = client.put("/api/settings/cloud", json={
        "provider": "s3",
        "config": rotated,
        "remote_base": "synapse-tests",
        "auto": False,
        "mode": "push",
    })
    assert response.status_code == 200

    response = client.put("/api/settings/cloud", json={
        "provider": "s3",
        "config": {**rotated, "bucket": "different-bucket"},
        "remote_base": "synapse-tests",
        "auto": False,
        "mode": "push",
    })
    assert response.status_code == 409
    assert "locked" in response.text


def test_action_endpoint_returns_standard_job(client, monkeypatch):
    from app.routers import media_storage as media_router

    project, _artifact, _local = _media_project(prefix="action-job")
    _enable(project.id)
    monkeypatch.setattr(
        media_router.celery,
        "send_task",
        lambda *_args, **_kwargs: SimpleNamespace(id="media-task-id"),
    )
    response = client.post(
        f"/api/projects/{project.id}/media-storage/sync")
    assert response.status_code == 200
    job = response.json()
    assert job["project_id"] == project.id
    assert job["task"] == media_storage.ACTION_TASKS["sync"]
    assert job["status"] == "queued"
    assert job["celery_id"] == "media-task-id"
    with get_session() as session:
        stored = session.get(Job, job["id"])
        stored.status = "done"
        session.add(stored)
        session.commit()


def test_media_action_fences_new_pipeline_work(client):
    project, _artifact, _local = _media_project(prefix="pipeline-fence")
    with get_session() as session:
        job = Job(
            project_id=project.id,
            task=media_storage.ACTION_TASKS["evict"],
            status="running",
        )
        session.add(job)
        session.commit()
        session.refresh(job)
        job_id = job.id
    response = client.post(f"/api/projects/{project.id}/run/transcribe")
    assert response.status_code == 409
    assert "media storage action" in response.text
    with get_session() as session:
        job = session.get(Job, job_id)
        job.status = "canceled"
        session.add(job)
        session.commit()


def test_project_delete_refuses_to_orphan_verified_remote_media(
    client, monkeypatch,
):
    project, _artifact, local = _media_project(prefix="delete-fence")
    row = _enable(project.id)
    _remote_copy(monkeypatch)
    media_storage.sync_media_object(row.id)
    response = client.delete(f"/api/projects/{project.id}")
    assert response.status_code == 409
    assert "remote media" in response.text
    assert local.exists()
    with get_session() as session:
        assert session.get(Project, project.id) is not None
        assert session.get(MediaObject, row.id).remote_key


def test_legacy_only_media_must_be_purged_before_project_deletion(
    client, monkeypatch,
):
    project, _artifact, local = _media_project(prefix="legacy-only-fence")
    row = _enable(project.id)
    with get_session() as session:
        media_storage.set_policy(
            session, project.id, media_storage.KEEP_LOCAL)

    response = client.delete(f"/api/projects/{project.id}")
    assert response.status_code == 409
    assert "legacy cloud paths" in response.text
    deleted: list[str] = []
    monkeypatch.setattr(
        media_storage,
        "_deletefile_idempotent",
        lambda destination: deleted.append(destination),
    )

    result = media_storage.purge_project_remote_media(project.id)
    assert result["cleared"] == 1
    assert result["remote_objects_deleted"] == 0
    assert result["legacy_objects_deleted"] == 1
    assert local.is_file()
    assert media_storage._legacy_remote_destination(
        f"media:{project.slug}/source.mp3") in deleted
    with get_session() as session:
        policy = session.get(ProjectMediaPolicy, project.id)
        assert policy.storage_target_id is None
        assert session.get(MediaObject, row.id).remote_key == ""


def test_keep_local_purge_retains_shared_object_then_deletes_last_reference(
    monkeypatch,
):
    first, _artifact, first_local = _media_project(
        prefix="shared-first", payload=b"shared-content")
    second, _artifact, second_local = _media_project(
        prefix="shared-second", payload=b"shared-content")
    first_row = _enable(first.id)
    second_row = _enable(second.id)
    remote = _remote_copy(monkeypatch)
    media_storage.sync_media_object(first_row.id)
    media_storage.sync_media_object(second_row.id)
    with get_session() as session:
        first_stored = session.get(MediaObject, first_row.id)
        second_stored = session.get(MediaObject, second_row.id)
        assert first_stored.remote_key == second_stored.remote_key
        remote_key = first_stored.remote_key
        media_storage.set_policy(session, first.id, media_storage.KEEP_LOCAL)
        media_storage.set_policy(session, second.id, media_storage.KEEP_LOCAL)

    def delete_remote(key: str):
        remote.pop(media_storage._remote_destination(key), None)

    monkeypatch.setattr(media_storage, "_delete_remote_key", delete_remote)
    monkeypatch.setattr(
        media_storage,
        "_deletefile_idempotent",
        lambda destination: remote.pop(destination, None),
    )
    first_result = media_storage.purge_project_remote_media(first.id)
    assert first_result == {
        "cleared": 1,
        "remote_objects_deleted": 0,
        "shared_objects_retained": 1,
        "legacy_objects_deleted": 1,
        "legacy_objects_retained": 0,
    }
    assert first_local.exists() and second_local.exists()
    assert media_storage._remote_destination(remote_key) in remote
    with get_session() as session:
        assert session.get(MediaObject, first_row.id).remote_key == ""
        assert session.get(MediaObject, second_row.id).remote_key == remote_key

    second_result = media_storage.purge_project_remote_media(second.id)
    assert second_result == {
        "cleared": 1,
        "remote_objects_deleted": 1,
        "shared_objects_retained": 0,
        "legacy_objects_deleted": 1,
        "legacy_objects_retained": 0,
    }
    assert media_storage._remote_destination(remote_key) not in remote
    with get_session() as session:
        assert session.get(MediaObject, second_row.id).remote_key == ""


def test_project_sync_skips_retained_ineligible_media(monkeypatch):
    project, _artifact, _local = _media_project(prefix="skip-ineligible")
    row = _enable(project.id)
    stale_path = library.settings.media_dir / project.slug / "stale.mp3"
    stale_path.write_bytes(b"local-only stale bytes")
    with get_session() as session:
        session.add(MediaObject(
            project_id=project.id,
            artifact_id=None,
            role="stale_local_media",
            local_path=f"media:{project.slug}/stale.mp3",
            state=media_storage.ERROR,
            size_bytes=stale_path.stat().st_size,
        ))
        session.commit()
    _remote_copy(monkeypatch)

    result = media_storage.sync_project_media(project.id)
    assert result == {"verified": 1, "skipped": 1}
    with get_session() as session:
        assert session.get(MediaObject, row.id).state == media_storage.VERIFIED


def test_running_media_integrity_action_cannot_be_canceled(client):
    project, _artifact, _local = _media_project(prefix="cancel-running")
    with get_session() as session:
        job = Job(
            project_id=project.id,
            task=media_storage.ACTION_TASKS["purge"],
            status="running",
        )
        session.add(job)
        session.commit()
        session.refresh(job)
        job_id = job.id
    response = client.post(f"/api/jobs/{job_id}/cancel")
    assert response.status_code == 409
    assert "cannot be canceled" in response.text
    with get_session() as session:
        stored = session.get(Job, job_id)
        assert stored.status == "running"
        stored.status = "error"
        session.add(stored)
        session.commit()


def test_policy_change_is_rejected_while_media_action_is_running(client):
    project, _artifact, _local = _media_project(prefix="policy-job-fence")
    with get_session() as session:
        media_storage.set_policy(
            session, project.id, media_storage.KEEP_LOCAL)
        job = Job(
            project_id=project.id,
            task=media_storage.ACTION_TASKS["purge"],
            status="running",
        )
        session.add(job)
        session.commit()
        session.refresh(job)
        job_id = job.id

    response = client.put(
        f"/api/projects/{project.id}/media-storage",
        json={"mode": "cloud_primary"},
    )
    assert response.status_code == 409
    assert "active media storage action" in response.text
    with get_session() as session:
        assert session.get(ProjectMediaPolicy, project.id).mode == \
            media_storage.KEEP_LOCAL
        job = session.get(Job, job_id)
        job.status = "error"
        session.add(job)
        session.commit()


def test_interrupted_purge_never_reauthorizes_eviction(monkeypatch):
    project, _artifact, local = _media_project(
        prefix="purge-crash", payload=b"purge crash safety")
    row = _enable(project.id)
    remote = _remote_copy(monkeypatch)
    media_storage.sync_media_object(row.id)
    with get_session() as session:
        stored = session.get(MediaObject, row.id)
        remote_key = stored.remote_key
        media_storage.set_policy(
            session, project.id, media_storage.KEEP_LOCAL)

    def delete_then_crash(key: str):
        remote.pop(media_storage._remote_destination(key), None)
        raise SystemExit("simulated process death after remote delete")

    monkeypatch.setattr(media_storage, "_delete_remote_key", delete_then_crash)
    with pytest.raises(SystemExit, match="simulated process death"):
        media_storage.purge_project_remote_media(project.id)
    with get_session() as session:
        interrupted = session.get(MediaObject, row.id)
        assert interrupted.state == media_storage.PURGING
        assert interrupted.remote_key == remote_key
        assert interrupted.verified_at is not None
    assert local.is_file()

    media_storage.recover_interrupted_media_storage()
    with get_session() as session:
        recovered = session.get(MediaObject, row.id)
        assert recovered.state == media_storage.ERROR
        assert recovered.remote_key == remote_key
        assert recovered.verified_at is not None
        media_storage.set_policy(
            session, project.id, media_storage.CLOUD_PRIMARY)
    with pytest.raises(media_storage.MediaStorageError, match="not durably verified"):
        media_storage.evict_media_object(row.id)
    assert local.is_file()

    with get_session() as session:
        media_storage.set_policy(
            session, project.id, media_storage.KEEP_LOCAL)
    monkeypatch.setattr(media_storage, "_delete_remote_key", lambda _key: None)
    monkeypatch.setattr(
        media_storage, "_deletefile_idempotent", lambda _destination: None)
    result = media_storage.purge_project_remote_media(project.id)
    assert result["cleared"] == 1
    with get_session() as session:
        cleared = session.get(MediaObject, row.id)
        assert cleared.state == media_storage.LOCAL
        assert cleared.remote_key == ""


def test_eviction_revalidates_remote_bytes_immediately_before_local_removal(
    monkeypatch,
):
    project, _artifact, local = _media_project(
        prefix="remote-revalidate", payload=b"authoritative local")
    row = _enable(project.id)
    remote = _remote_copy(monkeypatch)
    media_storage.sync_media_object(row.id)
    with get_session() as session:
        stored = session.get(MediaObject, row.id)
        destination = media_storage._remote_destination(stored.remote_key)
    remote[destination] = b"externally corrupted"

    with pytest.raises(media_storage.MediaStorageError, match="no longer matches"):
        media_storage.evict_media_object(row.id)
    assert local.read_bytes() == b"authoritative local"
    with get_session() as session:
        failed = session.get(MediaObject, row.id)
        assert failed.state == media_storage.ERROR
        assert failed.verified_at is None


def test_low_disk_admission_fails_before_upload_staging(monkeypatch):
    project, _artifact, local = _media_project(
        prefix="low-disk", payload=b"x" * 1024)
    row = _enable(project.id)
    monkeypatch.setattr(
        media_storage.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(
            total=10 * 1024**3,
            used=10 * 1024**3 - 1024,
            free=1024,
        ),
    )
    with pytest.raises(media_storage.MediaStorageError, match="insufficient free"):
        media_storage.sync_media_object(row.id)
    assert local.is_file()
    with get_session() as session:
        stored = session.get(MediaObject, row.id)
        assert stored.state == media_storage.LOCAL
        assert stored.remote_key == ""
        assert stored.staging_path == ""


def test_corrupt_remote_key_cannot_broaden_purge(monkeypatch):
    from app.tasks import cloud

    project, _artifact, local = _media_project(prefix="corrupt-remote-key")
    row = _enable(project.id)
    _remote_copy(monkeypatch)
    media_storage.sync_media_object(row.id)
    with get_session() as session:
        stored = session.get(MediaObject, row.id)
        stored.remote_key = "../../other-project"
        session.add(stored)
        session.commit()
        media_storage.set_policy(
            session, project.id, media_storage.KEEP_LOCAL)
    calls: list[list[str]] = []
    monkeypatch.setattr(cloud, "_rclone", lambda args: calls.append(list(args)))

    with pytest.raises(media_storage.MediaStorageError, match="remote key is invalid"):
        media_storage.purge_project_remote_media(project.id)
    assert calls == []
    assert local.is_file()


def test_cleanup_target_cannot_be_silently_rebound_without_remote_references(
    client,
):
    project, _artifact, _local = _media_project(prefix="target-rebind")
    _enable(project.id)
    with get_session() as session:
        stale_target = MediaStorageTarget(
            identity_hash=f"stale-{uuid.uuid4().hex}",
            provider="s3",
            remote_base="retired",
            config_fingerprint="stale",
        )
        session.add(stale_target)
        session.flush()
        policy = session.get(ProjectMediaPolicy, project.id)
        policy.storage_target_id = stale_target.id
        session.add(policy)
        session.commit()
        stale_target_id = stale_target.id

    current = get_setting("cloud.config")
    response = client.put("/api/settings/cloud", json={
        "provider": "s3",
        "config": {**current, "secret_access_key": "rebound-secret"},
        "remote_base": "synapse-tests",
        "auto": False,
        "mode": "push",
    })
    assert response.status_code == 409
    assert "locked" in response.text
    with get_session() as session:
        policy = session.get(ProjectMediaPolicy, project.id)
        assert policy.storage_target_id == stale_target_id


def test_cloud_primary_policy_rejects_non_s3_target_even_without_references(client):
    project, _artifact, _local = _media_project(prefix="non-s3-policy")
    _enable(project.id)
    response = client.put("/api/settings/cloud", json={
        "provider": "drive",
        "config": {"token": "{\"access_token\":\"x\"}"},
        "remote_base": "synapse-tests",
        "auto": False,
        "mode": "push",
    })
    assert response.status_code == 409
    assert "S3-compatible" in response.text
    assert get_setting("cloud.provider") == "s3"


def test_status_preserves_remote_reference_when_artifact_media_path_changes(
    client, monkeypatch,
):
    project, artifact, original = _media_project(prefix="path-change")
    row = _enable(project.id)
    _remote_copy(monkeypatch)
    media_storage.sync_media_object(row.id)
    replacement = original.with_name("replacement.mp3")
    replacement.write_bytes(b"replacement bytes")
    with get_session() as session:
        stored_row = session.get(MediaObject, row.id)
        remote_key = stored_row.remote_key
        target_id = stored_row.storage_target_id
        stored_artifact = session.get(Artifact, artifact.id)
        stored_artifact.media_path = (
            f"media:{project.slug}/replacement.mp3")
        session.add(stored_artifact)
        session.commit()

    response = client.get(f"/api/projects/{project.id}/media-storage")
    assert response.status_code == 200
    with get_session() as session:
        preserved = session.get(MediaObject, row.id)
        assert preserved.local_path == f"media:{project.slug}/source.mp3"
        assert preserved.remote_key == remote_key
        assert preserved.storage_target_id == target_id
        assert preserved.state == media_storage.ERROR


def test_legacy_path_sync_rechecks_cloud_primary_policy_after_staging(
    monkeypatch,
):
    from app.tasks import cloud

    project, _artifact, _local = _media_project(prefix="legacy-policy-race")
    real_stage = cloud._stage_public_path

    def stage_then_enable(path: str):
        snapshot = real_stage(path)
        with get_session() as session:
            media_storage.set_policy(
                session, project.id, media_storage.CLOUD_PRIMARY)
        return snapshot

    calls: list[list[str]] = []
    monkeypatch.setattr(cloud, "_stage_public_path", stage_then_enable)
    monkeypatch.setattr(cloud, "_rclone", lambda args: calls.append(list(args)))
    result = cloud.sync_paths.run([f"media:{project.slug}/source.mp3"])
    assert result == {"uploaded": 0, "skipped": 1}
    assert calls == []


def test_legacy_path_sync_fences_cleanup_before_remote_mutation(monkeypatch):
    from app.tasks import cloud

    project, _artifact, _local = _media_project(prefix="legacy-cleanup-fence")
    _enable(project.id)
    with get_session() as session:
        media_storage.set_policy(
            session, project.id, media_storage.KEEP_LOCAL)
        policy = session.get(ProjectMediaPolicy, project.id)
        policy.storage_target_id = None
        session.add(policy)
        session.commit()

    def fail_after_fence(_args: list[str]):
        with get_session() as session:
            policy = session.get(ProjectMediaPolicy, project.id)
            assert policy.storage_target_id is not None
        raise RuntimeError("simulated legacy upload failure")

    monkeypatch.setattr(cloud, "_rclone", fail_after_fence)
    with pytest.raises(RuntimeError, match="simulated legacy upload failure"):
        cloud.sync_paths.run([f"media:{project.slug}/source.mp3"])
    with get_session() as session:
        policy = session.get(ProjectMediaPolicy, project.id)
        assert policy.storage_target_id is not None


def test_recovery_skips_transient_state_owned_by_live_media_job():
    project, _artifact, local = _media_project(prefix="live-recovery")
    row = _enable(project.id)
    token = uuid.uuid4().hex
    with get_session() as session:
        stored = session.get(MediaObject, row.id)
        stored.state = media_storage.UPLOADING
        stored.eviction_token = token
        stored.staging_path = media_storage._transfer_staging_spec(
            stored, "uploads", token)
        staged = media_storage.resolve_local_path(stored.staging_path)
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_bytes(b"active upload snapshot")
        session.add(stored)
        job = Job(
            project_id=project.id,
            task=media_storage.ACTION_TASKS["sync"],
            status="running",
            celery_id="live-worker",
        )
        session.add(job)
        session.commit()
        session.refresh(job)
        job_id = job.id

    result = media_storage.recover_interrupted_media_storage()
    assert result["skipped_live"] >= 1
    assert staged.is_file()
    with get_session() as session:
        assert session.get(MediaObject, row.id).state == media_storage.UPLOADING
        job = session.get(Job, job_id)
        job.status = "error"
        session.add(job)
        session.commit()

    media_storage.recover_interrupted_media_storage()
    assert not staged.exists()
    assert local.is_file()
    with get_session() as session:
        assert session.get(MediaObject, row.id).state == media_storage.ERROR


def test_paper_series_deletion_blocks_live_media_lease(client):
    project, series, artifact, _path, row = _paper_series_media(
        prefix="series-live-lease")
    with get_session() as session:
        lease = media_storage.acquire_lease(
            session, row.id, owner="test-series-playback", minutes=5)
        lease_id = lease.id

    response = client.delete(f"/api/paper-series/{series.id}")
    assert response.status_code == 409
    assert "played or downloaded" in response.text
    with get_session() as session:
        assert session.get(PaperSeries, series.id) is not None
        assert session.get(Artifact, artifact.id) is not None
        assert session.get(MediaObject, row.id) is not None

    media_storage.release_lease(lease_id)
    response = client.delete(f"/api/paper-series/{series.id}")
    assert response.status_code == 200
    with get_session() as session:
        assert session.get(Artifact, artifact.id) is None
        assert session.get(MediaObject, row.id) is None
        assert session.get(Project, project.id) is not None


def test_paper_series_deletion_blocks_queued_project_media_action(client):
    project, series, _artifact, _path, _row = _paper_series_media(
        prefix="series-media-job")
    with get_session() as session:
        job = Job(
            project_id=project.id,
            task=media_storage.ACTION_TASKS["sync"],
            status="queued",
            celery_id="queued-media",
        )
        session.add(job)
        session.commit()
        session.refresh(job)
        job_id = job.id
    response = client.delete(f"/api/paper-series/{series.id}")
    assert response.status_code == 409
    assert "active media storage action" in response.text
    with get_session() as session:
        job = session.get(Job, job_id)
        job.status = "error"
        session.add(job)
        session.commit()
    assert client.delete(f"/api/paper-series/{series.id}").status_code == 200


def test_project_deletion_blocks_live_lease_and_playback_blocks_deleting_project(
    client,
):
    project, artifact, _local = _media_project(prefix="project-live-lease")
    response = client.get(f"/api/projects/{project.id}/media-storage")
    row_id = response.json()["objects"][0]["id"]
    with get_session() as session:
        lease = media_storage.acquire_lease(
            session, row_id, owner="test-project-playback", minutes=5)
        lease_id = lease.id
    response = client.delete(f"/api/projects/{project.id}")
    assert response.status_code == 409
    assert "played or downloaded" in response.text
    media_storage.release_lease(lease_id)

    with get_session() as session:
        stored_project = session.get(Project, project.id)
        stored_project.deleting = True
        session.add(stored_project)
        session.commit()
    with pytest.raises(media_storage.MediaStorageBusy, match="deletion has started"):
        media_storage.prepare_artifact_playback(artifact.id)
    with get_session() as session:
        assert not session.exec(select(MediaLease).where(
            MediaLease.media_object_id == row_id
        )).first()
        stored_project = session.get(Project, project.id)
        stored_project.deleting = False
        session.add(stored_project)
        session.commit()
    assert client.delete(f"/api/projects/{project.id}").status_code == 200


def test_schema_v4_contains_media_storage_tables():
    with get_session() as session:
        version = session.exec(text(
            "SELECT COALESCE(MAX(version), 0) FROM schema_version"
        )).one()[0]
        tables = {
            row[0] for row in session.exec(text(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )).all()
        }
    assert version == 4
    assert {
        "mediastoragetarget",
        "projectmediapolicy",
        "mediaobject",
        "medialease",
    } <= tables
