from __future__ import annotations

import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlmodel import SQLModel, Session, select

from app import llm, repository as repository_store
from app.context import current_job_id
from app.db import _migrate, get_session
from app.models import (
    Job, Project, RepositoryChunk, RepositoryFile, RepositorySnapshot,
    RepositorySource, RepositorySynthesisCache, Setting,
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


@pytest.mark.parametrize(("runtime_digest", "error_pattern"), [
    ("changed-map-digest", "changed digest"),
    (None, "did not report a digest"),
])
def test_repository_map_unverified_digest_does_not_write_cache(
    monkeypatch, runtime_digest, error_pattern,
):
    suffix = "missing" if runtime_digest is None else "changed"
    project, source = _repository_project(f"map-digest-{suffix}")
    with get_session() as session:
        snapshot = RepositorySnapshot(
            source_id=source.id,
            requested_ref="main",
            resolved_sha="9" * 40,
            status="ready",
        )
        session.add(snapshot)
        session.commit()
        session.refresh(snapshot)
        source_row = session.get(RepositorySource, source.id)
        source_row.current_snapshot_id = snapshot.id
        session.add(source_row)
        repository_file = RepositoryFile(
            snapshot_id=snapshot.id,
            path="src/digest.py",
            content_hash="digest-file",
            line_count=1,
        )
        session.add(repository_file)
        session.commit()
        session.refresh(repository_file)
        chunk = RepositoryChunk(
            file_id=repository_file.id,
            chunk_index=0,
            evidence_id=f"EDIGESTMAP{suffix.upper()}",
            start_line=1,
            end_line=1,
            body="VALUE = 1",
            body_hash="digest-body",
            content_hash="digest-body",
        )
        session.add(chunk)
        session.commit()
        session.refresh(chunk)
        chunk_id = chunk.id
        evidence = [{
            "chunk_id": chunk.id,
            "evidence_id": chunk.evidence_id,
            "path": repository_file.path,
            "start_line": 1,
            "end_line": 1,
            "body": chunk.body,
            "kind": "source",
            "symbol": "",
        }]

    monkeypatch.setattr(
        llm, "resolve_model", lambda _function: ("ollama", "mutable-map"))
    monkeypatch.setattr(
        repository_tasks,
        "_installed_model_digest",
        lambda _provider, _model, **_kwargs: "pinned-map-digest",
    )
    monkeypatch.setattr(llm, "complete_json", lambda *_args, **_kwargs: {
        "summary": "Valid map output.",
        "facts": [],
        "symbols": [],
        "dependencies": [],
        "commands": [],
        "knowledge": [],
    })
    monkeypatch.setattr(llm, "last_call_diagnostics", lambda: {
        "model_digest": runtime_digest,
        "attempts": [{"status": "ok"}],
    })

    with pytest.raises(RuntimeError, match=error_pattern):
        repository_tasks._map_evidence(0, project.id, evidence)

    with get_session() as session:
        stored = session.get(RepositoryChunk, chunk_id)
        assert stored.summary_config_hash == ""
        assert stored.summary_text == ""


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


def _reduction_limits() -> dict[str, int]:
    return {
        "max_chunks": 64,
        "max_input_chars": 800_000,
        "max_new_map_calls": 64,
        "reduce_batch_chars": 900,
        "reduce_max_tokens": 800,
        "reduce_max_subdivision_depth": 4,
        # Keep the exact writer envelope tight enough that these reducer-unit
        # fixtures exercise compression while preserving the 900-character
        # per-call packing limit.
        "final_input_chars": 2_000,
    }


def test_repository_active_job_telemetry_includes_map_reduce_and_writer():
    from app.routers.system import _job_model_functions

    project = Project(
        id=101,
        slug="repository-telemetry",
        title="Repository telemetry",
        source="https://github.com/acme/example",
        source_type="github",
    )
    job = Job(
        id=202,
        project_id=project.id,
        task="repo_inventory",
        status="running",
    )

    assert _job_model_functions(job, project) == (
        "repository_map",
        "repository_reduce",
        "repository_inventory",
    )


def test_repository_provenance_records_resolved_limits_and_contract_versions():
    from app import provenance

    project, _snapshot_id = _ready_snapshot("repository-contract-provenance")
    with get_session() as session:
        current = session.get(Project, project.id)
        config = provenance.effective_config("repo_inventory", current)

    analysis = config["repository"]["analysis"]
    assert analysis["limits"]["reduce_batch_chars"] >= 4_000
    assert analysis["limits"]["reduce_max_tokens"] >= 400
    assert analysis["map_contract_version"] == repository_tasks._MAP_CONTRACT_VERSION
    assert (
        analysis["reduction_contract_version"]
        == repository_tasks._REDUCTION_CONTRACT_VERSION
    )
    assert (
        analysis["reduction_planner_version"]
        == repository_tasks._REDUCTION_PLANNER_VERSION
    )


def test_repository_map_reuses_valid_legacy_rows_and_recomputes_empty_rows(
        monkeypatch):
    project, snapshot_id = _ready_snapshot("map-contract-cache-upgrade")
    monkeypatch.setattr(
        llm, "resolve_model",
        lambda function: ("ollama", "legacy-map-model")
        if function == "repository_map"
        else pytest.fail(f"unexpected model function {function}"),
    )
    monkeypatch.setattr(
        repository_tasks,
        "_installed_model_digest",
        lambda _provider, _model, **_kwargs: "sha256:current-map-model",
    )
    legacy_base = repository_tasks._map_config_hash(
        "ollama", "legacy-map-model", contract_version=1)
    evidence: list[dict] = []
    with get_session() as session:
        repository_file = RepositoryFile(
            snapshot_id=snapshot_id,
            path="src/example.py",
            content_hash="map-contract-file",
            line_count=2,
        )
        session.add(repository_file)
        session.commit()
        session.refresh(repository_file)
        for index, summary_json in enumerate((
            json.dumps({
                "summary": "Valid legacy map.",
                "role": "legacy role",
                "facts": [],
                "symbols": [],
                "dependencies": [],
                "commands": [],
                "knowledge": [],
            }),
            "{}",
        )):
            item = {
                "evidence_id": f"EMAPCONTRACT{index}",
                "path": "src/example.py",
                "start_line": index + 1,
                "end_line": index + 1,
                "kind": "code",
                "symbol": "",
            }
            chunk = RepositoryChunk(
                file_id=repository_file.id,
                chunk_index=index,
                evidence_id=item["evidence_id"],
                start_line=item["start_line"],
                end_line=item["end_line"],
                kind="code",
                body=f"value_{index} = {index}",
                body_hash=f"map-contract-body-{index}",
                content_hash=f"map-contract-body-{index}",
                summary_text="legacy cache row",
                summary_json=summary_json,
                summary_config_hash=repository_tasks._map_item_config_hash(
                    legacy_base, item),
            )
            session.add(chunk)
            session.commit()
            session.refresh(chunk)
            evidence.append({**item, "chunk_id": chunk.id})

    calls: list[str] = []

    def complete_json(_function, _system, user, **_kwargs):
        calls.append(user)
        return {
            "summary": "Recomputed non-empty map.",
            "role": "current role",
            "facts": [],
            "symbols": [],
            "dependencies": [],
            "commands": [],
            "knowledge": [],
        }

    monkeypatch.setattr(llm, "complete_json", complete_json)
    monkeypatch.setattr(llm, "last_call_diagnostics", lambda: {
        "requested_context": 8_192,
        "effective_context": 8_192,
        "native_context": 32_768,
        "timeout_seconds": 120,
        "max_output_tokens": 1_600,
        "model_digest": "sha256:current-map-model",
        "attempts": [{"status": "ok"}],
    })
    summaries, coverage = repository_tasks._map_evidence(
        0, project.id, evidence)

    assert len(summaries) == 2
    assert len(calls) == 1
    assert coverage["cache"]["reused_chunk_summaries"] == 1
    assert coverage["cache"]["legacy_chunk_summaries"] == 1
    assert coverage["cache"]["new_chunk_summaries"] == 1
    assert any("historical Ollama digest" in warning
               for warning in coverage["warnings"])
    with get_session() as session:
        recomputed = session.exec(select(RepositoryChunk).where(
            RepositoryChunk.evidence_id == "EMAPCONTRACT1"
        )).one()
        assert recomputed.summary_text == "Recomputed non-empty map."
        assert recomputed.summary_config_hash != repository_tasks._map_item_config_hash(
            legacy_base, evidence[1])


def test_repository_map_rejects_empty_live_contract():
    with pytest.raises(
        repository_tasks.RepositoryMapContractError,
        match="no usable summary",
    ):
        repository_tasks._sanitize_map({}, {
            "evidence_id": "EEMPTYMAP",
            "path": "empty.py",
            "start_line": 1,
            "end_line": 1,
        })


def _reduction_reply(batch: list[dict]) -> dict:
    evidence_ids = sorted(repository_tasks._nested_evidence_ids(batch))
    return {
        "summary": "Bounded summary.",
        "facts": [],
        "symbols": [],
        "dependencies": [],
        "commands": [],
        "knowledge": [],
        "evidence_ids": evidence_ids,
    }


def _reduction_summaries(count: int = 4, body_chars: int = 300) -> list[dict]:
    return [{
        "summary": f"summary-{index}-" + ("x" * body_chars),
        "facts": [],
        "symbols": [],
        "dependencies": [],
        "commands": [],
        "knowledge": [],
        "evidence_ids": [f"EREDUCE{index:04d}"],
    } for index in range(count)]


def _ready_snapshot(slug: str):
    project, source = _repository_project(slug)
    with get_session() as session:
        snapshot = RepositorySnapshot(
            source_id=source.id,
            requested_ref="main",
            resolved_sha=("d" * 36) + f"{source.id:04d}"[-4:],
            status="ready",
        )
        session.add(snapshot)
        session.commit()
        session.refresh(snapshot)
        snapshot_id = snapshot.id
        source_row = session.get(RepositorySource, source.id)
        source_row.current_snapshot_id = snapshot_id
        session.add(source_row)
        session.commit()
    return project, snapshot_id


def _padded_json_object(key: str, target_chars: int) -> dict[str, str]:
    empty_chars = len(json.dumps(
        {key: ""}, sort_keys=True, default=str))
    assert target_chars >= empty_chars
    value = {key: "x" * (target_chars - empty_chars)}
    assert len(json.dumps(value, sort_keys=True, default=str)) == target_chars
    return value


def _exact_envelope_limits() -> dict[str, int]:
    return {
        "max_chunks": 64,
        "max_input_chars": 800_000,
        "max_new_map_calls": 64,
        "reduce_batch_chars": 20_000,
        "reduce_max_tokens": 1_600,
        "reduce_max_subdivision_depth": 6,
        "final_input_chars": 64_000,
    }


def _exact_envelope_overhead(monkeypatch) -> None:
    facts = _padded_json_object("facts_padding", 12_840)
    facts_warning = "bounded scan facts omitted lower-priority entries"

    def source_metadata(_source, _snapshot, coverage):
        assert facts_warning in coverage["warnings"]
        metadata = {
            "warnings": list(coverage["warnings"]),
            "metadata_padding": "",
        }
        empty_chars = len(json.dumps(
            metadata, sort_keys=True, default=str))
        metadata["metadata_padding"] = "x" * (2_172 - empty_chars)
        assert len(json.dumps(
            metadata, sort_keys=True, default=str)) == 2_172
        return metadata

    monkeypatch.setattr(
        repository_tasks, "_source_metadata",
        source_metadata,
    )
    monkeypatch.setattr(
        repository_tasks, "_bounded_scan_facts",
        lambda _snapshot, _max_chars: (facts, facts_warning),
    )


def test_repository_writer_prompt_ignores_operational_cache_counters():
    source = SimpleNamespace(
        canonical_url="https://github.com/example/cache-stable",
        owner="example",
        repository="cache-stable",
        requested_ref="main",
        include_paths="[]",
        exclude_paths="[]",
    )
    snapshot = SimpleNamespace(
        resolved_sha="a" * 40,
        commit_url="https://github.com/example/cache-stable/commit/" + "a" * 40,
        scanner_version="test-scanner",
        facts="{}",
    )
    first_coverage = {
        "cache": {
            "reused_chunk_summaries": 0,
            "new_chunk_summaries": 64,
        },
        "warnings": [],
    }
    reused_coverage = {
        "cache": {
            "reused_chunk_summaries": 64,
            "new_chunk_summaries": 0,
        },
        "warnings": [],
    }

    first = repository_tasks._repository_writer_base(
        source, snapshot, first_coverage, [], final_budget=64_000)
    reused = repository_tasks._repository_writer_base(
        source, snapshot, reused_coverage, [], final_budget=64_000)

    assert first == reused
    assert first_coverage["cache"]["new_chunk_summaries"] == 64
    assert reused_coverage["cache"]["reused_chunk_summaries"] == 64
    assert '"cache"' not in first[0]


def test_repository_multiple_reduce_batches_stop_when_exact_writer_envelope_fits(
    monkeypatch,
):
    project, snapshot_id = _ready_snapshot("exact-writer-envelope-fit")
    summaries = _reduction_summaries(count=11, body_chars=2_828)
    summaries[0]["summary"] += "x"
    expected = deepcopy(summaries)
    context_chars = len(json.dumps(
        summaries, sort_keys=True, default=str))
    assert context_chars == 32_661
    assert [len(batch) for batch in repository_tasks._batches(
        summaries, _exact_envelope_limits()["reduce_batch_chars"]
    )] == [6, 5]

    monkeypatch.setattr(
        repository_tasks, "_analysis_limits", _exact_envelope_limits)
    _exact_envelope_overhead(monkeypatch)
    monkeypatch.setattr(
        llm, "resolve_model",
        lambda _function: ("ollama", "repository-reducer"),
    )
    monkeypatch.setattr(
        repository_tasks, "_installed_model_digest",
        lambda *_args, **_kwargs: "digest_unavailable",
    )
    monkeypatch.setattr(
        llm, "complete_json",
        lambda *_args, **_kwargs: pytest.fail(
            "writer-fit evidence was sent through an unnecessary reduction"),
    )
    with get_session() as session:
        job = Job(
            project_id=project.id, task="repo_inventory", status="running")
        session.add(job)
        session.commit()
        session.refresh(job)
        job_id = job.id

    coverage = {"cache": {}, "warnings": []}
    result = repository_tasks._hierarchical_context(
        job_id, snapshot_id, summaries, "repo_inventory", coverage)

    assert result == expected
    assert coverage["warnings"] == []
    assert repository_tasks._nested_evidence_ids(result) == {
        f"EREDUCE{index:04d}" for index in range(11)
    }
    with get_session() as session:
        diagnostics = json.loads(session.get(Job, job_id).diagnostics)
        expected_reduction = {
            "batch": 0,
            "batch_count": 0,
            "batch_input_limit_chars": 20_000,
            "complete": True,
            "input_chars": 32_661,
            "items": 11,
            "level": 0,
            "purpose": "repo_inventory",
            "subdivision_depth": 0,
            "writer_input_chars": 47_788,
            "writer_input_limit_chars": 64_000,
            "writer_overhead_chars": 15_127,
        }
        assert expected_reduction.items() <= diagnostics["reduction"].items()
        assert diagnostics["cache"]["reductions_new"] == 0
        assert diagnostics["cache"]["reductions_reused"] == 0
        assert diagnostics["cache"]["singleton_passthroughs"] == 0
        assert diagnostics["cause"] == ""
        rows = session.exec(select(RepositorySynthesisCache).where(
            RepositorySynthesisCache.snapshot_id == snapshot_id
        )).all()
        assert rows == []


def test_repository_reduction_stops_after_level_one_reaches_exact_writer_envelope(
    monkeypatch,
):
    project, snapshot_id = _ready_snapshot("exact-writer-envelope-transition")
    summaries = _reduction_summaries(count=11, body_chars=4_500)
    target = _reduction_summaries(count=11, body_chars=2_828)
    target[0]["summary"] += "x"
    assert len(json.dumps(
        summaries, sort_keys=True, default=str)) == 51_052
    assert len(json.dumps(target, sort_keys=True, default=str)) == 32_661
    assert [len(batch) for batch in repository_tasks._batches(
        summaries, _exact_envelope_limits()["reduce_batch_chars"]
    )] == [4, 4, 3]

    monkeypatch.setattr(
        repository_tasks, "_analysis_limits", _exact_envelope_limits)
    _exact_envelope_overhead(monkeypatch)
    monkeypatch.setattr(
        llm, "resolve_model",
        lambda _function: ("ollama", "repository-reducer"),
    )
    monkeypatch.setattr(
        repository_tasks, "_installed_model_digest",
        lambda *_args, **_kwargs: "digest_unavailable",
    )
    target_by_id = {
        item["evidence_ids"][0]: item for item in target
    }
    calls: list[list[str]] = []

    def compress_level_one(*args, **kwargs):
        batch = args[6]
        calls.append([
            str(item["evidence_ids"][0]) for item in batch
        ])
        if len(calls) > 3:
            pytest.fail(
                "the planner started a second reduction level after the "
                "level-one writer envelope already fit"
            )
        level_state = kwargs["level_state"]
        level_state["model_calls"] += 1
        level_state["model_reductions_accepted"] += len(batch)
        return [
            deepcopy(target_by_id[str(item["evidence_ids"][0])])
            for item in batch
        ]

    monkeypatch.setattr(
        repository_tasks, "_reduce_batch_adaptive", compress_level_one)
    with get_session() as session:
        job = Job(
            project_id=project.id, task="repo_inventory", status="running")
        session.add(job)
        session.commit()
        session.refresh(job)
        job_id = job.id

    result = repository_tasks._hierarchical_context(
        job_id, snapshot_id, summaries, "repo_inventory", {
            "cache": {}, "warnings": [],
        })

    assert result == target
    assert [len(batch) for batch in calls] == [4, 4, 3]
    assert repository_tasks._nested_evidence_ids(result) == {
        f"EREDUCE{index:04d}" for index in range(11)
    }
    with get_session() as session:
        diagnostics = json.loads(session.get(Job, job_id).diagnostics)
        assert diagnostics["reduction"]["level"] == 1
        assert diagnostics["reduction"]["complete"] is True
        assert diagnostics["reduction"]["writer_input_chars"] == 47_788
        assert diagnostics["reduction"]["writer_input_limit_chars"] == 64_000


def test_repository_no_progress_over_exact_writer_envelope_still_fails(
    monkeypatch,
):
    project, snapshot_id = _ready_snapshot("exact-writer-envelope-stagnation")
    summaries = _reduction_summaries(count=11, body_chars=4_500)
    context_chars = len(json.dumps(
        summaries, sort_keys=True, default=str))
    assert context_chars == 51_052
    assert [len(batch) for batch in repository_tasks._batches(
        summaries, _exact_envelope_limits()["reduce_batch_chars"]
    )] == [4, 4, 3]

    calls: list[list[dict]] = []
    monkeypatch.setattr(
        repository_tasks, "_analysis_limits", _exact_envelope_limits)
    _exact_envelope_overhead(monkeypatch)
    monkeypatch.setattr(
        llm, "resolve_model",
        lambda _function: ("ollama", "repository-reducer"),
    )
    monkeypatch.setattr(
        repository_tasks, "_installed_model_digest",
        lambda *_args, **_kwargs: "digest_unavailable",
    )

    def invalid_reduction(_function, _system, user, **_kwargs):
        batch = json.loads(user)
        calls.append(batch)
        return {
            "summary": "Evidence ledger omitted.",
            "facts": [],
            "symbols": [],
            "dependencies": [],
            "commands": [],
            "knowledge": [],
            "evidence_ids": [],
        }

    monkeypatch.setattr(llm, "complete_json", invalid_reduction)
    monkeypatch.setattr(llm, "last_call_diagnostics", lambda: {
        "attempts": [{"status": "ok"}],
    })
    with get_session() as session:
        job = Job(
            project_id=project.id, task="repo_inventory", status="running")
        session.add(job)
        session.commit()
        session.refresh(job)
        job_id = job.id

    with pytest.raises(RuntimeError, match="made no bounded progress"):
        repository_tasks._hierarchical_context(
            job_id, snapshot_id, summaries, "repo_inventory", {"cache": {}})

    assert calls
    assert all(len(batch) > 1 for batch in calls)
    with get_session() as session:
        diagnostics = json.loads(session.get(Job, job_id).diagnostics)
        stagnation = diagnostics["stagnation"]
        assert stagnation["reason"] == "all_multi_item_reductions_failed"
        assert stagnation["level"] == 1
        assert stagnation["input_items"] == 11
        assert stagnation["output_items"] == 11
        assert stagnation["input_chars"] == 51_052
        assert stagnation["output_chars"] == 51_052
        assert stagnation["input_writer_chars"] == 66_179
        assert stagnation["output_writer_chars"] == 66_179
        assert stagnation["writer_overhead_chars"] == 15_127
        assert stagnation["writer_input_limit_chars"] == 64_000
        assert stagnation["top_level_batches"] == 3
        assert stagnation["model_calls"] == len(calls)
        assert stagnation["model_reductions_accepted"] == 0
        assert stagnation["accepted_reductions_total"] == 0
        assert stagnation["accepted_reductions"] == 0
        assert stagnation["cache_hits"] == 0
        assert stagnation["singleton_passthroughs"] == 11
        assert stagnation["subdivisions"] == len(calls)
        assert stagnation["outcome_counts"] == {
            "invalid_structure": len(calls),
        }
        assert stagnation["evidence_id_count_before"] == 11
        assert stagnation["evidence_id_count_after"] == 11
        assert stagnation["evidence_preserved"] is True
        assert diagnostics["cache"]["singleton_passthroughs"] == 11
        assert diagnostics["reduction"]["level"] == 1
        assert diagnostics["reduction"]["items"] == 11
        assert diagnostics["reduction"]["complete"] is False
        assert "writer base 66179/64000 chars" in diagnostics["cause"]
        rows = session.exec(select(RepositorySynthesisCache).where(
            RepositorySynthesisCache.snapshot_id == snapshot_id
        )).all()
        assert rows == []


def test_repository_fixed_writer_overhead_above_budget_fails_before_reduction(
    monkeypatch,
):
    project, snapshot_id = _ready_snapshot("fixed-writer-overhead")
    summaries = _reduction_summaries(count=1, body_chars=20)
    metadata = _padded_json_object("metadata_padding", 30_000)
    facts = _padded_json_object("facts_padding", 40_000)
    monkeypatch.setattr(
        repository_tasks, "_analysis_limits", _exact_envelope_limits)
    monkeypatch.setattr(
        repository_tasks, "_source_metadata",
        lambda _source, _snapshot, _coverage: metadata,
    )
    monkeypatch.setattr(
        repository_tasks, "_bounded_scan_facts",
        lambda _snapshot, _max_chars: (facts, None),
    )
    monkeypatch.setattr(
        llm, "resolve_model",
        lambda _function: ("ollama", "repository-reducer"),
    )
    monkeypatch.setattr(
        repository_tasks, "_installed_model_digest",
        lambda *_args, **_kwargs: "digest_unavailable",
    )
    monkeypatch.setattr(
        llm, "complete_json",
        lambda *_args, **_kwargs: pytest.fail(
            "reducer ran after fixed writer overhead exhausted the budget"),
    )
    with get_session() as session:
        job = Job(
            project_id=project.id, task="repo_inventory", status="running")
        session.add(job)
        session.commit()
        session.refresh(job)
        job_id = job.id

    with pytest.raises(
        RuntimeError,
        match="fixed metadata and scan facts exceed.*final input budget",
    ):
        repository_tasks._hierarchical_context(
            job_id, snapshot_id, summaries, "repo_inventory", {"cache": {}})

    with get_session() as session:
        diagnostics = json.loads(session.get(Job, job_id).diagnostics)
        assert "70117/64000 chars" in diagnostics["cause"]
        assert "empty evidence list" in diagnostics["cause"]
        stagnation = diagnostics["stagnation"]
        assert stagnation["reason"] == "fixed_writer_overhead_exceeds_budget"
        assert stagnation["writer_overhead_chars"] == 70_115
        assert stagnation["evidence_context_chars"] == 2
        assert stagnation["output_writer_chars"] == 70_117
        assert stagnation["writer_input_limit_chars"] == 64_000
        assert diagnostics["reduction"]["complete"] is False


def test_repository_reductions_are_cached_across_document_purposes(monkeypatch):
    project, snapshot_id = _ready_snapshot("shared-reduction-cache")
    summaries = _reduction_summaries()
    calls: list[dict] = []
    monkeypatch.setattr(repository_tasks, "_analysis_limits", _reduction_limits)
    monkeypatch.setattr(
        llm, "resolve_model",
        lambda function: ("ollama", "repository-reducer")
        if function == "repository_reduce"
        else pytest.fail(f"unexpected model function {function}"),
    )

    def complete_json(function, _system, user, **kwargs):
        assert function == "repository_reduce"
        assert kwargs["transient_attempts"] == 1
        assert kwargs["retries"] == 0
        batch = json.loads(user)
        calls.append(batch)
        return _reduction_reply(batch)

    monkeypatch.setattr(llm, "complete_json", complete_json)
    monkeypatch.setattr(llm, "last_call_diagnostics", lambda: {
        "provider": "ollama",
        "model": "repository-reducer",
        "requested_context": 2_048,
        "effective_context": 4_096,
        "native_context": 32_768,
        "timeout_seconds": 300,
        "max_output_tokens": 800,
        "attempts": [{"status": "ok"}],
    })
    with get_session() as session:
        first_job = Job(
            project_id=project.id, task="repo_inventory", status="running")
        second_job = Job(
            project_id=project.id, task="repo_usage", status="running")
        session.add(first_job)
        session.add(second_job)
        session.commit()
        session.refresh(first_job)
        session.refresh(second_job)
        first_job_id, second_job_id = first_job.id, second_job.id

    first = repository_tasks._hierarchical_context(
        first_job_id, snapshot_id, summaries, "repo_inventory",
        {"cache": {
            "reused_chunk_summaries": 3,
            "new_chunk_summaries": 1,
        }},
    )
    first_call_count = len(calls)
    second = repository_tasks._hierarchical_context(
        second_job_id, snapshot_id, summaries, "repo_usage",
        {"cache": {
            "reused_chunk_summaries": 4,
            "new_chunk_summaries": 0,
        }},
    )
    assert first == second
    assert first_call_count > 0
    assert len(calls) == first_call_count
    assert repository_tasks._nested_evidence_ids(first) == {
        f"EREDUCE{index:04d}" for index in range(4)
    }
    with get_session() as session:
        cache_rows = session.exec(select(RepositorySynthesisCache).where(
            RepositorySynthesisCache.snapshot_id == snapshot_id
        )).all()
        assert len(cache_rows) == first_call_count
        assert {row.purpose for row in cache_rows} == {"shared_analysis"}
        first_diagnostics = json.loads(session.get(Job, first_job_id).diagnostics)
        second_diagnostics = json.loads(session.get(Job, second_job_id).diagnostics)
        assert first_diagnostics["effective_model"] == {
            "provider": "ollama",
            "model": "repository-reducer",
            "digest": "digest_unavailable",
        }
        assert first_diagnostics["cache"]["reductions_new"] == first_call_count
        assert first_diagnostics["cache"]["retained_leaf_maps"] == 4
        assert second_diagnostics["cache"]["reductions_reused"] == first_call_count
        assert second_diagnostics["cache"]["leaf_maps_reused"] == 4
        for job_id in (first_job_id, second_job_id):
            job = session.get(Job, job_id)
            job.status = "done"
            session.add(job)
            session.commit()


@pytest.mark.parametrize(("runtime_digest", "error_pattern"), [
    ("changed-reducer-digest", "changed digest"),
    (None, "did not report a digest"),
])
def test_repository_reduction_unverified_digest_does_not_write_cache(
    monkeypatch, runtime_digest, error_pattern,
):
    suffix = "missing" if runtime_digest is None else "changed"
    _project, snapshot_id = _ready_snapshot(f"reduction-digest-{suffix}")
    summaries = _reduction_summaries()
    monkeypatch.setattr(repository_tasks, "_analysis_limits", _reduction_limits)
    monkeypatch.setattr(
        llm, "resolve_model", lambda _function: ("ollama", "mutable-reducer"))
    monkeypatch.setattr(
        repository_tasks,
        "_installed_model_digest",
        lambda _provider, _model, **_kwargs: "pinned-reducer-digest",
    )
    monkeypatch.setattr(
        llm,
        "complete_json",
        lambda _function, _system, user, **_kwargs: _reduction_reply(
            json.loads(user)),
    )
    monkeypatch.setattr(llm, "last_call_diagnostics", lambda: {
        "model_digest": runtime_digest,
        "attempts": [{"status": "ok"}],
    })

    with pytest.raises(RuntimeError, match=error_pattern):
        repository_tasks._hierarchical_context(
            0, snapshot_id, summaries, "repo_inventory", {"cache": {}})

    with get_session() as session:
        rows = session.exec(select(RepositorySynthesisCache).where(
            RepositorySynthesisCache.snapshot_id == snapshot_id
        )).all()
        assert rows == []


def test_repository_final_synthesis_missing_digest_does_not_write_artifact(
    monkeypatch,
):
    source = SimpleNamespace(canonical_url="https://github.com/example/demo")
    snapshot = SimpleNamespace(id=7, resolved_sha="b" * 40)
    monkeypatch.setattr(
        repository_tasks,
        "_repository_context",
        lambda *_args, **_kwargs: (source, snapshot, [], {}),
    )
    monkeypatch.setattr(
        repository_tasks,
        "_analysis_limits",
        lambda: {"final_input_chars": 100_000},
    )
    monkeypatch.setattr(
        repository_tasks, "_bounded_scan_facts", lambda *_args: ([], None))
    monkeypatch.setattr(
        repository_tasks, "_source_metadata", lambda *_args: {})
    monkeypatch.setattr(
        llm, "resolve_model", lambda _function: ("ollama", "mutable-writer"))
    monkeypatch.setattr(
        repository_tasks,
        "_installed_model_digest",
        lambda _provider, _model, **_kwargs: "pinned-writer-digest",
    )
    monkeypatch.setattr(repository_tasks, "progress", lambda *_args: None)
    monkeypatch.setattr(
        repository_tasks, "_update_job_diagnostics", lambda *_args: None)
    monkeypatch.setattr(llm, "complete", lambda *_args, **_kwargs: "Draft")
    monkeypatch.setattr(llm, "last_call_diagnostics", lambda: {
        "attempts": [{"status": "ok"}],
    })
    monkeypatch.setattr(
        repository_tasks,
        "_write_repository_artifact",
        lambda *_args, **_kwargs: pytest.fail(
            "artifact was written without a verified model digest"),
    )

    with pytest.raises(RuntimeError, match="did not report a digest"):
        repository_tasks.generate_repository_document(
            1,
            2,
            artifact_type="repo_inventory",
            function="repository_inventory",
            prompt_name="repository_inventory",
            title_prefix="Repository inventory",
        )


@pytest.mark.parametrize(("context_mode", "expected_excerpt_chars"), [
    ("partial", 5),
    ("zero_space", 0),
])
def test_repository_document_freezes_limits_and_bounds_prior_guide_context(
    monkeypatch, context_mode, expected_excerpt_chars,
):
    final_budget = 300
    prior_prefix = "\n\nPRIOR REPOSITORY GUIDES (untrusted data):\n"
    base_chars = (
        final_budget - len(prior_prefix) - expected_excerpt_chars
        if context_mode == "partial"
        else final_budget
    )
    resolved_limits = {
        "max_chunks": 64,
        "max_input_chars": 800_000,
        "max_new_map_calls": 64,
        "reduce_batch_chars": 20_000,
        "reduce_max_tokens": 1_600,
        "reduce_max_subdivision_depth": 6,
        "final_input_chars": final_budget,
    }
    limit_calls = 0

    def one_limit_snapshot():
        nonlocal limit_calls
        limit_calls += 1
        if limit_calls > 1:
            pytest.fail(
                "repository settings were resolved again after planning began")
        return dict(resolved_limits)

    source = SimpleNamespace(
        canonical_url="https://github.com/example/frozen-limits")
    snapshot = SimpleNamespace(id=7, resolved_sha="b" * 40)
    coverage = {"warnings": [], "cache": {}}
    context_calls: list[dict[str, int]] = []

    def repository_context(_job_id, _project_id, _purpose, *, limits):
        context_calls.append(dict(limits))
        return source, snapshot, [], coverage

    facts_warning = "bounded deterministic facts warning"
    captured_users: list[str] = []
    captured_artifact: dict = {}
    monkeypatch.setattr(
        repository_tasks, "_analysis_limits", one_limit_snapshot)
    monkeypatch.setattr(
        repository_tasks, "_repository_context", repository_context)
    monkeypatch.setattr(
        repository_tasks,
        "_repository_writer_base",
        lambda *_args, **_kwargs: (
            "b" * base_chars, {"facts": []}, facts_warning),
    )
    monkeypatch.setattr(
        llm, "resolve_model", lambda _function: ("ollama", "frozen-writer"))
    monkeypatch.setattr(
        repository_tasks,
        "_installed_model_digest",
        lambda *_args, **_kwargs: "pinned-writer-digest",
    )
    monkeypatch.setattr(repository_tasks, "progress", lambda *_args: None)
    monkeypatch.setattr(
        repository_tasks, "_update_job_diagnostics", lambda *_args: None)
    monkeypatch.setattr(repository_tasks, "get_prompt", lambda _name: "prompt")

    def complete(_function, _system, user, **_kwargs):
        captured_users.append(user)
        return "Draft"

    monkeypatch.setattr(llm, "complete", complete)
    monkeypatch.setattr(llm, "last_call_diagnostics", lambda: {
        "model_digest": "pinned-writer-digest",
        "attempts": [{"status": "ok"}],
    })
    monkeypatch.setattr(
        repository_tasks, "_snapshot_bundle",
        lambda _project_id: (source, snapshot, []),
    )
    monkeypatch.setattr(
        repository_tasks, "_validate_and_render_citations",
        lambda body, *_args, **_kwargs: (body, 0),
    )

    def write_artifact(*_args, **kwargs):
        captured_artifact.update(kwargs)
        return 77

    monkeypatch.setattr(
        repository_tasks, "_write_repository_artifact", write_artifact)

    artifact_id = repository_tasks.generate_repository_document(
        1,
        2,
        artifact_type="repo_architecture",
        function="repository_architecture",
        prompt_name="repository_architecture",
        title_prefix="Repository architecture",
        additional_context="0123456789",
    )

    assert artifact_id == 77
    assert limit_calls == 1
    assert context_calls == [resolved_limits]
    assert len(captured_users) == 1
    assert len(captured_users[0]) <= final_budget
    if expected_excerpt_chars:
        assert captured_users[0].endswith(
            prior_prefix + "0123456789"[:expected_excerpt_chars])
        assert len(captured_users[0]) == final_budget
    else:
        assert captured_users[0] == "b" * final_budget
        assert prior_prefix not in captured_users[0]
    assert coverage["warnings"].count(facts_warning) == 1
    assert any(
        f"limited to {expected_excerpt_chars} characters" in warning
        for warning in coverage["warnings"]
    )
    assert captured_artifact["analysis_signature"] == {
        "limits": resolved_limits,
        "map_contract_version": repository_tasks._MAP_CONTRACT_VERSION,
        "reduction_contract_version": (
            repository_tasks._REDUCTION_CONTRACT_VERSION),
        "reduction_planner_version": (
            repository_tasks._REDUCTION_PLANNER_VERSION),
    }


def test_repository_under_limit_singleton_is_passed_through_losslessly(
    monkeypatch,
):
    project, snapshot_id = _ready_snapshot("singleton-passthrough")
    summaries = _reduction_summaries(count=5, body_chars=180)
    singleton = summaries[-1]
    singleton["role"] = "packing-boundary fixture"
    singleton["facts"] = [{
        "claim": "Retain this exact boundary summary.",
        "kind": "architecture",
        "evidence_ids": list(singleton["evidence_ids"]),
    }]
    expected_singleton = deepcopy(singleton)
    assert [len(batch) for batch in repository_tasks._batches(
        summaries, _reduction_limits()["reduce_batch_chars"]
    )] == [2, 2, 1]

    calls: list[list[dict]] = []
    monkeypatch.setattr(repository_tasks, "_analysis_limits", _reduction_limits)
    monkeypatch.setattr(
        llm, "resolve_model",
        lambda _function: ("ollama", "repository-reducer"),
    )
    monkeypatch.setattr(
        repository_tasks,
        "_installed_model_digest",
        lambda *_args, **_kwargs: "digest_unavailable",
    )

    def complete_json(_function, _system, user, **_kwargs):
        batch = json.loads(user)
        calls.append(batch)
        return _reduction_reply(batch)

    monkeypatch.setattr(llm, "complete_json", complete_json)
    monkeypatch.setattr(llm, "last_call_diagnostics", lambda: {
        "attempts": [{"status": "ok"}],
    })
    with get_session() as session:
        job = Job(
            project_id=project.id, task="repo_inventory", status="running")
        session.add(job)
        session.commit()
        session.refresh(job)
        job_id = job.id

    result = repository_tasks._hierarchical_context(
        job_id, snapshot_id, summaries, "repo_inventory", {"cache": {}})

    assert [len(batch) for batch in calls] == [2, 2]
    retained = next(
        item for item in result
        if item.get("evidence_ids") == singleton["evidence_ids"])
    assert retained == expected_singleton
    assert repository_tasks._nested_evidence_ids(result) == {
        f"EREDUCE{index:04d}" for index in range(5)
    }
    with get_session() as session:
        rows = session.exec(select(RepositorySynthesisCache).where(
            RepositorySynthesisCache.snapshot_id == snapshot_id
        )).all()
        assert len(rows) == 2
        diagnostics = json.loads(session.get(Job, job_id).diagnostics)
        outcomes = [item["outcome"] for item in diagnostics["attempts"]]
        assert outcomes.count("singleton_passthrough") == 1
        assert diagnostics["cache"]["singleton_passthroughs"] == 1
        assert diagnostics["cache"]["reductions_new"] == 2
        assert diagnostics["cause"] == ""
        assert diagnostics["reduction"]["complete"] is True
        job = session.get(Job, job_id)
        job.status = "done"
        session.add(job)
        session.commit()


def test_repository_subdivision_passes_through_valid_singleton(monkeypatch):
    project, snapshot_id = _ready_snapshot("subdivision-singleton-passthrough")
    summaries = _reduction_summaries(count=5, body_chars=150)
    singleton = summaries[2]
    expected_singleton = deepcopy(singleton)
    assert [len(batch) for batch in repository_tasks._batches(
        summaries, _reduction_limits()["reduce_batch_chars"]
    )] == [3, 2]

    calls: list[list[dict]] = []
    monkeypatch.setattr(repository_tasks, "_analysis_limits", _reduction_limits)
    monkeypatch.setattr(
        llm, "resolve_model",
        lambda _function: ("ollama", "repository-reducer"),
    )
    monkeypatch.setattr(
        repository_tasks,
        "_installed_model_digest",
        lambda *_args, **_kwargs: "digest_unavailable",
    )

    def complete_json(_function, _system, user, **_kwargs):
        batch = json.loads(user)
        calls.append(batch)
        if len(batch) == 3:
            reply = _reduction_reply(batch)
            reply["evidence_ids"] = reply["evidence_ids"][:1]
            return reply
        return _reduction_reply(batch)

    monkeypatch.setattr(llm, "complete_json", complete_json)
    monkeypatch.setattr(llm, "last_call_diagnostics", lambda: {
        "attempts": [{"status": "ok"}],
    })
    with get_session() as session:
        job = Job(
            project_id=project.id, task="repo_inventory", status="running")
        session.add(job)
        session.commit()
        session.refresh(job)
        job_id = job.id

    result = repository_tasks._hierarchical_context(
        job_id, snapshot_id, summaries, "repo_inventory", {"cache": {}})

    assert [len(batch) for batch in calls] == [3, 2, 2]
    assert all(len(batch) != 1 for batch in calls)
    retained = next(
        item for item in result
        if item.get("evidence_ids") == singleton["evidence_ids"])
    assert retained == expected_singleton
    assert repository_tasks._nested_evidence_ids(result) == {
        f"EREDUCE{index:04d}" for index in range(5)
    }
    with get_session() as session:
        rows = session.exec(select(RepositorySynthesisCache).where(
            RepositorySynthesisCache.snapshot_id == snapshot_id
        )).all()
        assert len(rows) == 2
        diagnostics = json.loads(session.get(Job, job_id).diagnostics)
        outcomes = [item["outcome"] for item in diagnostics["attempts"]]
        assert {"invalid_structure", "subdivided",
                "singleton_passthrough", "ok"} <= set(outcomes)
        assert diagnostics["cache"]["singleton_passthroughs"] == 1
        assert diagnostics["cause"] == ""
        job = session.get(Job, job_id)
        job.status = "done"
        session.add(job)
        session.commit()


def test_repository_all_singleton_level_still_uses_model_compression(monkeypatch):
    project, snapshot_id = _ready_snapshot("all-singleton-compression")
    summaries = _reduction_summaries(count=2, body_chars=800)
    assert [len(batch) for batch in repository_tasks._batches(
        summaries, _reduction_limits()["reduce_batch_chars"]
    )] == [1, 1]

    calls: list[list[dict]] = []
    monkeypatch.setattr(repository_tasks, "_analysis_limits", _reduction_limits)
    monkeypatch.setattr(
        llm, "resolve_model",
        lambda _function: ("ollama", "repository-reducer"),
    )
    monkeypatch.setattr(
        repository_tasks,
        "_installed_model_digest",
        lambda *_args, **_kwargs: "digest_unavailable",
    )

    def complete_json(_function, _system, user, **_kwargs):
        batch = json.loads(user)
        calls.append(batch)
        return _reduction_reply(batch)

    monkeypatch.setattr(llm, "complete_json", complete_json)
    monkeypatch.setattr(llm, "last_call_diagnostics", lambda: {
        "attempts": [{"status": "ok"}],
    })
    with get_session() as session:
        job = Job(
            project_id=project.id, task="repo_inventory", status="running")
        session.add(job)
        session.commit()
        session.refresh(job)
        job_id = job.id

    result = repository_tasks._hierarchical_context(
        job_id, snapshot_id, summaries, "repo_inventory", {"cache": {}})

    assert [len(batch) for batch in calls] == [1, 1]
    assert repository_tasks._nested_evidence_ids(result) == {
        "EREDUCE0000", "EREDUCE0001",
    }
    with get_session() as session:
        diagnostics = json.loads(session.get(Job, job_id).diagnostics)
        assert diagnostics["cache"]["singleton_passthroughs"] == 0
        assert "singleton_passthrough" not in {
            item["outcome"] for item in diagnostics["attempts"]
        }
        job = session.get(Job, job_id)
        job.status = "done"
        session.add(job)
        session.commit()


def test_repository_singleton_compression_without_progress_is_bounded(monkeypatch):
    project, snapshot_id = _ready_snapshot("singleton-no-progress")
    summaries = _reduction_summaries(count=2, body_chars=500)
    assert [len(batch) for batch in repository_tasks._batches(
        summaries, _reduction_limits()["reduce_batch_chars"]
    )] == [1, 1]

    calls: list[list[dict]] = []
    limits = _reduction_limits()
    limits["final_input_chars"] = 1_800
    monkeypatch.setattr(
        repository_tasks, "_analysis_limits", lambda: dict(limits))
    monkeypatch.setattr(
        llm, "resolve_model",
        lambda _function: ("ollama", "repository-reducer"),
    )
    monkeypatch.setattr(
        repository_tasks,
        "_installed_model_digest",
        lambda *_args, **_kwargs: "digest_unavailable",
    )

    def complete_json(_function, _system, user, **_kwargs):
        batch = json.loads(user)
        calls.append(batch)
        return batch[0]

    monkeypatch.setattr(llm, "complete_json", complete_json)
    monkeypatch.setattr(llm, "last_call_diagnostics", lambda: {
        "attempts": [{"status": "ok"}],
    })
    with get_session() as session:
        job = Job(
            project_id=project.id, task="repo_inventory", status="running")
        session.add(job)
        session.commit()
        session.refresh(job)
        job_id = job.id

    with pytest.raises(RuntimeError, match="made no bounded progress"):
        repository_tasks._hierarchical_context(
            job_id, snapshot_id, summaries, "repo_inventory", {"cache": {}})

    assert [len(batch) for batch in calls] == [1, 1]
    with get_session() as session:
        job = session.get(Job, job_id)
        diagnostics = json.loads(job.diagnostics)
        assert "made no bounded progress" in diagnostics["cause"]
        assert diagnostics["cache"]["singleton_passthroughs"] == 0
        job.status = "done"
        session.add(job)
        session.commit()


def test_repository_timeout_subdivides_without_repeating_identical_batch(monkeypatch):
    project, snapshot_id = _ready_snapshot("adaptive-reduction-timeout")
    summaries = _reduction_summaries(count=5, body_chars=150)
    attempted_inputs: list[list[dict]] = []
    latest = {"attempts": []}
    monkeypatch.setattr(repository_tasks, "_analysis_limits", _reduction_limits)
    monkeypatch.setattr(
        llm, "resolve_model",
        lambda _function: ("ollama", "repository-reducer"),
    )

    def complete_json(_function, _system, user, **kwargs):
        assert kwargs["transient_attempts"] == 1
        assert kwargs["retries"] == 0
        batch = json.loads(user)
        attempted_inputs.append(batch)
        if len(batch) > 2:
            latest.clear()
            latest.update({
                "requested_context": 2_048,
                "effective_context": 4_096,
                "native_context": 32_768,
                "timeout_seconds": 60,
                "max_output_tokens": 800,
                "attempts": [{
                    "status": "error",
                    "error_type": "ReadTimeout",
                    "error": "timed out",
                }],
            })
            raise TimeoutError("repository reduction timed out")
        latest.clear()
        latest.update({
            "requested_context": 2_048,
            "effective_context": 4_096,
            "native_context": 32_768,
            "timeout_seconds": 60,
            "max_output_tokens": 800,
            "attempts": [{"status": "ok"}],
        })
        return _reduction_reply(batch)

    monkeypatch.setattr(llm, "complete_json", complete_json)
    monkeypatch.setattr(
        llm, "last_call_diagnostics", lambda: dict(latest))
    with get_session() as session:
        job = Job(
            project_id=project.id, task="repo_inventory", status="running")
        session.add(job)
        session.commit()
        session.refresh(job)
        job_id = job.id

    result = repository_tasks._hierarchical_context(
        job_id, snapshot_id, summaries, "repo_inventory",
        {"cache": {
            "reused_chunk_summaries": 4,
            "new_chunk_summaries": 0,
        }},
    )
    assert repository_tasks._nested_evidence_ids(result) == {
        f"EREDUCE{index:04d}" for index in range(5)
    }
    attempted_hashes = [
        repository_tasks._digest(batch) for batch in attempted_inputs
    ]
    assert len(attempted_hashes) == len(set(attempted_hashes))
    assert any(len(batch) > 2 for batch in attempted_inputs)
    assert any(len(batch) == 2 for batch in attempted_inputs)
    assert all(len(batch) != 1 for batch in attempted_inputs)
    with get_session() as session:
        job = session.get(Job, job_id)
        diagnostics = json.loads(job.diagnostics)
        outcomes = [item["outcome"] for item in diagnostics["attempts"]]
        assert "timeout" in outcomes
        assert "subdivided" in outcomes
        assert "singleton_passthrough" in outcomes
        assert "ok" in outcomes
        assert diagnostics["cause"] == ""
        assert diagnostics["context"]["effective"] == 4_096
        job.status = "done"
        session.add(job)
        session.commit()


def test_repository_single_item_contract_failure_is_transparent(monkeypatch):
    _project, snapshot_id = _ready_snapshot("single-reduction-failure")
    summaries = _reduction_summaries(count=1, body_chars=1_500)
    assert repository_tasks._validated_singleton_passthrough(
        summaries, _reduction_limits()["reduce_batch_chars"]
    ) is None
    monkeypatch.setattr(repository_tasks, "_analysis_limits", _reduction_limits)
    monkeypatch.setattr(
        llm, "resolve_model",
        lambda _function: ("ollama", "repository-reducer"),
    )
    monkeypatch.setattr(llm, "complete_json", lambda *_args, **_kwargs: {
        "summary": "Evidence ledger was omitted.",
        "facts": [],
        "symbols": [],
        "dependencies": [],
        "commands": [],
        "knowledge": [],
        "evidence_ids": [],
    })
    monkeypatch.setattr(llm, "last_call_diagnostics", lambda: {
        "attempts": [{"status": "ok"}],
    })
    with pytest.raises(RuntimeError, match="single evidence summary cannot be reduced"):
        repository_tasks._hierarchical_context(
            0, snapshot_id, summaries, "repo_inventory", {"cache": {}})


def test_repository_resource_safety_failure_does_not_subdivide(monkeypatch):
    from app.local_model_safety import LocalModelSafetyError

    _project, snapshot_id = _ready_snapshot("reduction-resource-safety")
    summaries = _reduction_summaries(count=1, body_chars=1_500)
    calls = 0
    monkeypatch.setattr(repository_tasks, "_analysis_limits", _reduction_limits)
    monkeypatch.setattr(
        llm, "resolve_model",
        lambda _function: ("ollama", "repository-reducer"),
    )

    def blocked(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise LocalModelSafetyError(
            "ollama_model_blocked",
            "replacement model still exceeds the safe resource budget",
            http_status=409,
            model="repository-reducer",
            assessment={"tier": "blocked"},
        )

    monkeypatch.setattr(llm, "complete_json", blocked)
    monkeypatch.setattr(llm, "last_call_diagnostics", lambda: {
        "attempts": [{
            "status": "error",
            "error_type": "LocalModelSafetyError",
            "error": "replacement model still exceeds the safe resource budget",
        }],
    })

    with pytest.raises(RuntimeError) as raised:
        repository_tasks._hierarchical_context(
            0, snapshot_id, summaries, "repo_inventory", {"cache": {}})

    message = str(raised.value)
    assert calls == 1
    assert "failure is not recoverable by subdivision" in message
    assert "outcome=resource_safety" in message
    assert "subdivision depth" not in message


@pytest.mark.parametrize(("code", "outcome"), [
    ("ollama_model_blocked", "resource_safety"),
    ("ollama_model_not_installed", "model_not_installed"),
    ("ollama_capability_mismatch", "capability_mismatch"),
    ("future_safety_code", "model_safety"),
])
def test_repository_model_safety_outcomes_are_precise(code, outcome):
    from app.local_model_safety import LocalModelSafetyError

    error = LocalModelSafetyError(
        code,
        "model admission failed",
        http_status=409,
        model="fixture",
    )

    assert repository_tasks._reduction_failure(error, {}) == (outcome, False)


def test_v4_upgrade_adds_repository_cache_and_diagnostics_without_losing_rows(
        tmp_path):
    upgrade_engine = create_engine(f"sqlite:///{tmp_path / 'v4-repository.sqlite3'}")
    with upgrade_engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE schema_version (version INTEGER NOT NULL, "
            "applied TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        connection.exec_driver_sql(
            "INSERT INTO schema_version(version) VALUES (4)")
        connection.exec_driver_sql(
            "CREATE TABLE project (id INTEGER PRIMARY KEY, slug VARCHAR, "
            "title VARCHAR, source VARCHAR, source_type VARCHAR, "
            "status VARCHAR DEFAULT 'new', deleting BOOLEAN DEFAULT 0, "
            "created DATETIME)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE job (id INTEGER PRIMARY KEY, project_id INTEGER, "
            "paper_series_id INTEGER, paper_part_id INTEGER, task VARCHAR, "
            "status VARCHAR, progress VARCHAR, error VARCHAR, celery_id VARCHAR, "
            "parent_job_id INTEGER, options VARCHAR DEFAULT '{}', "
            "started DATETIME, finished DATETIME, heartbeat DATETIME, "
            "created DATETIME, updated DATETIME)"
        )
        connection.exec_driver_sql(
            "INSERT INTO project(id,slug,title,source,source_type,status,deleting) "
            "VALUES (1,'legacy-repo','Legacy repository','https://github.com/x/y',"
            "'github','done',0)"
        )
        connection.exec_driver_sql(
            "INSERT INTO job(id,project_id,task,status,progress,error,celery_id,options) "
            "VALUES (7,1,'repo_inventory','error','reducing','timeout','','{}')"
        )
    SQLModel.metadata.create_all(upgrade_engine)
    with Session(upgrade_engine) as session:
        source = RepositorySource(
            project_id=1,
            owner="x",
            repository="y",
            canonical_url="https://github.com/x/y",
            local_only=True,
        )
        session.add(source)
        session.commit()
        session.refresh(source)
        snapshot = RepositorySnapshot(
            source_id=source.id,
            resolved_sha="e" * 40,
            status="ready",
        )
        session.add(snapshot)
        session.commit()
        session.refresh(snapshot)
        repository_file = RepositoryFile(
            snapshot_id=snapshot.id,
            path="README.md",
            content_hash="file",
        )
        session.add(repository_file)
        session.commit()
        session.refresh(repository_file)
        session.add(RepositoryChunk(
            file_id=repository_file.id,
            chunk_index=0,
            evidence_id="ELEGACY",
            start_line=1,
            end_line=1,
            body="legacy mapped evidence",
            body_hash="body",
            content_hash="body",
            summary_text="legacy map",
            summary_json='{"summary":"legacy map"}',
            summary_config_hash="legacy-qwen3-8b-map",
        ))
        session.commit()
    with upgrade_engine.begin() as connection:
        _migrate(connection)
        _migrate(connection)
        assert connection.exec_driver_sql(
            "SELECT MAX(version) FROM schema_version"
        ).scalar() == 5
        job_columns = {
            row[1] for row in connection.exec_driver_sql(
                "PRAGMA table_info('job')")
        }
        assert "diagnostics" in job_columns
        assert connection.exec_driver_sql(
            "SELECT status,progress,error,diagnostics FROM job WHERE id=7"
        ).one() == ("error", "reducing", "timeout", "{}")
        tables = {
            row[0] for row in connection.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "repositorysynthesiscache" in tables
        indexes = {
            row[1] for row in connection.exec_driver_sql(
                "PRAGMA index_list('repositorysynthesiscache')")
        }
        assert "uq_repository_synthesis_cache_key" in indexes
        assert connection.exec_driver_sql(
            "SELECT value FROM setting "
            "WHERE key='repository.local_model'"
        ).scalar() == '"qwen3:8b"'
        assert connection.exec_driver_sql(
            "SELECT value FROM setting "
            "WHERE key='repository.reduce_model'"
        ).first() is None


def test_v4_upgrade_preserves_explicit_mapper_and_adopts_new_reducer_default(
        tmp_path):
    upgrade_engine = create_engine(f"sqlite:///{tmp_path / 'v4-model.sqlite3'}")
    SQLModel.metadata.create_all(upgrade_engine)
    with Session(upgrade_engine) as session:
        session.add(Setting(
            key="repository.local_model",
            value=json.dumps("administrator-choice:latest"),
        ))
        session.commit()
    with upgrade_engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE IF NOT EXISTS schema_version "
            "(version INTEGER NOT NULL, "
            "applied TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        connection.exec_driver_sql(
            "INSERT INTO schema_version(version) VALUES (4)")
        _migrate(connection)
        assert connection.exec_driver_sql(
            "SELECT value FROM setting "
            "WHERE key='repository.local_model'"
        ).scalar() == json.dumps("administrator-choice:latest")
        assert connection.exec_driver_sql(
            "SELECT value FROM setting "
            "WHERE key='repository.reduce_model'"
        ).first() is None
