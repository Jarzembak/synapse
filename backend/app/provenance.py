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
    "deepdive_claude": "deepdive_claude",
    "deepdive_gemini": "deepdive_gemini",
    "deepdive_merged": "merge",
    "podcast_script": "podcast_script",
    "podcast_audio": "tts",
    "trimmed_audio": "trim",
    "mindmap": "mindmap",
}

STEP_INPUT_TYPES: dict[str, list[str]] = {
    "download": [],
    "transcribe": [],
    "correct": ["transcript"],
    "summarize": ["corrected", "transcript"],
    "deepdive_claude": ["corrected", "transcript"],
    "deepdive_gemini": ["corrected", "transcript"],
    "merge": ["deepdive_claude", "deepdive_gemini"],
    "quickref": ["deepdive_merged"],
    "podcast_script": ["deepdive_merged"],
    "tts": ["podcast_script"],
    "trim": ["transcript"],
    "mindmap": ["deepdive_merged"],
}

STEP_FUNCTION = {
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
}

STEP_PROMPTS: dict[str, list[str]] = {
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
    requested = list(STEP_INPUT_TYPES.get(step, []))
    artifacts = session.exec(
        select(Artifact).where(Artifact.project_id == project.id)
    ).all()
    by_type = {artifact.type: artifact for artifact in artifacts}
    # Corrected/raw is a fallback pair: use only the richest one that exists.
    if requested == ["corrected", "transcript"]:
        requested = ["corrected"] if "corrected" in by_type else ["transcript"]
    return [(artifact_type, by_type.get(artifact_type)) for artifact_type in requested]


def upstream_state(session: Session, project: Project, step: str) -> list[dict]:
    if step in {"ingest", "download", "transcribe"}:
        return [{"type": "source", "hash": _digest(_source_signature(project))}]
    out: list[dict] = []
    for artifact_type, artifact in _resolved_inputs(session, project, step):
        out.append({
            "type": artifact_type,
            "artifact_id": artifact.id if artifact else None,
            "hash": _body_hash(artifact) if artifact else "missing",
        })
    return out


def effective_config(step: str) -> dict:
    from . import llm
    from .tasks.prompts import get_prompt

    value: dict = {"step": step}
    function = STEP_FUNCTION.get(step)
    if function:
        provider, model = llm.resolve_model(function)
        value.update({
            "function": function,
            "provider": provider,
            "model": model,
            "params": get_setting(f"params.{function}") or {},
        })
    prompts = STEP_PROMPTS.get(step, [])
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
    return value


def signatures(session: Session, project: Project, step: str) -> tuple[str, str, dict]:
    upstream = upstream_state(session, project, step)
    config = effective_config(step)
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

    if step == "ingest":
        marker = get_setting(f"step_signature.{project.id}.ingest")
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
        select(Artifact).where(Artifact.project_id == project.id, Artifact.type == output)
    ).first()
    if not artifact:
        return False
    if not artifact.input_hash or not artifact.config_hash:
        return _legacy_stale(session, project, artifact, step)
    expected_input, expected_config, _detail = signatures(session, project, step)
    return artifact.input_hash != expected_input or artifact.config_hash != expected_config
