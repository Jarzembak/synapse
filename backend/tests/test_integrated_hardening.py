"""Focused regressions for the durable pipeline/library features.

These tests exercise state transitions and cross-table/file invariants that are
easy to miss in endpoint smoke tests: terminal job fencing, profile closure,
staleness propagation, retrieval chunk replacement, FK-safe deletion, vault
reconstruction, and backup verification.
"""
from __future__ import annotations

import json
import sqlite3
import zipfile
from array import array

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select, text

from app import backup, library, provenance, search
from app.db import get_session
from app.main import app
from app.models import (
    Artifact,
    ChunkEmbedding,
    Job,
    Project,
    QuickRef,
    QuickRefSource,
    SearchChunk,
)
from app.settings_store import get_setting, set_setting
from app.tasks.common import pipeline_task, transition_job


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


def _project(slug: str, *, source_type: str = "local") -> Project:
    with get_session() as session:
        project = Project(
            slug=slug,
            title=slug.replace("-", " ").title(),
            source=f"{slug}.mp4" if source_type == "local"
            else f"https://example.com/{slug}",
            source_type=source_type,
        )
        session.add(project)
        session.commit()
        session.refresh(project)
        return project


def test_project_upload_streams_private_media_validates_and_deletes(client):
    payload = b"streamed-media-block-" * 60_000  # crosses the 1 MiB read boundary
    response = client.post(
        "/api/projects/upload?filename=camera.MP4&title=Private+Stream+Upload",
        content=payload,
        headers={"Content-Type": "application/octet-stream"},
    )
    assert response.status_code == 200, response.text
    project = response.json()
    assert project["source_type"] == "upload"
    assert project["source"] == "uploaded.mp4"
    assert project["title"] == "Private Stream Upload"
    private_file = (
        library.settings.media_dir / project["slug"] / project["source"]
    )
    assert private_file.read_bytes() == payload
    assert private_file.parent == library.settings.media_dir / project["slug"]

    empty = client.post(
        "/api/projects/upload?filename=empty.mp3",
        content=b"",
        headers={"Content-Type": "application/octet-stream"},
    )
    assert empty.status_code == 400
    unsupported = client.post(
        "/api/projects/upload?filename=notes.txt",
        content=b"not media",
        headers={"Content-Type": "application/octet-stream"},
    )
    assert unsupported.status_code == 415
    assert not list((library.settings.media_dir / ".uploads").glob("upload-*"))

    assert client.delete(f"/api/projects/{project['id']}").status_code == 200
    assert not private_file.parent.exists()
    with get_session() as session:
        assert session.get(Project, project["id"]) is None


def test_ingest_upload_publishes_playable_source_audio_sidecar(client):
    payload = b"ID3" + b"playable-upload-audio" * 100
    response = client.post(
        "/api/projects/upload?filename=meeting.mp3&title=Playable+Audio+Upload",
        content=payload,
        headers={"Content-Type": "application/octet-stream"},
    )
    assert response.status_code == 200, response.text
    project = response.json()
    with get_session() as session:
        job = Job(project_id=project["id"], task="ingest")
        session.add(job)
        session.commit()
        session.refresh(job)
        job_id = job.id

    from app.tasks import ingest as ingest_module

    result = ingest_module.ingest.run(job_id, project["id"])
    copied_audio = library.settings.media_dir / project["slug"] / "source.mp3"
    assert result == str(copied_audio)
    assert copied_audio.read_bytes() == payload

    with get_session() as session:
        job = session.get(Job, job_id)
        stored_project = session.get(Project, project["id"])
        artifact = session.exec(select(Artifact).where(
            Artifact.project_id == project["id"], Artifact.type == "source_audio",
        )).one()
        assert job.status == "done"
        assert stored_project.status == "ingested"
        assert artifact.path == f"projects/{project['slug']}/source_audio.md"
        assert artifact.media_path == f"media:{project['slug']}/source.mp3"
        artifact_id = artifact.id
    meta, body = library.read_doc(f"projects/{project['slug']}/source_audio.md")
    assert meta["media"] == f"media:{project['slug']}/source.mp3"
    assert meta["filesize_bytes"] == len(payload)
    assert "timestamp playback" in body

    playable = client.get(f"/api/media/{artifact_id}")
    assert playable.status_code == 200
    assert playable.headers["content-type"].startswith("audio/mpeg")
    assert playable.content == payload
    assert client.delete(f"/api/projects/{project['id']}").status_code == 200
    assert not copied_audio.parent.exists()


def test_late_pipeline_completion_cannot_resurrect_canceled_job(client):
    project = _project("terminal-fence")
    with get_session() as session:
        job = Job(project_id=project.id, task="terminal-fence-step")
        session.add(job)
        session.commit()
        session.refresh(job)
        job_id = job.id

    executed = []

    @pipeline_task
    def terminal_fence_step(current_job_id: int, _project_id: int):
        executed.append(current_job_id)
        with get_session() as session:
            assert transition_job(
                session, current_job_id, {"running"}, "canceled",
                error="canceled while provider call was in flight",
            )
        return "late result"

    assert terminal_fence_step(job_id, project.id) == "late result"
    assert executed == [job_id]
    with get_session() as session:
        job = session.get(Job, job_id)
        assert job.status == "canceled" and job.finished is not None
        assert "in flight" in job.error
        assert not transition_job(session, job_id, {"queued", "running"}, "done")
        assert session.get(Job, job_id).status == "canceled"


def test_canceled_job_cannot_publish_an_artifact(client):
    project = _project("publication-fence")
    with get_session() as session:
        job = Job(project_id=project.id, task="publication-fence-step")
        session.add(job)
        session.commit()
        session.refresh(job)
        job_id = job.id

    @pipeline_task
    def publication_fence_step(current_job_id: int, project_id: int):
        with get_session() as session:
            assert transition_job(
                session, current_job_id, {"running"}, "canceled",
                error="cancel during provider response",
            )
        with get_session() as session:
            library.write_artifact(
                session, project_id=project_id, project_slug=project.slug,
                type="summary", title="Late summary", body="must not publish",
            )

    with pytest.raises(RuntimeError, match="canceled before artifact publication"):
        publication_fence_step(job_id, project.id)
    with get_session() as session:
        assert session.get(Job, job_id).status == "canceled"
        assert not session.exec(select(Artifact).where(
            Artifact.project_id == project.id, Artifact.type == "summary",
        )).first()
    assert not library.lib_path(f"projects/{project.slug}/summary.md").exists()


def test_canceling_run_all_fences_and_revokes_children(client, monkeypatch):
    project = _project("cancel-tree")
    with get_session() as session:
        parent = Job(
            # Queued also exercises cancel-before-pickup and avoids depending
            # on the suite having no other globally serialized run-all active.
            project_id=project.id, task="run_all", status="queued",
            celery_id="parent-celery-id",
        )
        session.add(parent)
        session.commit()
        session.refresh(parent)
        child = Job(
            project_id=project.id, task="summarize", status="running",
            parent_job_id=parent.id, celery_id="child-celery-id",
        )
        session.add(child)
        session.commit()
        session.refresh(child)
        parent_id, child_id = parent.id, child.id

    revoked = []
    from app.tasks.celery_app import celery

    monkeypatch.setattr(
        celery.control, "revoke",
        lambda celery_id, terminate=False: revoked.append((celery_id, terminate)),
    )
    response = client.post(f"/api/jobs/{parent_id}/cancel")
    assert response.status_code == 200
    with get_session() as session:
        parent = session.get(Job, parent_id)
        child = session.get(Job, child_id)
        assert parent.status == child.status == "canceled"
        assert parent.finished is not None and child.finished is not None
        assert not transition_job(session, child_id, {"queued", "running"}, "done")
    assert set(revoked) == {
        ("parent-celery-id", True), ("child-celery-id", True),
    }


def test_pipeline_profiles_select_dependency_closed_applicable_steps(client):
    from app.tasks.orchestrate import _selected_steps

    local = Project(
        slug="profile-local", title="Profile Local", source="local.mp4",
        source_type="local",
    )
    assert _selected_steps(local, {"profile": "quick"}) == {
        "ingest", "transcribe", "correct", "summarize",
    }
    assert _selected_steps(local, {"steps": ["tts"]}) == {
        "ingest", "transcribe", "correct", "deepdive_claude",
        "deepdive_gemini", "merge", "podcast_script", "tts",
    }
    empty_project = _project("profile-empty-steps")
    empty = client.post(f"/api/projects/{empty_project.id}/run_all", json={
        "profile": "full", "steps": [], "force_steps": [],
    })
    assert empty.status_code == 400
    with get_session() as session:
        assert not session.exec(select(Job).where(
            Job.project_id == empty_project.id, Job.task == "run_all"
        )).first()

    profile_key = "hardening-review"
    response = client.put(f"/api/settings/profiles/{profile_key}", json={
        "label": "Hardening review",
        "description": "Summary and map with their prerequisites",
        "steps": ["summarize", "mindmap", "summarize"],
    })
    assert response.status_code == 200
    try:
        selected = _selected_steps(local, {"profile": profile_key})
        assert {"summarize", "mindmap", "merge"} <= selected
        assert {"tts", "trim", "download", "quickref"}.isdisjoint(selected)
        saved = client.get("/api/settings/profiles").json()[profile_key]
        assert saved["custom"] is True
        assert saved["steps"] == ["summarize", "mindmap"]
    finally:
        assert client.delete(f"/api/settings/profiles/{profile_key}").status_code == 200


def test_staleness_propagates_from_changed_upstream_configuration(client):
    project = _project("stale-propagation")
    old_glossary = get_setting("glossary", [])
    try:
        with get_session() as session:
            library.write_artifact(
                session, project_id=project.id, project_slug=project.slug,
                type="transcript", title="Transcript", body="[00:00:01] raw text",
            )
            library.write_artifact(
                session, project_id=project.id, project_slug=project.slug,
                type="corrected", title="Corrected", body="[00:00:01] corrected text",
            )
            library.write_artifact(
                session, project_id=project.id, project_slug=project.slug,
                type="summary", title="Summary", body="Current summary",
            )
        with get_session() as session:
            project = session.get(Project, project.id)
            assert not provenance.is_step_stale(session, project, "correct")
            assert not provenance.is_step_stale(session, project, "summarize")

        set_setting("glossary", [*old_glossary, "staleness-regression-marker"])
        with get_session() as session:
            project = session.get(Project, project.id)
            assert provenance.is_step_stale(session, project, "correct")
            # A summary remains stale while the corrected transcript it depends
            # on is stale, even before that transcript is regenerated.
            assert provenance.is_step_stale(session, project, "summarize")
    finally:
        set_setting("glossary", old_glossary)


def test_uploaded_source_signature_tracks_private_uploaded_file(client):
    project = _project("uploaded-source-signature")
    with get_session() as session:
        project = session.get(Project, project.id)
        project.source_type = "upload"
        project.source = "browser-upload.mp4"
        project_id, project_slug, filename = project.id, project.slug, project.source
        session.add(project)
        session.commit()
    uploaded = library.settings.media_dir / project_slug / filename
    uploaded.parent.mkdir(parents=True, exist_ok=True)
    uploaded.write_bytes(b"first upload")
    with get_session() as session:
        project = session.get(Project, project_id)
        first, _config, _detail = provenance.signatures(session, project, "transcribe")
    uploaded.write_bytes(b"replacement upload with a different size")
    with get_session() as session:
        project = session.get(Project, project_id)
        second, _config, _detail = provenance.signatures(session, project, "transcribe")
    assert first != second


def test_run_all_failure_skips_run_order_consumers(client, monkeypatch):
    from app.tasks import orchestrate

    project = _project("run-failure-propagation")
    source = library.settings.media_dir / project.slug / "source.wav"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"ingested audio")
    with get_session() as session:
        library.write_artifact(
            session, project_id=project.id, project_slug=project.slug,
            type="transcript", title="Transcript", body="[00:00:01] raw fallback",
        )
        library.write_artifact(
            session, project_id=project.id, project_slug=project.slug,
            type="corrected", title="Corrected", body="[00:00:01] stale correction",
        )
        parent = Job(
            project_id=project.id, task="test_run_all", status="running",
            options=json.dumps({
                "steps": ["summarize"], "force_steps": ["correct"],
            }),
        )
        session.add(parent)
        session.commit()
        session.refresh(parent)
        parent_id = parent.id

    dispatched = []

    def fail_correction(parent_job_id, project_id, step):
        dispatched.append(step)
        with get_session() as session:
            child = Job(
                project_id=project_id, task=step, status="error",
                parent_job_id=parent_job_id, error="correction provider failed",
            )
            session.add(child)
            session.commit()
            session.refresh(child)
            return child, None

    monkeypatch.setattr(orchestrate, "_dispatch_step", fail_correction)
    monkeypatch.setattr(orchestrate.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(orchestrate, "maybe_start_next_run_all", lambda: None)
    orchestrate.run_all.run(parent_id, project.id)

    assert dispatched == ["correct"]
    with get_session() as session:
        summary = session.exec(select(Job).where(
            Job.project_id == project.id, Job.task == "summarize",
            Job.parent_job_id == parent_id,
        )).one()
        assert summary.status == "error"
        assert "Correction pass" in summary.error
        assert session.get(Job, parent_id).status == "error"


def test_chunk_search_replaces_old_chunks_embeddings_and_fts(client, monkeypatch):
    project = _project("chunk-replacement")
    old_body = "\n".join(
        f"[00:00:{index % 60:02d}] orionneedle detail {index} " + "x" * 35
        for index in range(90)
    )
    with get_session() as session:
        artifact = library.write_artifact(
            session, project_id=project.id, project_slug=project.slug,
            type="transcript", title="Chunk transcript", body=old_body,
        )
        library.apply_tags(session, artifact, ["chunk-hardening"])
        chunks = session.exec(
            select(SearchChunk).where(SearchChunk.artifact_id == artifact.id)
        ).all()
        assert len(chunks) > 1
        first = chunks[0]
        session.add(ChunkEmbedding(
            chunk_id=first.id, model="test-embedding", dimensions=1,
            vector=array("f", [1.0]).tobytes(), body_hash=first.body_hash,
        ))
        session.commit()
        artifact_id = artifact.id
        old_chunk_ids = {chunk.id for chunk in chunks}

    monkeypatch.setattr(search, "semantic_chunks", lambda *_args, **_kwargs: [])
    with get_session() as session:
        results = search.hybrid_chunks(
            session, "orionneedle", artifact_types={"transcript"},
            project_id=project.id, tags={"chunk-hardening"},
        )
        assert results and all(item["artifact_id"] == artifact_id for item in results)
        assert results[0]["start_time"] is not None

        library.write_artifact(
            session, project_id=project.id, project_slug=project.slug,
            type="transcript", title="Chunk transcript",
            body="[00:02:00] zephyrreplacement is the only searchable text now",
        )

    with get_session() as session:
        new_chunks = session.exec(
            select(SearchChunk).where(SearchChunk.artifact_id == artifact_id)
        ).all()
        assert len(new_chunks) == 1
        assert "zephyrreplacement" in new_chunks[0].body
        assert search.fts_chunks(session, "orionneedle") == []
        assert search.fts_chunks(session, "zephyrreplacement") == [new_chunks[0].id]
        assert not session.exec(
            select(ChunkEmbedding).where(
                ChunkEmbedding.chunk_id.in_(old_chunk_ids),
                ChunkEmbedding.model == "test-embedding",
            )
        ).first()


def test_project_delete_rejects_active_work_then_cleans_fk_dependents(client):
    project = _project("delete-fk-cleanup")
    with get_session() as session:
        artifact = library.write_artifact(
            session, project_id=project.id, project_slug=project.slug,
            type="summary", title="Delete me", body="deletion chunk body",
        )
        library.apply_tags(session, artifact, ["delete-hardening"])
        chunks = session.exec(
            select(SearchChunk).where(SearchChunk.artifact_id == artifact.id)
        ).all()
        for chunk in chunks:
            session.add(ChunkEmbedding(
                chunk_id=chunk.id, model="delete-model", dimensions=1,
                vector=array("f", [1.0]).tobytes(), body_hash=chunk.body_hash,
            ))
        ref = QuickRef(
            kind="tool", slug="delete-fk-source", title="Delete FK source",
            path="tools/delete-fk-source.md", aliases="[]",
        )
        session.add(ref)
        session.flush()
        session.add(QuickRefSource(quickref_id=ref.id, project_id=project.id))
        active = Job(project_id=project.id, task="summarize")
        session.add(active)
        session.commit()
        session.refresh(active)
        artifact_id, active_id = artifact.id, active.id
        chunk_ids = {chunk.id for chunk in chunks}
    set_setting(f"step_signature.{project.id}.ingest", {"input_hash": "old"})
    set_setting(f"step_signature.{project.id}.quickref", {"input_hash": "old"})

    response = client.delete(f"/api/projects/{project.id}")
    assert response.status_code == 409
    assert library.lib_path(f"projects/{project.slug}/summary.md").exists()
    with get_session() as session:
        assert transition_job(session, active_id, {"queued"}, "canceled")

    response = client.delete(f"/api/projects/{project.id}")
    assert response.status_code == 200
    with get_session() as session:
        assert session.get(Project, project.id) is None
        assert session.get(Artifact, artifact_id) is None
        assert not session.exec(select(Job).where(Job.project_id == project.id)).first()
        assert not session.exec(
            select(QuickRefSource).where(QuickRefSource.project_id == project.id)
        ).first()
        assert not session.exec(
            select(SearchChunk).where(SearchChunk.artifact_id == artifact_id)
        ).first()
        assert not session.exec(
            select(ChunkEmbedding).where(ChunkEmbedding.chunk_id.in_(chunk_ids))
        ).first()
        assert session.exec(text(
            "SELECT COUNT(*) FROM artifact_fts WHERE artifact_id=:id"
        ).bindparams(id=artifact_id)).one()[0] == 0
        assert session.exec(text(
            "SELECT COUNT(*) FROM chunk_fts WHERE artifact_id=:id"
        ).bindparams(id=artifact_id)).one()[0] == 0
    assert get_setting(f"step_signature.{project.id}.ingest", "missing") == "missing"
    assert get_setting(f"step_signature.{project.id}.quickref", "missing") == "missing"


def test_recover_interrupted_project_deletion_restores_or_finishes_staging(client):
    from app.recovery import recover_interrupted_deletions

    project = _project("recover-staged-delete")
    with get_session() as session:
        project = session.get(Project, project.id)
        project.deleting = True
        project_id, project_slug = project.id, project.slug
        session.add(project)
        session.commit()

    staged_paths = []
    for root in (
        library.settings.library_dir / "projects",
        library.settings.media_dir,
    ):
        staged = root / ".trash" / f"{project_slug}.delete-{project_id}"
        staged.mkdir(parents=True, exist_ok=True)
        (staged / "sentinel.txt").write_text("restore me", encoding="utf-8")
        staged_paths.append((root / project_slug, staged))
    orphan = (library.settings.library_dir / "projects" / ".trash"
              / "already-committed.delete-999999")
    orphan.mkdir(parents=True)
    (orphan / "sentinel.txt").write_text("remove me", encoding="utf-8")

    result = recover_interrupted_deletions()
    assert result["restored"] >= 2 and result["removed"] >= 1
    with get_session() as session:
        assert session.get(Project, project_id).deleting is False
    for original, staged in staged_paths:
        assert (original / "sentinel.txt").read_text(encoding="utf-8") == "restore me"
        assert not staged.exists()
    assert not orphan.exists()


def test_recovery_reconstructs_project_quickref_tags_and_indexes_idempotently(client):
    from app.recovery import rebuild_from_vault

    project_rel = "projects/recovery-reconstructed/summary.md"
    earlier_rel = "projects/recovery-reconstructed/corrected.md"
    quickref_rel = "tools/recovery-reconstructed-tool.md"
    # This sorts first and lacks source metadata, so recovery initially creates
    # a local placeholder before a later document supplies the original URL.
    library._write_doc(earlier_rel, {
        "type": "corrected", "title": "Corrected — Recovery Reconstructed",
    }, "earlier recovered content")
    library._write_doc(project_rel, {
        "type": "summary",
        "title": "Recovered summary",
        "project_title": "Recovery Reconstructed",
        "source_url": "https://example.com/recovery-reconstructed",
        "tags": ["recovery-hardening"],
        "input_hash": "input-signature",
        "config_hash": "config-signature",
        "provenance": {"step": "summarize", "source": "vault"},
    }, "recoverable project knowledge")
    library._write_doc(quickref_rel, {
        "type": "quickref_tool",
        "title": "Recovered Tool",
        "aliases": ["recovery alias"],
        "tags": ["recovery-hardening"],
    }, "Source: [[projects/recovery-reconstructed/deepdive_merged]]")

    with get_session() as session:
        first = rebuild_from_vault(session)
        assert first["reconciled"] >= 3
        project = session.exec(
            select(Project).where(Project.slug == "recovery-reconstructed")
        ).one()
        artifact = session.exec(
            select(Artifact).where(Artifact.path == project_rel)
        ).one()
        ref = session.exec(
            select(QuickRef).where(QuickRef.slug == "recovery-reconstructed-tool")
        ).one()
        assert project.source == "https://example.com/recovery-reconstructed"
        assert project.source_type == "url"
        assert artifact.project_id == project.id
        assert artifact.input_hash == "input-signature"
        assert artifact.config_hash == "config-signature"
        assert json.loads(artifact.provenance)["source"] == "vault"
        assert library.current_tags(session, artifact.id) == ["recovery-hardening"]
        assert library.search_fts(session, "recoverable") == [artifact.id]
        chunks_before = len(session.exec(
            select(SearchChunk).where(SearchChunk.artifact_id == artifact.id)
        ).all())
        assert chunks_before > 0
        assert session.get(QuickRefSource, (ref.id, project.id)) is not None

        rebuild_from_vault(session)
        assert len(session.exec(
            select(Artifact).where(Artifact.path == project_rel)
        ).all()) == 1
        assert len(session.exec(
            select(SearchChunk).where(SearchChunk.artifact_id == artifact.id)
        ).all()) == chunks_before
        assert session.get(QuickRefSource, (ref.id, project.id)) is not None


def test_backup_create_and_verify_checks_archive_and_sqlite_integrity(tmp_path, monkeypatch):
    db_path = tmp_path / "db" / "synapse.sqlite3"
    library_dir = tmp_path / "library"
    media_dir = tmp_path / "media"
    backup_dir = tmp_path / "backups"
    db_path.parent.mkdir(parents=True)
    library_dir.mkdir()
    (library_dir / "notes.md").write_text("vault note", encoding="utf-8")
    ignored = library_dir / ".trash" / "ignored.md"
    ignored.parent.mkdir()
    ignored.write_text("ignore me", encoding="utf-8")
    source_audio = media_dir / "demo" / "source_audio.mp3"
    source_audio.parent.mkdir(parents=True)
    source_audio.write_bytes(b"archived audio")
    (source_audio.parent / "working.wav").write_bytes(b"working file")
    with sqlite3.connect(db_path) as connection:
        connection.execute("CREATE TABLE marker(value TEXT NOT NULL)")
        connection.execute("INSERT INTO marker VALUES ('snapshot-data')")
        connection.commit()

    monkeypatch.setattr(backup.settings, "db_path", db_path)
    monkeypatch.setattr(backup.settings, "library_dir", library_dir)
    monkeypatch.setattr(backup.settings, "media_dir", media_dir)
    monkeypatch.setattr(backup.settings, "backup_dir", backup_dir)
    monkeypatch.setattr(backup.settings, "backup_encryption_key", "")
    monkeypatch.setattr(backup, "get_setting", lambda _key, default=None: 10)

    created = backup.create_backup(include_media=True)
    report = backup.verify_backup(created)
    assert report["valid"] is True
    assert report["database_integrity"] == "ok"
    assert "archived_media" in report["manifest"]["includes"]
    with zipfile.ZipFile(created) as archive:
        names = set(archive.namelist())
        assert "library/notes.md" in names
        assert "library/.trash/ignored.md" not in names
        assert "media/demo/source_audio.mp3" in names
        assert "media/demo/working.wav" not in names
        restored_db = tmp_path / "restored.sqlite3"
        restored_db.write_bytes(archive.read("database/synapse.sqlite3"))
    with sqlite3.connect(restored_db) as connection:
        assert connection.execute("SELECT value FROM marker").fetchone()[0] == "snapshot-data"

    corrupt = backup_dir / "synapse-corrupt.zip"
    with zipfile.ZipFile(corrupt, "w") as archive:
        archive.writestr("manifest.json", json.dumps({
            "format": 1, "includes": ["database", "library"],
        }))
        archive.writestr("database/synapse.sqlite3", b"not a sqlite database")
        archive.writestr("library/notes.md", "still has a valid ZIP CRC")
    corrupt_report = backup.verify_backup(corrupt)
    assert corrupt_report["valid"] is False
    assert corrupt_report["database_integrity"] != "ok"

    monkeypatch.setattr(backup.settings, "backup_encryption_key", "test-only-backup-key")
    encrypted = backup.create_backup(include_media=False)
    encrypted_report = backup.verify_backup(encrypted)
    assert encrypted.suffix == ".enc"
    assert encrypted_report["valid"] is True
    assert encrypted_report["database_integrity"] == "ok"
    assert "archived_media" not in encrypted_report["manifest"]["includes"]
