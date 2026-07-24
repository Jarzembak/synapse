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

from sqlmodel import select

from .. import library, llm, repository as repository_store
from ..db import get_session
from ..models import Artifact, Job, RepositoryChunk
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
            "reduce_batch_chars", 48_000, 8_000, 200_000),
        # Conservative room for a substantial model response within common
        # local-model context windows. Every final synthesis source shares it.
        "final_input_chars": bounded(
            "final_input_chars", 64_000, 32_000, 160_000),
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


def _map_config_hash(provider: str, model: str) -> str:
    return _digest({
        "schema": 1,
        "provider": provider,
        "model": model,
        "params": get_setting("params.repository_map") or {},
        "reasoning_effort": "none",
        "map_prompt": _digest(get_prompt("repository_map")),
    })


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
    return {
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


def _chunk_for_item(session, item: dict) -> RepositoryChunk | None:
    chunk_id = item.get("chunk_id") or item.get("id")
    if chunk_id:
        return session.get(RepositoryChunk, int(chunk_id))
    return session.exec(select(RepositoryChunk).where(
        RepositoryChunk.evidence_id == str(item.get("evidence_id") or "")
    )).first()


def _map_evidence(job_id: int, project_id: int, evidence: list[dict]) -> tuple[list[dict], dict]:
    selected, coverage = _select_evidence(evidence)
    provider, model = llm.resolve_model("repository_map")
    config_hash = _map_config_hash(provider, model)
    summaries: list[dict] = []
    new_calls = 0
    reused = 0
    skipped_uncached = 0
    max_new_calls = coverage["limits"]["max_new_map_calls"]

    for position, item in enumerate(selected, 1):
        item_config_hash = _map_item_config_hash(config_hash, item)
        with get_session() as session:
            chunk = _chunk_for_item(session, item)
            if chunk is None:
                raise RuntimeError(
                    f"repository evidence {item.get('evidence_id')!r} has no indexed chunk")
            cached = repository_store.get_chunk_summary(chunk, item_config_hash)
            chunk_body = str(chunk.body)
        if cached:
            data = cached.get("data") if isinstance(cached, dict) else None
            if isinstance(data, dict):
                summaries.append(_sanitize_map(data, item))
                reused += 1
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
        raw = llm.complete_json(
            "repository_map", get_prompt("repository_map"),
            "EVIDENCE METADATA:\n" + json.dumps(header, sort_keys=True)
             + "\n\nBEGIN UNTRUSTED REPOSITORY EXCERPT\n"
            + chunk_body
            + "\nEND UNTRUSTED REPOSITORY EXCERPT",
            max_tokens=1_600, provider=provider, model=model,
        )
        summary = _sanitize_map(raw if isinstance(raw, dict) else {}, item)
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

    if skipped_uncached:
        coverage["warnings"].append(
            f"{skipped_uncached} selected chunks had no reusable summary and exceeded the "
            "new-model-call budget; increase repository.analysis.max_new_map_calls and rerun.")
    coverage["analyzed_evidence_chunks"] = len(summaries)
    coverage["skipped_evidence_chunks"] = len(evidence) - len(summaries)
    coverage["analyzed_files"] = len({item.get("path") for item in summaries})
    coverage["cache"] = {
        "reused_chunk_summaries": reused,
        "new_chunk_summaries": new_calls,
        "summary_config_hash": config_hash,
    }
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


def _sanitize_reduce(raw: dict, allowed: set[str], fallback: list[dict]) -> dict:
    facts = []
    used: set[str] = set()
    for fact in raw.get("facts", []) if isinstance(raw, dict) else []:
        if not isinstance(fact, dict):
            continue
        ids = [str(value) for value in fact.get("evidence_ids", [])
               if str(value) in allowed]
        claim = str(fact.get("claim") or "").strip()
        if claim and ids:
            used.update(ids)
            facts.append({"claim": claim,
                          "kind": str(fact.get("kind") or "observation"),
                          "evidence_ids": sorted(set(ids))})
    root_ids = [str(value) for value in raw.get("evidence_ids", [])
                if str(value) in allowed] if isinstance(raw, dict) else []
    used.update(root_ids)
    # A reducer may omit ids from its JSON despite following the facts.  Keep
    # the valid union as a lossless citation backstop, never an invented id.
    if not used:
        used.update(value for item in fallback for value in _evidence_ids(item))
    return {
        "summary": str(raw.get("summary") or "").strip(),
        "facts": facts,
        "symbols": _clean_strings(raw.get("symbols")),
        "dependencies": _clean_strings(raw.get("dependencies")),
        "commands": _clean_strings(raw.get("commands")),
        "knowledge": _clean_strings(raw.get("knowledge")),
        "evidence_ids": sorted(used),
    }


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


def _hierarchical_context(job_id: int, summaries: list[dict], purpose: str) -> list[dict]:
    limits = _analysis_limits()
    limit = min(limits["reduce_batch_chars"], limits["final_input_chars"] // 2)
    items = list(summaries)
    level = 0
    provider, model = llm.resolve_model("repository_map")
    while len(_batches(items, limit)) > 1:
        level += 1
        batches = _batches(items, limit)
        reduced: list[dict] = []
        for index, batch in enumerate(batches, 1):
            progress(job_id, f"reducing {purpose} evidence level {level}, "
                    f"batch {index}/{len(batches)}")
            allowed = {value for item in batch for value in _evidence_ids(item)}
            raw = llm.complete_json(
                "repository_map",
                get_prompt("repository_reduce")
                + f"\nThis reduction is preparing evidence for: {purpose}.",
                json.dumps(batch, sort_keys=True), max_tokens=3500,
                provider=provider, model=model,
            )
            reduced.append(_sanitize_reduce(
                raw if isinstance(raw, dict) else {}, allowed, batch))
        if len(reduced) >= len(items):
            # This can occur only when every item individually exceeds the
            # configured batch size. Map outputs are generation-bounded, but
            # fail transparently rather than truncating one.
            raise RuntimeError(
                "structured evidence summaries exceed the reduction batch budget; "
                "increase repository.analysis.reduce_batch_chars")
        items = reduced
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
    context = _hierarchical_context(job_id, summaries, purpose)
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
    progress(job_id, f"writing {title_prefix.lower()} ({model})")
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
    body = llm.complete(
        function, get_prompt(prompt_name), user,
        provider=provider, model=model, max_tokens=4_000).strip()
    _source, _snapshot, evidence = _snapshot_bundle(project_id)
    supplied_ids = {
        value for item in context for value in _evidence_ids(item)
    } | _nested_evidence_ids(facts)
    evidence = [item for item in evidence
                if str(item.get("evidence_id")) in supplied_ids]
    body, citation_count = _validate_and_render_citations(
        body, source, snapshot, evidence, require=True)
    if additional_warnings:
        coverage.setdefault("warnings", []).extend(additional_warnings)
    if artifact_type == "repo_inventory":
        body = _coverage_notice(coverage) + "\n\n" + body
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
