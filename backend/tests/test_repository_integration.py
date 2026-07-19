"""Cross-cutting repository privacy, retrieval, and lifecycle regressions."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select, text

from app import backup, categories, library, llm, provenance, repository, search, tagging
from app.db import get_session
from app.main import app
from app.models import (
    Artifact, Job, Project, QuickRef, QuickRefSource, RepositoryChunk,
    RepositoryFile, RepositorySnapshot, RepositorySource, Tag,
)
from app.tasks import audio, cloud, quickref


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


def _seed_repository(slug: str, *, private: bool = True,
                     local_only: bool = True) -> tuple[int, int, int]:
    sha = "a" * 40
    with get_session() as session:
        project = Project(
            slug=slug, title="Private Static Analyzer",
            source="https://github.com/example/static-analyzer",
            source_type="github",
        )
        session.add(project)
        session.flush()
        source = RepositorySource(
            project_id=project.id, owner="example", repository="static-analyzer",
            canonical_url="https://github.com/example/static-analyzer",
            requested_ref="main", default_branch="main", pending_sha=sha,
            is_private=private, local_only=local_only,
        )
        session.add(source)
        session.flush()
        snapshot = RepositorySnapshot(
            source_id=source.id, requested_ref="main", resolved_sha=sha,
            status="ready", relative_path=f"{slug}/{sha}", file_count=1,
            indexed_file_count=1,
        )
        session.add(snapshot)
        session.flush()
        source.current_snapshot_id = snapshot.id
        session.add(source)
        repo_file = RepositoryFile(
            snapshot_id=snapshot.id, path="src/analyzer.py", size_bytes=80,
            line_count=3, language="Python", analysis_priority=100,
        )
        session.add(repo_file)
        session.flush()
        body = "class StaticAnalyzer:\n    def inspect(self, repository):\n        return repository"
        chunk = RepositoryChunk(
            file_id=repo_file.id, chunk_index=0, evidence_id=f"E{slug}",
            start_line=1, end_line=3, body=body, body_hash="body-hash",
            content_hash="content-hash", estimated_tokens=20,
        )
        session.add(chunk)
        session.flush()
        session.exec(text(
            "INSERT INTO repository_chunk_fts"
            "(body, chunk_id, file_id, snapshot_id, project_id) "
            "VALUES (:body, :chunk, :file, :snapshot, :project)"
        ).bindparams(
            body=body, chunk=chunk.id, file=repo_file.id,
            snapshot=snapshot.id, project=project.id,
        ))
        session.commit()
        return project.id, source.id, snapshot.id


def test_repository_source_search_and_private_qa_stay_local(client, monkeypatch):
    project_id, _source_id, _snapshot_id = _seed_repository("repo-private-search")

    response = client.get(
        "/api/library/hybrid",
        params={"q": "StaticAnalyzer", "project_id": project_id},
    )
    assert response.status_code == 200
    result = response.json()["results"][0]
    assert result["source_kind"] == "repository"
    assert result["path"] == "src/analyzer.py"
    assert result["start_line"] == 1 and result["end_line"] == 3
    assert result["commit_sha"] == "a" * 40
    assert result["source_url"].endswith("src/analyzer.py#L1-L3")
    assert result["restricted"] is True

    calls: list[dict] = []

    def complete(_function, _system, _user, **kwargs):
        calls.append(kwargs)
        return "The analyzer is defined in the cited source. [S1]"

    monkeypatch.setattr("app.routers.search.llm.complete", complete)
    answer = client.post("/api/library/ask", json={
        "question": "StaticAnalyzer", "project_id": project_id,
    })
    assert answer.status_code == 200
    assert calls and calls[0]["local_only"] is True
    assert answer.json()["sources"][0]["marker"] == "S1"


def test_private_repository_requires_encrypted_backups(monkeypatch):
    monkeypatch.setattr(backup.settings, "backup_encryption_key", "")
    with pytest.raises(ValueError, match="BACKUP_ENCRYPTION_KEY"):
        backup.create_backup(include_media=False, include_repositories=False)


def test_restricted_artifact_is_skipped_by_path_cloud_sync(tmp_path):
    with get_session() as session:
        project = session.exec(
            select(Project).where(Project.slug == "repo-private-search")
        ).one()
        artifact = library.write_artifact(
            session, project_id=project.id, project_slug=project.slug,
            type="summary", title="Private repository overview",
            body="Private implementation details.",
        )
        assert artifact.restricted is True
        path = artifact.path

    result = cloud.sync_paths.run([path])
    assert result == {"uploaded": 0, "skipped": 1}


def test_private_repository_podcast_never_uses_gemini_tts(monkeypatch):
    project_id, _source_id, _snapshot_id = _seed_repository("repo-private-tts")
    with get_session() as session:
        project = session.get(Project, project_id)
        artifact = library.write_artifact(
            session, project_id=project_id, project_slug=project.slug,
            type="podcast_script", title="Private repository podcast",
            body="HOST_A: Welcome.\nHOST_B: Let us inspect the architecture.",
        )
        assert artifact.restricted is True
        job = Job(project_id=project_id, task="tts")
        session.add(job)
        session.commit()
        session.refresh(job)

    with get_session() as session:
        project = session.get(Project, project_id)
        effective = provenance.effective_config("tts", project)
    assert effective["provider"] == "piper"

    provider_used: list[str] = []

    def local_tts(_lines, workdir: Path, _progress):
        provider_used.append("piper")
        output = workdir / "private-podcast.mp3"
        output.write_bytes(b"local audio")
        return output

    monkeypatch.setattr(audio.llm, "resolve_model", lambda _fn: ("gemini", "cloud-tts"))
    monkeypatch.setattr(audio, "_tts_piper", local_tts)
    monkeypatch.setattr(
        audio, "_tts_gemini",
        lambda *_args, **_kwargs: pytest.fail("private script reached Gemini TTS"),
    )
    monkeypatch.setattr(
        audio, "_store_audio",
        lambda *_args, provider, **_kwargs: provider_used.append(provider),
    )
    audio.tts.run(job.id, project_id)
    assert provider_used == ["piper", "piper"]


def test_project_delete_removes_repository_rows_and_snapshot(client):
    project_id, source_id, snapshot_id = _seed_repository("repo-delete-lifecycle")
    snapshot_dir = library.settings.repository_dir / str(source_id) / ("a" * 40)
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    (snapshot_dir / "README.md").write_text("delete me", encoding="utf-8")
    history_dir = library.settings.library_dir / ".history" / "projects" / "repo-delete-lifecycle"
    history_dir.mkdir(parents=True, exist_ok=True)
    (history_dir / "summary.md.old.md").write_text("private history", encoding="utf-8")

    response = client.delete(f"/api/projects/{project_id}")
    assert response.status_code == 200
    assert not snapshot_dir.parent.exists()
    assert not history_dir.exists()
    with get_session() as session:
        assert session.get(Project, project_id) is None
        assert session.get(RepositorySource, source_id) is None
        assert session.get(RepositorySnapshot, snapshot_id) is None
        assert not session.exec(
            select(RepositoryFile).where(RepositoryFile.snapshot_id == snapshot_id)
        ).first()


def test_visibility_escalation_restricts_existing_derivatives(monkeypatch):
    project_id, source_id, _snapshot_id = _seed_repository(
        "repo-visibility-escalation", private=False, local_only=False)
    with get_session() as session:
        project = session.get(Project, project_id)
        artifact = library.write_artifact(
            session, project_id=project_id, project_slug=project.slug,
            type="summary", title="Initially public",
            body="![canary](https://attacker.invalid/private-canary)",
        )
        library.apply_tags(session, artifact, ["private-codename"])
        artifact_id = artifact.id

        # The global quick-ref row belongs to its most recent writer, not every
        # contributor.  A visibility change for an older contributor must
        # still restrict and sanitize the merged document.
        other = Project(
            slug="repo-visibility-other", title="Other public project",
            source="https://example.invalid/public", source_type="url")
        session.add(other)
        session.commit()
        session.refresh(other)
        shared = library.write_artifact(
            session, project_id=other.id, project_slug=other.slug,
            type="quickref_tool", title="Visibility shared tool",
            body="![shared-secret](https://attacker.invalid/shared-canary)",
            rel_path="tools/visibility-shared-tool.md",
        )
        ref = QuickRef(
            kind="tool", slug="visibility-shared-tool",
            title="Visibility shared tool", path=shared.path, aliases="[]")
        session.add(ref)
        session.commit()
        session.refresh(ref)
        session.add(QuickRefSource(quickref_id=ref.id, project_id=project_id))
        session.commit()
        shared_id = shared.id

    repository.set_github_token("github_pat_" + "v" * 48)

    def github_json(url: str, *, token: str = ""):
        if "/commits/" in url:
            return {"sha": "a" * 40, "commit": {"committer": {}}}
        return {"private": True}

    monkeypatch.setattr(repository, "_github_json", github_json)
    try:
        with get_session() as session:
            source = session.get(RepositorySource, source_id)
            result = repository.check_repository_update(source, session=session)
            session.commit()
            assert result["is_private"] is True and result["local_only"] is True
        with get_session() as session:
            artifact = session.get(Artifact, artifact_id)
            shared = session.get(Artifact, shared_id)
            tag = session.exec(select(Tag).where(Tag.name == "private-codename")).one()
            assert artifact.restricted is True
            assert shared.restricted is True
            assert tag.restricted is True
            _meta, body = library.read_doc(artifact.path)
            assert "attacker.invalid" not in body
            _meta, shared_body = library.read_doc(shared.path)
            assert "attacker.invalid" not in shared_body
    finally:
        repository.delete_github_token()


def test_public_quickref_never_merges_a_sticky_private_document(monkeypatch):
    with get_session() as session:
        project = Project(
            slug="quickref-public-reset", title="Public reset source",
            source="https://example.invalid/public-reset", source_type="url")
        session.add(project)
        session.commit()
        session.refresh(project)
        old = library.write_artifact(
            session, project_id=project.id, project_slug=project.slug,
            type="quickref_tool", title="Resettable security tool",
            body="PRIVATE_CANARY_MUST_NOT_REACH_MODEL",
            rel_path="tools/resettable-security-tool.md")
        old.restricted = True
        session.add(old)
        library.write_artifact(
            session, project_id=project.id, project_slug=project.slug,
            type="quickref_tool", title="Unrelated public collision",
            body="must not be overwritten",
            rel_path="tools/resettable-security-tool-public.md")
        ref = QuickRef(
            kind="tool", slug="resettable-security-tool",
            title="Resettable security tool", path=old.path, aliases="[]")
        session.add(ref)
        session.commit()
        session.refresh(ref)
        stale = Project(
            slug="quickref-private-old-contributor", title="Old contributor",
            source="https://example.invalid/old", source_type="url")
        session.add(stale)
        session.commit()
        session.refresh(stale)
        session.add(QuickRefSource(quickref_id=ref.id, project_id=stale.id))
        session.commit()
        stale_id = stale.id
        project_id = project.id

    prompts: list[str] = []
    monkeypatch.setattr(llm, "resolve_model", lambda _fn: ("test", "test-model"))
    monkeypatch.setattr(
        llm, "complete",
        lambda _fn, _system, user, **_kwargs:
            prompts.append(user) or "fresh public quick-reference")
    monkeypatch.setattr(quickref, "auto_tag", lambda *_args, **_kwargs: None)

    quickref._upsert_quickref(
        project_id, "quickref-public-reset", "Public reset source",
        "Resettable security tool", "tool", "resettable-security-tool",
        "ordinary public deep dive", categories.category_map())

    assert prompts and all(
        "PRIVATE_CANARY_MUST_NOT_REACH_MODEL" not in prompt for prompt in prompts)
    with get_session() as session:
        ref = session.exec(select(QuickRef).where(
            QuickRef.slug == "resettable-security-tool")).one()
        assert ref.path == "tools/resettable-security-tool-public-2.md"
        old = session.exec(select(Artifact).where(
            Artifact.path == "tools/resettable-security-tool.md")).one()
        fresh = session.exec(select(Artifact).where(
            Artifact.path == ref.path)).one()
        contributor_ids = session.exec(select(QuickRefSource.project_id).where(
            QuickRefSource.quickref_id == ref.id)).all()
        assert old.restricted is True
        assert fresh.restricted is False
        assert contributor_ids == [project_id]
        assert stale_id not in contributor_ids
    _meta, body = library.read_doc("tools/resettable-security-tool-public-2.md")
    assert body.startswith("fresh public quick-reference")
    _meta, collision = library.read_doc("tools/resettable-security-tool-public.md")
    assert collision == "must not be overwritten"


def test_public_tagger_never_receives_private_vocabulary(monkeypatch):
    captured: list[str] = []
    monkeypatch.setattr(
        tagging.llm, "complete_json",
        lambda _fn, _system, user, **_kwargs: captured.append(user) or {"tags": []},
    )
    with get_session() as session:
        tagging.tag_text(session, "Public", "summary", "ordinary public text")
    assert captured and "private-codename" not in captured[0]


def test_private_processing_rejects_remote_ollama(monkeypatch):
    monkeypatch.setattr(llm.settings, "ollama_base_url", "https://hosted.example.com")
    with pytest.raises(RuntimeError, match="local Ollama"):
        llm.require_local_ollama_endpoint()


def test_local_repository_embeddings_reject_cloud_models():
    with pytest.raises(ValueError, match="cloud model"):
        search.embed_texts(
            ["repository query"], model="embedding:latest-cloud",
            local_only=True)


def test_raw_repository_backup_always_requires_encryption(monkeypatch):
    monkeypatch.setattr(backup.settings, "backup_encryption_key", "")
    with pytest.raises(ValueError, match="repository snapshots"):
        backup.create_backup(include_media=False, include_repositories=True)


def test_cloud_full_stage_excludes_repository_derivatives_and_untracked_files(tmp_path):
    with get_session() as session:
        project = Project(
            slug="repo-cloud-allowlist", title="Cloud allowlist",
            source="https://github.com/example/cloud-allowlist", source_type="github")
        session.add(project)
        session.flush()
        session.add(RepositorySource(
            project_id=project.id, owner="example", repository="cloud-allowlist",
            canonical_url=project.source, requested_ref="main", default_branch="main"))
        session.commit()
        repository_artifact = library.write_artifact(
            session, project_id=project.id, project_slug=project.slug,
            type="summary", title="Public artifact", body="public body")
        assert repository_artifact.repository_derived is True
        assert repository_artifact.restricted is True
        repository_path = repository_artifact.path
        ordinary = Project(
            slug="ordinary-cloud-allowlist", title="Ordinary cloud allowlist",
            source="https://example.invalid/video", source_type="url")
        session.add(ordinary)
        session.commit()
        session.refresh(ordinary)
        ordinary_artifact = library.write_artifact(
            session, project_id=ordinary.id, project_slug=ordinary.slug,
            type="summary", title="Ordinary public artifact", body="public body")
        ordinary_path = ordinary_artifact.path
    untracked = library.settings.library_dir / "projects" / "repo-cloud-allowlist" / "untracked.mp3"
    untracked.write_bytes(b"must not upload")
    staged = cloud._stage_public_library()
    try:
        assert not (staged / repository_path).exists()
        assert (staged / ordinary_path).is_file()
        assert not (staged / "projects" / "repo-cloud-allowlist" / "untracked.mp3").exists()
    finally:
        import shutil
        shutil.rmtree(staged, ignore_errors=True)


def test_repository_origin_stays_sticky_after_project_lineage_is_deleted(client):
    with get_session() as session:
        project = Project(
            slug="repo-sticky-origin-delete", title="Sticky origin",
            source="https://github.com/example/sticky-origin", source_type="github")
        session.add(project)
        session.flush()
        session.add(RepositorySource(
            project_id=project.id, owner="example", repository="sticky-origin",
            canonical_url=project.source, requested_ref="main",
            default_branch="main", local_only=True))
        session.commit()
        artifact = library.write_artifact(
            session, project_id=project.id, project_slug=project.slug,
            type="quickref_tool", title="Sticky repository tool",
            body="Repository-derived knowledge.",
            rel_path="tools/sticky-repository-tool.md")
        ref = QuickRef(
            kind="tool", slug="sticky-repository-tool",
            title="Sticky repository tool", path=artifact.path, aliases="[]")
        session.add(ref)
        session.commit()
        session.refresh(ref)
        session.add(QuickRefSource(
            quickref_id=ref.id, project_id=project.id))
        session.commit()
        project_id, artifact_id, artifact_path = (
            project.id, artifact.id, artifact.path)

    assert client.delete(f"/api/projects/{project_id}").status_code == 200
    with get_session() as session:
        retained = session.get(Artifact, artifact_id)
        assert retained is not None and retained.project_id is None
        assert retained.repository_derived is True
        assert retained.restricted is True
        assert library.artifact_is_repository_derived(session, retained) is True
    staged = cloud._stage_public_library()
    try:
        assert not (staged / artifact_path).exists()
    finally:
        import shutil
        shutil.rmtree(staged, ignore_errors=True)


def test_audio_publication_rolls_back_when_sidecar_is_rejected(tmp_path, monkeypatch):
    produced = tmp_path / "produced.mp3"
    produced.write_bytes(b"private audio")
    monkeypatch.setattr(audio.media, "duration_seconds", lambda _path: 1.0)
    monkeypatch.setattr(
        audio.library, "write_artifact",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("canceled")),
    )
    with pytest.raises(RuntimeError, match="canceled"):
        audio._store_audio(
            999999, "canceled-private-audio", "Canceled", produced,
            type="podcast_audio", title_prefix="Podcast", provider="piper",
            model="local", note="local",
        )
    assert not library.lib_path(
        "projects/canceled-private-audio/podcast_audio.mp3").exists()
