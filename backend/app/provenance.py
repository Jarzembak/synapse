"""Artifact provenance and staleness evaluation.

Each output records the substantive upstream content and the effective model,
prompt, and tuning configuration that produced it.  Changing an input does not
delete downstream work; it marks it stale and lets the user rerun the affected
subgraph deliberately.
"""
from __future__ import annotations

import hashlib
import json

from sqlmodel import Session, select

from .config import advanced
from .models import Artifact, Project
from .settings_store import get_setting, set_setting

ARTIFACT_STEP = {
    "source_video": "download",
    "source_audio": "ingest",
    "transcript": "transcribe",
    "corrected": "correct",
    "summary": "summarize",
    "repo_inventory": "repo_inventory",
    "repo_usage": "repo_usage",
    "repo_architecture": "repo_architecture",
    "repo_expertise": "repo_expertise",
    "repo_environment": "repo_environment",
    "deepdive_claude": "deepdive_claude",
    "deepdive_gemini": "deepdive_gemini",
    "deepdive_merged": "merge",
    "podcast_script": "podcast_script",
    "podcast_audio": "tts",
    "trimmed_audio": "trim",
    "mindmap": "mindmap",
    "source_paper": "paper_extract",
    "paper_extraction_report": "paper_extract",
    "paper_coverage": "paper_analyze",
    "paper_argument_map": "paper_analyze",
    "paper_mindmap": "paper_analyze",
    "paper_quick_references": "paper_analyze",
}

STEP_INPUT_TYPES: dict[str, list[str]] = {
    "repo_snapshot": [],
    "repo_inventory": [],
    "download": [],
    "transcribe": [],
    "correct": ["transcript"],
    "summarize": ["corrected", "transcript"],
    "repo_usage": ["repo_inventory"],
    "repo_architecture": ["repo_inventory"],
    "repo_expertise": ["repo_inventory"],
    "repo_environment": ["repo_inventory"],
    "deepdive_claude": ["corrected", "transcript"],
    "deepdive_gemini": ["corrected", "transcript"],
    "merge": ["deepdive_claude", "deepdive_gemini"],
    "quickref": ["deepdive_merged"],
    "podcast_script": ["deepdive_merged"],
    "tts": ["podcast_script"],
    "trim": ["transcript"],
    "mindmap": ["deepdive_merged"],
    "paper_extract": [],
    "paper_analyze": ["paper_extraction_report"],
}

STEP_FUNCTION = {
    "repo_inventory": "repository_inventory",
    "transcribe": "asr",
    "correct": "correct",
    "summarize": "summarize",
    "deepdive_claude": "deepdive_claude",
    "deepdive_gemini": "deepdive_gemini",
    "merge": "merge",
    "quickref": "quickref",
    "podcast_script": "podcast_script",
    "tts": "tts",
    "trim": "trim_spans",
    "mindmap": "mindmap",
    "repo_usage": "repository_usage",
    "repo_architecture": "repository_architecture",
    "repo_expertise": "repository_expertise",
    "repo_environment": "repository_environment",
    "paper_analyze": "paper_synthesis",
}

STEP_PROMPTS: dict[str, list[str]] = {
    "repo_inventory": ["repository_map", "repository_reduce", "repository_inventory"],
    "correct": ["correct"],
    "summarize": ["summary"],
    "deepdive_claude": ["deepdive"],
    "deepdive_gemini": ["deepdive"],
    "merge": ["merge"],
    "quickref": [
        "extract_entities", "quickref_merge", "quickref_tool",
        "quickref_technique", "quickref_concept", "quickref_technology",
    ],
    "podcast_script": ["podcast_outline", "podcast_segment"],
    "trim": ["trim_spans"],
    "mindmap": ["mindmap"],
    "repo_usage": ["repository_reduce", "repository_usage"],
    "repo_architecture": ["repository_reduce", "repository_architecture"],
    "repo_expertise": ["repository_reduce", "repository_expertise"],
    "repo_environment": ["repository_reduce", "repository_environment"],
    "paper_analyze": ["paper_map", "paper_reduce", "paper_shared", "paper_plan"],
}

REPOSITORY_INPUT_TYPES: dict[str, list[str]] = {
    "repo_snapshot": [],
    "repo_inventory": [],
    "summarize": ["repo_inventory"],
    "repo_usage": ["repo_inventory"],
    "repo_architecture": ["repo_inventory"],
    "repo_expertise": ["repo_inventory"],
    "repo_environment": ["repo_inventory"],
    "deepdive_claude": [
        "summary", "repo_usage", "repo_architecture",
        "repo_expertise", "repo_environment",
    ],
    "deepdive_gemini": [
        "summary", "repo_usage", "repo_architecture",
        "repo_expertise", "repo_environment",
    ],
}

REPOSITORY_STEP_FUNCTION = {
    **STEP_FUNCTION,
    "summarize": "repository_overview",
}

REPOSITORY_STEP_PROMPTS = {
    **STEP_PROMPTS,
    "repo_inventory": ["repository_map", "repository_reduce", "repository_inventory"],
    "summarize": ["repository_map", "repository_reduce", "repository_overview"],
    "repo_usage": ["repository_map", "repository_reduce", "repository_usage"],
    "repo_architecture": [
        "repository_map", "repository_reduce", "repository_architecture"],
    "repo_expertise": ["repository_map", "repository_reduce", "repository_expertise"],
    "repo_environment": [
        "repository_map", "repository_reduce", "repository_environment"],
    "deepdive_claude": [
        "repository_map", "repository_reduce", "repository_deepdive_a"],
    "deepdive_gemini": [
        "repository_map", "repository_reduce", "repository_deepdive_b"],
    "merge": ["repository_merge"],
}


def _digest(value) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _body_hash(artifact: Artifact) -> str:
    from . import library

    try:
        _meta, body = library.read_doc(artifact.path)
    except (FileNotFoundError, OSError):
        return "missing"
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _source_signature(project: Project) -> dict:
    value = {"source": project.source, "source_type": project.source_type}
    if project.source_type == "github":
        try:
            from .db import get_session
            from .repository import (SCANNER_VERSION, current_repository_snapshot,
                                     repository_scan_config_hash,
                                     repository_source_for_project)

            with get_session() as session:
                source = repository_source_for_project(session, project.id)
                snapshot = current_repository_snapshot(session, project.id)
                value.update({
                    "canonical_url": getattr(source, "canonical_url", project.source),
                    "requested_ref": getattr(source, "requested_ref", ""),
                    "pending_sha": getattr(source, "pending_sha", ""),
                    "resolved_sha": getattr(snapshot, "resolved_sha", "missing"),
                    "include_paths": getattr(source, "include_paths", "[]"),
                    "exclude_paths": getattr(source, "exclude_paths", "[]"),
                    "scanner_version": SCANNER_VERSION,
                    "scanner_config_hash": (
                        repository_scan_config_hash(source) if source else "missing"),
                    "stored_scanner_version": getattr(snapshot, "scanner_version", ""),
                    "stored_scan_config_hash": getattr(snapshot, "scan_config_hash", ""),
                })
        except Exception:
            value["missing"] = True
        return value
    if project.source_type == "paper":
        try:
            from .db import get_session
            from .models import PaperSource

            with get_session() as session:
                source = session.exec(select(PaperSource).where(
                    PaperSource.project_id == project.id
                )).first()
                if source is None:
                    value["missing"] = True
                else:
                    value.update({
                        "source_hash": source.source_hash,
                        "source_bytes": source.size_bytes,
                        "page_count": source.page_count,
                        "ocr_languages": source.ocr_languages,
                        "local_only": source.local_only,
                        "parser_version": source.parser_version,
                        "parser_config_hash": source.parser_config_hash,
                        "acknowledged_pages": source.acknowledged_pages,
                    })
        except Exception:
            value["missing"] = True
        return value
    if project.source_type in {"local", "upload"}:
        try:
            from .tasks.media import resolve_local_source, resolve_uploaded_source

            path = (resolve_uploaded_source(project.slug, project.source)
                    if project.source_type == "upload"
                    else resolve_local_source(project.source))
            stat = path.stat()
            value.update({"size": stat.st_size, "mtime_ns": stat.st_mtime_ns})
        except (OSError, ValueError):
            value["missing"] = True
    return value


def _resolved_inputs(session: Session, project: Project,
                     step: str) -> list[tuple[str, Artifact | None]]:
    """Resolve fallback inputs exactly as the pipeline does at execution time."""
    requested = list(
        (REPOSITORY_INPUT_TYPES if project.source_type == "github" else STEP_INPUT_TYPES)
        .get(step, STEP_INPUT_TYPES.get(step, [])))
    artifacts = session.exec(
        select(Artifact).where(
            Artifact.project_id == project.id,
            Artifact.paper_series_id == None,  # noqa: E711
            Artifact.paper_part_id == None,  # noqa: E711
        )
    ).all()
    by_type = {artifact.type: artifact for artifact in artifacts}
    # Corrected/raw is a fallback pair: use only the richest one that exists.
    if requested == ["corrected", "transcript"]:
        requested = ["corrected"] if "corrected" in by_type else ["transcript"]
    return [(artifact_type, by_type.get(artifact_type)) for artifact_type in requested]


def upstream_state(session: Session, project: Project, step: str) -> list[dict]:
    if step in {"ingest", "download", "transcribe", "repo_snapshot", "repo_inventory",
                "paper_extract"}:
        return [{"type": "source", "hash": _digest(_source_signature(project))}]
    out: list[dict] = []
    if project.source_type == "github":
        source = _source_signature(project)
        out.append({"type": "repository_source", "hash": _digest(source), **source})
    elif project.source_type == "paper":
        source = _source_signature(project)
        out.append({"type": "paper_source", "hash": _digest(source), **source})
    for artifact_type, artifact in _resolved_inputs(session, project, step):
        out.append({
            "type": artifact_type,
            "artifact_id": artifact.id if artifact else None,
            "hash": _body_hash(artifact) if artifact else "missing",
        })
    return out


def effective_config(step: str, project: Project | None = None) -> dict:
    from . import llm
    from .tasks.prompts import get_prompt

    value: dict = {"step": step}
    repository = bool(project and project.source_type == "github")
    paper = bool(project and project.source_type == "paper")
    functions = REPOSITORY_STEP_FUNCTION if repository else STEP_FUNCTION
    function = functions.get(step)
    if function:
        with llm.project_scope(
                project.id if project else None,
                local_only=(True if repository else None)):
            provider, model = llm.resolve_model(function)
        value.update({
            "function": function,
            "provider": provider,
            "model": model,
            "params": get_setting(f"params.{function}") or {},
        })
        if provider in ("ollama", "openai_compat"):
            # only knobs that change what the model sees/produces are hashed;
            # timeout and keep_alive stay out. json_mode grammar-constrains
            # decoding, so it counts — but only on steps that request JSON.
            local = advanced("local")
            local_sig: dict = {}
            if provider == "ollama":
                local_sig["num_ctx"] = local.get("num_ctx")
                if not repository:
                    # repository steps force think off, so the knob cannot
                    # change their output
                    local_sig["think"] = local.get("think")
            if repository or step in {"trim", "mindmap", "podcast_script",
                                      "quickref"}:
                # repository map/reduce phases request native JSON too
                local_sig["json_mode"] = local.get("json_mode")
            if local_sig:
                value["local"] = local_sig
    prompts = (REPOSITORY_STEP_PROMPTS if repository else STEP_PROMPTS).get(step, [])
    if prompts:
        value["prompts"] = {
            name: hashlib.sha256(get_prompt(name).encode("utf-8")).hexdigest()
            for name in prompts
        }
    if step in {"correct", "summarize", "deepdive_claude", "deepdive_gemini",
                "merge", "quickref", "podcast_script", "mindmap"}:
        value["pipeline"] = advanced("pipeline")
    if step in {"tts", "trim"}:
        value["audio"] = advanced("audio")
        value["voices"] = {
            "kokoro": get_setting("tts.voices") or {},
            "piper": get_setting("tts.piper_voices") or {},
            "gemini": get_setting("tts.gemini_voices") or {},
        }
    if step == "transcribe":
        value["asr"] = advanced("asr")
        value["compute"] = advanced("compute")
    if step == "download":
        value["max_height"] = get_setting("download.max_height", 1080)
    if step == "correct":
        value["glossary"] = get_setting("glossary", [])
    if step == "quickref":
        value["categories"] = get_setting("quickref.custom_categories") or []
    if repository:
        with llm.project_scope(project.id, local_only=True):
            map_provider, map_model = llm.resolve_model("repository_map")
        value["repository"] = {
            "local_model": get_setting("repository.local_model", "qwen3:8b"),
            "static_only": True,
            "reasoning_effort": "none",
            "source": _source_signature(project),
            "analysis": get_setting("repository.analysis") or {},
            "map_model": {"provider": map_provider, "model": map_model,
                          "params": get_setting("params.repository_map") or {}},
        }
    if paper and project:
        models = {}
        for paper_function in (
            "paper_map", "paper_reduce", "paper_synthesis", "paper_plan",
            "paper_script", "paper_memory",
        ):
            with llm.project_scope(project.id):
                provider, model = llm.resolve_model(paper_function)
            models[paper_function] = {
                "provider": provider,
                "model": model,
                "params": get_setting(f"params.{paper_function}") or {},
            }
        value["paper"] = {
            "source": _source_signature(project),
            "models": models,
            "analysis": get_setting("paper.analysis") or {},
        }
        # Avoid duplicating pydantic settings imports at module load and keep
        # the exact configured limits in the signature.
        from .config import settings

        value["paper"]["limits"] = {
            "max_upload_bytes": settings.max_paper_upload_bytes,
            "max_pages": settings.max_paper_pages,
            "max_extracted_chars": settings.max_paper_extracted_chars,
            "target_minutes": settings.paper_target_minutes,
            "max_parts": settings.paper_max_parts,
        }
    return value


def signatures(session: Session, project: Project, step: str) -> tuple[str, str, dict]:
    upstream = upstream_state(session, project, step)
    config = effective_config(step, project)
    return _digest(upstream), _digest(config), {"step": step, "upstream": upstream,
                                                "config": config}


def capture_for_artifact(session: Session, project_id: int | None,
                         artifact_type: str) -> tuple[str, str, dict]:
    if not project_id:
        return "", "", {}
    project = session.get(Project, project_id)
    step = ARTIFACT_STEP.get(artifact_type)
    if not project or not step:
        return "", "", {}
    return signatures(session, project, step)


def record_nonartifact_step(session: Session, project: Project, step: str) -> None:
    input_hash, config_hash, detail = signatures(session, project, step)
    set_setting(f"step_signature.{project.id}.{step}", {
        "input_hash": input_hash, "config_hash": config_hash, "detail": detail,
    })


def _legacy_stale(session: Session, project: Project, artifact: Artifact,
                  step: str) -> bool:
    upstream = upstream_state(session, project, step)
    ids = [item.get("artifact_id") for item in upstream if item.get("artifact_id")]
    if not ids:
        return False
    rows = session.exec(select(Artifact).where(Artifact.id.in_(ids))).all()
    return any(row.updated > artifact.updated for row in rows)


def is_step_stale(session: Session, project: Project, step: str,
                  *, _seen: set[str] | None = None) -> bool:
    # Staleness is transitive even before a stale upstream artifact is rerun.
    # Example: changing the correction glossary makes `correct` stale; every
    # consumer of that corrected transcript must also be shown as stale now,
    # rather than only after correction produces a different body.
    seen = set(_seen or ())
    if step in seen:
        return False
    seen.add(step)
    for artifact_type, artifact in _resolved_inputs(session, project, step):
        upstream_step = ARTIFACT_STEP.get(artifact_type)
        if (artifact is not None and upstream_step
                and is_step_stale(session, project, upstream_step, _seen=seen)):
            return True

    if step in {"ingest", "repo_snapshot"}:
        marker = get_setting(f"step_signature.{project.id}.{step}")
        if not marker:
            return False
        expected_input, expected_config, _detail = signatures(session, project, step)
        return marker.get("input_hash") != expected_input or marker.get("config_hash") != expected_config
    if step == "quickref":
        marker = get_setting(f"step_signature.{project.id}.quickref")
        if not marker:
            return False
        expected_input, expected_config, _detail = signatures(session, project, step)
        return marker.get("input_hash") != expected_input or marker.get("config_hash") != expected_config

    from .tasks.orchestrate import STEP_OUTPUT

    output = STEP_OUTPUT.get(step)
    if not output:
        return False
    artifact = session.exec(
        select(Artifact).where(
            Artifact.project_id == project.id,
            Artifact.paper_series_id == None,  # noqa: E711
            Artifact.paper_part_id == None,  # noqa: E711
            Artifact.type == output,
        )
    ).first()
    if not artifact:
        return False
    if not artifact.input_hash or not artifact.config_hash:
        return _legacy_stale(session, project, artifact, step)
    expected_input, expected_config, _detail = signatures(session, project, step)
    return artifact.input_hash != expected_input or artifact.config_hash != expected_config
