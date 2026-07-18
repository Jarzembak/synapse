from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app import llm, repository as repository_store
from app.context import current_job_id
from app.db import get_session
from app.models import (
    Job, Project, RepositoryChunk, RepositoryFile, RepositorySnapshot,
    RepositorySource,
)
from app.settings_store import get_setting, set_setting
from app.tasks import repository as repository_tasks
from app.tasks.orchestrate import (
    REPOSITORY_STEPS, _selected_steps, applicable_steps, deps_for, step_done,
    pipeline_profiles,
)


def _repository_project(slug: str, *, private: bool = False):
    with get_session() as session:
        project = Project(
            slug=slug, title=slug.replace("-", " ").title(),
            source=f"https://github.com/example/{slug}", source_type="github")
        session.add(project)
        session.commit()
        session.refresh(project)
        source = RepositorySource(
            project_id=project.id, owner="example", repository=slug,
            canonical_url=project.source, requested_ref="main",
            default_branch="main", is_private=private, local_only=True)
        session.add(source)
        session.commit()
        session.refresh(project)
        session.refresh(source)
        session.expunge(project)
        session.expunge(source)
        return project, source


def test_repository_graph_is_source_aware_and_omits_media_steps():
    project = Project(
        slug="repo-graph", title="Repo Graph",
        source="https://github.com/example/repo-graph", source_type="github")
    expected = [name for name, _label in REPOSITORY_STEPS]
    assert applicable_steps(project) == expected
    assert {"ingest", "download", "transcribe", "correct", "trim"}.isdisjoint(expected)
    assert {"quickref", "podcast_script", "tts", "mindmap"} <= set(expected)

    selected = _selected_steps(project, {"profile": "full"})
    assert selected == set(expected)
    deps = deps_for(project, run=True)
    assert deps["repo_inventory"] == {"repo_snapshot"}
    assert deps["summarize"] == {"repo_inventory"}
    assert deps["deepdive_claude"] == {
        "summarize", "repo_usage", "repo_architecture",
        "repo_expertise", "repo_environment",
    }
    assert pipeline_profiles(project)["repository"]["steps"] == expected


def test_snapshot_step_reopens_when_scanner_policy_changes():
    project, source = _repository_project("scanner-policy-staleness")
    with get_session() as session:
        source = session.get(RepositorySource, source.id)
        snapshot = RepositorySnapshot(
            source_id=source.id, requested_ref="main", resolved_sha="c" * 40,
            status="ready", scan_config_hash="old-scanner-policy")
        session.add(snapshot)
        session.commit()
        session.refresh(snapshot)
        source.current_snapshot_id = snapshot.id
        session.add(source)
        session.commit()
        project = session.get(Project, project.id)
        assert step_done(session, project, "repo_snapshot") is False
        snapshot.scan_config_hash = repository_store.repository_scan_config_hash(source)
        session.add(snapshot)
        session.commit()
        assert step_done(session, project, "repo_snapshot") is True


def test_private_repository_job_forces_cloud_request_to_local_ollama(monkeypatch):
    project, _source = _repository_project("private-local-boundary", private=True)
    with get_session() as session:
        job = Job(project_id=project.id, task="summarize", status="running")
        session.add(job)
        session.commit()
        session.refresh(job)
        job_id = job.id

    old_model = get_setting("model.repository_overview")
    old_local = get_setting("repository.local_model")
    calls = []
    monkeypatch.setattr(llm, "_anthropic", lambda *args, **kwargs: pytest.fail(
        "private repository constructed a cloud-model request"))
    monkeypatch.setattr(llm, "_ollama", lambda system, user, model, max_tokens, temp,
                        json_format=False, **_kwargs: (
        calls.append(model) or "local result"))
    try:
        set_setting("model.repository_overview", {
            "provider": "anthropic", "model": "cloud-model"})
        set_setting("repository.local_model", "private-local-model")
        token = current_job_id.set(job_id)
        try:
            assert llm.complete(
                "repository_overview", "system", "private source excerpt",
                provider="anthropic", model="cloud-model") == "local result"
        finally:
            current_job_id.reset(token)
        assert calls == ["private-local-model"]
    finally:
        set_setting("model.repository_overview", old_model)
        set_setting("repository.local_model", old_local)
        with get_session() as session:
            leftover = session.get(Job, job_id)
            if leftover:
                session.delete(leftover)
                session.commit()


def test_evidence_map_cache_reuses_structured_summary(monkeypatch):
    project, source = _repository_project("map-cache-reuse")
    with get_session() as session:
        snapshot = RepositorySnapshot(
            source_id=source.id, requested_ref="main", resolved_sha="a" * 40,
            status="ready")
        session.add(snapshot)
        session.commit()
        session.refresh(snapshot)
        source = session.get(RepositorySource, source.id)
        source.current_snapshot_id = snapshot.id
        session.add(source)
        file = RepositoryFile(
            snapshot_id=snapshot.id, path="src/main.py", content_hash="file-hash",
            size_bytes=20, line_count=1, language="Python")
        session.add(file)
        session.commit()
        session.refresh(file)
        chunk = RepositoryChunk(
            file_id=file.id, chunk_index=0, evidence_id="E1234567890ABCDEF",
            start_line=1, end_line=1, body="print('safe static text')",
            body_hash="body-hash", content_hash="body-hash")
        session.add(chunk)
        session.commit()
        session.refresh(chunk)
        evidence = [{
            "chunk_id": chunk.id, "evidence_id": chunk.evidence_id,
            "path": file.path, "start_line": 1, "end_line": 1,
            "body": chunk.body, "kind": "source", "symbol": "",
        }]

    calls = []
    monkeypatch.setattr(llm, "complete_json", lambda *args, **kwargs: (
        calls.append(args[2]) or {
            "summary": "Program entrypoint", "role": "entrypoint",
            "facts": [{"claim": "prints text", "kind": "architecture"}],
            "symbols": [], "dependencies": [], "commands": [], "knowledge": [],
        }))
    first, first_coverage = repository_tasks._map_evidence(0, project.id, evidence)
    second, second_coverage = repository_tasks._map_evidence(0, project.id, evidence)
    assert len(calls) == 1
    assert first == second
    assert first[0]["evidence_ids"] == ["E1234567890ABCDEF"]
    assert first_coverage["cache"]["new_chunk_summaries"] == 1
    assert second_coverage["cache"]["reused_chunk_summaries"] == 1


def test_repository_citations_are_validated_and_pinned_to_sha(monkeypatch):
    source = SimpleNamespace(canonical_url="https://github.com/example/demo")
    snapshot = SimpleNamespace(id=7, resolved_sha="b" * 40)
    evidence = [{
        "evidence_id": "EABC123", "path": "src/main.py",
        "start_line": 10, "end_line": 14,
    }]
    validated = []
    monkeypatch.setattr(
        repository_tasks.repository_store, "validate_repository_citations",
        lambda session, snapshot_id, ids: validated.append((snapshot_id, ids)) or {
            ids[0]: object()} if ids else {})
    rendered, count = repository_tasks._validate_and_render_citations(
        "Detected entrypoint [E:EABC123].", source, snapshot, evidence)
    assert count == 1
    assert f"/blob/{'b' * 40}/src/main.py#L10-L14" in rendered
    assert "<!--E:EABC123-->" in rendered
    assert validated == [(7, ["EABC123"])]

    with pytest.raises(RuntimeError, match="invalid repository evidence"):
        repository_tasks._validate_and_render_citations(
            "Invented [E:ENOTREAL]", source, snapshot, evidence)


def test_map_budget_prioritizes_high_value_files_without_slicing(monkeypatch):
    monkeypatch.setattr(repository_tasks, "_analysis_limits", lambda: {
        "max_chunks": 2, "max_input_chars": 20_000,
        "max_new_map_calls": 2, "reduce_batch_chars": 48_000,
    })
    evidence = [
        {"evidence_id": "E1", "path": "src/zeta.py", "start_line": 1,
         "body": "z" * 100},
        {"evidence_id": "E2", "path": "README.md", "start_line": 1,
         "body": "r" * 100},
        {"evidence_id": "E3", "path": "package.json", "start_line": 1,
         "body": "p" * 100},
    ]
    selected, coverage = repository_tasks._select_evidence(evidence)
    assert {item["path"] for item in selected} == {"README.md", "package.json"}
    assert all(len(item["body"]) == 100 for item in selected)
    assert coverage["skipped_evidence_chunks"] == 1
    assert coverage["warnings"]


def test_synthesis_coverage_uses_snapshot_denominator_and_prioritizes_scan_coverage():
    snapshot = SimpleNamespace(facts=json.dumps({
        "coverage": {
            "file_count": 10,
            "files_with_evidence": 6,
            "excluded_file_count": 4,
        },
        "dependencies": [{"name": f"dep-{index}"} for index in range(500)],
    }))
    bounded, _warning = repository_tasks._bounded_scan_facts(snapshot, 8_000)
    assert bounded["coverage"]["file_count"] == 10
    notice = repository_tasks._coverage_notice({
        "analyzed_evidence_chunks": 4,
        "total_evidence_chunks": 8,
        "analyzed_files": 3,
        "total_snapshot_files": 10,
        "files_with_evidence": 6,
        "indexed_file_count": 6,
        "excluded_file_count": 4,
        "warnings": [],
    })
    assert "3/10 snapshot files" in notice
    assert "6 files produced evidence" in notice
    assert "4 were excluded" in notice
