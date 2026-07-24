"""Audience-track and multipart production for page-grounded papers.

Study guides consume only assigned evidence and are independent. Scripts are
sequential because each finalized script emits an immutable continuity-memory
revision. Audio is deliberately downstream of (and irrelevant to) memory.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Iterable

from sqlmodel import select, text

from .. import library, llm, paper as paper_store
from ..config import advanced
from ..context import current_job_id
from ..db import get_session
from ..models import (
    Artifact, PaperChunk, PaperMemoryRevision, PaperPartEvidence, PaperSeries,
    PaperSeriesPart, PaperSource, Project, utcnow,
)
from ..settings_store import get_setting
from . import media
from .celery_app import celery
from .common import auto_tag, get_project, pipeline_task, progress
from .paper import (
    _compact_map_for_prompt, _paper_analysis_settings, hierarchical_reduce,
    latest_analysis_bundle, map_all_evidence, paper_analysis_config_signature,
    paper_analysis_lineage, paper_model_execution_signature,
    paper_source_signature_from_model,
)
from .prompts import get_prompt


SUITE: tuple[tuple[str, str, str], ...] = (
    ("paper_overview", "Audience overview",
     "Give an accurate orientation to the paper's question, argument, approach, findings, and stakes."),
    ("paper_methods", "Methods and reproducibility guide",
     "Explain methods, datasets/materials, conditions, dependencies, reproducibility steps, and gaps."),
    ("paper_evidence", "Evidence and results guide",
     "Trace results to methods and evidence, preserving uncertainty, effect direction, and negative findings."),
    ("paper_prerequisites", "Prerequisite knowledge and terminology",
     "Teach prerequisites and terminology in dependency order for this audience."),
    ("paper_critique", "Balanced limitations and critique",
     "Separate the paper's stated limitations from balanced model critique and unresolved questions."),
    ("paper_deepdive_explanatory", "Explanatory deep dive",
     "Build a cohesive, detailed explanation of the paper without flattening its qualifications."),
    ("paper_deepdive_methodology", "Critical-methodology deep dive",
     "Interrogate design, measurement, inference, reproducibility, and threats to validity fairly."),
    ("paper_study_guide", "Definitive study guide",
     "Merge the full teaching value into a navigable definitive study guide for this audience."),
)

_SEGMENT_MARKER = re.compile(
    r"<!--\s*SEGMENT_EVIDENCE\s*:\s*([^>]*?)\s*-->", re.IGNORECASE)
_PAPER_TOKEN = re.compile(r"\[P:[A-Za-z0-9_.:-]+\]")
_PAPER_LINK = re.compile(
    r"\[[^\]]*\]\(/api/papers/[^)]*\)", re.IGNORECASE)
MAX_MEMORY_ITEMS_PER_FIELD = 200
MAX_MEMORY_EVIDENCE_IDS = 2_000


def _digest(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _loads(value: str | None, fallback):
    try:
        parsed = json.loads(value or "")
    except (TypeError, json.JSONDecodeError):
        return fallback
    return parsed if isinstance(parsed, type(fallback)) else fallback


def _series_rows(project_id: int, series_id: int,
                 part_id: int | None = None) -> tuple[Project, PaperSource, PaperSeries, PaperSeriesPart | None]:
    with get_session() as session:
        project = get_project(session, project_id)
        source = session.exec(select(PaperSource).where(
            PaperSource.project_id == project_id
        )).first()
        series = session.get(PaperSeries, series_id)
        part = session.get(PaperSeriesPart, part_id) if part_id else None
        if project.source_type != "paper" or not source:
            raise ValueError("paper source metadata is missing")
        if not series or series.project_id != project_id:
            raise ValueError("paper audience track not found")
        if part_id and (not part or part.series_id != series_id):
            raise ValueError("paper series part not found")
        paper_store.require_analysis_ready(source)
        return project, source, series, part


def _series_parts(series_id: int, *, from_position: int = 1) -> list[PaperSeriesPart]:
    with get_session() as session:
        return list(session.exec(select(PaperSeriesPart).where(
            PaperSeriesPart.series_id == series_id,
            PaperSeriesPart.position >= from_position,
        ).order_by(PaperSeriesPart.position)).all())


def _begin_part_step(part_id: int, step: str) -> str:
    attr = f"{step}_status"
    with get_session() as session:
        part = session.get(PaperSeriesPart, part_id)
        if part is None:
            raise ValueError("paper series part not found")
        previous = str(getattr(part, attr))
        setattr(part, attr, "generating")
        part.status = "generating"
        part.updated = utcnow()
        session.add(part)
        session.commit()
    return previous


def _fail_part_step(part_id: int, step: str, previous: str) -> None:
    """Expose step failure without invalidating a safely preserved output."""
    attr = f"{step}_status"
    with get_session() as session:
        part = session.get(PaperSeriesPart, part_id)
        if part is None:
            return
        if step in {"guide", "audio"} and previous in {"done", "complete", "stale"}:
            setattr(part, attr, previous)
        else:
            setattr(part, attr, "error")
        part.status = "error"
        if step == "script":
            # A script artifact may have been published before memory
            # synthesis failed.  Prevent downstream use of a mismatched
            # script/memory pair and invalidate audio made from the old text.
            if part.audio_status != "pending":
                part.audio_status = "stale"
            part.stale = True
            following = session.exec(select(PaperSeriesPart).where(
                PaperSeriesPart.series_id == part.series_id,
                PaperSeriesPart.position > part.position,
            )).all()
            for candidate in following:
                if candidate.script_status != "pending":
                    candidate.script_status = "stale"
                if candidate.audio_status != "pending":
                    candidate.audio_status = "stale"
                candidate.stale = True
                candidate.updated = utcnow()
                session.add(candidate)
        part.updated = utcnow()
        session.add(part)
        session.commit()


def _assigned_chunks(part_id: int) -> tuple[list[PaperChunk], list[PaperChunk], list[dict]]:
    with get_session() as session:
        rows = session.exec(select(PaperPartEvidence).where(
            PaperPartEvidence.part_id == part_id
        )).all()
        chunks = {
            chunk.id: chunk for chunk in session.exec(select(PaperChunk).where(
                PaperChunk.id.in_([row.chunk_id for row in rows])
            )).all()
        } if rows else {}
        primary, bridge, ledger = [], [], []
        for row in rows:
            chunk = chunks.get(row.chunk_id)
            if not chunk:
                continue
            (primary if row.role == "primary" else bridge).append(chunk)
            ledger.append({
                "evidence_id": chunk.evidence_id,
                "role": row.role,
                "importance": row.importance,
                "reason": row.reason,
                "page": chunk.page_number,
                "section": _loads(chunk.section_path, []),
            })
    key = lambda chunk: (chunk.page_number, chunk.chunk_index)
    return sorted(primary, key=key), sorted(bridge, key=key), sorted(
        ledger, key=lambda row: (row["page"], row["evidence_id"]))


def _part_context(job_id: int, project_id: int, _series_id: int,
                   part: PaperSeriesPart,
                   _purpose: str) -> tuple[
                       list[dict], list[PaperChunk], list[dict], dict[str, str]]:
    primary, bridge, ledger = _assigned_chunks(part.id)
    chunks = primary + bridge
    if not primary:
        raise RuntimeError(f"Part {part.position} has no primary evidence assignment")
    wanted = {chunk.evidence_id for chunk in chunks}
    maps, _coverage = map_all_evidence(job_id, project_id)
    selected = [item for item in maps if item.get("evidence_id") in wanted]
    if {item.get("evidence_id") for item in selected} != wanted:
        raise RuntimeError("part evidence is missing a current leaf map")
    reduced, _meta = hierarchical_reduce(
        job_id, project_id, selected,
        # Reductions summarize evidence, not an audience's prose style.  A
        # stable purpose lets guide/script generation and independently
        # planned audience tracks reuse identical reduction work.
        purpose="paper_part_assigned_evidence",
    )
    upstream_analysis = {
        "analysis_config_signature": paper_analysis_config_signature(
            project_id)["signature"],
        "reduced_context_digest": _digest(reduced),
    }
    return reduced, chunks, ledger, upstream_analysis


def _latest_memory(series_id: int, *, part_id: int | None = None) -> PaperMemoryRevision | None:
    with get_session() as session:
        query = select(PaperMemoryRevision).where(
            PaperMemoryRevision.series_id == series_id
        )
        if part_id is not None:
            query = query.where(PaperMemoryRevision.part_id == part_id)
        return session.exec(query.order_by(PaperMemoryRevision.revision.desc())).first()


def _prior_memory(series_id: int, position: int) -> PaperMemoryRevision | None:
    if position <= 1:
        return None
    with get_session() as session:
        prior = session.exec(select(PaperSeriesPart).where(
            PaperSeriesPart.series_id == series_id,
            PaperSeriesPart.position == position - 1,
        )).first()
    return _latest_memory(series_id, part_id=prior.id) if prior else None


def _signature(source: PaperSource, series: PaperSeries, *, function: str,
               prompt: str, provider: str, model: str,
               evidence: Iterable[str], part: PaperSeriesPart | None = None,
               memory: PaperMemoryRevision | None = None,
               upstream_analysis: dict[str, str] | None = None,
               dependent_executions: dict[str, dict] | None = None,
               extra: dict | None = None) -> tuple[str, str, dict]:
    evidence_ids = sorted({str(value) for value in evidence if value})
    input_value = {
        "source_hash": source.source_hash,
        "parser_version": source.parser_version,
        "parser_config_hash": source.parser_config_hash,
        "acknowledged_pages": _loads(source.acknowledged_pages, []),
        "audience": series.audience,
        "plan_hash": series.plan_hash,
        "plan_version": series.plan_version,
        "evidence_ids": evidence_ids,
        "part_id": part.id if part else None,
        "part_position": part.position if part else None,
        "memory_revision_id": memory.id if memory else None,
        "memory_hash": memory.content_hash if memory else None,
        "user_guidance": {
            "series": series.user_guidance,
            "part": part.user_guidance if part else "",
        },
    }
    plan_lineage = _loads(series.plan_json, {}).get("analysis_lineage")
    if isinstance(plan_lineage, dict) and plan_lineage:
        input_value["plan_analysis_lineage"] = plan_lineage
    if upstream_analysis:
        input_value["upstream_analysis"] = upstream_analysis
    if extra:
        input_value["dependencies"] = extra
    config = {
        "function": function,
        "provider": provider,
        "model": model,
        "prompt_hash": _digest(prompt),
        "execution": paper_model_execution_signature(
            function,
            provider,
            model,
            local_only=bool(source.local_only),
        ),
        "paper_analysis": get_setting("paper.analysis") or {},
    }
    if dependent_executions:
        config["dependent_executions"] = dependent_executions
    input_hash, config_hash = _digest(input_value), _digest(config)
    provenance = {
        "schema": 2,
        "source_kind": "paper",
        "source": paper_source_signature_from_model(source),
        "input": input_value,
        "config": config,
        "output_scope": {
            "paper_series_id": series.id,
            "paper_part_id": part.id if part else None,
            "audience": series.audience,
        },
        **(extra or {}),
    }
    if upstream_analysis:
        provenance["upstream_analysis"] = upstream_analysis
    return input_hash, config_hash, provenance


def _write_markdown(project: Project, source: PaperSource, series: PaperSeries,
                    *, artifact_type: str, title: str, body: str,
                    function: str, prompt: str, provider: str, model: str,
                    evidence_ids: Iterable[str], part: PaperSeriesPart | None = None,
                    memory: PaperMemoryRevision | None = None,
                    upstream_analysis: dict[str, str] | None = None,
                    dependent_executions: dict[str, dict] | None = None,
                    extra_meta: dict | None = None) -> Artifact:
    input_hash, config_hash, provenance = _signature(
        source, series, function=function, prompt=prompt,
        provider=provider, model=model, evidence=evidence_ids,
        part=part, memory=memory, upstream_analysis=upstream_analysis,
        dependent_executions=dependent_executions,
    )
    base = f"projects/{project.slug}/series/{series.id}-{series.audience}"
    rel_path = (f"{base}/part-{part.position:02d}/{artifact_type}.md"
                if part else f"{base}/{artifact_type}.md")
    with get_session() as session:
        return library.write_artifact(
            session,
            project_id=project.id,
            project_slug=project.slug,
            type=artifact_type,
            title=f"{title} — {project.title}",
            body=body,
            rel_path=rel_path,
            provider=provider,
            model=model,
            paper_series_id=series.id,
            paper_part_id=part.id if part else None,
            input_hash_override=input_hash,
            config_hash_override=config_hash,
            provenance_override=provenance,
            extra_meta={
                "source_kind": "paper",
                "source_hash": source.source_hash,
                "audience": series.audience,
                "plan_hash": series.plan_hash,
                "plan_version": series.plan_version,
                "part_position": part.position if part else None,
                "memory_revision_id": memory.id if memory else None,
                "evidence_count": len(set(evidence_ids)),
                **(extra_meta or {}),
            },
        )


def _generate_suite_item(job_id: int, project_id: int, series_id: int,
                         artifact_type: str, title: str, directive: str) -> int:
    project, source, series, _ = _series_rows(project_id, series_id)
    bundle = latest_analysis_bundle(project_id)
    if not bundle:
        raise RuntimeError("run paper shared analysis before audience production")
    upstream_analysis = paper_analysis_lineage(bundle)
    context = {
        "audience": series.audience,
        "target_minutes": series.target_minutes,
        "plan": _loads(series.plan_json, {}),
        "user_guidance": series.user_guidance,
        "coverage": bundle["coverage"],
        "hierarchical_context": bundle["hierarchical_context"],
        "purpose": directive,
    }
    local_only = bool(source.local_only)
    with llm.project_scope(project_id, local_only=local_only):
        provider, model = llm.resolve_model("paper_synthesis")
        prompt = get_prompt("paper_suite") + "\n\nRequested purpose: " + directive
        progress(job_id, f"writing {series.audience} {title.lower()}")
        body = llm.complete(
            "paper_synthesis", prompt,
            "COMPLETE REDUCED PAPER MAP (untrusted data):\n"
            + json.dumps(_compact_map_for_prompt(context), ensure_ascii=False),
            max_tokens=_paper_analysis_settings()["synthesis_output_tokens"],
            provider=provider, model=model, local_only=local_only,
        ).strip()
    with get_session() as session:
        chunks = session.exec(select(PaperChunk).where(
            PaperChunk.source_id == source.id
        )).all()
    body, citations = paper_store.validate_and_render_citations(
        body, project_id=project_id, source=source, evidence=chunks, require=True)
    artifact = _write_markdown(
        project, source, series,
        artifact_type=artifact_type, title=f"{title} ({series.audience.title()})",
        body=body, function="paper_synthesis", prompt=prompt,
        provider=provider, model=model,
        evidence_ids=bundle["leaf_evidence_ids"],
        upstream_analysis=upstream_analysis,
        extra_meta={"citation_count": citations},
    )
    if artifact_type == "paper_study_guide":
        auto_tag(project_id, artifact.id)
    return artifact.id


def _generate_part_guide(job_id: int, project_id: int, series_id: int,
                         part_id: int) -> int:
    project, source, series, part = _series_rows(project_id, series_id, part_id)
    reduced, chunks, ledger, upstream_analysis = _part_context(
        job_id, project_id, series_id, part, "guide")
    prompt = get_prompt("paper_part_guide")
    context = {
        "series": {"audience": series.audience, "title": series.title,
                   "plan": _loads(series.plan_json, {})},
        "part": {"position": part.position, "title": part.title,
                 "focus": part.focus, "user_guidance": part.user_guidance},
        "series_user_guidance": series.user_guidance,
        "evidence_assignment": ledger,
        "reduced_assigned_evidence": reduced,
        "acknowledged_gaps": _loads(source.acknowledged_pages, []),
    }
    local_only = bool(source.local_only)
    with llm.project_scope(project_id, local_only=local_only):
        provider, model = llm.resolve_model("paper_synthesis")
        progress(job_id, f"writing Part {part.position} study guide")
        body = llm.complete(
            "paper_synthesis", prompt,
            json.dumps(_compact_map_for_prompt(context), ensure_ascii=False),
            max_tokens=_paper_analysis_settings()["synthesis_output_tokens"],
            provider=provider, model=model, local_only=local_only,
        ).strip()
    body, citations = paper_store.validate_and_render_citations(
        body, project_id=project_id, source=source, evidence=chunks, require=True)
    artifact = _write_markdown(
        project, source, series, part=part,
        artifact_type="paper_part_guide",
        title=f"Part {part.position}: {part.title} — Study guide & show notes",
        body=body, function="paper_synthesis", prompt=prompt,
        provider=provider, model=model,
        evidence_ids=[row["evidence_id"] for row in ledger],
        upstream_analysis=upstream_analysis,
        extra_meta={"citation_count": citations, "evidence_assignment": ledger},
    )
    with get_session() as session:
        stored = session.get(PaperSeriesPart, part_id)
        stored.guide_status = "done"
        stored.updated = utcnow()
        session.add(stored)
        session.commit()
    return artifact.id


def generate_part_guide(job_id: int, project_id: int, series_id: int,
                        part_id: int) -> int:
    previous = _begin_part_step(part_id, "guide")
    try:
        return _generate_part_guide(job_id, project_id, series_id, part_id)
    except Exception:
        _fail_part_step(part_id, "guide", previous)
        raise


def _normalize_script(body: str, chunks: list[PaperChunk]) -> tuple[str, set[str], int]:
    known = {chunk.evidence_id: chunk for chunk in chunks}
    default_ids = list(known)
    lines = body.splitlines()
    output: list[str] = []
    current_segment = False
    segment_has_marker = False
    ledgers: list[list[str]] = []

    def add_marker(ids: list[str]) -> None:
        valid = [value for value in ids if value in known]
        if not valid:
            valid = default_ids
        if not valid:
            raise RuntimeError("script segment has no valid assigned evidence")
        unique = list(dict.fromkeys(valid))
        ledgers.append(unique)
        output.append(f"<!--SEGMENT_EVIDENCE:{','.join(unique)}-->")
        links = []
        for evidence_id in unique:
            chunk = known[evidence_id]
            section = _loads(chunk.section_path, [])
            label = f"p. {chunk.page_number}"
            if section:
                label += " — " + " > ".join(section)
            links.append(
                f"[{label}](/api/papers/SOURCE#page={chunk.page_number})"
                f"<!--P:{evidence_id}-->")
        output.append("> Evidence: " + "; ".join(links))

    for raw_line in lines:
        line = raw_line.rstrip()
        if line.startswith("## Segment"):
            if current_segment and not segment_has_marker:
                add_marker(default_ids)
            output.append(line)
            current_segment = True
            segment_has_marker = False
            continue
        match = _SEGMENT_MARKER.search(line)
        if match:
            if not current_segment:
                raise RuntimeError("script evidence ledger appeared before a segment heading")
            if segment_has_marker:
                raise RuntimeError("script segment contained more than one evidence ledger")
            requested = [value.strip() for value in match.group(1).split(",") if value.strip()]
            unknown = sorted(set(requested) - set(known))
            if unknown:
                raise RuntimeError("script cited unassigned evidence: " + ", ".join(unknown[:10]))
            add_marker(requested)
            segment_has_marker = True
            continue
        if re.match(r"\s*HOST_[AB]\s*:", line):
            # Page citations remain in the adjacent non-spoken evidence ledger.
            line = _PAPER_LINK.sub("", line)
            line = _PAPER_TOKEN.sub("", line)
            line = re.sub(r"/api/papers/\S+", "", line)
        output.append(line)
    if not current_segment:
        output.insert(0, "## Segment: Main discussion")
        add_at = 1
        marker_lines: list[str] = []
        original_output = output
        output = original_output[:add_at]
        # add_marker appends to the current output binding.
        add_marker(default_ids)
        output.extend(original_output[add_at:])
    elif not segment_has_marker:
        add_marker(default_ids)
    cited = {value for ledger in ledgers for value in ledger}
    return "\n".join(output).strip(), cited, len(ledgers)


def _render_script_links(body: str, project_id: int) -> str:
    return body.replace("/api/papers/SOURCE", f"/api/papers/{project_id}/source")


def _memory_state(raw: Any, evidence_ids: set[str],
                  prior: dict[str, Any] | None = None) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    prior = prior if isinstance(prior, dict) else {}
    list_fields = (
        "terminology", "introduced_topics", "completed_topics", "deferred_topics",
        "covered_claims", "covered_examples", "used_stories_analogies",
        "open_questions", "promised_callbacks", "resolved_callbacks",
        "handoff_notes",
    )
    cumulative = {
        "terminology", "introduced_topics", "completed_topics",
        "covered_claims", "covered_examples", "used_stories_analogies",
        "resolved_callbacks",
    }

    def values(source: dict[str, Any], field: str) -> list[Any]:
        value = source.get(field, [])
        return value if isinstance(value, list) else ([value] if value else [])

    def dedupe(items: list[Any]) -> list[Any]:
        output, seen = [], set()
        for item in items:
            key = json.dumps(item, sort_keys=True, ensure_ascii=False, default=str)
            if key in seen:
                continue
            seen.add(key)
            output.append(item)
        return output

    state: dict[str, Any] = {}
    for field in list_fields:
        current = values(raw, field)
        previous = values(prior, field)
        if field in cumulative:
            state[field] = dedupe(
                previous + current)[:MAX_MEMORY_ITEMS_PER_FIELD]
        else:
            # These are open/current queues.  A model may intentionally clear
            # one after a callback is resolved; if it omits the field entirely,
            # preserve the prior queue instead of losing continuity.
            state[field] = dedupe(
                current if field in raw else previous
            )[:MAX_MEMORY_ITEMS_PER_FIELD]
    prior_evidence = {
        str(value) for value in values(prior, "evidence_ids") if value
    }
    complete_evidence = sorted(prior_evidence | evidence_ids)
    state["evidence_ids"] = complete_evidence[:MAX_MEMORY_EVIDENCE_IDS]
    state["evidence_id_count"] = len(complete_evidence)
    state["evidence_ids_digest"] = _digest(complete_evidence)
    state["evidence_ids_truncated"] = (
        len(complete_evidence) > MAX_MEMORY_EVIDENCE_IDS)
    return state


def _generate_part_script(job_id: int, project_id: int, series_id: int,
                          part_id: int, *, previous_status: str) -> tuple[int, int]:
    project, source, series, part = _series_rows(project_id, series_id, part_id)
    prior = _prior_memory(series_id, part.position)
    if part.position > 1 and prior is None:
        raise RuntimeError("the previous part must have a finalized memory revision")
    reduced, chunks, ledger, upstream_analysis = _part_context(
        job_id, project_id, series_id, part, "script")
    prior_state = _loads(prior.state_json, {}) if prior else {}
    prompt = get_prompt("paper_part_script")
    context = {
        "series": {"title": series.title, "audience": series.audience,
                   "target_minutes": series.target_minutes,
                   "plan": _loads(series.plan_json, {})},
        "part": {"position": part.position, "title": part.title,
                 "focus": part.focus},
        "series_memory": prior_state,
        "user_guidance": {"series": series.user_guidance, "part": part.user_guidance},
        "evidence_assignment": ledger,
        "reduced_assigned_evidence": reduced,
    }
    local_only = bool(source.local_only)
    with llm.project_scope(project_id, local_only=local_only):
        provider, model = llm.resolve_model("paper_script")
        progress(job_id, f"writing Part {part.position} script")
        body = llm.complete(
            "paper_script", prompt,
            json.dumps(_compact_map_for_prompt(context), ensure_ascii=False),
            max_tokens=8_000, provider=provider, model=model,
            local_only=local_only,
        ).strip()
    body, cited, segment_count = _normalize_script(body, chunks)
    body = _render_script_links(body, project_id)
    prior_script_done = previous_status in {"done", "complete"}
    memory_prompt = get_prompt("paper_memory")
    with llm.project_scope(project_id, local_only=local_only):
        memory_provider, memory_model = llm.resolve_model("paper_memory")
    memory_execution = paper_model_execution_signature(
        "paper_memory",
        memory_provider,
        memory_model,
        local_only=local_only,
        json_format=True,
    )
    artifact = _write_markdown(
        project, source, series, part=part,
        artifact_type="paper_part_script",
        title=f"Part {part.position}: {part.title} — Two-host script",
        body=body, function="paper_script", prompt=prompt,
        provider=provider, model=model, evidence_ids=cited, memory=prior,
        upstream_analysis=upstream_analysis,
        dependent_executions={"paper_memory": memory_execution},
        extra_meta={"segments": segment_count, "evidence_assignment": ledger},
    )
    with llm.project_scope(project_id, local_only=local_only):
        raw_memory = llm.complete_json(
            "paper_memory", memory_prompt,
            "PRIOR MEMORY:\n" + json.dumps(prior_state, ensure_ascii=False)
            + "\n\nFINALIZED SCRIPT:\n" + body,
            max_tokens=3_000, provider=memory_provider, model=memory_model,
            local_only=local_only,
        )
    state = _memory_state(raw_memory, cited, prior_state)
    with get_session() as session:
        revision_number = int(session.exec(select(PaperMemoryRevision.revision).where(
            PaperMemoryRevision.series_id == series_id
        ).order_by(PaperMemoryRevision.revision.desc())).first() or 0) + 1
        content_hash = _digest({
            "source_hash": source.source_hash,
            "plan_hash": series.plan_hash,
            "part_id": part_id,
            "parent": prior.content_hash if prior else None,
            "script_hash": hashlib.sha256(body.encode("utf-8")).hexdigest(),
            "state": state,
            "memory_prompt_hash": _digest(memory_prompt),
            "memory_execution": memory_execution,
        })
        # Finalizing a regenerated script is itself a new immutable continuity
        # event, even when a deterministic model happened to reproduce the
        # same script text.  Do not coalesce revisions by content hash.
        memory = PaperMemoryRevision(
            series_id=series_id, part_id=part_id,
            parent_revision_id=prior.id if prior else None,
            revision=revision_number, state_json=json.dumps(state, sort_keys=True),
            content_hash=content_hash,
        )
        session.add(memory)
        stored = session.get(PaperSeriesPart, part_id)
        stored.script_status = "done"
        if prior_script_done and stored.audio_status != "pending":
            # The current part's audio was synthesized from the superseded
            # script.  Audio does not affect memory, but it must be rebuilt.
            stored.audio_status = "stale"
            stored.stale = True
        else:
            stored.stale = False
        stored.updated = utcnow()
        session.add(stored)
        if prior_script_done:
            following = session.exec(select(PaperSeriesPart).where(
                PaperSeriesPart.series_id == series_id,
                PaperSeriesPart.position > part.position,
            )).all()
            for candidate in following:
                if candidate.script_status != "pending":
                    candidate.script_status = "stale"
                if candidate.audio_status != "pending":
                    candidate.audio_status = "stale"
                candidate.stale = True
                candidate.updated = utcnow()
                session.add(candidate)
        session.commit()
        session.refresh(memory)
    return artifact.id, memory.id


def generate_part_script(job_id: int, project_id: int, series_id: int,
                         part_id: int) -> tuple[int, int]:
    previous = _begin_part_step(part_id, "script")
    try:
        return _generate_part_script(
            job_id, project_id, series_id, part_id,
            previous_status=previous,
        )
    except Exception:
        _fail_part_step(part_id, "script", previous)
        raise


def _generate_part_audio(job_id: int, project_id: int, series_id: int,
                         part_id: int) -> int:
    project, source, series, part = _series_rows(project_id, series_id, part_id)
    with get_session() as session:
        script = session.exec(select(Artifact).where(
            Artifact.project_id == project_id,
            Artifact.paper_series_id == series_id,
            Artifact.paper_part_id == part_id,
            Artifact.type == "paper_part_script",
        )).first()
        if not script:
            raise RuntimeError("generate the part script before audio")
        _meta, body = library.read_doc(script.path)
        script_provenance = _loads(script.provenance, {})
        script_evidence_ids = (
            script_provenance.get("input", {}).get("evidence_ids", [])
            if isinstance(script_provenance.get("input"), dict) else []
        )
    from .audio import _tts_gemini, _tts_kokoro, _tts_piper, parse_script

    lines = parse_script(body)
    if not lines:
        raise RuntimeError("paper part script has no HOST_A:/HOST_B: dialogue")
    local_only = bool(source.local_only)
    with llm.project_scope(project_id, local_only=local_only):
        provider, model = llm.resolve_model("tts")
    work = media.workdir(project.slug) / f"paper-series-{series_id}-part-{part_id}"
    work.mkdir(parents=True, exist_ok=True)
    on_progress = lambda message: progress(job_id, message)  # noqa: E731
    produced: Path | None = None
    try:
        if provider == "gemini":
            produced = _tts_gemini(lines, work, model, on_progress)
        elif provider == "piper":
            produced = _tts_piper(lines, work, on_progress)
        else:
            produced = _tts_kokoro(lines, work, on_progress)
        base = f"projects/{project.slug}/series/{series.id}-{series.audience}/part-{part.position:02d}"
        media_rel = f"{base}/paper_part_audio.mp3"
        destination = library.lib_path(media_rel)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + f".{os.getpid()}.tmp")
        previous = destination.read_bytes() if destination.exists() else None
        try:
            shutil.copyfile(produced, temporary)
            os.replace(temporary, destination)
            with get_session() as session:
                input_hash, config_hash, provenance = _signature(
                    source, series, function="tts", prompt="paper part TTS",
                    provider=provider, model=model,
                    evidence=script_evidence_ids,
                    part=part, memory=_latest_memory(series_id, part_id=part_id),
                    extra={"script_artifact_id": script.id,
                           "script_input_hash": script.input_hash,
                           "script_body_hash": hashlib.sha256(
                               body.encode("utf-8")).hexdigest()},
                )
                artifact = library.write_artifact(
                    session, project_id=project_id, project_slug=project.slug,
                    type="paper_part_audio",
                    title=f"Part {part.position}: {part.title} — Podcast audio",
                    body=("Audio generated from the scoped two-host script. "
                          "Continuity memory depends on the script, not this audio rerun."),
                    rel_path=f"{base}/paper_part_audio.md", media_rel=media_rel,
                    provider=provider, model=model,
                    paper_series_id=series_id, paper_part_id=part_id,
                    input_hash_override=input_hash,
                    config_hash_override=config_hash,
                    provenance_override=provenance,
                    extra_meta={"duration_seconds": media.duration_seconds(destination),
                                "audience": series.audience,
                                "part_position": part.position},
                )
        except Exception:
            if previous is None:
                destination.unlink(missing_ok=True)
            else:
                library._atomic_write_bytes(destination, previous)
            raise
        finally:
            temporary.unlink(missing_ok=True)
        with get_session() as session:
            stored = session.get(PaperSeriesPart, part_id)
            stored.audio_status = "done"
            stored.updated = utcnow()
            session.add(stored)
            session.commit()
        return artifact.id
    finally:
        if not advanced("audio").get("keep_intermediates", False):
            shutil.rmtree(work, ignore_errors=True)


def generate_part_audio(job_id: int, project_id: int, series_id: int,
                        part_id: int) -> int:
    previous = _begin_part_step(part_id, "audio")
    try:
        return _generate_part_audio(job_id, project_id, series_id, part_id)
    except Exception:
        _fail_part_step(part_id, "audio", previous)
        raise


def _refresh_completion(series_id: int) -> None:
    with get_session() as session:
        series = session.get(PaperSeries, series_id)
        parts = session.exec(select(PaperSeriesPart).where(
            PaperSeriesPart.series_id == series_id
        )).all()
        for part in parts:
            complete = all(status in {"done", "complete"} for status in (
                part.guide_status, part.script_status, part.audio_status))
            part.status = "complete" if complete else "generating"
            if complete:
                part.stale = False
            part.updated = utcnow()
            session.add(part)
        if series:
            series.status = "complete" if parts and all(
                part.status == "complete" for part in parts) else "running"
            series.updated = utcnow()
            session.add(series)
        session.commit()


def _threaded_guide(job_id: int, project_id: int, series_id: int,
                    part_id: int) -> int:
    token = current_job_id.set(job_id)
    try:
        return generate_part_guide(job_id, project_id, series_id, part_id)
    finally:
        current_job_id.reset(token)


@celery.task(name="paper_series_run")
@pipeline_task
def paper_series_run(job_id: int, project_id: int, series_id: int):
    _project, _source, series, _part = _series_rows(project_id, series_id)
    if series.status not in {"approved", "running", "complete"}:
        raise RuntimeError("approve the paper audience plan before production")
    try:
        with get_session() as session:
            stored = session.get(PaperSeries, series_id)
            stored.status = "running"
            stored.updated = utcnow()
            session.add(stored)
            session.commit()
        for artifact_type, title, directive in SUITE:
            _generate_suite_item(
                job_id, project_id, series_id, artifact_type, title, directive)
        parts = _series_parts(series_id)
        with ThreadPoolExecutor(max_workers=min(3, max(1, len(parts)))) as executor:
            list(executor.map(
                lambda part: _threaded_guide(
                    job_id, project_id, series_id, part.id),
                parts,
            ))
        # The continuity boundary is the script: strict part order is required.
        for part in parts:
            generate_part_script(job_id, project_id, series_id, part.id)
        # Audio can be rerun independently and never mutates memory.
        for part in parts:
            generate_part_audio(job_id, project_id, series_id, part.id)
        _refresh_completion(series_id)
        return {"series_id": series_id, "parts": len(parts), "status": "complete"}
    except Exception:
        with get_session() as session:
            stored = session.get(PaperSeries, series_id)
            if stored:
                stored.status = "error"
                stored.updated = utcnow()
                session.add(stored)
                session.commit()
        raise


@celery.task(name="paper_part_step")
@pipeline_task
def paper_part_step(job_id: int, project_id: int, series_id: int,
                    part_id: int, step: str):
    if step == "guide":
        result = generate_part_guide(job_id, project_id, series_id, part_id)
    elif step == "script":
        result = generate_part_script(job_id, project_id, series_id, part_id)
    elif step == "audio":
        result = generate_part_audio(job_id, project_id, series_id, part_id)
    else:
        raise ValueError("paper part step must be guide, script, or audio")
    _refresh_completion(series_id)
    return result


@celery.task(name="paper_rebuild_following")
@pipeline_task
def paper_rebuild_following(job_id: int, project_id: int, series_id: int,
                            part_id: int):
    _project, _source, _series, start = _series_rows(
        project_id, series_id, part_id)
    parts = _series_parts(series_id, from_position=start.position)
    for part in parts:
        generate_part_script(job_id, project_id, series_id, part.id)
        generate_part_audio(job_id, project_id, series_id, part.id)
    _refresh_completion(series_id)
    return {"rebuilt_from": start.position, "parts": [part.id for part in parts]}
