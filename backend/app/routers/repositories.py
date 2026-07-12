from __future__ import annotations

import json
import re

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from sqlmodel import select, text

from .. import library
from ..db import get_session
from ..models import Job, Project, RepositoryFile, RepositorySnapshot, RepositorySource, utcnow
from ..repository import (
    RepositoryError,
    check_repository_update,
    current_repository_snapshot,
    delete_github_token,
    get_github_token,
    github_token_configured,
    normalize_scope_patterns,
    preflight_repository,
    read_repository_file,
    repository_local_model,
    repository_scan_settings,
    repository_source_for_project,
    set_github_token,
    validate_repository_local_model,
)
from ..settings_store import set_settings_if_no_repository_jobs

router = APIRouter(prefix="/api/repositories", tags=["repositories"])

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
# ASCII source spelling avoids Windows code-page mojibake while the API still
# returns the familiar bullet mask.
_MASK = "\u2022\u2022\u2022\u2022set\u2022\u2022\u2022\u2022"


class CredentialRequest(BaseModel):
    token: str


@router.get("/credentials")
def get_credentials():
    configured = github_token_configured()
    return {"configured": configured, "token": _MASK if configured else ""}


@router.put("/credentials")
def save_credentials(req: CredentialRequest):
    try:
        set_github_token(req.token)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    return {"configured": True, "token": _MASK}


@router.delete("/credentials")
def clear_credentials():
    delete_github_token()
    return {"configured": False, "token": ""}


class RepositorySettingsRequest(BaseModel):
    local_model: str | None = None
    limits: dict[str, int] | None = None
    default_exclusions: list[str] | None = None


def _settings_payload() -> dict:
    scan = repository_scan_settings()
    exclusions = scan.pop("default_exclusions")
    return {
        "local_model": repository_local_model(),
        "limits": scan,
        "default_exclusions": exclusions,
        "host": "github.com",
        "static_only": True,
    }


@router.get("/settings")
def get_repository_settings():
    return _settings_payload()


@router.put("/settings")
def save_repository_settings(req: RepositorySettingsRequest):
    current = repository_scan_settings()
    updates: dict[str, object] = {}
    if req.local_model is not None:
        try:
            model = validate_repository_local_model(req.local_model)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        updates["repository.local_model"] = model

    ranges = {
        "max_download_bytes": (1024 * 1024, 5 * 1024 * 1024 * 1024),
        "max_unpacked_bytes": (1024 * 1024, 10 * 1024 * 1024 * 1024),
        "max_files": (1, 500_000),
        "max_file_bytes": (1024, 1024 * 1024 * 1024),
        "max_text_file_bytes": (1024, 100 * 1024 * 1024),
        "max_indexed_bytes": (1024, 2 * 1024 * 1024 * 1024),
        "chunk_lines": (10, 5000),
        "chunk_chars": (1000, 500_000),
        "max_compression_ratio": (2, 1000),
        "max_map_chunks": (1, 5000),
        "max_map_input_chars": (10_000, 100_000_000),
    }
    if req.limits is not None:
        unknown = set(req.limits) - set(ranges)
        if unknown:
            raise HTTPException(422, f"unknown repository limit(s): {', '.join(sorted(unknown))}")
        for key, value in req.limits.items():
            if isinstance(value, bool) or not isinstance(value, int) \
                    or not ranges[key][0] <= value <= ranges[key][1]:
                low, high = ranges[key]
                raise HTTPException(422, f"{key} must be between {low} and {high}")
            current[key] = value
    if req.default_exclusions is not None:
        try:
            current["default_exclusions"] = normalize_scope_patterns(req.default_exclusions)
        except ValueError as exc:
            raise HTTPException(422, str(exc))
    if current["max_text_file_bytes"] > current["max_file_bytes"]:
        raise HTTPException(422, "max_text_file_bytes cannot exceed max_file_bytes")
    if current["max_indexed_bytes"] > current["max_unpacked_bytes"]:
        raise HTTPException(422, "max_indexed_bytes cannot exceed max_unpacked_bytes")
    updates["repository.scan"] = current
    try:
        set_settings_if_no_repository_jobs(updates)
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    return _settings_payload()


class RepositoryPreflightRequest(BaseModel):
    url: str
    ref: str = ""
    include_paths: list[str] = Field(default_factory=list)
    exclude_paths: list[str] = Field(default_factory=list)


def _ref_kind(requested_ref: str) -> str:
    return "commit" if _SHA_RE.fullmatch(requested_ref.lower()) else "branch_or_tag"


def _preflight_source(data: dict) -> dict:
    return {
        "url": data["canonical_url"],
        "canonical_url": data["canonical_url"],
        "owner": data["owner"],
        "name": data["repository"],
        "repository": data["repository"],
        "full_name": f"{data['owner']}/{data['repository']}",
        "privacy": "private" if data["is_private"] else "public",
        "private": data["is_private"],
        "is_private": data["is_private"],
        "local_only": data["local_only"],
        "default_branch": data["default_branch"],
        "requested_ref": data["requested_ref"],
        "resolved_ref": data["resolved_sha"],
        "ref_kind": _ref_kind(data["requested_ref"]),
        "commit_sha": data["resolved_sha"],
        "commit_url": data["commit_url"],
        "commit_time": data["commit_time"],
        "description": data["description"],
        "include_paths": data["include_paths"],
        "exclude_paths": data["exclude_paths"],
    }


def _run_preflight(req: RepositoryPreflightRequest) -> dict:
    try:
        return preflight_repository(
            req.url, req.ref, include_paths=req.include_paths,
            exclude_paths=req.exclude_paths)
    except (ValueError, RepositoryError) as exc:
        raise HTTPException(422, str(exc))


@router.post("/preflight")
def preflight(req: RepositoryPreflightRequest):
    data = _run_preflight(req)
    source = _preflight_source(data)
    # Source aliases are also top-level for compatibility with early clients.
    return {
        **source,
        "source": source,
        "coverage_preview": data["coverage_preview"],
        "limits": data["limits"],
        "provider": "ollama",
        "local_model": repository_local_model(),
        "static_only": True,
        "warnings": data["warnings"],
    }


class RepositoryCreateRequest(RepositoryPreflightRequest):
    title: str | None = None
    analyze: bool = True
    expected_sha: str | None = None


def _source_payload(source: RepositorySource, snapshot: RepositorySnapshot | None = None) -> dict:
    commit_sha = snapshot.resolved_sha if snapshot else source.pending_sha
    return {
        "id": source.id,
        "project_id": source.project_id,
        "url": source.canonical_url,
        "canonical_url": source.canonical_url,
        "owner": source.owner,
        "name": source.repository,
        "repository": source.repository,
        "full_name": f"{source.owner}/{source.repository}",
        "privacy": "private" if source.is_private else "public",
        "private": source.is_private,
        "is_private": source.is_private,
        "local_only": True,
        "default_branch": source.default_branch,
        "requested_ref": source.requested_ref,
        "resolved_ref": commit_sha,
        "ref_kind": _ref_kind(source.requested_ref),
        "commit_sha": commit_sha,
        "description": source.description,
        "include_paths": json.loads(source.include_paths or "[]"),
        "exclude_paths": json.loads(source.exclude_paths or "[]"),
        "pending_sha": source.pending_sha or None,
        "cloud_purge_pending": bool(source.cloud_purge_pending),
    }


def _snapshot_payload(source: RepositorySource,
                      snapshot: RepositorySnapshot | None) -> dict | None:
    if snapshot:
        return {
            "id": snapshot.id,
            "project_id": source.project_id,
            "status": snapshot.status,
            "commit_sha": snapshot.resolved_sha,
            "requested_ref": snapshot.requested_ref,
            "resolved_ref": snapshot.resolved_sha,
            "archive_bytes": snapshot.archive_bytes,
            "expanded_bytes": snapshot.total_bytes,
            "file_count": snapshot.file_count,
            "indexed_file_count": snapshot.indexed_file_count,
            "indexed_bytes": snapshot.indexed_bytes,
            "excluded_file_count": snapshot.excluded_file_count,
            "omitted_links": json.loads(snapshot.omitted_links or "[]"),
            "manifest_hash": snapshot.manifest_hash,
            "path": snapshot.relative_path,
            "created": snapshot.created,
            "completed": snapshot.completed,
        }
    if source.pending_sha:
        return {
            "id": None,
            "project_id": source.project_id,
            "status": "pending",
            "commit_sha": source.pending_sha,
            "requested_ref": source.requested_ref,
            "resolved_ref": source.pending_sha,
            "archive_bytes": 0,
            "expanded_bytes": 0,
            "file_count": 0,
            "indexed_file_count": 0,
            "indexed_bytes": 0,
            "excluded_file_count": 0,
            "omitted_links": [],
            "manifest_hash": "",
            "path": "",
            "created": source.updated,
            "completed": None,
        }
    return None


def _coverage_payload(source: RepositorySource,
                      snapshot: RepositorySnapshot | None) -> dict:
    try:
        preview = json.loads(source.coverage_preview or "{}")
    except json.JSONDecodeError:
        preview = {}
    if not snapshot:
        return {"preview": preview, "ready": False}
    try:
        facts = json.loads(snapshot.facts or "{}")
    except json.JSONDecodeError:
        facts = {}
    scan_coverage = facts.get("coverage", {})
    if not isinstance(scan_coverage, dict):
        scan_coverage = {}
    return {
        "preview": preview,
        "ready": snapshot.status == "ready",
        "total_files": snapshot.file_count,
        "file_count": snapshot.file_count,
        "indexed_file_count": snapshot.indexed_file_count,
        "indexed_bytes": snapshot.indexed_bytes,
        "total_bytes": snapshot.total_bytes,
        "excluded_file_count": snapshot.excluded_file_count,
        "files_with_evidence": int(scan_coverage.get("files_with_evidence") or 0),
        "evidence_chunk_count": int(scan_coverage.get("evidence_chunk_count") or 0),
        "exclusion_reason_counts": scan_coverage.get("exclusion_reason_counts", {}),
        "omitted_link_count": len(json.loads(snapshot.omitted_links or "[]")),
        "secret_finding_count": snapshot.secret_finding_count,
        "languages": facts.get("languages", []),
        "frameworks": facts.get("frameworks", []),
    }


@router.post("")
def create_repository_project(req: RepositoryCreateRequest):
    data = _run_preflight(req)
    if req.expected_sha is not None:
        expected = req.expected_sha.strip().lower()
        if not _SHA_RE.fullmatch(expected):
            raise HTTPException(422, "expected_sha must be a full commit SHA")
        if data["resolved_sha"] != expected:
            raise HTTPException(
                409,
                "the selected ref moved after inspection; inspect the repository again",
            )
    display_title = (req.title or "").strip() or data["title"]
    slug = library.make_slug(display_title)
    with get_session() as session:
        base, number = slug, 1
        while session.exec(select(Project).where(Project.slug == slug)).first():
            number += 1
            slug = f"{base}-{number}"
        project = Project(
            slug=slug,
            title=display_title,
            source=data["canonical_url"],
            source_type="github",
            status="new",
        )
        session.add(project)
        session.flush()
        source = RepositorySource(
            project_id=project.id,
            owner=data["owner"],
            repository=data["repository"],
            canonical_url=data["canonical_url"],
            description=data["description"],
            requested_ref=data["requested_ref"],
            default_branch=data["default_branch"],
            is_private=data["is_private"],
            local_only=True,
            credential_ref="github.credentials.default" if data["is_private"] else "",
            include_paths=json.dumps(data["include_paths"]),
            exclude_paths=json.dumps(data["exclude_paths"]),
            coverage_preview=json.dumps(data["coverage_preview"]),
            pending_sha=data["resolved_sha"],
        )
        session.add(source)
        session.commit()
        session.refresh(project)
        session.refresh(source)
        return {
            "project": project.model_dump(),
            "source": _source_payload(source),
            "snapshot": _snapshot_payload(source, None),
            "coverage": _coverage_payload(source, None),
            "analyze": req.analyze,
            "warnings": data["warnings"],
        }


@router.get("/{project_id}")
def repository_detail(project_id: int):
    with get_session() as session:
        project = session.get(Project, project_id)
        source = repository_source_for_project(session, project_id)
        if not project or project.source_type != "github" or not source:
            raise HTTPException(404, "repository project was not found")
        snapshot = current_repository_snapshot(session, project_id, require_ready=False)
        return {
            "project": project.model_dump(),
            "source": _source_payload(source, snapshot if snapshot and snapshot.status == "ready" else None),
            "snapshot": _snapshot_payload(source, snapshot),
            "coverage": _coverage_payload(source, snapshot),
            "update": {
                "pending": bool(source.pending_sha),
                "target_sha": source.pending_sha or None,
            },
        }


@router.post("/{project_id}/check-update")
def check_update(project_id: int):
    with get_session() as session:
        source = repository_source_for_project(session, project_id)
        if not source:
            raise HTTPException(404, "repository project was not found")
        try:
            result = check_repository_update(source, session=session)
            session.commit()
            return result
        except RepositoryError as exc:
            raise HTTPException(422, str(exc))


class RepositoryUpdateRequest(BaseModel):
    analyze: bool = True
    target_sha: str | None = None


@router.post("/{project_id}/update")
def prepare_update(project_id: int, req: RepositoryUpdateRequest | None = None):
    request = req or RepositoryUpdateRequest()
    with get_session() as session:
        source = repository_source_for_project(session, project_id)
        if not source:
            raise HTTPException(404, "repository project was not found")
        active = session.exec(
            select(Job).where(
                Job.project_id == project_id,
                Job.status.in_(("queued", "running")),
            )
        ).first()
        if active:
            raise HTTPException(
                409,
                "wait for the active project run to finish before selecting an update",
            )
        try:
            update = check_repository_update(source, session=session)
        except RepositoryError as exc:
            raise HTTPException(422, str(exc))
        if request.target_sha is not None:
            requested_target = request.target_sha.strip().lower()
            if not _SHA_RE.fullmatch(requested_target):
                raise HTTPException(422, "target_sha must be a full commit SHA")
            if requested_target != update["target_sha"]:
                raise HTTPException(
                    409,
                    "the selected ref moved after the update check; check again before updating",
                )
        # Claim SQLite's writer lease only for the final state transition. A
        # run that started during the network check is observed and cannot be
        # switched to a different snapshot mid-pipeline.
        session.commit()
        session.exec(text("BEGIN IMMEDIATE"))
        active = session.exec(
            select(Job).where(
                Job.project_id == project_id,
                Job.status.in_(("queued", "running")),
            )
        ).first()
        if active:
            session.rollback()
            raise HTTPException(
                409,
                "a project run started during the update check; wait for it to finish",
            )
        source = repository_source_for_project(session, project_id)
        if not source:
            session.rollback()
            raise HTTPException(404, "repository project was not found")
        if update["changed"]:
            source.pending_sha = update["target_sha"]
            source.updated = utcnow()
            if source.is_private:
                source.local_only = True
            session.add(source)
            session.commit()
            session.refresh(source)
        return {
            **update,
            "analyze": request.analyze,
            "source": _source_payload(source),
            "snapshot": _snapshot_payload(source, None) if update["changed"] else None,
        }


@router.get("/{project_id}/files")
def repository_files(project_id: int, include_excluded: bool = False):
    with get_session() as session:
        snapshot = current_repository_snapshot(session, project_id)
        if not snapshot:
            raise HTTPException(409, "repository snapshot is not ready")
        query = select(RepositoryFile).where(RepositoryFile.snapshot_id == snapshot.id)
        if not include_excluded:
            query = query.where(RepositoryFile.excluded == False)  # noqa: E712
        rows = session.exec(query.order_by(RepositoryFile.path)).all()
        return [{
            "id": row.id,
            "path": row.path,
            "size_bytes": row.size_bytes,
            "line_count": row.line_count,
            "language": row.language,
            "role": row.role,
            "excluded": row.excluded,
            "exclusion_reason": row.exclusion_reason,
            "lfs_pointer": row.lfs_pointer,
            "submodule": row.submodule,
            "symlink": row.symlink,
        } for row in rows]


@router.get("/{project_id}/files/{file_id}")
def repository_file(project_id: int, file_id: int,
                    start_line: int = Query(default=1, ge=1),
                    end_line: int | None = Query(default=None, ge=1)):
    with get_session() as session:
        source = repository_source_for_project(session, project_id)
        row = session.get(RepositoryFile, file_id)
        snapshot = session.get(RepositorySnapshot, row.snapshot_id) if row else None
        if not source or not row or not snapshot or snapshot.source_id != source.id:
            raise HTTPException(404, "repository file was not found")
        if row.restricted:
            raise HTTPException(403, "secret-bearing repository files are not exposed")
        if row.excluded:
            raise HTTPException(
                415, "excluded repository files are not exposed in the source viewer")
        if row.binary or row.lfs_pointer or row.submodule:
            raise HTTPException(415, "repository file is not readable source text")
        final_line = min(row.line_count or start_line, end_line or start_line + 499)
        if final_line < start_line:
            raise HTTPException(422, "end_line must be at or after start_line")
        try:
            body = read_repository_file(
                snapshot, row.path, start_line=start_line, end_line=final_line)
        except RepositoryError as exc:
            raise HTTPException(422, str(exc))
        return {
            "id": row.id,
            "snapshot_id": snapshot.id,
            "commit_sha": snapshot.resolved_sha,
            "path": row.path,
            "start_line": start_line,
            "end_line": final_line,
            "body": body,
            "github_url": (
                f"{source.canonical_url}/blob/{snapshot.resolved_sha}/{row.path}"
                f"#L{start_line}-L{final_line}"
            ),
        }
