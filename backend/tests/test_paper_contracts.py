from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest
from sqlmodel import select

from app import llm
from app.db import get_session, init_db
from app.models import (
    PaperChunk,
    PaperSeries,
    PaperSeriesPart,
    PaperSource,
    Project,
)
from app.tasks import paper as paper_tasks


def _chunks(count: int):
    return [
        SimpleNamespace(
            evidence_id=f"P{index:04d}-CONTRACT",
            page_number=index,
        )
        for index in range(1, count + 1)
    ]


def test_canonical_map_preserves_all_thirteen_structured_fields():
    evidence_id = "P0001-CANONICAL"
    expected_fields = (
        "definitions",
        "claims",
        "hypotheses",
        "methods",
        "datasets_materials",
        "results",
        "assumptions",
        "limitations",
        "prerequisites",
        "bibliography_relationships",
        "referenced_visuals",
        "topics",
        "open_questions",
    )
    assert paper_tasks._STRUCTURED_FIELDS == expected_fields
    assert len(expected_fields) == 13

    raw = {
        "summary": "Canonical summary",
        "role": "Canonical role",
        "evidence_ids": [evidence_id],
        **{
            field: [{
                "text": f"{field} fact",
                "importance": "critical",
                "evidence_ids": [evidence_id],
            }]
            for field in expected_fields
        },
    }
    mapped = paper_tasks._sanitize_map(
        raw,
        {evidence_id},
        leaf=True,
        fallback_summary="must not replace the canonical summary",
    )

    assert mapped["summary"] == "Canonical summary"
    assert mapped["role"] == "Canonical role"
    assert mapped["evidence_ids"] == [evidence_id]
    assert mapped["contract_fallback"] is False
    for field in expected_fields:
        assert len(mapped[field]) == 1
        item = mapped[field][0]
        assert {
            key: item[key]
            for key in ("text", "importance", "evidence_ids")
        } == {
            "text": f"{field} fact",
            "importance": "critical",
            "evidence_ids": [evidence_id],
        }
        assert len(item[paper_tasks._SOURCE_ITEM_IDS_KEY]) == 1


def test_custom_prompt_cannot_remove_the_mandatory_output_contract(monkeypatch):
    monkeypatch.setattr(
        paper_tasks,
        "get_prompt",
        lambda _name: "Apply my preferred teaching voice.",
    )

    resolved = paper_tasks._paper_prompt(
        "paper_map", paper_tasks.PAPER_MAP_PROMPT
    )

    assert resolved.startswith("Apply my preferred teaching voice.")
    assert "MANDATORY SYNAPSE OUTPUT CONTRACT" in resolved
    for field in paper_tasks._STRUCTURED_FIELDS:
        assert f'"{field}"' in resolved


def test_nested_model_item_preserves_its_importance_and_direct_evidence_id():
    evidence_id = "P0002-NESTED"
    mapped = paper_tasks._sanitize_map(
        {
            "claims": {
                "primary_claim": {
                    "statement": "The intervention reduced the measured error.",
                    "importance": "critical",
                    "evidence_id": evidence_id,
                },
            },
        },
        {evidence_id},
        leaf=True,
    )

    assert len(mapped["claims"]) == 1
    claim = mapped["claims"][0]
    assert claim["text"] == "The intervention reduced the measured error."
    assert claim["importance"] == "critical"
    assert claim["evidence_ids"] == [evidence_id]
    assert mapped["contract_fallback"] is False


def test_reducer_refuses_ids_found_only_in_recursively_nested_metadata():
    evidence_id = "P0003-REDUCER"
    reduced = paper_tasks._sanitize_map(
        {
            "claims": [{
                "text": "This claim has no direct citation ledger.",
                "importance": "critical",
                "metadata": {
                    "source": {
                        "evidence_ids": [evidence_id],
                    },
                },
            }],
        },
        {evidence_id},
        leaf=False,
    )

    assert reduced["claims"] == []
    # The backend-owned root ledger remains complete even when an unsupported
    # reducer item is rejected.
    assert reduced["evidence_ids"] == [evidence_id]


def test_cached_fallback_map_preserves_its_contract_fallback_marker():
    evidence_id = "P0004-FALLBACK"
    first = paper_tasks._sanitize_map(
        {"summary": "A block the model did not classify."},
        {evidence_id},
        leaf=True,
    )
    assert first["contract_fallback"] is True
    assert len(first["topics"]) == 1
    assert first["topics"][0]["importance"] == "supporting"
    assert first["topics"][0]["evidence_ids"] == [evidence_id]
    assert first["topics"][0]["text"]

    cached_body = json.loads(json.dumps(first))
    restored = paper_tasks._sanitize_map(
        cached_body,
        {evidence_id},
        leaf=True,
    )

    assert restored["contract_fallback"] is True
    assert restored["topics"] == first["topics"]


def test_duplicate_map_entries_keep_the_highest_importance():
    evidence_id = "P0005-DEDUP"
    mapped = paper_tasks._sanitize_map(
        {
            "claims": [
                {
                    "text": "The same normalized claim.",
                    "importance": "supporting",
                    "evidence_ids": [evidence_id],
                },
                {
                    "text": "The same normalized claim.",
                    "importance": "critical",
                    "evidence_ids": [evidence_id],
                },
            ],
        },
        {evidence_id},
        leaf=True,
    )

    assert len(mapped["claims"]) == 1
    assert mapped["claims"][0]["importance"] == "critical"
    assert mapped["claims"][0]["evidence_ids"] == [evidence_id]


def test_reduction_coverage_repairs_every_field_id_pair_at_highest_importance():
    first_id = "P0101-REPAIR"
    second_id = "P0102-REPAIR"
    allowed = {first_id, second_id}
    source_maps = [
        paper_tasks._sanitize_map(
            {
                "claims": [{
                    "text": "The central claim.",
                    "importance": "supporting",
                    "evidence_ids": [first_id],
                }],
                "results": [{
                    "text": "The measured result.",
                    "importance": "medium",
                    "evidence_ids": [first_id],
                }],
                "methods": [{
                    "text": "The measurement method.",
                    "importance": "important",
                    "evidence_ids": [second_id],
                }],
            },
            allowed,
            leaf=False,
        ),
        paper_tasks._sanitize_map(
            {
                "claims": [{
                    "text": "The central claim.",
                    "importance": "high",
                    "evidence_ids": [first_id],
                }],
                "results": [{
                    "text": "The measured result.",
                    "importance": "essential",
                    "evidence_ids": [first_id],
                }],
                "limitations": [{
                    "text": "The principal validity limitation.",
                    "importance": "primary",
                    "evidence_ids": [second_id],
                }],
            },
            allowed,
            leaf=False,
        ),
    ]
    for source in source_maps:
        paper_tasks._ensure_source_item_lineage(source)
    claim_lineage = source_maps[0]["claims"][0][
        paper_tasks._SOURCE_ITEM_IDS_KEY
    ]
    reduced = paper_tasks._sanitize_map(
        {
            "claims": [{
                "text": "The central claim.",
                "importance": "low",
                "evidence_ids": [first_id],
                "source_item_ids": claim_lineage,
            }],
        },
        allowed,
        leaf=False,
    )
    initial_pairs = paper_tasks._structured_field_id_pairs(reduced)
    required_pairs = {
        pair
        for source in source_maps
        for pair in paper_tasks._structured_field_id_pairs(source)
    }

    repair = paper_tasks._repair_reduction_coverage(
        reduced,
        source_maps,
        allowed,
    )

    assert paper_tasks._structured_field_id_pairs(reduced) == required_pairs
    assert repair["field_id_pairs"] == len(required_pairs - initial_pairs) == 3
    assert repair["evidence_ids"] == [first_id, second_id]
    assert {
        key: reduced["claims"][0][key]
        for key in ("text", "importance", "evidence_ids")
    } == {
        "text": "The central claim.",
        "importance": "critical",
        "evidence_ids": [first_id],
    }
    assert {
        key: reduced["results"][0][key]
        for key in ("text", "importance", "evidence_ids")
    } == {
        "text": "The measured result.",
        "importance": "critical",
        "evidence_ids": [first_id],
    }
    assert {
        key: reduced["methods"][0][key]
        for key in ("text", "importance", "evidence_ids")
    } == {
        "text": "The measurement method.",
        "importance": "major",
        "evidence_ids": [second_id],
    }
    assert {
        key: reduced["limitations"][0][key]
        for key in ("text", "importance", "evidence_ids")
    } == {
        "text": "The principal validity limitation.",
        "importance": "critical",
        "evidence_ids": [second_id],
    }


def test_reduction_lineage_preserves_distinct_same_field_items():
    evidence_id = "P0103-LINEAGE"
    allowed = {evidence_id}
    source = paper_tasks._sanitize_map(
        {
            "claims": [
                {
                    "text": "The intervention reduces adjudicated errors.",
                    "importance": "critical",
                    "evidence_ids": [evidence_id],
                },
                {
                    "text": "The paper does not establish a latency benefit.",
                    "importance": "major",
                    "evidence_ids": [evidence_id],
                },
            ],
        },
        allowed,
        leaf=True,
    )
    first = source["claims"][0]
    reduced = paper_tasks._sanitize_map(
        {
            "claims": [{
                "text": first["text"],
                "importance": first["importance"],
                "evidence_ids": first["evidence_ids"],
                "source_item_ids": first[paper_tasks._SOURCE_ITEM_IDS_KEY],
            }],
        },
        allowed,
        leaf=False,
    )

    repair = paper_tasks._repair_reduction_coverage(
        reduced, [source], allowed
    )

    assert repair["field_id_pairs"] == 0
    assert repair["lineage_units"] == 1
    assert {
        entry["text"] for entry in reduced["claims"]
    } == {
        "The intervention reduces adjudicated errors.",
        "The paper does not establish a latency benefit.",
    }


def test_reduction_restores_importance_even_with_complete_lineage():
    evidence_id = "P0104-IMPORTANCE"
    allowed = {evidence_id}
    source = paper_tasks._sanitize_map(
        {
            "results": [{
                "text": "The primary result remains central.",
                "importance": "critical",
                "evidence_ids": [evidence_id],
            }],
        },
        allowed,
        leaf=True,
    )
    source_entry = source["results"][0]
    reduced = paper_tasks._sanitize_map(
        {
            "results": [{
                "text": source_entry["text"],
                "importance": "supporting",
                "evidence_ids": [evidence_id],
                "source_item_ids": source_entry[
                    paper_tasks._SOURCE_ITEM_IDS_KEY
                ],
            }],
        },
        allowed,
        leaf=False,
    )

    repair = paper_tasks._repair_reduction_coverage(
        reduced, [source], allowed
    )

    assert repair["lineage_units"] == 0
    assert repair["field_id_pairs"] == 0
    assert reduced["results"][0]["importance"] == "critical"


def test_noncanonical_importance_aliases_normalize_predictably():
    cases = {
        " low ": "supporting",
        "MINOR": "supporting",
        "optional": "supporting",
        "context": "supporting",
        "contextual": "supporting",
        "medium": "major",
        "IMPORTANT": "major",
        "substantive": "major",
        "high": "critical",
        "ESSENTIAL": "critical",
        "primary": "critical",
        "central": "critical",
    }
    for raw, expected in cases.items():
        assert paper_tasks._importance(raw) == expected

    assert paper_tasks._importance("not-a-level") == "supporting"
    assert paper_tasks._importance("not-a-level", "major") == "major"
    assert paper_tasks._importance(None, "critical") == "critical"


def test_plan_honors_supporting_omission_and_backfills_critical_omission():
    chunks = _chunks(2)
    critical_id, supporting_id = [chunk.evidence_id for chunk in chunks]
    topics = [
        {
            "id": "T-critical",
            "title": "Critical result",
            "importance": "critical",
            "evidence_ids": [critical_id],
        },
        {
            "id": "T-supporting",
            "title": "Supporting context",
            "importance": "supporting",
            "evidence_ids": [supporting_id],
        },
    ]
    plan = paper_tasks._clean_plan(
        {
            "parts": [{"title": "The complete critical argument"}],
            "omissions": [
                {
                    "topic_id": "T-critical",
                    "evidence_id": critical_id,
                    "importance": "critical",
                    "reason": "The model incorrectly tried to omit it.",
                },
                {
                    "topic_id": "T-supporting",
                    "evidence_id": supporting_id,
                    "importance": "supporting",
                    "reason": "Deferred to keep the part focused.",
                },
            ],
        },
        audience="practitioner",
        chunks=chunks,
        importance={
            critical_id: "critical",
            supporting_id: "supporting",
        },
        topics=topics,
    )

    primary_ids = [
        evidence_id
        for part in plan["parts"]
        for evidence_id in part["primary_evidence_ids"]
    ]
    assert primary_ids == [critical_id]
    assert plan["omissions"] == [{
        "evidence_id": supporting_id,
        "importance": "supporting",
        "reason": "Deferred to keep the part focused.",
    }]
    assert plan["coverage"] == {
        "total_evidence_blocks": 2,
        "assigned_primary_blocks": 1,
        "omitted_supporting_blocks": 1,
        "critical_total": 1,
        "critical_assigned": 1,
        "major_total": 0,
        "major_assigned": 0,
        "complete_for_approval": True,
    }


def test_plan_accepts_scalar_dict_and_api_shaped_evidence_assignments():
    chunks = _chunks(3)
    first_id, second_id, third_id = [chunk.evidence_id for chunk in chunks]
    plan = paper_tasks._clean_plan(
        {
            "parts": [
                {
                    "title": "Scalar canonical ID",
                    "primary_evidence_ids": third_id,
                },
                {
                    "title": "Object canonical ID",
                    "primary_evidence_ids": {"evidence_id": first_id},
                },
                {
                    "title": "API-shaped assignments",
                    "evidence": [
                        {
                            "evidence_id": second_id,
                            "role": "primary",
                            "importance": "major",
                        },
                        {
                            "evidence_id": third_id,
                            "role": "bridge",
                            "importance": "major",
                        },
                    ],
                },
            ],
        },
        audience="expert",
        chunks=chunks,
        importance={chunk.evidence_id: "major" for chunk in chunks},
        topics=[],
    )

    assert [
        part["primary_evidence_ids"] for part in plan["parts"]
    ] == [[third_id], [first_id], [second_id]]
    assert [
        part["bridge_evidence_ids"] for part in plan["parts"]
    ] == [[], [], [third_id]]
    assert [
        evidence_id
        for part in plan["parts"]
        for evidence_id in part["primary_evidence_ids"]
    ] == [third_id, first_id, second_id]


def test_sparse_five_part_model_output_is_pruned_to_nonempty_consecutive_parts():
    chunks = _chunks(2)
    plan = paper_tasks._clean_plan(
        {
            "parts": [
                {"title": f"Proposed Part {position}"}
                for position in range(1, 6)
            ],
        },
        audience="generalist",
        chunks=chunks,
        importance={chunk.evidence_id: "major" for chunk in chunks},
        topics=[],
    )

    assert len(plan["parts"]) == 2
    assert [part["position"] for part in plan["parts"]] == [1, 2]
    assert all(part["primary_evidence_ids"] for part in plan["parts"])
    assert {
        evidence_id
        for part in plan["parts"]
        for evidence_id in part["primary_evidence_ids"]
    } == {chunk.evidence_id for chunk in chunks}


def test_generate_series_plan_preserves_requested_track_contract_and_real_part_ids(
    monkeypatch,
):
    init_db()
    suffix = uuid.uuid4().hex
    requested_title = "Human-authored forty-minute arc"
    user_guidance = "Lead with the practical consequence, then explain the method."
    with get_session() as session:
        project = Project(
            slug=f"paper-plan-contract-{suffix}",
            title="Plan contract fixture",
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
            local_only=False,
            privacy_locked=True,
            status="ready",
            quality_grade="GOOD",
            page_count=2,
            parser_version="fixture-v1",
            parser_config_hash="fixture-config",
        )
        session.add(source)
        session.flush()
        critical = PaperChunk(
            source_id=source.id,
            chunk_index=0,
            evidence_id=f"P0001-{suffix[:12]}",
            page_number=1,
            section_path=json.dumps(["Results"]),
            kind="result",
            body="The primary result is both material and uncertain.",
            body_hash="a" * 64,
            estimated_tokens=12,
        )
        major = PaperChunk(
            source_id=source.id,
            chunk_index=1,
            evidence_id=f"P0002-{suffix[:12]}",
            page_number=2,
            section_path=json.dumps(["Methods"]),
            kind="method",
            body="The method establishes how the primary result was measured.",
            body_hash="b" * 64,
            estimated_tokens=12,
        )
        session.add(critical)
        session.add(major)
        series = PaperSeries(
            project_id=project.id,
            audience="practitioner",
            status="draft",
            title=requested_title,
            target_minutes=40,
            plan_version=0,
            plan_json="{}",
            user_guidance=user_guidance,
        )
        session.add(series)
        session.commit()
        session.refresh(project)
        session.refresh(series)
        project_id = project.id
        series_id = series.id
        critical_id = critical.evidence_id
        major_id = major.evidence_id

    bundle = {
        "schema": 2,
        "source_hash": suffix.ljust(64, "0")[:64],
        "analysis_config_signature": "analysis-signature",
        "hierarchical_context_digest": "context-digest",
        "hierarchical_context": [{
            "summary": "Complete reduced context",
            "claims": [{
                "text": "The critical result.",
                "importance": "critical",
                "evidence_ids": [critical_id],
            }],
            "methods": [{
                "text": "The major method.",
                "importance": "major",
                "evidence_ids": [major_id],
            }],
            "evidence_ids": [critical_id, major_id],
        }],
        "leaf_evidence_ids": [critical_id, major_id],
        "evidence_importance": {
            critical_id: "critical",
            major_id: "major",
        },
        "topics": [
            {
                "id": "T-critical-result",
                "type": "results",
                "title": "Critical result",
                "importance": "critical",
                "evidence_ids": [critical_id],
            },
            {
                "id": "T-major-method",
                "type": "methods",
                "title": "Major method",
                "importance": "major",
                "evidence_ids": [major_id],
            },
        ],
        "critical_evidence_ids": [critical_id],
        "major_evidence_ids": [major_id],
        "coverage": {
            "total_evidence_blocks": 2,
            "mapped_evidence_blocks": 2,
            "unmapped_evidence_blocks": 0,
        },
    }
    captured: dict[str, object] = {}

    def complete_json(function, _system, user, **_kwargs):
        captured["function"] = function
        captured["context"] = json.loads(user.split("\n", 1)[1])
        return {
            # Saved track metadata is authoritative over conflicting model
            # suggestions.
            "title": "Model-proposed replacement title",
            "target_minutes": 60,
            "parts": [
                {
                    "title": "What the result means",
                    "focus": "Explain the result before its implementation details.",
                    "primary_evidence_ids": [critical_id],
                },
                {
                    "title": "How it was measured",
                    "focus": "Connect the method to the reported result.",
                    "primary_evidence_ids": [major_id],
                    "bridge_evidence_ids": [critical_id],
                },
            ],
            "omissions": [],
        }

    monkeypatch.setattr(
        paper_tasks,
        "latest_analysis_bundle",
        lambda _project_id: bundle,
    )
    monkeypatch.setattr(
        paper_tasks,
        "build_analysis_bundle",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("the supplied analysis bundle must be reused")
        ),
    )
    monkeypatch.setattr(
        llm,
        "resolve_model",
        lambda _function: ("anthropic", "fixture-plan-model"),
    )
    monkeypatch.setattr(llm, "complete_json", complete_json)
    monkeypatch.setattr(paper_tasks, "_cache_get", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(paper_tasks, "_cache_put", lambda *_args, **_kwargs: None)

    plan = paper_tasks.generate_series_plan(
        0,
        project_id,
        series_id,
        force=True,
    )

    assert captured["function"] == "paper_plan"
    planning_context = captured["context"]
    assert planning_context["requested_target_minutes"] == 40
    assert planning_context["requested_title"] == requested_title
    assert planning_context["user_guidance"] == user_guidance
    assert set(planning_context["required_primary_evidence_ids"]) == {
        critical_id,
        major_id,
    }
    assert {
        row["evidence_id"]: row["importance"]
        for row in planning_context["required_evidence_ledger"]
    } == {
        critical_id: "critical",
        major_id: "major",
    }

    assert plan["schema"] == 2
    assert plan["title"] == requested_title
    assert plan["target_minutes"] == 40
    assert len(plan["parts"]) == 2
    assert all(isinstance(part.get("id"), int) for part in plan["parts"])

    with get_session() as session:
        persisted_series = session.get(PaperSeries, series_id)
        persisted_plan = json.loads(persisted_series.plan_json)
        persisted_parts = session.exec(
            select(PaperSeriesPart)
            .where(PaperSeriesPart.series_id == series_id)
            .order_by(PaperSeriesPart.position)
        ).all()

        assert persisted_series.title == requested_title
        assert persisted_series.target_minutes == 40
        assert persisted_series.user_guidance == user_guidance
        assert persisted_plan["schema"] == 2
        assert persisted_plan["title"] == requested_title
        assert persisted_plan["target_minutes"] == 40
        assert [part["id"] for part in persisted_plan["parts"]] == [
            part.id for part in persisted_parts
        ]
        assert [part["id"] for part in plan["parts"]] == [
            part.id for part in persisted_parts
        ]
        assert all(part.target_minutes == 40 for part in persisted_parts)


def test_draft_selected_series_plans_only_selects_unplanned_version_zero_tracks(
    monkeypatch,
):
    init_db()
    suffix = uuid.uuid4().hex
    edited_plan = {
        "title": "User-edited schema-less plan",
        "parts": [{"title": "Keep this edit"}],
    }
    with get_session() as session:
        project = Project(
            slug=f"paper-draft-selection-{suffix}",
            title="Draft selection fixture",
            source="paper.pdf",
            source_type="paper",
        )
        session.add(project)
        session.flush()
        unplanned = PaperSeries(
            project_id=project.id,
            audience="generalist",
            status="draft",
            plan_version=0,
            plan_json="{}",
        )
        edited = PaperSeries(
            project_id=project.id,
            audience="practitioner",
            status="draft",
            plan_version=3,
            plan_json=json.dumps(edited_plan),
        )
        approved_unplanned = PaperSeries(
            project_id=project.id,
            audience="expert",
            status="approved",
            plan_version=0,
            plan_json="{}",
        )
        session.add(unplanned)
        session.add(edited)
        session.add(approved_unplanned)
        session.commit()
        session.refresh(project)
        session.refresh(unplanned)
        session.refresh(edited)
        project_id = project.id
        unplanned_id = unplanned.id
        edited_id = edited.id

    calls: list[tuple[int, int, int]] = []

    def generate(job_id, selected_project_id, series_id):
        calls.append((job_id, selected_project_id, series_id))
        return {"schema": 2}

    monkeypatch.setattr(paper_tasks, "generate_series_plan", generate)

    selected = paper_tasks.draft_selected_series_plans(77, project_id)

    assert selected == [unplanned_id]
    assert calls == [(77, project_id, unplanned_id)]
    with get_session() as session:
        edited = session.get(PaperSeries, edited_id)
        assert edited.status == "draft"
        assert edited.plan_version == 3
        assert json.loads(edited.plan_json) == edited_plan


def test_generate_series_plan_preserves_concurrently_saved_newer_plan_and_parts(
    monkeypatch,
):
    init_db()
    suffix = uuid.uuid4().hex
    with get_session() as session:
        project = Project(
            slug=f"paper-plan-race-{suffix}",
            title="Concurrent plan fixture",
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
            local_only=False,
            privacy_locked=True,
            status="ready",
            quality_grade="GOOD",
            page_count=1,
            parser_version="fixture-v1",
            parser_config_hash="fixture-config",
        )
        session.add(source)
        session.flush()
        chunk = PaperChunk(
            source_id=source.id,
            chunk_index=0,
            evidence_id=f"P0001-{suffix[:12]}",
            page_number=1,
            section_path=json.dumps(["Results"]),
            kind="result",
            body="The critical result used by the planning race fixture.",
            body_hash="c" * 64,
            estimated_tokens=12,
        )
        session.add(chunk)
        series = PaperSeries(
            project_id=project.id,
            audience="expert",
            status="draft",
            title="Original requested title",
            target_minutes=50,
            plan_version=0,
            plan_json="{}",
        )
        session.add(series)
        session.commit()
        session.refresh(project)
        session.refresh(series)
        project_id = project.id
        series_id = series.id
        evidence_id = chunk.evidence_id

    bundle = {
        "schema": 2,
        "source_hash": suffix.ljust(64, "0")[:64],
        "analysis_config_signature": "race-analysis-signature",
        "hierarchical_context_digest": "race-context-digest",
        "hierarchical_context": [{
            "summary": "Concurrent fixture context",
            "results": [{
                "text": "The critical result.",
                "importance": "critical",
                "evidence_ids": [evidence_id],
            }],
            "evidence_ids": [evidence_id],
        }],
        "leaf_evidence_ids": [evidence_id],
        "evidence_importance": {evidence_id: "critical"},
        "topics": [{
            "id": "T-race-result",
            "type": "results",
            "title": "Critical result",
            "importance": "critical",
            "evidence_ids": [evidence_id],
        }],
        "critical_evidence_ids": [evidence_id],
        "major_evidence_ids": [],
        "coverage": {
            "total_evidence_blocks": 1,
            "mapped_evidence_blocks": 1,
            "unmapped_evidence_blocks": 0,
        },
    }
    concurrent: dict[str, object] = {}

    def complete_json(_function, _system, _user, **_kwargs):
        with get_session() as session:
            series = session.get(PaperSeries, series_id)
            newer_part = PaperSeriesPart(
                series_id=series_id,
                position=1,
                title="Concurrent user-authored part",
                focus="This part must survive the stale model response.",
                target_minutes=45,
            )
            session.add(newer_part)
            session.flush()
            newer_plan = {
                "schema": 2,
                "title": "Concurrent user-authored plan",
                "target_minutes": 45,
                "parts": [{
                    "id": newer_part.id,
                    "position": 1,
                    "title": newer_part.title,
                    "focus": newer_part.focus,
                    "target_minutes": newer_part.target_minutes,
                    "evidence": [],
                }],
                "omissions": [],
            }
            series.title = newer_plan["title"]
            series.target_minutes = 45
            series.plan_version = 1
            series.plan_json = json.dumps(newer_plan, sort_keys=True)
            session.add(series)
            session.commit()
            concurrent["part_id"] = newer_part.id
            concurrent["plan"] = newer_plan
        return {
            "title": "Stale model plan",
            "parts": [{
                "title": "Stale generated part",
                "primary_evidence_ids": [evidence_id],
            }],
            "omissions": [],
        }

    monkeypatch.setattr(
        paper_tasks,
        "latest_analysis_bundle",
        lambda _project_id: bundle,
    )
    monkeypatch.setattr(
        llm,
        "resolve_model",
        lambda _function: ("anthropic", "fixture-plan-model"),
    )
    monkeypatch.setattr(llm, "complete_json", complete_json)
    monkeypatch.setattr(paper_tasks, "_cache_get", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(paper_tasks, "_cache_put", lambda *_args, **_kwargs: None)

    with pytest.raises(ValueError, match="newer plan was preserved"):
        paper_tasks.generate_series_plan(
            0,
            project_id,
            series_id,
            force=True,
        )

    with get_session() as session:
        preserved_series = session.get(PaperSeries, series_id)
        preserved_parts = session.exec(
            select(PaperSeriesPart).where(
                PaperSeriesPart.series_id == series_id
            )
        ).all()

        assert preserved_series.status == "draft"
        assert preserved_series.plan_version == 1
        assert preserved_series.title == "Concurrent user-authored plan"
        assert preserved_series.target_minutes == 45
        assert json.loads(preserved_series.plan_json) == concurrent["plan"]
        assert len(preserved_parts) == 1
        assert preserved_parts[0].id == concurrent["part_id"]
        assert preserved_parts[0].title == "Concurrent user-authored part"
        assert preserved_parts[0].target_minutes == 45
