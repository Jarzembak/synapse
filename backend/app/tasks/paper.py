"""Dense, lossless-by-admission analysis tasks for research papers.

Every admitted :class:`~app.models.PaperChunk` is mapped exactly once per
analysis configuration.  Leaf maps and recursive reductions are content
addressed; audience plans reuse them instead of rereading or sampling the PDF.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
from collections import Counter
from typing import Any, Iterable

from sqlalchemy import text
from sqlmodel import select

from .. import library, llm, paper as paper_store
from ..config import advanced, settings
from ..db import get_session
from ..models import (
    Artifact, PaperChunk, PaperPartEvidence, PaperSeries, PaperSeriesPart,
    PaperSource, PaperSynthesisCache, Project, utcnow,
)
from ..settings_store import get_setting
from .common import auto_tag, get_project, pipeline_task, progress
from .prompts import (
    PAPER_MAP as PAPER_MAP_PROMPT,
    PAPER_PLAN as PAPER_PLAN_PROMPT,
    PAPER_REDUCE as PAPER_REDUCE_PROMPT,
    get_prompt,
)

log = logging.getLogger("synapse.paper.pipeline")


PAPER_ARGUMENT_PROMPT = """Write a structural claim and argument map for this paper.
Every paper-supported statement must end with one or more [P:evidence_id] tokens.
Distinguish hypotheses, premises, methods, observations/results, uncertainty,
counterevidence, limitations, and conclusions. Add clearly labeled sections for
Model-added background, Critique/assumptions, and Open questions; never present
those as claims made by the paper. Do not use external literature."""

PAPER_MINDMAP_PROMPT = """Create a whole-paper mind map as deeply nested Markdown.
Organize concepts, definitions, methods, datasets/materials, results,
uncertainties, limitations, and their relationships. Every paper-supported leaf
must include [P:evidence_id]. Label model-added organizational interpretation as
Interpretive structure. Do not interpret charts or diagrams and do not use
external literature."""

PAPER_QUICKREF_PROMPT = """Create compact paper-grounded quick references from the
complete evidence map: terminology, formulas/symbols, methods, datasets/materials,
key results with uncertainty, limitations, and reproducibility checks. Every
paper-supported bullet must include [P:evidence_id]. Separate Paper-supported
reference, Model-added background, Critique, and Open questions. No external
literature lookup."""

_STRUCTURED_FIELDS = (
    "definitions", "claims", "hypotheses", "methods", "datasets_materials",
    "results", "assumptions", "limitations", "prerequisites",
    "bibliography_relationships", "referenced_visuals", "topics",
    "open_questions",
)
_IMPORTANCE = {"supporting": 0, "major": 1, "critical": 2}
_IMPORTANCE_ALIASES = {
    "low": "supporting",
    "minor": "supporting",
    "optional": "supporting",
    "context": "supporting",
    "contextual": "supporting",
    "medium": "major",
    "important": "major",
    "substantive": "major",
    "high": "critical",
    "essential": "critical",
    "primary": "critical",
    "central": "critical",
}
PAPER_MAP_CONTRACT_VERSION = 5
PAPER_REDUCE_CONTRACT_VERSION = 5
PAPER_PLAN_CONTRACT_VERSION = 3
PAPER_PLAN_SCHEMA_VERSION = 2
_SOURCE_ITEM_IDS_KEY = "_source_item_ids"
_MAP_FIELD_SOURCES: dict[str, tuple[str, ...]] = {
    "definitions": (
        "definitions",
        "definitions_and_terminology",
    ),
    "claims": (
        "claims",
        "claims_and_hypotheses",
    ),
    "hypotheses": ("hypotheses",),
    "methods": (
        "methods",
        "methods_and_procedures",
    ),
    "datasets_materials": (
        "datasets_materials",
        "datasets_materials_populations_conditions",
        "datasets_materials_populations_and_experimental_conditions",
    ),
    "results": (
        "results",
        "uncertainty",
        "results_effect_direction_uncertainty_negative_findings",
        "results_effect_direction_magnitude_uncertainty_and_negative_findings",
    ),
    "assumptions": (
        "assumptions",
        "assumptions_and_prerequisites",
    ),
    "limitations": (
        "limitations",
        "limitations_and_threats_to_validity",
    ),
    "prerequisites": ("prerequisites",),
    "bibliography_relationships": (
        "bibliography_relationships",
        "bibliography_citation_relationships",
        "bibliography_citation_relationships_stated_in_this_excerpt",
    ),
    "referenced_visuals": (
        "referenced_visuals",
        "referenced_visuals_tables_figures",
        "referenced_tables_formulas_figures",
        "referenced_tables_formulas_and_figures",
    ),
    "topics": (
        "topics",
        "topics_and_open_questions",
    ),
    "open_questions": ("open_questions",),
}
_ENTRY_TEXT_KEYS = (
    "text", "claim", "summary", "statement", "description", "name", "term",
    "result", "method", "question", "reference", "relationship_note",
)
_ENTRY_CONTROL_KEYS = {
    "evidence_id", "evidence_ids", "importance",
    "source_item_ids", _SOURCE_ITEM_IDS_KEY,
}
_PAPER_CONTRACT_PROMPTS = {
    "paper_map": PAPER_MAP_PROMPT,
    "paper_reduce": PAPER_REDUCE_PROMPT,
    "paper_plan": PAPER_PLAN_PROMPT,
}


def _paper_prompt(name: str, fallback: str) -> str:
    """Resolve a configurable prompt without allowing its wire contract to drift."""
    try:
        resolved = get_prompt(name)
    except KeyError:
        resolved = fallback
    mandatory = _PAPER_CONTRACT_PROMPTS.get(name)
    if mandatory and resolved.strip() != mandatory.strip():
        return (
            resolved.rstrip()
            + "\n\nMANDATORY SYNAPSE OUTPUT CONTRACT (cannot be overridden):\n"
            + mandatory
        )
    return resolved


def _digest(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _json(value: Any, default: Any) -> Any:
    if isinstance(value, type(default)):
        return paper_store.normalize_paper_json(value)
    try:
        parsed = json.loads(value or "")
    except (TypeError, json.JSONDecodeError):
        return default
    return (
        paper_store.normalize_paper_json(parsed)
        if isinstance(parsed, type(default))
        else default
    )


def _paper_analysis_settings() -> dict[str, int]:
    configured = get_setting("paper.analysis") or {}

    def bounded(name: str, default: int, low: int, high: int) -> int:
        try:
            return max(low, min(int(configured.get(name, default)), high))
        except (TypeError, ValueError):
            return default

    return {
        "map_output_tokens": bounded("map_output_tokens", 2_400, 800, 6_000),
        "reduce_batch_tokens": bounded(
            "reduce_batch_tokens", 12_000, 4_000, 48_000),
        "reduce_output_tokens": bounded(
            "reduce_output_tokens", 4_000, 1_500, 8_000),
        "final_context_tokens": bounded(
            "final_context_tokens", 14_000, 6_000, 48_000),
        "synthesis_output_tokens": bounded(
            "synthesis_output_tokens", 5_000, 1_500, 10_000),
    }


def paper_model_execution_signature(
    function: str,
    provider: str,
    model: str,
    *,
    local_only: bool,
    json_format: bool = False,
) -> dict[str, Any]:
    """Describe the output-affecting settings used for one paper model call.

    Paper caches cannot key only on provider/model: Ollama's context window and
    context policy and thinking mode alter the generated evidence map, and
    native JSON enforcement changes structured map/reduction/planning calls.
    Record the configured context minimum plus the adaptive-policy version so
    caches become stale when sizing behavior changes.
    """
    value: dict[str, Any] = {
        "provider": provider,
        "model": model,
        "params": get_setting(f"params.{function}") or {},
    }
    if function == "tts":
        value["audio"] = advanced("audio")
        value["voices"] = {
            "kokoro": get_setting("tts.voices") or {},
            "piper": get_setting("tts.piper_voices") or {},
            "gemini": get_setting("tts.gemini_voices") or {},
        }
    if provider not in {"ollama", "openai_compat"}:
        return value

    local = advanced("local")
    provider_settings: dict[str, Any] = {}
    if provider == "ollama":
        num_ctx = int(local.get("num_ctx") or 16_384)
        provider_settings["configured_context_minimum"] = num_ctx
        provider_settings["context_policy"] = llm.LOCAL_CONTEXT_POLICY_VERSION
        provider_settings["automatic_context_cap"] = (
            llm.LOCAL_AUTOMATIC_CONTEXT_CAP)
        provider_settings["think"] = (
            False if local_only else local.get("think", "auto")
        )
    if json_format:
        provider_settings["json_mode"] = bool(local.get("json_mode", True))
    if provider_settings:
        value["provider_settings"] = provider_settings
    return value


def paper_analysis_lineage(bundle: dict[str, Any]) -> dict[str, str]:
    """Stable upstream identity shared by root and audience-specific outputs."""
    context_digest = str(bundle.get("hierarchical_context_digest") or "")
    if not context_digest:
        context_digest = _digest(bundle.get("hierarchical_context", []))
    return {
        "analysis_config_signature": str(
            bundle.get("analysis_config_signature") or ""),
        "reduced_context_digest": context_digest,
    }


def _ocr_languages(source: PaperSource) -> tuple[str, ...]:
    values = _json(source.ocr_languages, [])
    if not values:
        values = [part.strip() for part in str(settings.paper_ocr_languages).split(",")]
    return tuple(str(value).strip().lower() for value in values if str(value).strip())


def extraction_config(source: PaperSource) -> paper_store.PaperExtractionConfig:
    """Effective parser settings with environment defaults bounded by v1 caps."""
    return paper_store.PaperExtractionConfig(
        ocr_languages=_ocr_languages(source),
        max_file_bytes=min(
            int(settings.max_paper_upload_bytes), paper_store.MAX_PAPER_FILE_BYTES),
        max_pages=min(int(settings.max_paper_pages), paper_store.MAX_PAPER_PAGES),
        max_extracted_characters=min(
            int(settings.max_paper_extracted_chars),
            paper_store.MAX_PAPER_EXTRACTED_CHARACTERS,
        ),
        artifacts_path=os.environ.get(
            "DOCLING_ARTIFACTS_PATH", "/opt/docling/models"),
    )


def paper_source_signature(session, project_id: int) -> dict[str, Any]:
    source = paper_store.paper_source_for_project(session, project_id)
    if source is None:
        raise RuntimeError("paper source metadata is missing")
    value = {
        "source_hash": source.source_hash,
        "source_bytes": source.size_bytes,
        "parser_version": source.parser_version or paper_store.PARSER_VERSION,
        "parser_config_hash": source.parser_config_hash,
        "ocr_languages": list(_ocr_languages(source)),
        "local_only": bool(source.local_only),
        "quality_grade": source.quality_grade,
        "acknowledged_pages": _json(source.acknowledged_pages, []),
    }
    return {**value, "signature": _digest(value)}


def paper_analysis_config_signature(project_id: int) -> dict[str, Any]:
    with get_session() as session:
        source = paper_store.paper_source_for_project(session, project_id)
        if source is None:
            raise RuntimeError("paper source metadata is missing")
        local_only = bool(source.local_only)
        source_parser = {
            "version": source.parser_version,
            "config_hash": source.parser_config_hash,
        }
    prompts = {
        "map": _paper_prompt("paper_map", PAPER_MAP_PROMPT),
        "reduce": _paper_prompt("paper_reduce", PAPER_REDUCE_PROMPT),
        "argument": PAPER_ARGUMENT_PROMPT,
        "mindmap": PAPER_MINDMAP_PROMPT,
        "quickref": PAPER_QUICKREF_PROMPT,
        "shared": _paper_prompt("paper_shared", ""),
        "plan": _paper_prompt("paper_plan", PAPER_PLAN_PROMPT),
    }
    models: dict[str, dict[str, str]] = {}
    executions: dict[str, dict[str, Any]] = {}
    structured = {"paper_map", "paper_reduce", "paper_plan"}
    with llm.project_scope(project_id, local_only=local_only):
        for function in ("paper_map", "paper_reduce", "paper_synthesis", "paper_plan"):
            provider, model = llm.resolve_model(function)
            models[function] = {"provider": provider, "model": model}
            executions[function] = paper_model_execution_signature(
                function,
                provider,
                model,
                local_only=local_only,
                json_format=function in structured,
            )
    value = {
        "schema": 3,
        "contracts": {
            "map": PAPER_MAP_CONTRACT_VERSION,
            "reduce": PAPER_REDUCE_CONTRACT_VERSION,
            "plan": PAPER_PLAN_CONTRACT_VERSION,
        },
        "source_parser": source_parser,
        "prompts": {name: _digest(body) for name, body in prompts.items()},
        "models": models,
        "executions": executions,
        "analysis": _paper_analysis_settings(),
    }
    return {**value, "signature": _digest(value)}


def _cache_get(session, *, source_id: int, purpose: str,
               input_hash: str, config_hash: str) -> tuple[Any, PaperSynthesisCache] | None:
    row = session.exec(select(PaperSynthesisCache).where(
        PaperSynthesisCache.source_id == source_id,
        PaperSynthesisCache.purpose == purpose,
        PaperSynthesisCache.input_hash == input_hash,
        PaperSynthesisCache.config_hash == config_hash,
    ).order_by(PaperSynthesisCache.id.desc())).first()
    if row is None:
        return None
    try:
        return json.loads(row.body), row
    except json.JSONDecodeError:
        return None


def _cache_put(session, *, project_id: int, source_id: int, purpose: str,
               input_hash: str, config_hash: str, provider: str, model: str,
               body: Any, evidence_ids: Iterable[str]) -> PaperSynthesisCache:
    existing = _cache_get(
        session, source_id=source_id, purpose=purpose,
        input_hash=input_hash, config_hash=config_hash)
    if existing:
        return existing[1]
    row = PaperSynthesisCache(
        project_id=project_id,
        source_id=source_id,
        purpose=purpose,
        input_hash=input_hash,
        config_hash=config_hash,
        provider=provider,
        model=model,
        body=json.dumps(body, sort_keys=True, ensure_ascii=False),
        evidence_ids=json.dumps(sorted({str(value) for value in evidence_ids if value})),
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def _item_evidence_ids(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        one = value.get("evidence_id")
        if one:
            found.add(str(one))
        many = value.get("evidence_ids")
        if isinstance(many, (list, tuple, set)):
            found.update(str(item) for item in many if item)
        elif many:
            found.add(str(many))
    return found


def _source_item_ids(value: Any) -> set[str]:
    if not isinstance(value, dict):
        return set()
    raw = value.get(_SOURCE_ITEM_IDS_KEY)
    if raw in (None, "", []):
        raw = value.get("source_item_ids")
    values = raw if isinstance(raw, (list, tuple, set)) else [raw]
    return {
        str(item).strip()
        for item in values
        if str(item or "").strip()
    }


def _all_source_item_ids(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        found.update(_source_item_ids(value))
        for nested in value.values():
            found.update(_all_source_item_ids(nested))
    elif isinstance(value, list):
        for nested in value:
            found.update(_all_source_item_ids(nested))
    return found


def _all_evidence_ids(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        found.update(_item_evidence_ids(value))
        for nested in value.values():
            found.update(_all_evidence_ids(nested))
    elif isinstance(value, list):
        for nested in value:
            found.update(_all_evidence_ids(nested))
    return found


def _importance(value: Any, default: str = "supporting") -> str:
    rendered = str(value or default).strip().lower()
    rendered = _IMPORTANCE_ALIASES.get(rendered, rendered)
    return rendered if rendered in _IMPORTANCE else default


def _humanize_key(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value).replace("_", " ").strip()).capitalize()


def _render_nested(value: Any) -> str:
    """Render recoverable legacy JSON values without losing numeric detail."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        rendered = [_render_nested(item) for item in value]
        return "; ".join(item for item in rendered if item)
    if isinstance(value, dict):
        rendered = []
        for key, nested in value.items():
            if key in _ENTRY_CONTROL_KEYS:
                continue
            text_value = _render_nested(nested)
            if text_value:
                rendered.append(f"{_humanize_key(key)}: {text_value}")
        return "; ".join(rendered)
    return str(value).strip()


def _entry_candidates(value: Any) -> list[Any]:
    """Accept the canonical item arrays plus common model-shaped legacy JSON."""
    if isinstance(value, list):
        output: list[Any] = []
        for item in value:
            output.extend(_entry_candidates(item))
        return output
    if isinstance(value, dict):
        if any(str(value.get(key) or "").strip() for key in _ENTRY_TEXT_KEYS):
            return [value]
        importance = value.get("importance")
        output = []
        for key, nested in value.items():
            if key in _ENTRY_CONTROL_KEYS:
                continue
            if isinstance(nested, dict) and any(
                str(nested.get(name) or "").strip()
                for name in _ENTRY_TEXT_KEYS
            ):
                item = dict(nested)
                primary_text = next(
                    (nested.get(name) for name in _ENTRY_TEXT_KEYS
                     if str(nested.get(name) or "").strip()),
                    "",
                )
                extra_text = _render_nested({
                    name: child
                    for name, child in nested.items()
                    if name not in _ENTRY_CONTROL_KEYS
                    and name not in _ENTRY_TEXT_KEYS
                })
                rendered_text = str(primary_text).strip()
                if re.search(
                    r"\b(figure|table|equation|formula|visual)\b",
                    str(key).replace("_", " "),
                    flags=re.IGNORECASE,
                ):
                    rendered_text = f"{_humanize_key(key)}: {rendered_text}"
                if extra_text:
                    rendered_text = f"{rendered_text}; {extra_text}"
                item["text"] = rendered_text
                if importance and not item.get("importance"):
                    item["importance"] = importance
                output.append(item)
                continue
            text_value = _render_nested(nested)
            if not text_value:
                continue
            item: dict[str, Any] = {
                "text": f"{_humanize_key(key)}: {text_value}",
            }
            if importance:
                item["importance"] = importance
            output.append(item)
        return output
    if isinstance(value, (str, int, float, bool)):
        return [value]
    return []


def _map_field_value(raw: dict[str, Any], field: str) -> Any:
    sources = [
        raw[key] for key in _MAP_FIELD_SOURCES[field]
        if key in raw and raw[key] not in (None, "", [], {})
    ]
    if not sources:
        return []
    if len(sources) == 1:
        return sources[0]
    merged = []
    for source in sources:
        if isinstance(source, list):
            merged.extend(source)
        else:
            merged.append(source)
    return merged


def _legacy_summary(raw: dict[str, Any]) -> str:
    for key in ("summary", "summary_and_role", "summary_and_role_in_paper"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        candidates = _entry_candidates(value)
        rendered = []
        for candidate in candidates:
            if isinstance(candidate, dict):
                text_value = next(
                    (candidate.get(name) for name in _ENTRY_TEXT_KEYS
                     if str(candidate.get(name) or "").strip()),
                    "",
                )
            else:
                text_value = candidate
            if str(text_value or "").strip():
                rendered.append(str(text_value).strip())
        if rendered:
            return " ".join(rendered)
    return ""


def _sanitize_entries(value: Any, allowed: set[str], *,
                      leaf_default_ids: set[str] | None = None) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    seen: dict[tuple[str, tuple[str, ...]], int] = {}
    for entry in _entry_candidates(value):
        if isinstance(entry, (str, int, float, bool)):
            entry = {"text": entry}
        if not isinstance(entry, dict):
            continue
        text_value = next(
            (entry.get(key) for key in _ENTRY_TEXT_KEYS
             if str(entry.get(key) or "").strip()),
            "",
        )
        if not str(text_value or "").strip():
            continue
        # Reducers may cite only IDs attached directly to the retained item.
        # Recursively inheriting IDs from sibling/nested objects would make an
        # unsupported claim appear grounded in the entire input batch.
        ids = _item_evidence_ids(entry) & allowed
        if not ids and leaf_default_ids:
            ids = set(leaf_default_ids)
        if not ids:
            # A reduction item without valid evidence is unsupported and must
            # not be silently attached to the entire input batch.
            continue
        clean = {
            "text": str(text_value).strip(),
            "importance": _importance(entry.get("importance")),
            "evidence_ids": sorted(ids),
        }
        source_item_ids = _source_item_ids(entry)
        if source_item_ids:
            clean[_SOURCE_ITEM_IDS_KEY] = sorted(source_item_ids)
        signature = (clean["text"].casefold(), tuple(clean["evidence_ids"]))
        if signature in seen:
            existing = output[seen[signature]]
            if _IMPORTANCE[clean["importance"]] > _IMPORTANCE[existing["importance"]]:
                existing["importance"] = clean["importance"]
            merged_source_ids = (
                _source_item_ids(existing) | source_item_ids
            )
            if merged_source_ids:
                existing[_SOURCE_ITEM_IDS_KEY] = sorted(merged_source_ids)
            continue
        seen[signature] = len(output)
        output.append(clean)
    return output


def _ensure_source_item_lineage(mapped: dict[str, Any]) -> None:
    """Attach stable internal lineage to every admitted semantic item."""
    for field in _STRUCTURED_FIELDS:
        for entry in mapped.get(field, []):
            if not isinstance(entry, dict) or _source_item_ids(entry):
                continue
            entry[_SOURCE_ITEM_IDS_KEY] = [
                "I-" + _digest({
                    "field": field,
                    "text": str(entry.get("text") or "").strip(),
                    "evidence_ids": sorted(_item_evidence_ids(entry)),
                })[:20]
            ]


def _sanitize_map(raw: Any, allowed: set[str], *, leaf: bool,
                  fallback_summary: str = "") -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    default_ids = allowed if leaf else None
    result: dict[str, Any] = {
        "summary": str(_legacy_summary(raw) or fallback_summary).strip(),
        "role": str(raw.get("role") or "").strip(),
        "evidence_ids": sorted(allowed),
    }
    for field in _STRUCTURED_FIELDS:
        result[field] = _sanitize_entries(
            _map_field_value(raw, field),
            allowed,
            leaf_default_ids=default_ids,
        )
    has_semantic_entries = any(result[field] for field in _STRUCTURED_FIELDS)
    contract_fallback = bool(
        raw.get("contract_fallback") is True or not has_semantic_entries
    )
    if leaf and not has_semantic_entries:
        result["topics"] = [{
            "text": result["summary"] or result["role"] or "Paper evidence",
            "importance": "supporting",
            "evidence_ids": sorted(allowed),
        }]
    if leaf:
        _ensure_source_item_lineage(result)
    result["contract_fallback"] = contract_fallback
    # Never allow a reducer to make later evidence disappear. Its summary may
    # be terse, but the complete valid union survives for citation, assignment,
    # coverage, and drill-down to the cached leaf maps.
    result["evidence_ids"] = sorted(allowed)
    return result


def _structured_field_id_pairs(value: Any) -> set[tuple[str, str]]:
    """Return only direct structured support, excluding the root ID ledger."""
    if not isinstance(value, dict):
        return set()
    return {
        (field, evidence_id)
        for field in _STRUCTURED_FIELDS
        for entry in value.get(field, [])
        if isinstance(entry, dict)
        for evidence_id in _item_evidence_ids(entry)
    }


def _structured_lineage_units(value: Any) -> set[tuple[str, str]]:
    """Track every distinct semantic source item in its structured field."""
    if not isinstance(value, dict):
        return set()
    return {
        (field, source_item_id)
        for field in _STRUCTURED_FIELDS
        for entry in value.get(field, [])
        if isinstance(entry, dict)
        for source_item_id in _source_item_ids(entry)
    }


def _repair_reduction_coverage(
    reduced: dict[str, Any],
    source_maps: list[dict[str, Any]],
    allowed: set[str],
) -> dict[str, Any]:
    """Carry forward source items omitted or downgraded by a model reduction.

    The root ID ledger proves admission, not semantic retention. Stable item
    lineage distinguishes multiple claims/results that share a field and block.
    """
    for source in source_maps:
        _ensure_source_item_lineage(source)
    required_units = {
        unit
        for source in source_maps
        for unit in _structured_lineage_units(source)
    }
    required_field_pairs = {
        pair
        for source in source_maps
        for pair in _structured_field_id_pairs(source)
        if pair[1] in allowed
    }
    if {evidence_id for _field, evidence_id in required_field_pairs} != allowed:
        raise RuntimeError(
            "paper reduction input contains ledger-only evidence without "
            "direct item-level structured support"
        )
    allowed_source_ids = {
        source_id for _field, source_id in required_units
    }
    for field in _STRUCTURED_FIELDS:
        retained_entries = []
        for entry in reduced.get(field, []):
            if not isinstance(entry, dict):
                continue
            retained_source_ids = _source_item_ids(entry) & allowed_source_ids
            if retained_source_ids:
                entry[_SOURCE_ITEM_IDS_KEY] = sorted(retained_source_ids)
                retained_entries.append(entry)
        reduced[field] = retained_entries

    missing_units = required_units - _structured_lineage_units(reduced)
    missing_field_pairs = (
        required_field_pairs - _structured_field_id_pairs(reduced)
    )
    source_importance: dict[tuple[str, str], str] = {}
    for source in source_maps:
        for field in _STRUCTURED_FIELDS:
            for entry in source.get(field, []):
                if not isinstance(entry, dict):
                    continue
                entry_importance = _importance(entry.get("importance"))
                entry_source_ids = _source_item_ids(entry)
                entry_evidence_ids = _item_evidence_ids(entry) & allowed
                for source_item_id in entry_source_ids:
                    pair = (field, source_item_id)
                    prior = source_importance.get(pair, "supporting")
                    if _IMPORTANCE[entry_importance] > _IMPORTANCE[prior]:
                        source_importance[pair] = entry_importance
                if any(
                    (field, source_item_id) in missing_units
                    for source_item_id in entry_source_ids
                ) or any(
                    (field, evidence_id) in missing_field_pairs
                    for evidence_id in entry_evidence_ids
                ):
                    reduced[field].extend(
                        _sanitize_entries([entry], allowed)
                    )

    for field in _STRUCTURED_FIELDS:
        reduced[field] = _sanitize_entries(reduced.get(field, []), allowed)
        for entry in reduced[field]:
            required_importance = max(
                (
                    source_importance.get(
                        (field, source_item_id), "supporting"
                    )
                    for source_item_id in _source_item_ids(entry)
                ),
                key=lambda value: _IMPORTANCE[value],
                default="supporting",
            )
            if (
                _IMPORTANCE[required_importance]
                > _IMPORTANCE[_importance(entry.get("importance"))]
            ):
                entry["importance"] = required_importance

    remaining_units = required_units - _structured_lineage_units(reduced)
    remaining_pairs = (
        required_field_pairs - _structured_field_id_pairs(reduced)
    )
    if remaining_units or remaining_pairs:
        lineage_sample = ", ".join(
            f"{field}:{source_id}"
            for field, source_id in sorted(remaining_units)[:10]
        )
        evidence_sample = ", ".join(
            f"{field}:{evidence_id}"
            for field, evidence_id in sorted(remaining_pairs)[:10]
        )
        raise RuntimeError(
            "paper reduction could not retain item-level semantic coverage: "
            + "; ".join(
                value for value in (lineage_sample, evidence_sample) if value
            )
        )
    return {
        "lineage_units": len(missing_units),
        "source_item_ids": sorted({
            source_id for _field, source_id in missing_units
        }),
        "field_id_pairs": len(missing_field_pairs),
        "evidence_ids": sorted({
            evidence_id for _field, evidence_id in missing_field_pairs
        } | {
            evidence_id
            for source in source_maps
            for field in _STRUCTURED_FIELDS
            for entry in source.get(field, [])
            if isinstance(entry, dict)
            and any(
                (field, source_id) in missing_units
                for source_id in _source_item_ids(entry)
            )
            for evidence_id in _item_evidence_ids(entry) & allowed
        }),
    }


def _chunk_metadata(chunk: PaperChunk) -> dict[str, Any]:
    return {
        "evidence_id": chunk.evidence_id,
        "page_number": chunk.page_number,
        "section_path": _json(chunk.section_path, []),
        "bounding_box": _json(chunk.bbox, {}),
        "kind": chunk.kind,
        "quality_grade": chunk.quality_grade,
        "flags": _json(chunk.flags, []),
        "extraction_method": chunk.extraction_method,
    }


def _map_config_hash(source: PaperSource, provider: str, model: str) -> str:
    return _digest({
        "schema": 3,
        "contract_version": PAPER_MAP_CONTRACT_VERSION,
        "source_hash": source.source_hash,
        "parser_version": source.parser_version,
        "parser_config_hash": source.parser_config_hash,
        "prompt": _digest(_paper_prompt("paper_map", PAPER_MAP_PROMPT)),
        "execution": paper_model_execution_signature(
            "paper_map",
            provider,
            model,
            local_only=bool(source.local_only),
            json_format=True,
        ),
        "settings": _paper_analysis_settings(),
    })


def map_all_evidence(job_id: int, project_id: int) -> tuple[list[dict], dict]:
    """Map every stored block, reusing only exact content/config cache hits."""
    with get_session() as session:
        project = get_project(session, project_id)
        if project.source_type != "paper":
            raise ValueError("paper mapping is only applicable to paper projects")
        source = paper_store.paper_source_for_project(session, project_id)
        if source is None:
            raise RuntimeError("paper source metadata is missing")
        paper_store.require_analysis_ready(source)
        chunks = session.exec(select(PaperChunk).where(
            PaperChunk.source_id == source.id).order_by(PaperChunk.chunk_index)).all()
        if not chunks:
            raise RuntimeError("paper extraction produced no evidence chunks")
        source_id = source.id
        source_hash = source.source_hash
        local_only = bool(source.local_only)
        parser_version = source.parser_version
        parser_config_hash = source.parser_config_hash

    with llm.project_scope(project_id, local_only=local_only):
        provider, model = llm.resolve_model("paper_map")
    # The detached source is safe to read here; no lazy relationships exist.
    config_hash = _map_config_hash(source, provider, model)
    output: list[dict] = []
    reused = 0
    generated = 0
    for position, chunk in enumerate(chunks, 1):
        metadata = _chunk_metadata(chunk)
        input_hash = _digest({
            "source_hash": source_hash,
            "body_hash": chunk.body_hash,
            "metadata": metadata,
        })
        with get_session() as session:
            cached = _cache_get(
                session,
                source_id=source_id,
                purpose="leaf_map",
                input_hash=input_hash,
                config_hash=config_hash,
            )
        if cached and isinstance(cached[0], dict):
            mapped = _sanitize_map(
                cached[0], {chunk.evidence_id}, leaf=True,
                fallback_summary=chunk.body[:400])
            reused += 1
        else:
            progress(job_id, f"mapping paper evidence {position}/{len(chunks)}")
            raw = llm.complete_json(
                "paper_map",
                _paper_prompt("paper_map", PAPER_MAP_PROMPT),
                "EVIDENCE METADATA:\n"
                + json.dumps(metadata, sort_keys=True, ensure_ascii=False)
                + "\n\nBEGIN UNTRUSTED PAPER EVIDENCE\n"
                + chunk.body
                + "\nEND UNTRUSTED PAPER EVIDENCE",
                max_tokens=_paper_analysis_settings()["map_output_tokens"],
                provider=provider,
                model=model,
                local_only=local_only,
            )
            mapped = _sanitize_map(
                raw, {chunk.evidence_id}, leaf=True,
                fallback_summary=chunk.body[:400])
            with get_session() as session:
                _cache_put(
                    session,
                    project_id=project_id,
                    source_id=source_id,
                    purpose="leaf_map",
                    input_hash=input_hash,
                    config_hash=config_hash,
                    provider=provider,
                    model=model,
                    body=mapped,
                    evidence_ids=[chunk.evidence_id],
                )
            generated += 1
        mapped.update({
            "evidence_id": chunk.evidence_id,
            "page_number": chunk.page_number,
            "section_path": _json(chunk.section_path, []),
            "kind": chunk.kind,
            "quality_grade": chunk.quality_grade,
            "flags": _json(chunk.flags, []),
        })
        output.append(mapped)
    mapped_ids = {item["evidence_id"] for item in output}
    expected_ids = {chunk.evidence_id for chunk in chunks}
    if mapped_ids != expected_ids:
        missing = sorted(expected_ids - mapped_ids)
        raise RuntimeError(
            "paper map coverage invariant failed; missing evidence: "
            + ", ".join(missing[:10]))
    coverage = {
        "source_hash": source_hash,
        "parser_version": parser_version,
        "parser_config_hash": parser_config_hash,
        "total_evidence_blocks": len(chunks),
        "mapped_evidence_blocks": len(output),
        "unmapped_evidence_blocks": 0,
        "last_page_mapped": max(chunk.page_number for chunk in chunks),
        "contract_fallback_blocks": sum(
            bool(item.get("contract_fallback")) for item in output
        ),
        "contract_fallback_evidence_ids": [
            item["evidence_id"] for item in output
            if item.get("contract_fallback")
        ],
        "cache": {
            "reused_leaf_maps": reused,
            "generated_leaf_maps": generated,
            "leaf_config_hash": config_hash,
        },
        "sampling": False,
        "prefix_truncation": False,
    }
    return output, coverage


def _estimated_tokens(value: Any) -> int:
    return max(1, math.ceil(len(json.dumps(
        value, sort_keys=True, ensure_ascii=False, default=str)) / 4))


def _compact_map_for_prompt(
    value: Any,
    *,
    include_reduction_lineage: bool = False,
) -> Any:
    """Keep the lossless ID ledger in storage, not in every reducer prompt.

    The root ``evidence_ids`` union can itself exceed a model context for a PDF
    containing many small layout blocks. Facts/topics retain their supporting
    IDs; the complete ledger remains on the in-memory/cache object and is
    verified at each level. Reducers receive a count/hash for coverage auditing.
    """
    if isinstance(value, list):
        return [
            _compact_map_for_prompt(
                item,
                include_reduction_lineage=include_reduction_lineage,
            )
            for item in value
        ]
    if not isinstance(value, dict):
        return value
    is_map = "summary" in value and any(
        field in value for field in _STRUCTURED_FIELDS)
    output = {}
    for key, nested in value.items():
        if is_map and key == "evidence_ids":
            continue
        if key == _SOURCE_ITEM_IDS_KEY:
            if include_reduction_lineage:
                output["source_item_ids"] = sorted(_source_item_ids(value))
            continue
        output[key] = _compact_map_for_prompt(
            nested,
            include_reduction_lineage=include_reduction_lineage,
        )
    ids = value.get("evidence_ids")
    if is_map and isinstance(ids, list):
        output["evidence_coverage"] = {
            "count": len(ids),
            "hash": _digest(sorted(str(item) for item in ids if item)),
        }
    return output


def _estimated_prompt_tokens(value: Any) -> int:
    return _estimated_tokens(_compact_map_for_prompt(
        value,
        include_reduction_lineage=True,
    ))


def _pack(items: list[dict], limit_tokens: int) -> list[list[dict]]:
    batches: list[list[dict]] = []
    current: list[dict] = []
    current_tokens = 0
    for item in items:
        item_tokens = _estimated_prompt_tokens(item)
        if item_tokens > limit_tokens:
            raise RuntimeError(
                "one structured paper map exceeds the reduction input budget; "
                "increase paper.analysis.reduce_batch_tokens and rerun")
        if current and current_tokens + item_tokens > limit_tokens:
            batches.append(current)
            current = []
            current_tokens = 0
        current.append(item)
        current_tokens += item_tokens
    if current:
        batches.append(current)
    return batches


def hierarchical_reduce(job_id: int, project_id: int, maps: list[dict],
                        purpose: str = "whole_paper") -> tuple[list[dict], dict]:
    """Recursively pack/reduce maps while preserving the full evidence-id union."""
    if not maps:
        raise RuntimeError("cannot reduce an empty paper evidence map")
    with get_session() as session:
        source = paper_store.paper_source_for_project(session, project_id)
        if source is None:
            raise RuntimeError("paper source metadata is missing")
        paper_store.require_analysis_ready(source)
        source_id = source.id
        local_only = bool(source.local_only)
        source_hash = source.source_hash
        parser_version = source.parser_version
        parser_config_hash = source.parser_config_hash
    with llm.project_scope(project_id, local_only=local_only):
        provider, model = llm.resolve_model("paper_reduce")
    limits = _paper_analysis_settings()
    config_hash = _digest({
        "schema": 5,
        "contract_version": PAPER_REDUCE_CONTRACT_VERSION,
        "purpose": purpose,
        "source_hash": source_hash,
        "parser_version": parser_version,
        "parser_config_hash": parser_config_hash,
        "prompt": _digest(_paper_prompt("paper_reduce", PAPER_REDUCE_PROMPT)),
        "execution": paper_model_execution_signature(
            "paper_reduce",
            provider,
            model,
            local_only=local_only,
            json_format=True,
        ),
        "limits": limits,
    })
    for item in maps:
        _ensure_source_item_lineage(item)
    expected_ids = {value for item in maps for value in _all_evidence_ids(item)}
    expected_lineage_units = {
        unit for item in maps for unit in _structured_lineage_units(item)
    }
    directly_supported_ids = {
        evidence_id
        for item in maps
        for _field, evidence_id in _structured_field_id_pairs(item)
    }
    if directly_supported_ids != expected_ids:
        raise RuntimeError(
            "paper hierarchy contains ledger-only evidence without direct "
            "structured support"
        )
    items = list(maps)
    level = 0
    cache_reused = 0
    cache_generated = 0
    carried_lineage_units = 0
    carried_source_item_ids: set[str] = set()
    carried_field_id_pairs = 0
    carried_evidence_ids: set[str] = set()
    while _estimated_prompt_tokens(items) > limits["final_context_tokens"]:
        level += 1
        if level > 20:
            raise RuntimeError("paper evidence did not converge within 20 reduction levels")
        batches = _pack(items, limits["reduce_batch_tokens"])
        reduced: list[dict] = []
        for index, batch in enumerate(batches, 1):
            allowed = {value for item in batch for value in _all_evidence_ids(item)}
            if not allowed:
                raise RuntimeError("paper reduction input lost all evidence identifiers")
            input_hash = _digest(batch)
            cache_purpose = f"reduce:{purpose}:level:{level}"
            with get_session() as session:
                cached = _cache_get(
                    session,
                    source_id=source_id,
                    purpose=cache_purpose,
                    input_hash=input_hash,
                    config_hash=config_hash,
                )
            if cached and not isinstance(cached[0], dict):
                cached = None
            if cached and isinstance(cached[0], dict):
                item = _sanitize_map(cached[0], allowed, leaf=False)
                # Old or malformed cached reductions with only a root ledger
                # are not semantically usable. Regenerate them under the
                # current contract rather than blessing them as cache hits.
                if item.get("contract_fallback"):
                    cached = None
                else:
                    cache_reused += 1
            generated = not bool(cached)
            if not cached:
                progress(
                    job_id,
                    f"reducing paper evidence level {level}, batch {index}/{len(batches)}",
                )
                raw = llm.complete_json(
                    "paper_reduce",
                    _paper_prompt("paper_reduce", PAPER_REDUCE_PROMPT)
                    + f"\nReduction purpose: {purpose}.",
                    json.dumps(
                        _compact_map_for_prompt(
                            batch,
                            include_reduction_lineage=True,
                        ),
                        sort_keys=True,
                        ensure_ascii=False,
                    ),
                    max_tokens=limits["reduce_output_tokens"],
                    provider=provider,
                    model=model,
                    local_only=local_only,
                )
                fallback = " ".join(
                    str(value.get("summary") or "") for value in batch)[:1_500]
                item = _sanitize_map(
                    raw, allowed, leaf=False, fallback_summary=fallback)
                if item.get("contract_fallback"):
                    raise RuntimeError(
                        "paper reduction returned no directly cited structured "
                        "content; use a stronger model or revise the paper-reduce "
                        "prompt and rerun"
                    )
            repair = _repair_reduction_coverage(item, batch, allowed)
            carried_lineage_units += int(repair["lineage_units"])
            carried_source_item_ids.update(repair["source_item_ids"])
            carried_field_id_pairs += int(repair["field_id_pairs"])
            carried_evidence_ids.update(repair["evidence_ids"])
            if generated:
                with get_session() as session:
                    _cache_put(
                        session,
                        project_id=project_id,
                        source_id=source_id,
                        purpose=cache_purpose,
                        input_hash=input_hash,
                        config_hash=config_hash,
                        provider=provider,
                        model=model,
                        body=item,
                        evidence_ids=allowed,
                    )
                cache_generated += 1
            reduced.append(item)
        reduced_ids = {value for item in reduced for value in _all_evidence_ids(item)}
        current_ids = {value for item in items for value in _all_evidence_ids(item)}
        if reduced_ids != current_ids:
            raise RuntimeError("paper reduction dropped one or more evidence identifiers")
        prior_tokens = _estimated_prompt_tokens(items)
        next_tokens = _estimated_prompt_tokens(reduced)
        if next_tokens >= prior_tokens and len(reduced) >= len(items):
            raise RuntimeError(
                "paper structured reductions do not fit the configured context budget; "
                "increase paper.analysis.final_context_tokens")
        items = reduced
    final_ids = {value for item in items for value in _all_evidence_ids(item)}
    if final_ids != expected_ids:
        raise RuntimeError("hierarchical paper context does not cover every leaf map")
    final_direct_ids = {
        evidence_id
        for item in items
        for _field, evidence_id in _structured_field_id_pairs(item)
    }
    if final_direct_ids != expected_ids:
        raise RuntimeError(
            "hierarchical paper context lost direct semantic evidence support"
        )
    final_lineage_units = {
        unit for item in items for unit in _structured_lineage_units(item)
    }
    if not expected_lineage_units <= final_lineage_units:
        raise RuntimeError(
            "hierarchical paper context lost item-level semantic lineage"
        )
    return items, {
        "levels": level,
        "final_context_tokens": _estimated_prompt_tokens(items),
        "evidence_ids_preserved": len(final_ids),
        "semantic_carryforward": {
            "lineage_units": carried_lineage_units,
            "source_item_ids": sorted(carried_source_item_ids),
            "field_id_pairs": carried_field_id_pairs,
            "evidence_ids": sorted(carried_evidence_ids),
        },
        "cache": {
            "reused_reductions": cache_reused,
            "generated_reductions": cache_generated,
            "reduction_config_hash": config_hash,
        },
    }


def _evidence_importance(maps: list[dict]) -> dict[str, str]:
    ranks: dict[str, int] = {
        str(item.get("evidence_id")): 0 for item in maps if item.get("evidence_id")
    }
    major_fields = {"claims", "hypotheses", "methods", "results", "limitations"}
    for mapped in maps:
        for field in _STRUCTURED_FIELDS:
            for entry in mapped.get(field, []):
                default_rank = 1 if field in major_fields else 0
                rank = max(default_rank, _IMPORTANCE[_importance(entry.get("importance"))])
                for evidence_id in _item_evidence_ids(entry):
                    ranks[evidence_id] = max(ranks.get(evidence_id, 0), rank)
    labels = {rank: name for name, rank in _IMPORTANCE.items()}
    return {evidence_id: labels[rank] for evidence_id, rank in ranks.items()}


def _topic_inventory(maps: list[dict]) -> list[dict[str, Any]]:
    """Build stable approval topics from all leaf maps, never a sampled subset."""
    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    major_fields = {"claims", "hypotheses", "methods", "results", "limitations"}
    for mapped in maps:
        for field in _STRUCTURED_FIELDS:
            for entry in mapped.get(field, []):
                text_value = str(entry.get("text") or "").strip()
                if not text_value:
                    continue
                normalized = re.sub(r"\W+", " ", text_value.casefold()).strip()
                key = (field, normalized[:300])
                importance = _importance(
                    entry.get("importance"),
                    "major" if field in major_fields else "supporting",
                )
                if field in major_fields and importance == "supporting":
                    importance = "major"
                row = by_key.setdefault(key, {
                    "id": "T-" + _digest({"field": field, "text": normalized})[:16],
                    "type": field,
                    "title": text_value[:240],
                    "text": text_value,
                    "importance": importance,
                    "evidence_ids": [],
                })
                if _IMPORTANCE[importance] > _IMPORTANCE[row["importance"]]:
                    row["importance"] = importance
                row["evidence_ids"] = sorted(
                    set(row["evidence_ids"]) | _item_evidence_ids(entry))
    return sorted(
        by_key.values(),
        key=lambda row: (
            -_IMPORTANCE[row["importance"]],
            row["type"],
            row["title"].casefold(),
        ),
    )


def build_analysis_bundle(job_id: int, project_id: int) -> dict[str, Any]:
    maps, coverage = map_all_evidence(job_id, project_id)
    context, reductions = hierarchical_reduce(
        job_id, project_id, maps, purpose="shared_analysis")
    with get_session() as session:
        source = paper_store.paper_source_for_project(session, project_id)
        source_id = source.id
        local_only = bool(source.local_only)
    with llm.project_scope(project_id, local_only=local_only):
        provider, model = llm.resolve_model("paper_reduce")
    analysis_signature = paper_analysis_config_signature(project_id)
    importance = _evidence_importance(maps)
    topics = _topic_inventory(maps)
    context_digest = _digest(context)
    bundle = {
        "schema": 3,
        "source_hash": coverage["source_hash"],
        "analysis_config_signature": analysis_signature["signature"],
        "hierarchical_context": context,
        "hierarchical_context_digest": context_digest,
        "leaf_evidence_ids": [item["evidence_id"] for item in maps],
        "evidence_importance": importance,
        "topics": topics,
        "critical_topics": [
            topic for topic in topics if topic["importance"] == "critical"
        ],
        "critical_evidence_ids": sorted(
            evidence_id for evidence_id, value in importance.items()
            if value == "critical"),
        "major_evidence_ids": sorted(
            evidence_id for evidence_id, value in importance.items()
            if value == "major"),
        "coverage": {**coverage, "reductions": reductions},
    }
    input_hash = _digest(maps)
    config_hash = _digest({
        "schema": 3,
        "map_contract_version": PAPER_MAP_CONTRACT_VERSION,
        "reduce_contract_version": PAPER_REDUCE_CONTRACT_VERSION,
        "analysis": analysis_signature,
        "source_hash": coverage["source_hash"],
        "reduced_context_digest": context_digest,
    })
    with get_session() as session:
        _cache_put(
            session,
            project_id=project_id,
            source_id=source_id,
            purpose="analysis_bundle",
            input_hash=input_hash,
            config_hash=config_hash,
            provider=provider,
            model=model,
            body=bundle,
            evidence_ids=bundle["leaf_evidence_ids"],
        )
        source = paper_store.paper_source_for_project(session, project_id)
        source_coverage = _json(source.coverage_report, {})
        source_coverage.update({
            "mapped_evidence_blocks": coverage["mapped_evidence_blocks"],
            "unmapped_evidence_blocks": 0,
            "last_page_mapped": coverage["last_page_mapped"],
            "contract_fallback_blocks": coverage["contract_fallback_blocks"],
            "contract_fallback_evidence_ids": (
                coverage["contract_fallback_evidence_ids"]
            ),
            "topics": topics,
            "critical_total": len(bundle["critical_topics"]),
            "major_total": sum(topic["importance"] == "major" for topic in topics),
            "sampling": False,
            "prefix_truncation": False,
        })
        source.coverage_report = json.dumps(
            paper_store.normalize_paper_json(source_coverage),
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
        )
        source.updated = utcnow()
        session.add(source)
        session.commit()
    return bundle


def latest_analysis_bundle(project_id: int) -> dict[str, Any] | None:
    expected_signature = paper_analysis_config_signature(project_id)["signature"]
    with get_session() as session:
        source = paper_store.paper_source_for_project(session, project_id)
        if source is None:
            return None
        rows = session.exec(select(PaperSynthesisCache).where(
            PaperSynthesisCache.source_id == source.id,
            PaperSynthesisCache.purpose == "analysis_bundle",
        ).order_by(PaperSynthesisCache.id.desc())).all()
    for row in rows:
        try:
            value = json.loads(row.body)
        except json.JSONDecodeError:
            continue
        if (isinstance(value, dict)
                and value.get("source_hash") == source.source_hash
                and value.get("analysis_config_signature") == expected_signature):
            return value
    return None


def _root_provenance(project_id: int, source: PaperSource, *, function: str,
                     provider: str, model: str, prompt: str,
                     evidence_ids: Iterable[str], input_hash: str,
                     upstream_analysis: dict[str, str] | None = None,
                     extra: dict | None = None) -> tuple[str, str, dict[str, Any]]:
    if upstream_analysis:
        input_hash = _digest({
            "content_input_hash": input_hash,
            "upstream_analysis": upstream_analysis,
        })
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
        "analysis_settings": _paper_analysis_settings(),
    }
    config_hash = _digest({
        "source_hash": source.source_hash,
        "parser_version": source.parser_version,
        "parser_config_hash": source.parser_config_hash,
        **config,
    })
    provenance = {
        "schema": 2,
        "source_kind": "paper",
        "source": paper_source_signature_from_model(source),
        "input": {
            "hash": input_hash,
            "evidence_ids": sorted({str(value) for value in evidence_ids if value}),
        },
        "config": config,
        "output_scope": {"paper_series_id": None, "paper_part_id": None},
    }
    if upstream_analysis:
        provenance["upstream_analysis"] = upstream_analysis
    if extra:
        provenance.update(extra)
    return input_hash, config_hash, provenance


def paper_source_signature_from_model(source: PaperSource) -> dict[str, Any]:
    value = {
        "source_hash": source.source_hash,
        "parser_version": source.parser_version,
        "parser_config_hash": source.parser_config_hash,
        "ocr_languages": list(_ocr_languages(source)),
        "local_only": bool(source.local_only),
        "acknowledged_pages": _json(source.acknowledged_pages, []),
    }
    return {**value, "signature": _digest(value)}


def _write_root_artifact(
    project_id: int,
    *,
    artifact_type: str,
    title: str,
    body: str,
    source: PaperSource,
    function: str,
    provider: str,
    model: str,
    prompt: str,
    evidence_ids: Iterable[str],
    input_hash: str,
    upstream_analysis: dict[str, str] | None = None,
    extra_meta: dict | None = None,
    provenance_extra: dict | None = None,
) -> int:
    input_hash, config_hash, provenance = _root_provenance(
        project_id,
        source,
        function=function,
        provider=provider,
        model=model,
        prompt=prompt,
        evidence_ids=evidence_ids,
        input_hash=input_hash,
        upstream_analysis=upstream_analysis,
        extra=provenance_extra,
    )
    with get_session() as session:
        project = get_project(session, project_id)
        artifact = library.write_artifact(
            session,
            project_id=project_id,
            project_slug=project.slug,
            type=artifact_type,
            title=f"{title} — {project.title}",
            body=body,
            provider=provider or None,
            model=model or None,
            paper_series_id=None,
            paper_part_id=None,
            input_hash_override=input_hash,
            config_hash_override=config_hash,
            provenance_override=provenance,
            extra_meta={
                "source_kind": "paper",
                "source_hash": source.source_hash,
                "parser_version": source.parser_version,
                "parser_config_hash": source.parser_config_hash,
                "evidence_count": len(set(evidence_ids)),
                **(extra_meta or {}),
            },
        )
        artifact_id = artifact.id
    if artifact_type in {
        "paper_argument_map", "paper_mindmap", "paper_quick_references",
    }:
        auto_tag(project_id, artifact_id)
    return artifact_id


def _extraction_report_body(source: PaperSource) -> str:
    quality = _json(source.quality_report, {})
    coverage = _json(source.coverage_report, {})
    acknowledgements = _json(source.acknowledged_pages, [])
    blocked = paper_store.extraction_blockers(source)
    lines = [
        "# Source extraction and review",
        "",
        f"- Source SHA-256: `{source.source_hash}`",
        f"- PDF size: {source.size_bytes:,} bytes",
        f"- Pages: {source.page_count:,}",
        f"- Extracted characters: {source.extracted_characters:,}",
        f"- Evidence blocks: {int(coverage.get('evidence_block_count') or 0):,}",
        f"- Parser: `{source.parser_version}`",
        f"- OCR languages: {', '.join(_ocr_languages(source))}",
        f"- Document quality: **{source.quality_grade}**",
        f"- Analysis: **{'BLOCKED FOR REVIEW' if blocked else 'ready'}**",
        "",
        "No evidence blocks were representative-sampled or prefix-truncated.",
        "The original PDF remains in the project library and is permanently excluded "
        "from cloud synchronization.",
        "",
        "## Page quality",
        "",
        "| Page | Grade | Characters | Review |",
        "|---:|:---:|---:|:---|",
    ]
    acknowledged_pages = paper_store.acknowledged_page_numbers(source)
    for page in quality.get("pages", []):
        number = int(page.get("page_number") or 0)
        review = ("acknowledged gap" if number in acknowledged_pages
                  else "required" if page.get("grade") == "POOR" and page.get("nontrivial")
                  else "")
        link = f"[p. {number}](/api/papers/{source.project_id}/source#page={number})"
        lines.append(
            f"| {link} | {page.get('grade', 'UNKNOWN')} | "
            f"{int(page.get('extracted_characters') or 0):,} | {review} |")
    if acknowledgements:
        lines.extend(["", "## Acknowledged extraction gaps", ""])
        for item in acknowledgements:
            if isinstance(item, dict):
                lines.append(
                    f"- Page {item.get('page')}: {item.get('reason') or 'No reason recorded'}")
    visual_count = len(coverage.get("visual_review_evidence_ids") or [])
    unreliable_count = len(coverage.get("unreliable_evidence_ids") or [])
    lines.extend([
        "",
        "## Structural review flags",
        "",
        f"- Visual/caption locations requiring source review: {visual_count:,}",
        f"- Table/formula extractions flagged unreliable: {unreliable_count:,}",
    ])
    return "\n".join(lines)


def write_extraction_report(project_id: int, source: PaperSource) -> int:
    body = _extraction_report_body(source)
    input_hash = _digest({
        "source_hash": source.source_hash,
        "parser_config_hash": source.parser_config_hash,
        "quality": _json(source.quality_report, {}),
        "coverage": _json(source.coverage_report, {}),
        "acknowledged_pages": _json(source.acknowledged_pages, []),
    })
    return _write_root_artifact(
        project_id,
        artifact_type="paper_extraction_report",
        title="Paper extraction report",
        body=body,
        source=source,
        function="paper_extract",
        provider="docling",
        model=source.parser_version,
        prompt="deterministic extraction report schema 1",
        evidence_ids=[],
        input_hash=input_hash,
        extra_meta={"analysis_blocked": bool(paper_store.extraction_blockers(source))},
    )


def _coverage_body(source: PaperSource, bundle: dict[str, Any],
                   chunks: list[PaperChunk]) -> str:
    coverage = bundle["coverage"]
    importance = bundle["evidence_importance"]
    acknowledged = paper_store.acknowledged_page_numbers(source)
    page_counts = Counter(chunk.page_number for chunk in chunks)
    lines = [
        "# Paper analysis coverage",
        "",
        f"- Evidence mapped: **{coverage['mapped_evidence_blocks']:,}/"
        f"{coverage['total_evidence_blocks']:,}**",
        f"- Last source page mapped: **{coverage['last_page_mapped']:,}/"
        f"{source.page_count:,}**",
        f"- Critical evidence blocks: {sum(v == 'critical' for v in importance.values()):,}",
        f"- Major evidence blocks: {sum(v == 'major' for v in importance.values()):,}",
        f"- Leaf maps reused: {coverage['cache']['reused_leaf_maps']:,}",
        f"- Leaf maps generated: {coverage['cache']['generated_leaf_maps']:,}",
        f"- Contract-fallback leaf maps: "
        f"{coverage.get('contract_fallback_blocks', 0):,}",
        f"- Reduction levels: {coverage['reductions']['levels']:,}",
        f"- Reduction semantic lineage units carried forward: "
        f"{coverage['reductions'].get('semantic_carryforward', {}).get('lineage_units', 0):,}",
        "- Sampling: **none**",
        "- Prefix truncation: **none**",
        "",
        "Every admitted evidence block was mapped. Hierarchical reductions retain the "
        "complete evidence-ID union, and the cached leaf map remains available for "
        "drill-down and audience-track reuse.",
        "",
    ]
    if coverage.get("contract_fallback_blocks"):
        lines.extend([
            "> **Model contract fallbacks remain visible.** The following evidence "
            "blocks retained their excerpt as a supporting topic because the map "
            "model did not return structured classifications: "
            + ", ".join(coverage.get("contract_fallback_evidence_ids", [])[:50]),
            "",
        ])
    lines.extend([
        "## Evidence by page",
        "",
        "| Page | Blocks | Extraction gap |",
        "|---:|---:|:---|",
    ])
    for page in range(1, source.page_count + 1):
        link = f"[p. {page}](/api/papers/{source.project_id}/source#page={page})"
        lines.append(
            f"| {link} | {page_counts[page]:,} | "
            f"{'acknowledged' if page in acknowledged else ''} |")
    if acknowledged:
        lines.extend([
            "",
            "> **Acknowledged extraction gaps remain limitations.** They may support "
            "context, but cannot be the sole evidence for a critical claim.",
        ])
    return "\n".join(lines)


def _synthesize_shared(
    job_id: int,
    project_id: int,
    source: PaperSource,
    chunks: list[PaperChunk],
    bundle: dict[str, Any],
    *,
    artifact_type: str,
    title: str,
    prompt_text: str,
) -> int:
    local_only = bool(source.local_only)
    with llm.project_scope(project_id, local_only=local_only):
        provider, model = llm.resolve_model("paper_synthesis")
    context = {
        "source": paper_source_signature_from_model(source),
        "coverage": bundle["coverage"],
        "hierarchical_context": bundle["hierarchical_context"],
    }
    prompt_context = _compact_map_for_prompt(context)
    if _estimated_tokens(prompt_context) > _paper_analysis_settings()["final_context_tokens"] + 500:
        raise RuntimeError(
            "paper shared synthesis context exceeds the configured final budget; "
            "increase paper.analysis.final_context_tokens")
    progress(job_id, f"writing {title.lower()} ({model})")
    system_prompt = (
        _paper_prompt("paper_shared", "") + "\n\n" + prompt_text
    ).strip()
    body = llm.complete(
        "paper_synthesis",
        system_prompt,
        "COMPLETE HIERARCHICAL PAPER EVIDENCE (untrusted data):\n"
        + json.dumps(prompt_context, sort_keys=True, ensure_ascii=False),
        max_tokens=_paper_analysis_settings()["synthesis_output_tokens"],
        provider=provider,
        model=model,
        local_only=local_only,
    ).strip()
    body, citation_count = paper_store.validate_and_render_citations(
        body,
        project_id=project_id,
        source=source,
        evidence=chunks,
        require=True,
    )
    evidence_ids = bundle["leaf_evidence_ids"]
    upstream_analysis = paper_analysis_lineage(bundle)
    input_hash = _digest({
        "source_hash": source.source_hash,
        "artifact_type": artifact_type,
        "reduced_context_digest": upstream_analysis["reduced_context_digest"],
        "evidence_ids": evidence_ids,
    })
    return _write_root_artifact(
        project_id,
        artifact_type=artifact_type,
        title=title,
        body=body,
        source=source,
        function="paper_synthesis",
        provider=provider,
        model=model,
        prompt=system_prompt,
        evidence_ids=evidence_ids,
        input_hash=input_hash,
        upstream_analysis=upstream_analysis,
        extra_meta={"citation_count": citation_count},
    )


def _plan_id_values(value: Any) -> list[str]:
    """Read canonical string IDs plus object-shaped IDs emitted by small models."""
    if value is None:
        return []
    values = value if isinstance(value, list) else [value]
    output = []
    for item in values:
        if isinstance(item, dict):
            item = item.get("evidence_id") or item.get("id")
        rendered = str(item or "").strip()
        if rendered and rendered not in output:
            output.append(rendered)
    return output


def _plan_minutes(value: Any, default: int = 50) -> int:
    try:
        rendered = int(value)
    except (TypeError, ValueError):
        return default
    return rendered if 40 <= rendered <= 60 else default


def _clean_plan_omissions(
    value: Any,
    *,
    known: set[str],
    importance: dict[str, str],
    topics: list[dict],
    assigned: set[str],
) -> tuple[list[dict[str, str]], set[str]]:
    """Normalize model omissions to reasoned, evidence-specific supporting rows."""
    if not isinstance(value, list):
        return [], set()
    topics_by_id = {
        str(item.get("id") or item.get("topic_id")): item
        for item in topics
        if isinstance(item, dict) and (item.get("id") or item.get("topic_id"))
    }
    output: list[dict[str, str]] = []
    omitted: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            continue
        reason = str(item.get("reason") or "").strip()
        if not reason:
            continue
        topic_id = str(item.get("topic_id") or "").strip()
        topic = topics_by_id.get(topic_id)
        evidence_ids = _plan_id_values(item.get("evidence_id"))
        if not evidence_ids and topic and (
            topic.get("importance", "supporting") == "supporting"
        ):
            evidence_ids = _plan_id_values(topic.get("evidence_ids"))
        for evidence_id in evidence_ids:
            if (
                evidence_id not in known
                or evidence_id in assigned
                or evidence_id in omitted
                or importance.get(evidence_id) != "supporting"
            ):
                continue
            output.append({
                "evidence_id": evidence_id,
                "importance": "supporting",
                "reason": reason,
            })
            omitted.add(evidence_id)
    return output, omitted


def _clean_plan(raw: Any, *, audience: str, chunks: list[PaperChunk],
                importance: dict[str, str], topics: list[dict],
                requested_target_minutes: int | None = None,
                requested_title: str = "",
                prerequisite_evidence_ids: set[str] | None = None,
                ) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    if requested_target_minutes is None:
        target_minutes = _plan_minutes(raw.get("target_minutes"))
    else:
        target_minutes = _plan_minutes(requested_target_minutes)
    raw_parts = raw.get("parts") if isinstance(raw.get("parts"), list) else []
    if not raw_parts:
        raw_parts = [{
            "title": "Understanding the paper",
            "focus": "The paper's argument, method, evidence, and limitations",
            "duration_minutes": target_minutes,
        }]
    raw_parts = raw_parts[:5]
    known = {chunk.evidence_id: chunk for chunk in chunks}
    if not known:
        raise RuntimeError("paper series planning requires extracted evidence")
    assigned: set[str] = set()
    parts: list[dict[str, Any]] = []
    for position, value in enumerate(raw_parts, 1):
        value = value if isinstance(value, dict) else {}
        api_assignments = (
            value.get("evidence") if isinstance(value.get("evidence"), list) else []
        )
        if "primary_evidence_ids" in value:
            primary_source = value.get("primary_evidence_ids")
        elif api_assignments:
            primary_source = [
                item for item in api_assignments
                if isinstance(item, dict)
                and str(item.get("role") or "primary") == "primary"
            ]
        else:
            # Compatibility with the original central prompt, which called
            # this field evidence_ids even though the cleaner expected the
            # canonical primary_evidence_ids name.
            primary_source = value.get("evidence_ids")
        primary = [
            evidence_id for evidence_id in _plan_id_values(primary_source)
            if evidence_id in known and evidence_id not in assigned
        ]
        assigned.update(primary)
        if "bridge_evidence_ids" in value:
            bridge_source = value.get("bridge_evidence_ids")
        else:
            bridge_source = [
                item for item in api_assignments
                if isinstance(item, dict) and item.get("role") == "bridge"
            ]
        bridges = [
            evidence_id
            for evidence_id in _plan_id_values(bridge_source)
            if evidence_id in known and evidence_id not in primary
        ]
        duration = _plan_minutes(
            value.get("duration_minutes", value.get("target_minutes")),
            default=target_minutes,
        )
        objectives = value.get("learning_objectives")
        if not isinstance(objectives, list):
            objectives = []
        parts.append({
            "position": position,
            "title": str(value.get("title") or f"Part {position}").strip(),
            "focus": str(value.get("focus") or "").strip(),
            "duration_minutes": duration,
            "learning_objectives": [str(item).strip() for item in objectives
                                     if str(item).strip()],
            "primary_evidence_ids": primary,
            "bridge_evidence_ids": bridges,
        })
    # Match the plan-editor invariant: one evidence block can be bridge
    # material in at most two parts. Keep the earliest callbacks.
    bridge_uses: Counter[str] = Counter()
    for part in parts:
        bounded_bridges = []
        for evidence_id in part["bridge_evidence_ids"]:
            if bridge_uses[evidence_id] >= 2:
                continue
            bridge_uses[evidence_id] += 1
            bounded_bridges.append(evidence_id)
        part["bridge_evidence_ids"] = bounded_bridges
    # The model need not enumerate a huge evidence catalog. Assign every
    # non-omitted chunk exactly once in paper order, preserving full coverage
    # and ensuring late pages cannot disappear from a plan.
    omissions, omitted_evidence = _clean_plan_omissions(
        raw.get("omissions"),
        known=set(known),
        importance=importance,
        topics=topics,
        assigned=assigned,
    )
    if known and not assigned and omitted_evidence == set(known):
        # A series cannot consist exclusively of omissions. Retain the first
        # source block as primary teaching material and remove its omission.
        retained = chunks[0].evidence_id
        omitted_evidence.remove(retained)
        omissions = [
            item for item in omissions if item["evidence_id"] != retained
        ]
    for part in parts:
        part["bridge_evidence_ids"] = [
            evidence_id for evidence_id in part["bridge_evidence_ids"]
            if evidence_id not in omitted_evidence
        ]
    unassigned = [
        chunk for chunk in chunks
        if chunk.evidence_id not in assigned
        and chunk.evidence_id not in omitted_evidence
    ]
    chunk_positions = {
        chunk.evidence_id: index for index, chunk in enumerate(chunks)
    }
    for chunk in unassigned:
        # Backfill against the complete paper order, not the shorter unassigned
        # list. Otherwise one omitted final-page result is always placed in
        # Part 1 regardless of where it occurs in the source.
        source_position = chunk_positions[chunk.evidence_id]
        target = min(
            len(parts) - 1,
            (source_position * len(parts)) // max(1, len(chunks)),
        )
        parts[target]["primary_evidence_ids"].append(chunk.evidence_id)
    parts = [part for part in parts if part["primary_evidence_ids"]]
    if prerequisite_evidence_ids:
        # The map does not invent dependency edges, but evidence explicitly
        # classified as prerequisite must be taught before non-prerequisite
        # parts. Preserve the model's relative order within both groups.
        parts.sort(key=lambda part: (
            not bool(
                set(part["primary_evidence_ids"]) & prerequisite_evidence_ids
            ),
            part["position"],
        ))
    for position, part in enumerate(parts, 1):
        part["position"] = position
    primary_counts = Counter(
        evidence_id for part in parts for evidence_id in part["primary_evidence_ids"])
    accounted = set(primary_counts) | omitted_evidence
    if accounted != set(known) or any(
        count != 1 for count in primary_counts.values()
    ):
        raise RuntimeError("paper series plan failed complete, unique primary coverage")
    critical = {eid for eid, value in importance.items() if value == "critical"}
    major = {eid for eid, value in importance.items() if value == "major"}
    for part in parts:
        evidence = [{
            "evidence_id": evidence_id,
            "role": "primary",
            "importance": importance.get(evidence_id, "supporting"),
            "reason": "primary teaching assignment",
        } for evidence_id in part["primary_evidence_ids"]]
        evidence.extend({
            "evidence_id": evidence_id,
            "role": "bridge",
            "importance": importance.get(evidence_id, "supporting"),
            "reason": "bounded recap or callback",
        } for evidence_id in part["bridge_evidence_ids"])
        part["evidence"] = evidence
        part["evidence_ids"] = [item["evidence_id"] for item in evidence]
        assigned = set(part["primary_evidence_ids"])
        part["topics"] = [
            topic["id"] for topic in topics
            if assigned & set(topic.get("evidence_ids", []))
        ]
        part["target_minutes"] = part["duration_minutes"]
    return {
        "schema": PAPER_PLAN_SCHEMA_VERSION,
        "audience": audience,
        "title": str(
            requested_title or raw.get("title")
            or f"{audience.title()} paper series"
        ).strip(),
        "target_minutes": target_minutes,
        "parts": parts,
        "omissions": omissions,
        "topics": topics,
        "critical_topics": [
            topic for topic in topics if topic.get("importance") == "critical"
        ],
        "coverage": {
            "total_evidence_blocks": len(known),
            "assigned_primary_blocks": len(primary_counts),
            "omitted_supporting_blocks": len(omitted_evidence),
            "critical_total": len(critical),
            "critical_assigned": len(critical & set(primary_counts)),
            "major_total": len(major),
            "major_assigned": len(major & set(primary_counts)),
            "complete_for_approval": (
                critical <= set(primary_counts)
                and accounted == set(known)
            ),
        },
    }


def generate_series_plan(job_id: int, project_id: int, series_id: int,
                         *, force: bool = False) -> dict[str, Any]:
    bundle = latest_analysis_bundle(project_id)
    if bundle is None:
        bundle = build_analysis_bundle(job_id, project_id)
    with get_session() as session:
        source = paper_store.paper_source_for_project(session, project_id)
        paper_store.require_analysis_ready(source)
        series = session.get(PaperSeries, series_id)
        if not series or series.project_id != project_id:
            raise ValueError("paper audience track not found")
        if series.status != "draft":
            raise ValueError("only a draft audience track can be replanned")
        saved_plan = _json(series.plan_json, {})
        if series.plan_version > 0 and not force:
            # Saved draft plans may predate the current schema and contain user
            # edits. Only an explicit replan may replace them.
            return saved_plan
        start_plan_version = int(series.plan_version or 0)
        chunks = session.exec(select(PaperChunk).where(
            PaperChunk.source_id == source.id).order_by(PaperChunk.chunk_index)).all()
        local_only = bool(source.local_only)
        audience = series.audience
        requested_title = series.title
        requested_target_minutes = _plan_minutes(series.target_minutes)
        user_guidance = series.user_guidance
    with llm.project_scope(project_id, local_only=local_only):
        provider, model = llm.resolve_model("paper_plan")
    upstream_analysis = paper_analysis_lineage(bundle)
    required_primary_ids = sorted(
        set(bundle.get("critical_evidence_ids", []))
        | set(bundle.get("major_evidence_ids", []))
    )
    prerequisite_ids = sorted({
        str(evidence_id)
        for topic in bundle.get("topics", [])
        if isinstance(topic, dict) and topic.get("type") == "prerequisites"
        for evidence_id in topic.get("evidence_ids", [])
        if evidence_id
    })
    chunk_by_id = {chunk.evidence_id: chunk for chunk in chunks}
    context = {
        "audience": audience,
        "requested_title": requested_title,
        "requested_target_minutes": requested_target_minutes,
        "user_guidance": user_guidance,
        "source": paper_source_signature_from_model(source),
        "coverage": bundle["coverage"],
        "importance_counts": dict(Counter(bundle["evidence_importance"].values())),
        "required_primary_evidence_ids": required_primary_ids,
        "prerequisite_evidence_ids": prerequisite_ids,
        "required_evidence_ledger": [{
            "evidence_id": evidence_id,
            "page_number": chunk_by_id[evidence_id].page_number,
            "kind": chunk_by_id[evidence_id].kind,
            "importance": bundle["evidence_importance"].get(
                evidence_id, "supporting"),
        } for evidence_id in required_primary_ids if evidence_id in chunk_by_id],
        "hierarchical_context": bundle["hierarchical_context"],
        "upstream_analysis": upstream_analysis,
    }
    prompt_context = _compact_map_for_prompt(context)
    if _estimated_tokens(prompt_context) > _paper_analysis_settings()["final_context_tokens"] + 500:
        raise RuntimeError(
            "paper planning context exceeds the configured final budget; increase "
            "paper.analysis.final_context_tokens")
    input_hash = _digest(context)
    config_hash = _digest({
        "schema": 3,
        "contract_version": PAPER_PLAN_CONTRACT_VERSION,
        "audience": audience,
        "prompt": _digest(_paper_prompt("paper_plan", PAPER_PLAN_PROMPT)),
        "execution": paper_model_execution_signature(
            "paper_plan",
            provider,
            model,
            local_only=local_only,
            json_format=True,
        ),
        "source_hash": source.source_hash,
        "parser_config_hash": source.parser_config_hash,
        "upstream_analysis": upstream_analysis,
    })
    with get_session() as session:
        cached = _cache_get(
            session,
            source_id=source.id,
            purpose=f"audience_plan:{audience}",
            input_hash=input_hash,
            config_hash=config_hash,
        )
    if cached:
        raw = cached[0]
    else:
        progress(job_id, f"planning {audience} paper series ({model})")
        raw = llm.complete_json(
            "paper_plan",
            _paper_prompt("paper_plan", PAPER_PLAN_PROMPT),
            "AUDIENCE AND COMPLETE HIERARCHICAL EVIDENCE (untrusted data):\n"
            + json.dumps(prompt_context, sort_keys=True, ensure_ascii=False),
            max_tokens=5_000,
            provider=provider,
            model=model,
            local_only=local_only,
        )
        with get_session() as session:
            _cache_put(
                session,
                project_id=project_id,
                source_id=source.id,
                purpose=f"audience_plan:{audience}",
                input_hash=input_hash,
                config_hash=config_hash,
                provider=provider,
                model=model,
                body=raw,
                evidence_ids=bundle["leaf_evidence_ids"],
            )
    plan = _clean_plan(
        raw,
        audience=audience,
        chunks=chunks,
        importance=bundle["evidence_importance"],
        topics=bundle.get("topics", []),
        requested_target_minutes=requested_target_minutes,
        requested_title=requested_title,
        prerequisite_evidence_ids=set(prerequisite_ids),
    )
    plan["analysis_lineage"] = upstream_analysis
    with get_session() as session:
        # Serialize the final compare-and-swap plus part replacement. Without
        # a write reservation, a long model call could overwrite an edit or
        # approval committed after planning began.
        session.exec(text("BEGIN IMMEDIATE"))
        series = session.get(PaperSeries, series_id)
        if not series or series.project_id != project_id:
            raise ValueError("paper audience track was deleted while planning")
        if series.status != "draft":
            raise ValueError(
                "paper audience track changed status while planning; reload "
                "before replanning"
            )
        if int(series.plan_version or 0) != start_plan_version:
            raise ValueError(
                "paper audience plan changed while generation was running; "
                "the newer plan was preserved"
            )
        old_parts = session.exec(select(PaperSeriesPart).where(
            PaperSeriesPart.series_id == series_id)).all()
        if any(
            part.status != "planned"
            or part.guide_status != "pending"
            or part.script_status != "pending"
            or part.audio_status != "pending"
            for part in old_parts
        ):
            raise ValueError("generated parts are locked; edit future parts only")
        for part in old_parts:
            links = session.exec(select(PaperPartEvidence).where(
                PaperPartEvidence.part_id == part.id)).all()
            for link in links:
                session.delete(link)
            session.delete(part)
        session.flush()
        chunk_by_evidence = {chunk.evidence_id: chunk for chunk in chunks}
        for item in plan["parts"]:
            part = PaperSeriesPart(
                series_id=series_id,
                position=item["position"],
                title=item["title"],
                focus=item["focus"],
                target_minutes=item["target_minutes"],
            )
            session.add(part)
            session.flush()
            item["id"] = part.id
            for assignment in item["evidence"]:
                evidence_id = assignment["evidence_id"]
                chunk = chunk_by_evidence[evidence_id]
                session.add(PaperPartEvidence(
                    part_id=part.id,
                    chunk_id=chunk.id,
                    role=assignment["role"],
                    importance=assignment["importance"],
                    reason=assignment["reason"],
                ))
        series.title = plan["title"]
        series.target_minutes = plan["target_minutes"]
        series.max_parts = 5
        series.plan_version = start_plan_version + 1
        series.plan_json = json.dumps(plan, sort_keys=True, ensure_ascii=False)
        series.plan_hash = _digest({
            "source_hash": source.source_hash,
            "audience": audience,
            "plan": plan,
        })
        series.updated = utcnow()
        session.add(series)
        session.commit()
    return plan


def draft_selected_series_plans(job_id: int, project_id: int) -> list[int]:
    with get_session() as session:
        series_ids = list(session.exec(select(PaperSeries.id).where(
            PaperSeries.project_id == project_id,
            PaperSeries.status == "draft",
            PaperSeries.plan_version == 0,
        )).all())
    for series_id in series_ids:
        generate_series_plan(job_id, project_id, int(series_id))
    return [int(value) for value in series_ids]


# Import only after helper definitions. ``celery_app`` registers paper_series,
# which deliberately reuses these helpers; importing it at module top would
# expose a partially initialized module during direct test/API imports.
from .celery_app import celery  # noqa: E402


@celery.task(name="paper_extract", queue="paper")
@pipeline_task
def paper_extract(job_id: int, project_id: int):
    with get_session() as session:
        project = get_project(session, project_id)
        if project.source_type != "paper":
            raise ValueError("paper extraction is only applicable to paper projects")
        source = paper_store.paper_source_for_project(session, project_id)
        if source is None:
            raise RuntimeError("paper source metadata is missing")
        source.privacy_locked = True
        source.status = "extracting"
        source.error = ""
        source.updated = utcnow()
        session.add(source)
        session.commit()
        session.refresh(source)
        path = paper_store.paper_source_path(source)
        config = extraction_config(source)
    progress(job_id, "extracting PDF locally with Docling and Tesseract")
    try:
        result = paper_store.extract_pdf(path, config)
        with get_session() as session:
            source = paper_store.paper_source_for_project(session, project_id)
            paper_store.persist_extraction(session, source, result)
            session.refresh(source)
            source_id = source.id
        if get_setting("search.semantic_enabled", False):
            try:
                from .search import index_paper_chunks

                index_paper_chunks.delay(source_id)
            except Exception:
                log.warning(
                    "could not queue semantic indexing for paper source %s",
                    source_id,
                    exc_info=True,
                )
        report_id = write_extraction_report(project_id, source)
        progress(
            job_id,
            f"extracted {result.page_count} pages into {len(result.evidence)} evidence blocks",
        )
        return {"source_id": source_id, "artifact_id": report_id,
                "analysis_blocked": bool(paper_store.extraction_blockers(source))}
    except Exception as exc:
        with get_session() as session:
            source = paper_store.paper_source_for_project(session, project_id)
            if source:
                source.status = "error"
                source.error = str(exc)[:2_000]
                source.updated = utcnow()
                session.add(source)
                session.commit()
        raise


@celery.task(name="paper_analyze")
@pipeline_task
def paper_analyze(job_id: int, project_id: int):
    # Recheck quality here: run_all can enqueue this immediately after the
    # extraction task, without passing through an HTTP validation endpoint.
    with get_session() as session:
        project = get_project(session, project_id)
        if project.source_type != "paper":
            raise ValueError("paper analysis is only applicable to paper projects")
        source = paper_store.paper_source_for_project(session, project_id)
        if source is None:
            raise RuntimeError("paper source metadata is missing")
        paper_store.require_analysis_ready(source)
    bundle = build_analysis_bundle(job_id, project_id)
    with get_session() as session:
        source = paper_store.paper_source_for_project(session, project_id)
        chunks = session.exec(select(PaperChunk).where(
            PaperChunk.source_id == source.id).order_by(PaperChunk.chunk_index)).all()
    upstream_analysis = paper_analysis_lineage(bundle)
    coverage_body = _coverage_body(source, bundle, chunks)
    coverage_input_hash = _digest({
        "source_hash": source.source_hash,
        "coverage": bundle["coverage"],
        "importance": bundle["evidence_importance"],
        "acknowledged_pages": _json(source.acknowledged_pages, []),
        "upstream_analysis": upstream_analysis,
    })
    coverage_id = _write_root_artifact(
        project_id,
        artifact_type="paper_coverage",
        title="Paper analysis coverage",
        body=coverage_body,
        source=source,
        function="paper_analyze",
        provider="deterministic",
        model="paper-map-schema-1",
        prompt="deterministic coverage report schema 1",
        evidence_ids=bundle["leaf_evidence_ids"],
        input_hash=coverage_input_hash,
        upstream_analysis=upstream_analysis,
        extra_meta={"coverage": bundle["coverage"]},
    )
    shared_ids = {
        "paper_argument_map": _synthesize_shared(
            job_id, project_id, source, chunks, bundle,
            artifact_type="paper_argument_map",
            title="Paper claim and argument map",
            prompt_text=PAPER_ARGUMENT_PROMPT,
        ),
        "paper_mindmap": _synthesize_shared(
            job_id, project_id, source, chunks, bundle,
            artifact_type="paper_mindmap",
            title="Whole-paper mind map",
            prompt_text=PAPER_MINDMAP_PROMPT,
        ),
        "paper_quick_references": _synthesize_shared(
            job_id, project_id, source, chunks, bundle,
            artifact_type="paper_quick_references",
            title="Paper quick references",
            prompt_text=PAPER_QUICKREF_PROMPT,
        ),
    }
    planned_series = draft_selected_series_plans(job_id, project_id)
    return {
        "paper_coverage": coverage_id,
        **shared_ids,
        "planned_series_ids": planned_series,
        "mapped_evidence_blocks": bundle["coverage"]["mapped_evidence_blocks"],
    }


@celery.task(name="paper_plan")
@pipeline_task
def paper_plan(job_id: int, project_id: int, series_id: int | None = None):
    if series_id is None:
        return draft_selected_series_plans(job_id, project_id)
    return generate_series_plan(job_id, project_id, series_id, force=True)
