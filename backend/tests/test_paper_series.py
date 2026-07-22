from __future__ import annotations

import json
import uuid

import pytest
from fastapi import HTTPException
from sqlmodel import select

from app import library, llm
from app.context import current_job_id
from app.db import get_session, init_db
from app.models import (
    Artifact,
    Job,
    PaperChunk,
    PaperMemoryRevision,
    PaperSeries,
    PaperSeriesPart,
    PaperSource,
    Project,
)
from app.routers.papers import (
    VersionedRequest,
    _current_memory_revision,
    approve_paper_plan,
    rerun_extraction,
)
from app.tasks import paper_series
from app.settings_store import get_setting, set_setting


def _series_fixture(*, local_only: bool = False) -> tuple[int, int, int, int, PaperChunk]:
    init_db()
    suffix = uuid.uuid4().hex
    with get_session() as session:
        project = Project(
            slug=f"paper-series-{suffix}",
            title="Multipart continuity",
            source="paper.pdf",
            source_type="paper",
        )
        session.add(project)
        session.flush()
        source = PaperSource(
            project_id=project.id,
            original_filename="paper.pdf",
            source_hash=suffix.ljust(64, "0")[:64],
            relative_path=f"projects/{project.slug}/source/original.pdf",
            local_only=local_only,
            privacy_locked=True,
            status="ready",
            quality_grade="GOOD",
        )
        session.add(source)
        session.flush()
        chunk = PaperChunk(
            source_id=source.id,
            chunk_index=0,
            evidence_id=f"P-{suffix}-1",
            page_number=7,
            section_path=json.dumps(["Results"]),
            body="The reported result includes an uncertainty interval.",
            body_hash="a" * 64,
            estimated_tokens=12,
        )
        session.add(chunk)
        series = PaperSeries(
            project_id=project.id,
            audience="practitioner",
            status="approved",
            title="Results series",
            plan_version=1,
            plan_hash="plan-hash",
            plan_json=json.dumps({"parts": []}),
        )
        session.add(series)
        session.flush()
        first = PaperSeriesPart(
            series_id=series.id,
            position=1,
            title="The result",
        )
        second = PaperSeriesPart(
            series_id=series.id,
            position=2,
            title="What follows",
        )
        session.add(first)
        session.add(second)
        session.commit()
        session.refresh(chunk)
        return project.id, series.id, first.id, second.id, chunk


def test_local_only_paper_forces_chat_and_tts_to_local_providers(monkeypatch):
    project_id, _series_id, _first_id, _second_id, _chunk = _series_fixture(
        local_only=True)
    with get_session() as session:
        project = session.get(Project, project_id)
        artifact = library.write_artifact(
            session, project_id=project_id, project_slug=project.slug,
            type="paper_argument_map", title="Private argument map",
            body="Private paper-derived evidence.",
        )
        assert artifact.restricted is True
        assert library.artifact_is_cloud_excluded(session, artifact) is True
        job = Job(project_id=project_id, task="paper_analyze", status="running")
        session.add(job)
        session.commit()
        session.refresh(job)
        job_id = job.id

    previous_model = get_setting("repository.local_model")
    calls: list[str] = []
    monkeypatch.setattr(llm.settings, "ollama_base_url", "http://ollama:11434")
    monkeypatch.setattr(
        llm, "_anthropic",
        lambda *_args, **_kwargs: pytest.fail(
            "local-only paper constructed a cloud-model request"),
    )
    monkeypatch.setattr(
        llm, "_ollama",
        lambda _system, _user, model, _max_tokens, _temp,
        json_format=False, **_kwargs: calls.append(model) or "local result",
    )
    try:
        set_setting("repository.local_model", "paper-local-model")
        token = current_job_id.set(job_id)
        try:
            assert llm.complete(
                "paper_map", "system", "private paper evidence",
                provider="anthropic", model="cloud-model",
            ) == "local result"
        finally:
            current_job_id.reset(token)
        with llm.project_scope(project_id):
            assert llm.resolve_model("tts") == ("piper", "en_US-ryan-medium")
        assert calls == ["paper-local-model"]
    finally:
        set_setting("repository.local_model", previous_model)
        with get_session() as session:
            leftover = session.get(Job, job_id)
            if leftover:
                session.delete(leftover)
                session.commit()


def test_script_normalization_rejects_duplicate_ledgers_and_strips_spoken_links():
    chunk = PaperChunk(
        id=1,
        source_id=1,
        chunk_index=0,
        evidence_id="P-evidence-1",
        page_number=3,
        section_path=json.dumps(["Methods"]),
        body="Method evidence",
        body_hash="b" * 64,
    )
    body, cited, segments = paper_series._normalize_script(
        "## Segment: Method\n"
        "<!--SEGMENT_EVIDENCE:P-evidence-1-->\n"
        "HOST_A: The method [P:P-evidence-1] "
        "[p. 3](/api/papers/9/source#page=3) is useful.\n"
        "HOST_B: Agreed.",
        [chunk],
    )
    assert cited == {"P-evidence-1"}
    assert segments == 1
    spoken = "\n".join(
        line for line in body.splitlines() if line.startswith("HOST_")
    )
    assert "[P:" not in spoken
    assert "/api/papers/" not in spoken
    assert "p. 3" not in spoken
    assert "> Evidence: [p. 3" in body

    with pytest.raises(RuntimeError, match="more than one evidence ledger"):
        paper_series._normalize_script(
            "## Segment: Method\n"
            "<!--SEGMENT_EVIDENCE:P-evidence-1-->\n"
            "<!--SEGMENT_EVIDENCE:P-evidence-1-->\n"
            "HOST_A: Duplicate ledger.",
            [chunk],
        )


def test_memory_state_preserves_cumulative_continuity_and_all_prior_evidence():
    state = paper_series._memory_state(
        {
            "completed_topics": ["new result"],
            "open_questions": [],
            "promised_callbacks": ["return to limitations"],
        },
        {"P-current"},
        {
            "terminology": [{"term": "CI", "pronunciation": "see eye"}],
            "completed_topics": ["setup"],
            "open_questions": ["old question"],
            "evidence_ids": ["P-prior"],
        },
    )
    assert state["completed_topics"] == ["setup", "new result"]
    assert state["terminology"] == [
        {"term": "CI", "pronunciation": "see eye"}
    ]
    assert state["open_questions"] == []
    assert state["promised_callbacks"] == ["return to limitations"]
    assert state["evidence_ids"] == ["P-current", "P-prior"]


def test_memory_state_bounds_long_series_without_hiding_truncation():
    evidence_ids = {
        f"P-{index:04d}"
        for index in range(paper_series.MAX_MEMORY_EVIDENCE_IDS + 20)
    }
    state = paper_series._memory_state(
        {
            "covered_claims": [
                f"claim {index}"
                for index in range(paper_series.MAX_MEMORY_ITEMS_PER_FIELD + 50)
            ],
        },
        evidence_ids,
    )

    assert len(state["covered_claims"]) == paper_series.MAX_MEMORY_ITEMS_PER_FIELD
    assert len(state["evidence_ids"]) == paper_series.MAX_MEMORY_EVIDENCE_IDS
    assert state["evidence_id_count"] == len(evidence_ids)
    assert state["evidence_ids_truncated"] is True
    assert len(state["evidence_ids_digest"]) == 64


def test_approval_requires_explicit_accounting_for_supporting_evidence():
    _project_id, series_id, _first_id, _second_id, chunk = _series_fixture()
    second_evidence_id = f"{chunk.evidence_id}-supporting"
    with get_session() as session:
        source = session.get(PaperSource, chunk.source_id)
        session.add(PaperChunk(
            source_id=source.id,
            chunk_index=1,
            evidence_id=second_evidence_id,
            page_number=8,
            body="A supporting qualification.",
            body_hash="c" * 64,
        ))
        series = session.get(PaperSeries, series_id)
        series.status = "draft"
        series.plan_version = 2
        series.plan_json = json.dumps({
            "parts": [{
                "position": 1,
                "title": "The result",
                "evidence": [{
                    "evidence_id": chunk.evidence_id,
                    "role": "primary",
                    "importance": "major",
                    "reason": "central result",
                }],
            }],
            "topics": [],
            "critical_topics": [],
            "omissions": [],
        })
        session.add(series)
        session.commit()

    with pytest.raises(HTTPException, match="omission accounting") as exc:
        approve_paper_plan(series_id, VersionedRequest(expected_version=2))
    assert exc.value.status_code == 422
    assert second_evidence_id in str(exc.value.detail)

    with get_session() as session:
        series = session.get(PaperSeries, series_id)
        plan = json.loads(series.plan_json)
        plan["omissions"] = [{
            "evidence_id": second_evidence_id,
            "importance": "supporting",
            "reason": "Deferred to keep this first release within time.",
        }]
        series.plan_json = json.dumps(plan)
        session.add(series)
        session.commit()
    approved = approve_paper_plan(
        series_id, VersionedRequest(expected_version=2)
    )
    assert approved["status"] == "approved"


def test_extraction_rerun_refuses_to_strand_planned_track_assignments():
    project_id, _series_id, _first_id, _second_id, _chunk = _series_fixture()
    with pytest.raises(HTTPException, match="delete planned audience tracks") as exc:
        rerun_extraction(project_id)
    assert exc.value.status_code == 409


def test_script_regeneration_creates_revision_and_stales_current_audio_and_future(
    monkeypatch,
):
    project_id, series_id, first_id, second_id, chunk = _series_fixture()
    ledger = [{
        "evidence_id": chunk.evidence_id,
        "role": "primary",
        "importance": "critical",
        "reason": "central result",
        "page": chunk.page_number,
        "section": ["Results"],
    }]
    monkeypatch.setattr(
        paper_series,
        "_part_context",
        lambda *_args, **_kwargs: ([{"summary": "Result", "evidence_ids": [chunk.evidence_id]}], [chunk], ledger),
    )
    monkeypatch.setattr(paper_series.llm, "resolve_model", lambda _fn: ("test", "test-model"))
    monkeypatch.setattr(
        paper_series.llm,
        "complete",
        lambda *_args, **_kwargs: (
            "## Segment: Result\n"
            f"<!--SEGMENT_EVIDENCE:{chunk.evidence_id}-->\n"
            "HOST_A: The paper reports the result with uncertainty.\n"
            "HOST_B: That qualification matters."
        ),
    )
    monkeypatch.setattr(
        paper_series.llm,
        "complete_json",
        lambda *_args, **_kwargs: {
            "introduced_topics": ["uncertainty"],
            "completed_topics": ["central result"],
        },
    )
    monkeypatch.setattr(
        paper_series,
        "_write_markdown",
        lambda project, *_args, **_kwargs: Artifact(
            id=999,
            project_id=project.id,
            paper_series_id=series_id,
            paper_part_id=first_id,
            type="paper_part_script",
            title="Script",
            path="unused.md",
        ),
    )

    _artifact_id, first_memory_id = paper_series.generate_part_script(
        -1, project_id, series_id, first_id
    )
    with get_session() as session:
        first = session.get(PaperSeriesPart, first_id)
        second = session.get(PaperSeriesPart, second_id)
        first.audio_status = "done"
        second.script_status = "done"
        second.audio_status = "done"
        session.add(first)
        session.add(second)
        old_future = PaperMemoryRevision(
            series_id=series_id,
            part_id=second_id,
            parent_revision_id=first_memory_id,
            revision=2,
            state_json="{}",
            content_hash="future-memory",
        )
        session.add(old_future)
        session.commit()

    _artifact_id, regenerated_memory_id = paper_series.generate_part_script(
        -1, project_id, series_id, first_id
    )
    assert regenerated_memory_id != first_memory_id

    with get_session() as session:
        first = session.get(PaperSeriesPart, first_id)
        second = session.get(PaperSeriesPart, second_id)
        first_revisions = session.exec(select(PaperMemoryRevision).where(
            PaperMemoryRevision.part_id == first_id
        ).order_by(PaperMemoryRevision.revision)).all()
        assert len(first_revisions) == 2
        assert first_revisions[0].content_hash == first_revisions[1].content_hash
        assert first.audio_status == "stale"
        assert first.stale is True
        assert second.script_status == "stale"
        assert second.audio_status == "stale"
        assert second.stale is True
        active = _current_memory_revision(session, series_id)
        assert active.id == regenerated_memory_id
