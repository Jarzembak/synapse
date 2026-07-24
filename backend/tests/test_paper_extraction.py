from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlmodel import select

from app import llm
from app.db import get_session
from app.models import PaperChunk, PaperSource, Project
from app.paper import (
    PaperAnalysisBlocked, PaperExtractionConfig, PaperExtractionError,
    ParsedBlock, ParsedPageQuality, ParsedPaper, acknowledged_page_numbers,
    extract_pdf, extraction_blockers, persist_extraction,
    require_analysis_ready, validate_and_render_citations,
)
from app.tasks import paper as paper_tasks


def _pdf(tmp_path, name: str = "fixture.pdf"):
    path = tmp_path / name
    path.write_bytes(b"%PDF-1.7\n% deterministic parser fixture\n%%EOF\n")
    return path


class StaticParser:
    def __init__(self, parsed: ParsedPaper):
        self.parsed = parsed

    def parse(self, _path, _config):
        return self.parsed


def _parsed(*blocks: ParsedBlock, page_count: int | None = None,
            grades: dict[int, str] | None = None,
            document_grade: str = "GOOD") -> ParsedPaper:
    count = page_count or max(block.page_number for block in blocks)
    qualities = tuple(
        ParsedPageQuality(page, grade)
        for page, grade in sorted((grades or {}).items())
    )
    return ParsedPaper(
        page_count=count,
        blocks=tuple(blocks),
        page_quality=qualities,
        document_grade=document_grade,
        parser_name="fixture",
        parser_version="fixture-v1",
    )


def test_stable_page_evidence_ids_and_complete_block_splitting(tmp_path):
    source = _pdf(tmp_path)
    long_definition = "Definition 1: dense concept.\n\n" + ("evidence " * 350)
    parsed = _parsed(
        ParsedBlock(
            body=long_definition,
            page_number=1,
            kind="prose",
            section_path=("Foundations",),
            bbox={"l": 10, "t": 20, "r": 300, "b": 700},
        ),
        ParsedBlock(
            body="| input | output |\n|---|---|\n| a | b |",
            page_number=2,
            kind="table",
            section_path=("Methods",),
            bbox=None,
        ),
        ParsedBlock(
            body="Figure 4. Result geometry (visual not interpreted).",
            page_number=3,
            kind="visual",
            section_path=("Results",),
            bbox={"l": 20, "t": 40, "r": 500, "b": 600},
        ),
        page_count=3,
        grades={1: "GOOD", 2: "FAIR", 3: "GOOD"},
    )
    config = PaperExtractionConfig(
        ocr_languages=("eng", "deu"), max_evidence_characters=1_000)

    first = extract_pdf(source, config, parser=StaticParser(parsed))
    second = extract_pdf(source, config, parser=StaticParser(parsed))

    assert [row.evidence_id for row in first.evidence] == [
        row.evidence_id for row in second.evidence]
    definition_parts = [row for row in first.evidence if row.kind == "definition"]
    assert len(definition_parts) > 1
    assert "".join(row.body for row in definition_parts) == long_definition
    assert max(row.page_number for row in first.evidence) == 3
    table = next(row for row in first.evidence if row.kind == "table")
    visual = next(row for row in first.evidence if row.kind == "visual")
    assert "unreliable_extraction" in table.flags
    assert "visual_review_needed" in visual.flags
    assert first.coverage_report["sampling"] is False
    assert first.coverage_report["prefix_truncation"] is False


@pytest.mark.parametrize(("language", "body"), [
    ("eng", "Definition: evidence has a source."),
    ("spa", "Definición: la evidencia tiene una fuente."),
    ("fra", "Définition: les preuves ont une source."),
    ("deu", "Definition: Belege haben eine Quelle."),
])
def test_supported_ocr_languages_preserve_multilingual_definitions(
        tmp_path, language, body):
    source = _pdf(tmp_path, f"{language}.pdf")
    result = extract_pdf(
        source,
        PaperExtractionConfig(ocr_languages=(language,)),
        parser=StaticParser(_parsed(ParsedBlock(body=body, page_number=1))),
    )
    assert result.evidence[0].kind == "definition"
    assert result.evidence[0].body == body


def test_admission_limits_fail_without_partial_results(tmp_path):
    source = _pdf(tmp_path)
    parsed = _parsed(
        ParsedBlock(body="x" * 200, page_number=1),
        ParsedBlock(body="final page", page_number=2),
        page_count=2,
    )
    with pytest.raises(PaperExtractionError, match="configured limit"):
        extract_pdf(
            source,
            PaperExtractionConfig(max_pages=1),
            parser=StaticParser(parsed),
        )
    with pytest.raises(PaperExtractionError, match="no partial paper analysis"):
        extract_pdf(
            source,
            PaperExtractionConfig(max_extracted_characters=100),
            parser=StaticParser(parsed),
        )
    with pytest.raises(PaperExtractionError, match="bytes"):
        extract_pdf(
            source,
            PaperExtractionConfig(max_file_bytes=8),
            parser=StaticParser(parsed),
        )


def test_poor_nontrivial_page_blocks_until_named_acknowledgement(tmp_path):
    source_path = _pdf(tmp_path)
    parsed = _parsed(
        ParsedBlock(body="readable first page " * 5, page_number=1),
        ParsedBlock(body="poor scan content " * 5, page_number=2),
        ParsedBlock(body="x", page_number=3),
        page_count=3,
        grades={1: "GOOD", 2: "POOR", 3: "POOR"},
        document_grade="POOR",
    )
    result = extract_pdf(source_path, parser=StaticParser(parsed))
    # Page 3 is trivial prose, so only the named nontrivial page blocks.
    assert result.quality_report["poor_pages"] == [2]
    source = SimpleNamespace(
        status="review_required",
        quality_report=json.dumps(result.quality_report),
        acknowledged_pages="[]",
    )
    with pytest.raises(PaperAnalysisBlocked, match=r"page\(s\) 2"):
        require_analysis_ready(source)

    source.acknowledged_pages = json.dumps([
        {"page": 2, "reason": "Original scan is degraded; use with caution."}
    ])
    assert acknowledged_page_numbers(source) == {2}
    assert extraction_blockers(source) == []
    require_analysis_ready(source)


def _paper_project(slug: str, source_hash: str = "a" * 64):
    with get_session() as session:
        project = Project(
            slug=slug,
            title=slug.replace("-", " ").title(),
            source=f"projects/{slug}/source/original.pdf",
            source_type="paper",
        )
        session.add(project)
        session.commit()
        session.refresh(project)
        source = PaperSource(
            project_id=project.id,
            original_filename="fixture.pdf",
            source_hash=source_hash,
            relative_path=f"projects/{slug}/source/original.pdf",
            local_only=False,
            status="ready",
            parser_version="fixture-v1",
            parser_config_hash="fixture-config",
        )
        session.add(source)
        session.commit()
        session.refresh(project)
        session.refresh(source)
        session.expunge(project)
        session.expunge(source)
        return project, source


def test_persist_extraction_rebuilds_paper_fts(tmp_path):
    path = _pdf(tmp_path)
    parsed = _parsed(
        ParsedBlock(body="first indexed evidence", page_number=1),
        ParsedBlock(body="final page evidence", page_number=2),
        page_count=2,
        grades={1: "GOOD", 2: "GOOD"},
    )
    result = extract_pdf(path, parser=StaticParser(parsed))
    project, source = _paper_project("paper-fts-refresh", result.source_hash)
    with get_session() as session:
        source = session.get(PaperSource, source.id)
        rows = persist_extraction(session, source, result)
        assert len(rows) == 2
        session.refresh(source)
        assert source.status == "ready"
        indexed = session.exec(text(
            "SELECT body, page_number, evidence_id FROM paper_chunk_fts "
            "WHERE source_id=:source_id ORDER BY page_number"
        ).bindparams(source_id=source.id)).all()
        assert [row[0] for row in indexed] == [
            "first indexed evidence", "final page evidence"]
        assert indexed[-1][1] == 2


def test_same_pdf_can_be_imported_into_independent_projects(tmp_path):
    path = _pdf(tmp_path, "same-source.pdf")
    result = extract_pdf(path, parser=StaticParser(_parsed(
        ParsedBlock(body="shared immutable evidence", page_number=1),
        grades={1: "GOOD"},
    )))
    _first_project, first_source = _paper_project(
        "paper-duplicate-source-a", result.source_hash)
    _second_project, second_source = _paper_project(
        "paper-duplicate-source-b", result.source_hash)

    with get_session() as session:
        first = persist_extraction(
            session, session.get(PaperSource, first_source.id), result)
        second = persist_extraction(
            session, session.get(PaperSource, second_source.id), result)
        assert first[0].evidence_id == second[0].evidence_id
        assert first[0].source_id != second[0].source_id
        matches = session.exec(select(PaperChunk).where(
            PaperChunk.evidence_id == first[0].evidence_id,
        )).all()
        assert {row.source_id for row in matches} == {
            first_source.id, second_source.id,
        }


def test_paper_citations_validate_and_render_page_links():
    evidence = [{
        "evidence_id": "P0007-ABCDEF123",
        "page_number": 7,
        "section_path": ["Results", "Uncertainty"],
    }]
    rendered, count = validate_and_render_citations(
        "The interval is wide [P:P0007-ABCDEF123].",
        project_id=42,
        source=SimpleNamespace(source_hash="f" * 64),
        evidence=evidence,
    )
    assert count == 1
    assert "/api/papers/42/source#page=7" in rendered
    assert "<!--P:P0007-ABCDEF123-->" in rendered
    with pytest.raises(RuntimeError, match="invalid paper evidence"):
        validate_and_render_citations(
            "Invented [P:P9999-NOTREAL]",
            project_id=42,
            source=SimpleNamespace(source_hash="f" * 64),
            evidence=evidence,
        )


def test_map_cache_covers_final_page_and_reuses_every_leaf(monkeypatch, tmp_path):
    path = _pdf(tmp_path)
    parsed = _parsed(
        ParsedBlock(body="opening claim", page_number=1),
        ParsedBlock(body="middle method", page_number=2),
        ParsedBlock(body="critical finding on the final page", page_number=9),
        page_count=9,
        grades={1: "GOOD", 2: "GOOD", 9: "GOOD"},
    )
    result = extract_pdf(path, parser=StaticParser(parsed))
    project, source = _paper_project("paper-map-cache", result.source_hash)
    with get_session() as session:
        source = session.get(PaperSource, source.id)
        persist_extraction(session, source, result)

    calls: list[str] = []

    def complete_json(_function, _system, user, **_kwargs):
        evidence_id = json.loads(user.split("\n", 2)[1])["evidence_id"]
        calls.append(evidence_id)
        return {
            "summary": f"mapped {evidence_id}",
            "claims": [{
                "text": "paper claim",
                "importance": "critical" if "final" in user else "major",
                "evidence_ids": [evidence_id],
            }],
        }

    monkeypatch.setattr(llm, "resolve_model", lambda _function: ("ollama", "fixture"))
    monkeypatch.setattr(llm, "complete_json", complete_json)
    first, first_coverage = paper_tasks.map_all_evidence(0, project.id)
    second, second_coverage = paper_tasks.map_all_evidence(0, project.id)

    assert len(calls) == 3
    assert first == second
    assert first_coverage["mapped_evidence_blocks"] == 3
    assert first_coverage["last_page_mapped"] == 9
    assert second_coverage["cache"]["reused_leaf_maps"] == 3


def test_hierarchical_reduction_preserves_full_id_union(monkeypatch):
    project, _source = _paper_project("paper-reduce-union")
    maps = []
    expected = set()
    for index in range(12):
        evidence_id = f"P{index + 1:04d}-EVIDENCE{index:04d}"
        expected.add(evidence_id)
        maps.append({
            "summary": "dense summary " * 80,
            "topics": [{
                "text": f"topic {index} " + ("detail " * 30),
                "importance": "major",
                "evidence_ids": [evidence_id],
            }],
            "evidence_ids": [evidence_id],
        })
    calls = []
    monkeypatch.setattr(llm, "resolve_model", lambda _function: ("ollama", "fixture"))
    monkeypatch.setattr(llm, "complete_json", lambda *args, **kwargs: (
        calls.append(args[2]) or {"summary": "bounded reduction"}))
    monkeypatch.setattr(paper_tasks, "_paper_analysis_settings", lambda: {
        "map_output_tokens": 1_000,
        "reduce_batch_tokens": 1_000,
        "reduce_output_tokens": 1_000,
        "final_context_tokens": 180,
        "synthesis_output_tokens": 1_000,
    })

    context, report = paper_tasks.hierarchical_reduce(
        0, project.id, maps, purpose="test-union")
    assert calls
    assert paper_tasks._all_evidence_ids(context) == expected
    assert report["evidence_ids_preserved"] == len(expected)


def test_plan_normalization_assigns_every_late_block_once():
    chunks = [
        SimpleNamespace(evidence_id=f"P{page:04d}-X", page_number=page)
        for page in range(1, 11)
    ]
    importance = {
        chunk.evidence_id: ("critical" if chunk.page_number == 10 else "major")
        for chunk in chunks
    }
    topics = [{
        "id": "T-final-result",
        "title": "Final-page result",
        "importance": "critical",
        "evidence_ids": [chunks[-1].evidence_id],
    }]
    plan = paper_tasks._clean_plan(
        {
            "title": "Two-part arc",
            "parts": [
                {"title": "Foundations", "primary_evidence_ids": [chunks[0].evidence_id]},
                {"title": "Results and critique"},
            ],
        },
        audience="expert",
        chunks=chunks,
        importance=importance,
        topics=topics,
    )
    primary = [
        row["evidence_id"]
        for part in plan["parts"]
        for row in part["evidence"] if row["role"] == "primary"
    ]
    assert sorted(primary) == sorted(chunk.evidence_id for chunk in chunks)
    assert len(primary) == len(set(primary))
    assert any(
        "T-final-result" in part["topics"] and chunks[-1].evidence_id in part["evidence_ids"]
        for part in plan["parts"]
    )
    assert plan["coverage"]["critical_assigned"] == 1
