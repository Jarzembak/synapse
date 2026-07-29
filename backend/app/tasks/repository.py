"""Static, hierarchical repository-analysis tasks.

Raw repository text is handled only as one scanner-produced, line-addressed
evidence chunk at a time.  Chunk summaries are cached by content and analysis
configuration; every later synthesis operates on those structured summaries,
never on a concatenated or prefix-truncated pseudo-transcript.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from collections import defaultdict, deque
from pathlib import PurePosixPath
from urllib.parse import quote

from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from .. import library, llm, repository as repository_store
from ..config import advanced
from ..db import get_session
from ..models import (
    Artifact, Job, RepositoryChunk, RepositorySynthesisCache, utcnow,
)
from ..settings_store import get_setting
from .celery_app import celery
from .common import artifact_body, auto_tag, get_project, pipeline_task, progress
from .prompts import get_prompt

log = logging.getLogger("synapse.repository.pipeline")

_VISIBLE_CITATION = re.compile(r"\[E:([A-Za-z0-9][A-Za-z0-9_.:-]{0,160})\]")
_HIDDEN_CITATION = re.compile(r"<!--\s*E:([A-Za-z0-9][A-Za-z0-9_.:-]{0,160})\s*-->")
_IMPORTANT_NAMES = {
    "readme", "contributing", "architecture", "changelog", "makefile",
    "dockerfile", "compose.yml", "compose.yaml", "docker-compose.yml",
    "docker-compose.yaml", "package.json", "pyproject.toml", "setup.py",
    "requirements.txt", "cargo.toml", "go.mod", "pom.xml", "build.gradle",
    "gemfile", "composer.json", "environment.yml", ".env.example",
}
_MANIFEST_SUFFIXES = {
    ".lock", ".toml", ".yaml", ".yml", ".json", ".ini", ".cfg",
}
_SHARED_REDUCTION_PURPOSE = "shared_analysis"
_MAP_CONTRACT_VERSION = 2
_REDUCTION_CONTRACT_VERSION = 2
_MAX_REDUCTION_LEVELS = 12
_MAX_DIAGNOSTIC_ATTEMPTS = 48
_MAX_DIAGNOSTIC_DETAIL_CHARS = 500


class RepositoryMapContractError(RuntimeError):
    """The model returned no usable structure for an admitted evidence chunk."""


class RepositoryReductionContractError(RuntimeError):
    """The model returned parseable data that violates the lossless contract."""


class RepositoryReductionOversizedError(RuntimeError):
    """The model returned data too large to make hierarchical progress."""


def _digest(value) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _json(value, default):
    if isinstance(value, type(default)):
        return value
    try:
        parsed = json.loads(value or "")
        return parsed if isinstance(parsed, type(default)) else default
    except (TypeError, json.JSONDecodeError):
        return default


def _analysis_limits() -> dict[str, int]:
    configured = get_setting("repository.analysis") or {}
    scan = repository_store.repository_scan_settings()

    def bounded(name: str, default: int, low: int, high: int) -> int:
        try:
            return max(low, min(int(configured.get(name, default)), high))
        except (TypeError, ValueError):
            return default

    return {
        # Map coverage is part of the scanner/preflight policy shown in
        # Settings. Keep one authoritative budget rather than a hidden second
        # set of defaults in the generation task.
        "max_chunks": max(1, min(int(scan["max_map_chunks"]), 10000)),
        "max_input_chars": max(
            10_000, min(int(scan["max_map_input_chars"]), 100_000_000)),
        "max_new_map_calls": bounded(
            "max_new_map_calls", int(scan["max_map_chunks"]), 1, 10000),
        "reduce_batch_chars": bounded(
            "reduce_batch_chars", 20_000, 4_000, 200_000),
        "reduce_max_tokens": bounded(
            "reduce_max_tokens", 1_600, 400, 4_000),
        "reduce_max_subdivision_depth": bounded(
            "reduce_max_subdivision_depth", 6, 1, 10),
        # Conservative room for a substantial model response within common
        # local-model context windows. Every final synthesis source shares it.
        "final_input_chars": bounded(
            "final_input_chars", 64_000, 32_000, 160_000),
    }


def repository_analysis_signature() -> dict:
    """Return resolved, output-affecting repository analysis contracts."""
    return {
        "limits": _analysis_limits(),
        "map_contract_version": _MAP_CONTRACT_VERSION,
        "reduction_contract_version": _REDUCTION_CONTRACT_VERSION,
    }


def _priority(item: dict) -> int:
    path = str(item.get("path") or "")
    pure = PurePosixPath(path)
    name = pure.name.casefold()
    stem = pure.stem.casefold()
    score = int(item.get("analysis_priority") or 0)
    if name in _IMPORTANT_NAMES or stem in {"readme", "architecture", "contributing"}:
        score += 1000
    if name.endswith(tuple(_MANIFEST_SUFFIXES)) or "lock" in name:
        score += 700
    if any(part.casefold() in {"docs", ".github", "config", "configs"}
           for part in pure.parts[:-1]):
        score += 500
    if stem in {"main", "app", "index", "server", "cli", "manage", "entrypoint"}:
        score += 450
    if any(part.casefold() in {"test", "tests", "spec", "specs"}
           for part in pure.parts[:-1]) or name.startswith(("test_", "spec_")):
        score += 300
    return score


def _ordered_evidence(evidence: list[dict]) -> list[dict]:
    """Priority bands with directory round-robin for representative coverage."""
    bands: dict[int, dict[str, deque[dict]]] = defaultdict(lambda: defaultdict(deque))
    for item in evidence:
        path = str(item.get("path") or "")
        top = PurePosixPath(path).parts[0] if path else ""
        band = _priority(item) // 100
        bands[band][top].append(item)
    ordered: list[dict] = []
    for band in sorted(bands, reverse=True):
        groups = bands[band]
        for queue in groups.values():
            values = sorted(queue, key=lambda row: (
                str(row.get("path") or ""), int(row.get("start_line") or 0)))
            queue.clear()
            queue.extend(values)
        names = sorted(groups)
        while names:
            remaining: list[str] = []
            for name in names:
                queue = groups[name]
                if queue:
                    ordered.append(queue.popleft())
                if queue:
                    remaining.append(name)
            names = remaining
    return ordered


def _select_evidence(evidence: list[dict]) -> tuple[list[dict], dict]:
    limits = _analysis_limits()
    selected: list[dict] = []
    input_chars = 0
    skipped_chunks = 0
    skipped_chars = 0
    for item in _ordered_evidence(evidence):
        body_chars = int(item.get("body_chars") or len(str(item.get("body") or "")))
        if len(selected) >= limits["max_chunks"]:
            skipped_chunks += 1
            skipped_chars += body_chars
            continue
        if input_chars + body_chars > limits["max_input_chars"]:
            # Whole-chunk admission only: never silently slice source text.
            skipped_chunks += 1
            skipped_chars += body_chars
            continue
        selected.append(item)
        input_chars += body_chars
    files = {str(item.get("path") or "") for item in evidence}
    selected_files = {str(item.get("path") or "") for item in selected}
    coverage = {
        "total_evidence_chunks": len(evidence),
        "analyzed_evidence_chunks": len(selected),
        "skipped_evidence_chunks": skipped_chunks,
        "total_files_with_evidence": len(files),
        "analyzed_files": len(selected_files),
        "analyzed_source_chars": input_chars,
        "skipped_source_chars": skipped_chars,
        "limits": limits,
        "warnings": [],
    }
    if skipped_chunks:
        coverage["warnings"].append(
            f"{skipped_chunks} evidence chunks were outside the explicit analysis budget; "
            "manifests, documentation, entrypoints, configuration, tests, and representative "
            "modules were prioritized. Increase the repository scan analysis limits and rerun "
            "for more coverage.")
    return selected, coverage


def _snapshot_bundle(project_id: int) -> tuple[object, object, list[dict]]:
    with get_session() as session:
        source = repository_store.repository_source_for_project(session, project_id)
        snapshot = repository_store.current_repository_snapshot(session, project_id)
        if source is None or snapshot is None or snapshot.status != "ready":
            raise RuntimeError("repository snapshot is not ready; run Snapshot & scan first")
        if source.pending_sha:
            raise RuntimeError(
                "a repository update is pending; snapshot and scan it before analysis")
        expected_scan_hash = repository_store.repository_scan_config_hash(source)
        if (snapshot.scanner_version != repository_store.SCANNER_VERSION
                or snapshot.scan_config_hash != expected_scan_hash):
            raise RuntimeError(
                "repository scan policy changed; rerun Snapshot & scan before analysis")
        evidence = repository_store.list_repository_evidence(
            session, snapshot.id, include_body=False)
    return source, snapshot, list(evidence)


def _installed_model_digest(provider: str, model: str) -> str:
    """Return immutable local-model identity for cache signatures."""
    if provider != "ollama":
        return "not_applicable"
    try:
        from ..local_model_safety import inspect_model

        inventory, row = inspect_model(model)
        if inventory.get("ok") and row:
            return str(row.get("digest") or "digest_unavailable")
    except Exception:
        log.debug("could not inspect repository model digest", exc_info=True)
    # Successful work produced while inventory is temporarily unavailable gets
    # a distinct key and is safely recomputed once the digest becomes known.
    return "digest_unavailable"


def _map_config_hash(
    provider: str,
    model: str,
    *,
    contract_version: int = _MAP_CONTRACT_VERSION,
    model_digest: str | None = None,
) -> str:
    value = {
        "schema": contract_version,
        "provider": provider,
        "model": model,
        "params": get_setting("params.repository_map") or {},
        "reasoning_effort": "none",
        "map_prompt": _digest(get_prompt("repository_map")),
    }
    if contract_version >= 2:
        local = advanced("local") if provider == "ollama" else {}
        value.update({
            "model_digest": (
                model_digest
                if model_digest is not None
                else _installed_model_digest(provider, model)
            ),
            "local": ({
                "configured_context_minimum": local.get("num_ctx"),
                "context_policy": llm.LOCAL_CONTEXT_POLICY_VERSION,
                "automatic_context_cap": llm.LOCAL_AUTOMATIC_CONTEXT_CAP,
                "json_mode": local.get("json_mode"),
                "think": False,
            } if provider == "ollama" else {}),
        })
    return _digest(value)


def _map_item_config_hash(base_hash: str, item: dict) -> str:
    return _digest({
        "base": base_hash,
        "path": str(item.get("path") or ""),
        "start_line": int(item.get("start_line") or 1),
        "end_line": int(item.get("end_line") or item.get("start_line") or 1),
        "kind": str(item.get("kind") or ""),
        "symbol": str(item.get("symbol") or ""),
    })


def _clean_strings(value) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value
            if isinstance(item, (str, int, float)) and str(item).strip()]


def _sanitize_map(raw: dict, item: dict) -> dict:
    if not isinstance(raw, dict):
        raise RepositoryMapContractError(
            "repository evidence mapping did not return a JSON object")
    evidence_id = str(item["evidence_id"])
    facts = []
    for fact in raw.get("facts", []) if isinstance(raw, dict) else []:
        if isinstance(fact, dict):
            claim = str(fact.get("claim") or "").strip()
            kind = str(fact.get("kind") or "observation").strip()
        else:
            claim, kind = str(fact).strip(), "observation"
        if claim:
            facts.append({"claim": claim, "kind": kind,
                          "evidence_ids": [evidence_id]})
    sanitized = {
        "evidence_id": evidence_id,
        "evidence_ids": [evidence_id],
        "path": str(item.get("path") or ""),
        "start_line": int(item.get("start_line") or 1),
        "end_line": int(item.get("end_line") or item.get("start_line") or 1),
        "summary": str(raw.get("summary") or "").strip(),
        "role": str(raw.get("role") or "").strip(),
        "facts": facts,
        "symbols": _clean_strings(raw.get("symbols")),
        "dependencies": _clean_strings(raw.get("dependencies")),
        "commands": _clean_strings(raw.get("commands")),
        "knowledge": _clean_strings(raw.get("knowledge")),
    }
    if not sanitized["summary"]:
        raise RepositoryMapContractError(
            f"repository evidence mapping returned no usable summary for {evidence_id}")
    return sanitized


def _chunk_for_item(session, item: dict) -> RepositoryChunk | None:
    chunk_id = item.get("chunk_id") or item.get("id")
    if chunk_id:
        return session.get(RepositoryChunk, int(chunk_id))
    return session.exec(select(RepositoryChunk).where(
        RepositoryChunk.evidence_id == str(item.get("evidence_id") or "")
    )).first()


def _update_job_diagnostics(job_id: int, patch: dict) -> None:
    """Merge bounded structured diagnostics without disturbing job state."""
    if job_id <= 0:
        return
    try:
        with get_session() as session:
            job = session.get(Job, job_id)
            if job is None:
                return
            diagnostics = _json(job.diagnostics, {})
            for key, value in patch.items():
                if key == "attempts":
                    previous = diagnostics.get("attempts")
                    attempts = list(previous) if isinstance(previous, list) else []
                    for item in value if isinstance(value, list) else []:
                        if not isinstance(item, dict):
                            continue
                        bounded = dict(item)
                        bounded["detail"] = str(
                            bounded.get("detail") or ""
                        )[:_MAX_DIAGNOSTIC_DETAIL_CHARS]
                        attempts.append(bounded)
                    diagnostics["attempts"] = attempts[-_MAX_DIAGNOSTIC_ATTEMPTS:]
                elif isinstance(value, dict):
                    previous = diagnostics.get(key)
                    merged = dict(previous) if isinstance(previous, dict) else {}
                    merged.update(value)
                    diagnostics[key] = merged
                else:
                    diagnostics[key] = value
            job.diagnostics = json.dumps(
                diagnostics, sort_keys=True, separators=(",", ":"), default=str)
            job.updated = utcnow()
            session.add(job)
            session.commit()
    except Exception:
        # Diagnostics must never replace the actual pipeline outcome.
        log.warning(
            "could not persist repository diagnostics for job %s",
            job_id,
            exc_info=True,
        )


def _last_llm_diagnostics() -> dict:
    getter = getattr(llm, "last_call_diagnostics", None)
    if not callable(getter):
        return {}
    try:
        value = getter()
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _diagnostic_context(call: dict, max_tokens: int) -> dict:
    return {
        "requested": call.get("requested_context"),
        "effective": call.get("effective_context"),
        "native": call.get("native_context"),
        "timeout_seconds": call.get("timeout_seconds"),
        "max_output_tokens": int(
            call.get("max_output_tokens") or max_tokens),
    }


def _call_detail(exc: Exception | None, call: dict) -> str:
    details: list[str] = []
    if exc is not None:
        details.append(f"{exc.__class__.__name__}: {exc}")
    attempts = call.get("attempts")
    if isinstance(attempts, list) and attempts:
        latest = attempts[-1]
        if isinstance(latest, dict):
            status = latest.get("status")
            error_type = latest.get("error_type")
            message = latest.get("error_message") or latest.get("error")
            status_code = latest.get("status_code")
            rendered = ", ".join(
                str(value) for value in (
                    status,
                    error_type,
                    f"HTTP {status_code}" if status_code else None,
                    message,
                ) if value
            )
            if rendered:
                details.append(rendered)
    return " | ".join(details)[:_MAX_DIAGNOSTIC_DETAIL_CHARS]


def _map_evidence(job_id: int, project_id: int, evidence: list[dict]) -> tuple[list[dict], dict]:
    selected, coverage = _select_evidence(evidence)
    provider, model = llm.resolve_model("repository_map")
    model_digest = _installed_model_digest(provider, model)
    config_hash = _map_config_hash(
        provider, model, model_digest=model_digest)
    legacy_config_hash = _map_config_hash(
        provider, model, contract_version=1)
    summaries: list[dict] = []
    new_calls = 0
    reused = 0
    legacy_reused = 0
    skipped_uncached = 0
    max_new_calls = coverage["limits"]["max_new_map_calls"]
    _update_job_diagnostics(job_id, {
        "stage": "repository_map",
        "effective_model": {
            "provider": provider,
            "model": model,
            "digest": model_digest,
        },
        "cache": {
            "leaf_maps_reused": 0,
            "leaf_maps_new": 0,
            "legacy_leaf_maps_reused": 0,
        },
        "cause": "",
    })

    for position, item in enumerate(selected, 1):
        item_config_hash = _map_item_config_hash(config_hash, item)
        legacy_item_config_hash = _map_item_config_hash(
            legacy_config_hash, item)
        with get_session() as session:
            chunk = _chunk_for_item(session, item)
            if chunk is None:
                raise RuntimeError(
                    f"repository evidence {item.get('evidence_id')!r} has no indexed chunk")
            cached = repository_store.get_chunk_summary(chunk, item_config_hash)
            cached_is_legacy = False
            if cached is None and legacy_item_config_hash != item_config_hash:
                cached = repository_store.get_chunk_summary(
                    chunk, legacy_item_config_hash)
                cached_is_legacy = cached is not None
            chunk_body = str(chunk.body)
        if cached:
            data = cached.get("data") if isinstance(cached, dict) else None
            if isinstance(data, dict):
                try:
                    cached_summary = _sanitize_map(data, item)
                except RepositoryMapContractError:
                    log.warning(
                        "ignoring invalid cached repository map for evidence %s",
                        item.get("evidence_id"),
                    )
                else:
                    summaries.append(cached_summary)
                    reused += 1
                    legacy_reused += int(cached_is_legacy)
                    _update_job_diagnostics(job_id, {
                        "cache": {
                            "leaf_maps_reused": reused,
                            "leaf_maps_new": new_calls,
                            "legacy_leaf_maps_reused": legacy_reused,
                        },
                    })
                    continue
        if new_calls >= max_new_calls:
            skipped_uncached += 1
            continue

        progress(job_id, f"analyzing repository evidence {position}/{len(selected)}")
        header = {
            "evidence_id": item["evidence_id"],
            "path": item.get("path"),
            "start_line": item.get("start_line"),
            "end_line": item.get("end_line"),
            "kind": item.get("kind"),
            "symbol": item.get("symbol"),
        }
        try:
            raw = llm.complete_json(
                "repository_map", get_prompt("repository_map"),
                "EVIDENCE METADATA:\n" + json.dumps(header, sort_keys=True)
                 + "\n\nBEGIN UNTRUSTED REPOSITORY EXCERPT\n"
                + chunk_body
                + "\nEND UNTRUSTED REPOSITORY EXCERPT",
                max_tokens=1_600, provider=provider, model=model,
            )
            summary = _sanitize_map(raw, item)
        except Exception as exc:
            call = _last_llm_diagnostics()
            outcome, _can_subdivide = _reduction_failure(exc, call)
            detail = _call_detail(exc, call)
            _update_job_diagnostics(job_id, {
                "context": _diagnostic_context(call, 1_600),
                "attempts": [{
                    "outcome": f"map_{outcome}",
                    "detail": (
                        f"evidence {position}/{len(selected)} "
                        f"({item.get('evidence_id')}): {detail}"
                    ),
                }],
                "cause": detail or outcome,
            })
            raise
        with get_session() as session:
            chunk = _chunk_for_item(session, item)
            if chunk is None:
                raise RuntimeError("repository evidence disappeared during analysis")
            repository_store.set_chunk_summary(
                session, chunk.id, text_value=summary["summary"],
                data=summary, config_hash=item_config_hash)
            session.commit()
        summaries.append(summary)
        new_calls += 1
        call = _last_llm_diagnostics()
        _update_job_diagnostics(job_id, {
            "context": _diagnostic_context(call, 1_600),
            "cache": {
                "leaf_maps_reused": reused,
                "leaf_maps_new": new_calls,
                "legacy_leaf_maps_reused": legacy_reused,
            },
            "cause": "",
        })

    if skipped_uncached:
        coverage["warnings"].append(
            f"{skipped_uncached} selected chunks had no reusable summary and exceeded the "
            "new-model-call budget; increase repository.analysis.max_new_map_calls and rerun.")
    if legacy_reused:
        coverage["warnings"].append(
            f"{legacy_reused} compatible pre-v5 leaf maps were reused. Their "
            "historical Ollama digest was not recorded; newly generated maps "
            "are pinned to the current model digest.")
    coverage["analyzed_evidence_chunks"] = len(summaries)
    coverage["skipped_evidence_chunks"] = len(evidence) - len(summaries)
    coverage["analyzed_files"] = len({item.get("path") for item in summaries})
    coverage["cache"] = {
        "reused_chunk_summaries": reused,
        "new_chunk_summaries": new_calls,
        "legacy_chunk_summaries": legacy_reused,
        "summary_config_hash": config_hash,
    }
    coverage.setdefault("model_execution", {})["map"] = {
        "provider": provider,
        "model": model,
        "digest": model_digest,
        "contract_version": _MAP_CONTRACT_VERSION,
    }
    _update_job_diagnostics(job_id, {
        "cache": {
            "leaf_maps_reused": reused,
            "leaf_maps_new": new_calls,
            "legacy_leaf_maps_reused": legacy_reused,
        },
    })
    if not summaries and evidence:
        raise RuntimeError("repository analysis budget produced no evidence summaries")
    return summaries, coverage


def _evidence_ids(item: dict) -> list[str]:
    ids = item.get("evidence_ids") or (
        [item.get("evidence_id")] if item.get("evidence_id") else [])
    return sorted({str(value) for value in ids if value})


def _nested_evidence_ids(value) -> set[str]:
    """Collect only explicit evidence-id fields from bounded structured data."""
    found: set[str] = set()
    if isinstance(value, dict):
        evidence_id = value.get("evidence_id")
        if evidence_id:
            found.add(str(evidence_id))
        ids = value.get("evidence_ids")
        if isinstance(ids, list):
            found.update(str(item) for item in ids if item)
        for nested in value.values():
            found.update(_nested_evidence_ids(nested))
    elif isinstance(value, list):
        for nested in value:
            found.update(_nested_evidence_ids(nested))
    return found


def _sanitize_reduce(raw: dict, allowed: set[str]) -> dict:
    if not isinstance(raw, dict):
        raise RepositoryReductionContractError(
            "repository reduction did not return a JSON object")
    if not allowed:
        raise RepositoryReductionContractError(
            "repository reduction received no evidence identifiers")
    explicit_ids = _nested_evidence_ids(raw)
    unknown = sorted(explicit_ids - allowed)
    if unknown:
        raise RepositoryReductionContractError(
            "repository reduction invented evidence identifiers: "
            + ", ".join(unknown[:10]))
    missing = sorted(allowed - explicit_ids)
    if missing:
        raise RepositoryReductionContractError(
            "repository reduction omitted supporting evidence identifiers: "
            + ", ".join(missing[:10]))

    facts = []
    for fact in raw.get("facts", []):
        if not isinstance(fact, dict):
            continue
        ids = [str(value) for value in fact.get("evidence_ids", [])
               if str(value) in allowed]
        claim = str(fact.get("claim") or "").strip()
        if claim and ids:
            facts.append({"claim": claim,
                          "kind": str(fact.get("kind") or "observation"),
                          "evidence_ids": sorted(set(ids))})
    sanitized = {
        "summary": str(raw.get("summary") or "").strip(),
        "facts": facts,
        "symbols": _clean_strings(raw.get("symbols")),
        "dependencies": _clean_strings(raw.get("dependencies")),
        "commands": _clean_strings(raw.get("commands")),
        "knowledge": _clean_strings(raw.get("knowledge")),
        # The root ledger is always the exact input union. Facts retain their
        # narrower direct support, while later reductions cannot silently lose
        # evidence that was summarized at an earlier level.
        "evidence_ids": sorted(allowed),
    }
    if not (sanitized["summary"] or sanitized["facts"]
            or sanitized["symbols"] or sanitized["dependencies"]
            or sanitized["commands"] or sanitized["knowledge"]):
        raise RepositoryReductionContractError(
            "repository reduction returned no structured content")
    return sanitized


def _batches(items: list[dict], max_chars: int) -> list[list[dict]]:
    batches: list[list[dict]] = []
    current: list[dict] = []
    size = 0
    for item in items:
        item_size = len(json.dumps(item, sort_keys=True, default=str))
        if current and size + item_size > max_chars:
            batches.append(current)
            current, size = [], 0
        current.append(item)
        size += item_size
    if current:
        batches.append(current)
    return batches


def _reduction_config_hash(
    provider: str,
    model: str,
    max_tokens: int,
    *,
    model_digest: str | None = None,
) -> str:
    local = advanced("local") if provider == "ollama" else {}
    return _digest({
        "schema": _REDUCTION_CONTRACT_VERSION,
        "provider": provider,
        "model": model,
        "model_digest": (
            model_digest
            if model_digest is not None
            else _installed_model_digest(provider, model)
        ),
        "params": get_setting("params.repository_reduce") or {},
        "max_tokens": max_tokens,
        "prompt": _digest(get_prompt("repository_reduce")),
        "local": ({
            "configured_context_minimum": local.get("num_ctx"),
            "context_policy": llm.LOCAL_CONTEXT_POLICY_VERSION,
            "automatic_context_cap": llm.LOCAL_AUTOMATIC_CONTEXT_CAP,
            "json_mode": local.get("json_mode"),
            "think": False,
        } if provider == "ollama" else {}),
    })


def _cached_reduction(snapshot_id: int, batch: list[dict],
                      config_hash: str) -> dict | None:
    input_hash = _digest(batch)
    allowed = {value for item in batch for value in _evidence_ids(item)}
    with get_session() as session:
        cached = session.exec(select(RepositorySynthesisCache).where(
            RepositorySynthesisCache.snapshot_id == snapshot_id,
            RepositorySynthesisCache.purpose == _SHARED_REDUCTION_PURPOSE,
            RepositorySynthesisCache.input_hash == input_hash,
            RepositorySynthesisCache.config_hash == config_hash,
        )).first()
        if cached is None:
            return None
        raw = _json(cached.body, {})
    try:
        result = _sanitize_reduce(raw, allowed)
    except RepositoryReductionContractError:
        log.warning(
            "ignoring invalid repository reduction cache row %s",
            getattr(cached, "id", None),
        )
        return None
    if set(_json(cached.evidence_ids, [])) != allowed:
        log.warning(
            "ignoring repository reduction cache row %s with a mismatched evidence ledger",
            getattr(cached, "id", None),
        )
        return None
    return result


def _store_reduction(snapshot_id: int, batch: list[dict], config_hash: str,
                     provider: str, model: str, result: dict) -> None:
    input_hash = _digest(batch)
    with get_session() as session:
        cached = session.exec(select(RepositorySynthesisCache).where(
            RepositorySynthesisCache.snapshot_id == snapshot_id,
            RepositorySynthesisCache.purpose == _SHARED_REDUCTION_PURPOSE,
            RepositorySynthesisCache.input_hash == input_hash,
            RepositorySynthesisCache.config_hash == config_hash,
        )).first()
        if cached is None:
            cached = RepositorySynthesisCache(
                snapshot_id=snapshot_id,
                purpose=_SHARED_REDUCTION_PURPOSE,
                input_hash=input_hash,
                config_hash=config_hash,
            )
        cached.provider = provider
        cached.model = model
        cached.body = json.dumps(
            result, sort_keys=True, separators=(",", ":"), default=str)
        cached.evidence_ids = json.dumps(
            _evidence_ids(result), sort_keys=True, separators=(",", ":"))
        cached.updated = utcnow()
        session.add(cached)
        try:
            session.commit()
        except IntegrityError:
            # An unusual duplicate delivery or recovery race may have written
            # the same content-addressed key after our initial lookup. The
            # winner is equivalent for cache identity, so verify it exists
            # rather than failing after the model work already succeeded.
            session.rollback()
            winner = session.exec(select(RepositorySynthesisCache.id).where(
                RepositorySynthesisCache.snapshot_id == snapshot_id,
                RepositorySynthesisCache.purpose == _SHARED_REDUCTION_PURPOSE,
                RepositorySynthesisCache.input_hash == input_hash,
                RepositorySynthesisCache.config_hash == config_hash,
            )).first()
            if winner is None:
                raise


def _split_batch(batch: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split near the serialized midpoint without slicing any evidence item."""
    if len(batch) < 2:
        return batch, []
    sizes = [len(json.dumps(item, sort_keys=True, default=str)) for item in batch]
    midpoint = sum(sizes) / 2
    running = 0
    split_at = 1
    for index, size in enumerate(sizes[:-1], 1):
        running += size
        split_at = index
        if running >= midpoint:
            break
    return batch[:split_at], batch[split_at:]


def _reduction_failure(exc: Exception, call: dict) -> tuple[str, bool]:
    if isinstance(exc, (RepositoryMapContractError,
                        RepositoryReductionContractError)):
        return "invalid_structure", True
    if isinstance(exc, RepositoryReductionOversizedError):
        return "oversized", True
    if exc.__class__.__name__ == "ContextWindowError":
        return "context_window", True
    text_parts = [exc.__class__.__name__, str(exc)]
    for attempt in call.get("attempts", []) if isinstance(call, dict) else []:
        if isinstance(attempt, dict):
            text_parts.extend(str(attempt.get(key) or "") for key in (
                "status", "error_type", "error_message", "error"))
    text = " ".join(text_parts).casefold()
    if "timeout" in text or "timed out" in text:
        return "timeout", True
    if isinstance(exc, json.JSONDecodeError) or (
            isinstance(exc, ValueError) and "json" in text):
        return "invalid_json", True
    return "provider_error", False


def _reduce_batch_adaptive(
    job_id: int,
    snapshot_id: int,
    purpose: str,
    level: int,
    batch_number: int,
    batch_count: int,
    batch: list[dict],
    *,
    depth: int,
    limit: int,
    max_tokens: int,
    max_depth: int,
    provider: str,
    model: str,
    config_hash: str,
    cache_state: dict,
) -> list[dict]:
    input_json = json.dumps(
        batch, sort_keys=True, separators=(",", ":"), default=str)
    reduction_state = {
        "purpose": purpose,
        "level": level,
        "batch": batch_number,
        "batch_count": batch_count,
        "items": len(batch),
        "input_chars": len(input_json),
        "subdivision_depth": depth,
    }
    _update_job_diagnostics(job_id, {
        "reduction": reduction_state,
        "cache": cache_state,
    })
    depth_suffix = f", subdivision {depth}" if depth else ""
    progress(
        job_id,
        f"reducing {purpose} evidence level {level}, "
        f"batch {batch_number}/{batch_count}{depth_suffix}",
    )

    cached = _cached_reduction(snapshot_id, batch, config_hash)
    if cached is not None:
        cache_state["reductions_reused"] += 1
        _update_job_diagnostics(job_id, {
            "reduction": reduction_state,
            "cache": cache_state,
        })
        return [cached]

    call: dict = {}
    try:
        raw = llm.complete_json(
            "repository_reduce",
            get_prompt("repository_reduce"),
            input_json,
            max_tokens=max_tokens,
            provider=provider,
            model=model,
            retries=0,
            transient_attempts=1,
        )
        call = _last_llm_diagnostics()
        allowed = {
            value for item in batch for value in _evidence_ids(item)
        }
        result = _sanitize_reduce(raw, allowed)
        output_chars = len(json.dumps(
            result, sort_keys=True, separators=(",", ":"), default=str))
        if output_chars > limit or (
                len(batch) > 1 and output_chars >= len(input_json)):
            raise RepositoryReductionOversizedError(
                f"repository reduction produced {output_chars} characters "
                f"from {len(input_json)} input characters")
    except Exception as exc:
        call = _last_llm_diagnostics() or call
        outcome, can_subdivide = _reduction_failure(exc, call)
        detail = _call_detail(exc, call)
        _update_job_diagnostics(job_id, {
            "context": _diagnostic_context(call, max_tokens),
            "reduction": reduction_state,
            "attempts": [{
                "outcome": outcome,
                "level": level,
                "batch": batch_number,
                "depth": depth,
                "detail": detail,
            }],
            "cause": detail or outcome,
        })
        if can_subdivide and len(batch) > 1 and depth < max_depth:
            left, right = _split_batch(batch)
            _update_job_diagnostics(job_id, {
                "attempts": [{
                    "outcome": "subdivided",
                    "level": level,
                    "batch": batch_number,
                    "depth": depth,
                    "detail": (
                        f"split {len(batch)} items into "
                        f"{len(left)} and {len(right)} after {outcome}"
                    ),
                }],
            })
            return (
                _reduce_batch_adaptive(
                    job_id, snapshot_id, purpose, level, batch_number,
                    batch_count, left, depth=depth + 1, limit=limit,
                    max_tokens=max_tokens, max_depth=max_depth,
                    provider=provider, model=model, config_hash=config_hash,
                    cache_state=cache_state,
                )
                + _reduce_batch_adaptive(
                    job_id, snapshot_id, purpose, level, batch_number,
                    batch_count, right, depth=depth + 1, limit=limit,
                    max_tokens=max_tokens, max_depth=max_depth,
                    provider=provider, model=model, config_hash=config_hash,
                    cache_state=cache_state,
                )
            )
        evidence = sorted({
            value for item in batch for value in _evidence_ids(item)
        })
        boundary = (
            "single evidence summary cannot be reduced"
            if len(batch) == 1
            else f"subdivision depth {max_depth} was exhausted"
        )
        raise RuntimeError(
            f"repository reduction failed at level {level}, batch "
            f"{batch_number}/{batch_count}: {boundary}; outcome={outcome}; "
            f"evidence_ids={','.join(evidence[:10])}; "
            f"detail={detail or str(exc)[:_MAX_DIAGNOSTIC_DETAIL_CHARS]}"
        ) from exc

    _store_reduction(
        snapshot_id, batch, config_hash, provider, model, result)
    cache_state["reductions_new"] += 1
    _update_job_diagnostics(job_id, {
        "context": _diagnostic_context(call, max_tokens),
        "reduction": reduction_state,
        "cache": cache_state,
        "attempts": [{
            "outcome": "ok",
            "level": level,
            "batch": batch_number,
            "depth": depth,
            "detail": _call_detail(None, call),
        }],
        "cause": "",
    })
    return [result]


def _hierarchical_context(
    job_id: int,
    snapshot_id: int,
    summaries: list[dict],
    purpose: str,
    coverage: dict,
) -> list[dict]:
    limits = _analysis_limits()
    limit = min(limits["reduce_batch_chars"], limits["final_input_chars"] // 2)
    items = list(summaries)
    level = 0
    provider, model = llm.resolve_model("repository_reduce")
    model_digest = _installed_model_digest(provider, model)
    max_tokens = limits["reduce_max_tokens"]
    max_depth = limits["reduce_max_subdivision_depth"]
    config_hash = _reduction_config_hash(
        provider, model, max_tokens, model_digest=model_digest)
    coverage.setdefault("model_execution", {})["reduction"] = {
        "provider": provider,
        "model": model,
        "digest": model_digest,
        "contract_version": _REDUCTION_CONTRACT_VERSION,
    }
    map_cache = coverage.get("cache") if isinstance(coverage, dict) else {}
    map_cache = map_cache if isinstance(map_cache, dict) else {}
    cache_state = {
        "leaf_maps_reused": int(
            map_cache.get("reused_chunk_summaries") or 0),
        "leaf_maps_new": int(map_cache.get("new_chunk_summaries") or 0),
        "retained_leaf_maps": len(items),
        "reductions_reused": 0,
        "reductions_new": 0,
    }
    _update_job_diagnostics(job_id, {
        "stage": "repository_reduce",
        "effective_model": {
            "provider": provider,
            "model": model,
            "digest": model_digest,
        },
        "context": {
            "requested": None,
            "effective": None,
            "native": None,
            "timeout_seconds": None,
            "max_output_tokens": max_tokens,
        },
        "reduction": {
            "purpose": purpose,
            "level": 0,
            "batch": 0,
            "batch_count": 0,
            "items": len(items),
            "input_chars": len(json.dumps(items, default=str)),
            "subdivision_depth": 0,
        },
        "cache": cache_state,
        "attempts": [],
        "cause": "",
    })
    while True:
        batches = _batches(items, limit)
        has_oversized_item = any(
            len(json.dumps(item, sort_keys=True, default=str)) > limit
            for item in items
        )
        if len(batches) <= 1 and not has_oversized_item:
            break
        level += 1
        if level > _MAX_REDUCTION_LEVELS:
            cause = (
                "repository reduction exceeded the bounded hierarchical "
                f"depth of {_MAX_REDUCTION_LEVELS} levels"
            )
            _update_job_diagnostics(job_id, {"cause": cause})
            raise RuntimeError(cause)
        reduced: list[dict] = []
        for index, batch in enumerate(batches, 1):
            reduced.extend(_reduce_batch_adaptive(
                job_id, snapshot_id, purpose, level, index, len(batches), batch,
                depth=0, limit=limit, max_tokens=max_tokens,
                max_depth=max_depth, provider=provider, model=model,
                config_hash=config_hash, cache_state=cache_state,
            ))
        old_size = len(json.dumps(
            items, sort_keys=True, separators=(",", ":"), default=str))
        new_size = len(json.dumps(
            reduced, sort_keys=True, separators=(",", ":"), default=str))
        if len(reduced) >= len(items) and new_size >= old_size:
            cause = (
                "repository reduction made no bounded progress; "
                "a single structured summary may exceed the reduction budget"
            )
            _update_job_diagnostics(job_id, {"cause": cause})
            raise RuntimeError(cause)
        items = reduced
    _update_job_diagnostics(job_id, {
        "reduction": {
            "purpose": purpose,
            "level": level,
            "batch": 0,
            "batch_count": 0,
            "items": len(items),
            "input_chars": len(json.dumps(items, default=str)),
            "subdivision_depth": 0,
            "complete": True,
        },
        "cache": cache_state,
        "cause": "",
    })
    return items


def _repository_context(job_id: int, project_id: int, purpose: str) -> tuple[object, object, list[dict], dict]:
    source, snapshot, evidence = _snapshot_bundle(project_id)
    summaries, coverage = _map_evidence(job_id, project_id, evidence)
    scan_facts = _scan_facts(snapshot)
    scan_coverage = scan_facts.get("coverage", {})
    if not isinstance(scan_coverage, dict):
        scan_coverage = {}
    coverage.update({
        "total_snapshot_files": int(getattr(snapshot, "file_count", 0) or 0),
        "snapshot_total_bytes": int(getattr(snapshot, "total_bytes", 0) or 0),
        "indexed_file_count": int(
            getattr(snapshot, "indexed_file_count", 0) or 0),
        "indexed_bytes": int(getattr(snapshot, "indexed_bytes", 0) or 0),
        "excluded_file_count": int(
            getattr(snapshot, "excluded_file_count", 0) or 0),
        "files_with_evidence": int(
            scan_coverage.get("files_with_evidence")
            or coverage.get("total_files_with_evidence") or 0),
        "evidence_chunk_count": int(
            scan_coverage.get("evidence_chunk_count")
            or coverage.get("total_evidence_chunks") or 0),
        "exclusion_reason_counts": scan_coverage.get(
            "exclusion_reason_counts", {}),
        "omitted_link_count": int(scan_coverage.get("omitted_link_count") or 0),
    })
    context = _hierarchical_context(
        job_id, snapshot.id, summaries, purpose, coverage)
    return source, snapshot, context, coverage


def _scan_facts(snapshot) -> dict:
    return _json(getattr(snapshot, "facts", "{}"), {})


def _bounded_scan_facts(snapshot, max_chars: int) -> tuple[dict, str | None]:
    """Prioritize deterministic facts inside one explicit synthesis budget."""
    raw = _scan_facts(snapshot)
    ordered = [
        "scanner_version", "static_only", "coverage", "fact_limits", "runtimes",
        "manifests", "commands", "script_definitions", "dependencies",
        "environment", "containers", "ports", "frameworks", "languages",
        "submodules", "git_lfs", "facts_only_files",
    ]
    ordered.extend(sorted(set(raw) - set(ordered)))
    bounded: dict = {}
    omitted = 0
    for key in ordered:
        if key not in raw or key.startswith("_"):
            continue
        value = raw[key]
        candidates = value if isinstance(value, list) else [value]
        accepted: list = []
        for candidate in candidates:
            trial = dict(bounded)
            trial[key] = accepted + [candidate] if isinstance(value, list) else candidate
            if len(json.dumps(trial, sort_keys=True, default=str)) > max_chars:
                omitted += len(candidates) - len(accepted)
                break
            if isinstance(value, list):
                accepted.append(candidate)
            else:
                bounded[key] = candidate
                accepted = [candidate]
                break
        if isinstance(value, list) and accepted:
            bounded[key] = accepted
    warning = None
    if omitted:
        bounded.setdefault("fact_limits", {})["synthesis_omitted_items"] = omitted
        warning = (
            f"{omitted} deterministic scan-fact items exceeded the final prompt budget; "
            "runtime, manifest, command, dependency and environment facts were prioritized."
        )
    return bounded, warning


def _source_metadata(source, snapshot, coverage: dict) -> dict:
    return {
        "canonical_url": getattr(source, "canonical_url", ""),
        "owner": getattr(source, "owner", ""),
        "repository": getattr(source, "repository", getattr(source, "name", "")),
        "requested_ref": getattr(source, "requested_ref", ""),
        "resolved_sha": getattr(snapshot, "resolved_sha", ""),
        "commit_url": getattr(snapshot, "commit_url", ""),
        "include_paths": _json(getattr(source, "include_paths", "[]"), []),
        "exclude_paths": _json(getattr(source, "exclude_paths", "[]"), []),
        "scanner_version": getattr(snapshot, "scanner_version", ""),
        "static_only": True,
        "execution_performed": False,
        "coverage": coverage,
    }


def _citation_map(evidence: list[dict]) -> dict[str, dict]:
    return {str(item.get("evidence_id")): item for item in evidence
            if item.get("evidence_id")}


def _validate_and_render_citations(body: str, source, snapshot,
                                   evidence: list[dict], *, require: bool = True) -> tuple[str, int]:
    visible = _VISIBLE_CITATION.findall(body)
    hidden = _HIDDEN_CITATION.findall(body)
    ids = visible + hidden
    known = _citation_map(evidence)
    unknown = sorted(set(ids) - set(known))
    if unknown:
        raise RuntimeError(
            "model returned invalid repository evidence citation(s): "
            + ", ".join(unknown[:10]))
    if require and evidence and not ids:
        raise RuntimeError("repository document contained no validated evidence citations")
    with get_session() as session:
        validation = repository_store.validate_repository_citations(
            session, snapshot.id, sorted(set(ids)))
    if isinstance(validation, dict):
        invalid = validation.get("invalid") or validation.get("unknown") or []
        if invalid:
            raise RuntimeError("repository citation validation failed")

    canonical = str(getattr(source, "canonical_url", "")).rstrip("/")
    sha = str(getattr(snapshot, "resolved_sha", ""))

    def replace(match: re.Match) -> str:
        evidence_id = match.group(1)
        item = known[evidence_id]
        path = str(item.get("path") or "")
        start = int(item.get("start_line") or 1)
        end = int(item.get("end_line") or start)
        url = (f"{canonical}/blob/{sha}/{quote(path, safe='/')}"
               f"#L{start}-L{end}")
        label_path = path.replace("`", "'")
        label = f"{label_path}:L{start}" + (f"-L{end}" if end != start else "")
        return f"[`{label}`]({url})<!--E:{evidence_id}-->"

    return _VISIBLE_CITATION.sub(replace, body), len(set(ids))


def _coverage_notice(coverage: dict) -> str:
    analyzed = coverage.get("analyzed_evidence_chunks", 0)
    total = coverage.get("total_evidence_chunks", 0)
    files = coverage.get("analyzed_files", 0)
    evidence_files = coverage.get(
        "files_with_evidence", coverage.get("total_files_with_evidence", 0))
    total_files = coverage.get("total_snapshot_files", evidence_files)
    indexed_files = coverage.get("indexed_file_count", evidence_files)
    excluded_files = coverage.get("excluded_file_count", 0)
    lines = [
        "> **Static-analysis coverage:** "
        f"{analyzed}/{total} line-addressed evidence chunks across "
        f"{files}/{total_files} snapshot files were analyzed; {evidence_files} files produced "
        f"evidence, {indexed_files} were in the normal source index, and {excluded_files} "
        "were excluded from normal source indexing. No repository code was executed."
    ]
    reasons = coverage.get("exclusion_reason_counts") or {}
    if isinstance(reasons, dict) and reasons:
        rendered = ", ".join(
            f"{reason}={int(count)}" for reason, count in sorted(reasons.items()))
        lines.append(f"> **Catalogued omissions:** {rendered}.")
    omitted_links = int(coverage.get("omitted_link_count") or 0)
    if omitted_links:
        lines.append(
            f"> **Links not followed:** {omitted_links} symbolic link"
            f"{'s' if omitted_links != 1 else ''} were catalogued but not materialized.")
    for warning in coverage.get("warnings", []):
        lines.append(f"> **Coverage warning:** {warning}")
    return "\n>\n".join(lines)


def _write_repository_artifact(project_id: int, artifact_type: str,
                               title_prefix: str, body: str, *, provider: str,
                               model: str, source, snapshot, coverage: dict,
                               citation_count: int) -> int:
    with get_session() as session:
        project = get_project(session, project_id)
        existing = session.exec(select(Artifact).where(
            Artifact.project_id == project_id,
            Artifact.paper_series_id == None,  # noqa: E711
            Artifact.paper_part_id == None,  # noqa: E711
            Artifact.type == artifact_type,
        )).first()
        previous_commit = ""
        history_snapshot = None
        if existing:
            previous = _json(existing.provenance, {})
            previous_commit = str(
                previous.get("config", {}).get("repository", {})
                .get("source", {}).get("resolved_sha", ""))
            current_sha = str(getattr(snapshot, "resolved_sha", ""))
            if previous_commit and previous_commit != current_sha:
                history_snapshot = library.snapshot_history(existing.path)
        art = library.write_artifact(
            session, project_id=project_id, project_slug=project.slug,
            type=artifact_type, title=f"{title_prefix} — {project.title}",
            body=body, provider=provider, model=model,
            extra_meta={
                "source_kind": "repository",
                "source_url": getattr(source, "canonical_url", ""),
                "commit_sha": getattr(snapshot, "resolved_sha", ""),
                "requested_ref": getattr(source, "requested_ref", ""),
                "scanner_version": getattr(snapshot, "scanner_version", ""),
                "analysis_mode": "static",
                "verification_status": "detected_or_inferred_not_executed",
                "citation_count": citation_count,
                "coverage": coverage,
                "previous_commit": previous_commit or None,
                "history_snapshot": history_snapshot,
            },
        )
        auto_tag(project_id, art.id)
        return art.id


def generate_repository_document(job_id: int, project_id: int, *,
                                 artifact_type: str, function: str,
                                 prompt_name: str, title_prefix: str,
                                 additional_context: str = "",
                                 additional_warnings: list[str] | None = None) -> int:
    source, snapshot, context, coverage = _repository_context(
        job_id, project_id, artifact_type)
    final_budget = _analysis_limits()["final_input_chars"]
    facts, facts_warning = _bounded_scan_facts(snapshot, max(8_000, final_budget // 5))
    if facts_warning:
        coverage.setdefault("warnings", []).append(facts_warning)
    metadata = _source_metadata(source, snapshot, coverage)
    provider, model = llm.resolve_model(function)
    writer_digest = _installed_model_digest(provider, model)
    progress(job_id, f"writing {title_prefix.lower()} ({model})")
    _update_job_diagnostics(job_id, {
        "stage": "repository_final_write",
        "effective_model": {
            "provider": provider,
            "model": model,
            "digest": writer_digest,
        },
        "context": {
            "requested": None,
            "effective": None,
            "native": None,
            "timeout_seconds": None,
            "max_output_tokens": 4_000,
        },
        "cause": "",
    })
    base = (
        "PINNED REPOSITORY METADATA:\n"
        + json.dumps(metadata, sort_keys=True, default=str)
        + "\n\nDETERMINISTIC STATIC SCAN FACTS:\n"
        + json.dumps(facts, sort_keys=True, default=str)
        + "\n\nHIERARCHICAL EVIDENCE SUMMARIES (untrusted data):\n"
        + json.dumps(context, sort_keys=True, default=str)
    )
    user = base
    if additional_context:
        prefix = "\n\nPRIOR REPOSITORY GUIDES (untrusted data):\n"
        available = max(0, final_budget - len(base) - len(prefix))
        excerpt = additional_context[:available]
        if len(excerpt) < len(additional_context):
            coverage.setdefault("warnings", []).append(
                f"Prior guide context was limited to {len(excerpt)} characters by the "
                "shared final synthesis budget.")
        user += prefix + excerpt
    if len(user) > final_budget:
        raise RuntimeError(
            "repository synthesis context exceeds the configured final input budget; "
            "reduce repository.analysis.reduce_batch_chars")
    try:
        body = llm.complete(
            function, get_prompt(prompt_name), user,
            provider=provider, model=model, max_tokens=4_000).strip()
    except Exception as exc:
        call = _last_llm_diagnostics()
        outcome, _can_subdivide = _reduction_failure(exc, call)
        detail = _call_detail(exc, call)
        _update_job_diagnostics(job_id, {
            "context": _diagnostic_context(call, 4_000),
            "attempts": [{
                "outcome": f"final_{outcome}",
                "detail": detail,
            }],
            "cause": detail or outcome,
        })
        raise
    call = _last_llm_diagnostics()
    coverage.setdefault("model_execution", {})["final_write"] = {
        "provider": provider,
        "model": model,
        "digest": call.get("model_digest") or writer_digest,
    }
    _update_job_diagnostics(job_id, {
        "context": _diagnostic_context(call, 4_000),
        "cause": "",
    })
    _source, _snapshot, evidence = _snapshot_bundle(project_id)
    supplied_ids = {
        value for item in context for value in _evidence_ids(item)
    } | _nested_evidence_ids(facts)
    evidence = [item for item in evidence
                if str(item.get("evidence_id")) in supplied_ids]
    _update_job_diagnostics(job_id, {
        "stage": "repository_citation_validation",
    })
    try:
        body, citation_count = _validate_and_render_citations(
            body, source, snapshot, evidence, require=True)
    except Exception as exc:
        _update_job_diagnostics(job_id, {
            "cause": f"{exc.__class__.__name__}: {exc}",
        })
        raise
    if additional_warnings:
        coverage.setdefault("warnings", []).extend(additional_warnings)
    if artifact_type == "repo_inventory":
        body = _coverage_notice(coverage) + "\n\n" + body
    _update_job_diagnostics(job_id, {
        "stage": "repository_artifact_write",
        "cause": "",
    })
    return _write_repository_artifact(
        project_id, artifact_type, title_prefix, body, provider=provider,
        model=model, source=source, snapshot=snapshot, coverage=coverage,
        citation_count=citation_count)


def _bounded_guide_context(project_id: int, total_chars: int = 24_000) \
        -> tuple[str, list[str]]:
    guide_types = [
        "repo_inventory", "summary", "repo_usage", "repo_architecture",
        "repo_expertise", "repo_environment",
    ]
    documents: list[tuple[str, str]] = []
    warnings: list[str] = []
    with get_session() as session:
        for artifact_type in guide_types:
            try:
                body = artifact_body(session, project_id, artifact_type)
            except Exception:
                continue
            documents.append((artifact_type, body))
    per_guide_chars = max(2_000, total_chars // max(1, len(documents)))
    sections: list[str] = []
    for artifact_type, body in documents:
        excerpt = body[:per_guide_chars]
        if len(body) > len(excerpt):
            warnings.append(
                f"{artifact_type} exceeded the {per_guide_chars}-character "
                "deep-dive synthesis budget; its evidence map remains available separately."
            )
        sections.append(f"## {artifact_type}\n\n{excerpt}")
    joined = "\n\n---\n\n".join(sections)
    return joined[:total_chars], warnings


def generate_repository_deepdive(job_id: int, project_id: int, *, function: str,
                                 artifact_type: str, perspective: str) -> int:
    prompt_name = ("repository_deepdive_a" if perspective == "a"
                   else "repository_deepdive_b")
    label = ("Repository deep dive (architecture)" if perspective == "a"
             else "Repository deep dive (maintainer)")
    guide_context, guide_warnings = _bounded_guide_context(project_id)
    return generate_repository_document(
        job_id, project_id, artifact_type=artifact_type, function=function,
        prompt_name=prompt_name, title_prefix=label,
        additional_context=guide_context, additional_warnings=guide_warnings)


def merge_repository_deepdives(job_id: int, project_id: int) -> int:
    with get_session() as session:
        first = artifact_body(session, project_id, "deepdive_claude")
        second = artifact_body(session, project_id, "deepdive_gemini")
    source, snapshot, evidence = _snapshot_bundle(project_id)
    provider, model = llm.resolve_model("merge")
    progress(job_id, f"merging repository deep dives ({model})")
    merge_budget = _analysis_limits()["final_input_chars"]
    separator = "\n\n---\n\n## DOCUMENT 2\n\n"
    per_document = max(8_000, (merge_budget - len(separator) - 20) // 2)
    first_excerpt = first[:per_document]
    second_excerpt = second[:per_document]
    body = llm.complete(
        "merge", get_prompt("repository_merge"),
        f"## DOCUMENT 1\n\n{first_excerpt}{separator}{second_excerpt}",
        provider=provider, model=model, max_tokens=4_000).strip()
    body, citation_count = _validate_and_render_citations(
        body, source, snapshot, evidence, require=True)
    coverage = {
        "merged_artifacts": ["deepdive_claude", "deepdive_gemini"],
        "static_only": True,
        "warnings": (["One or both deep dives were excerpted to fit the shared merge "
                      "input budget."]
                     if len(first_excerpt) < len(first) or len(second_excerpt) < len(second)
                     else []),
    }
    return _write_repository_artifact(
        project_id, "deepdive_merged", "Repository deep dive (merged)", body,
        provider=provider, model=model, source=source, snapshot=snapshot,
        coverage=coverage, citation_count=citation_count)


def _cancelled(job_id: int) -> bool:
    with get_session() as session:
        job = session.get(Job, job_id)
        if not job or job.status != "running":
            return True
        if job.parent_job_id:
            parent = session.get(Job, job.parent_job_id)
            return not parent or parent.status != "running"
        return False


@celery.task(name="repo_snapshot")
@pipeline_task
def repo_snapshot(job_id: int, project_id: int):
    with get_session() as session:
        project = get_project(session, project_id)
        if project.source_type != "github":
            raise ValueError("repository snapshot is only applicable to GitHub projects")
        source = repository_store.repository_source_for_project(session, project_id)
        if source is None:
            raise RuntimeError("GitHub repository metadata is missing")
        expected_sha = source.pending_sha or None

    def report(message: str, current=None, total=None):
        suffix = ""
        if current is not None:
            suffix = f" ({current}/{total})" if total else f" ({current})"
        progress(job_id, message + suffix)

    snapshot = repository_store.ensure_snapshot(
        project_id, force=True, expected_sha=expected_sha,
        progress=report, cancelled=lambda: _cancelled(job_id))
    progress(job_id, f"pinned static snapshot {snapshot.resolved_sha[:12]} ready")
    return snapshot.id


@celery.task(name="repo_inventory")
@pipeline_task
def repo_inventory(job_id: int, project_id: int):
    return generate_repository_document(
        job_id, project_id, artifact_type="repo_inventory",
        function="repository_inventory", prompt_name="repository_inventory",
        title_prefix="Repository inventory")


def _guide_task(job_id: int, project_id: int, artifact_type: str,
                function: str, prompt: str, title: str):
    return generate_repository_document(
        job_id, project_id, artifact_type=artifact_type, function=function,
        prompt_name=prompt, title_prefix=title)


@celery.task(name="repo_usage")
@pipeline_task
def repo_usage(job_id: int, project_id: int):
    return _guide_task(job_id, project_id, "repo_usage", "repository_usage",
                       "repository_usage", "Repository setup & usage")


@celery.task(name="repo_architecture")
@pipeline_task
def repo_architecture(job_id: int, project_id: int):
    return _guide_task(job_id, project_id, "repo_architecture",
                       "repository_architecture", "repository_architecture",
                       "Repository architecture & code map")


@celery.task(name="repo_expertise")
@pipeline_task
def repo_expertise(job_id: int, project_id: int):
    return _guide_task(job_id, project_id, "repo_expertise",
                       "repository_expertise", "repository_expertise",
                       "Repository required knowledge")


@celery.task(name="repo_environment")
@pipeline_task
def repo_environment(job_id: int, project_id: int):
    return _guide_task(job_id, project_id, "repo_environment",
                       "repository_environment", "repository_environment",
                       "Repository dependencies & environment")
