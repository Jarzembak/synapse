from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlmodel import select, text

from .. import library
from ..config import settings
from ..db import get_session
from ..models import (
    Artifact,
    Job,
    PaperChunk,
    PaperMemoryRevision,
    PaperPartEvidence,
    PaperSeries,
    PaperSeriesPart,
    PaperSource,
    Project,
    utcnow,
)

router = APIRouter(prefix="/api/papers", tags=["papers"])
series_router = APIRouter(prefix="/api/paper-series", tags=["paper-series"])

# Environment settings may tighten v1 admission, never raise its hard bounds.
MAX_PAPER_BYTES = min(int(settings.max_paper_upload_bytes), 250 * 1024 * 1024)
MAX_PAPER_PAGES = min(int(settings.max_paper_pages), 500)
MAX_PAPER_CHARACTERS = min(int(settings.max_paper_extracted_chars), 5_000_000)
OCR_LANGUAGES = {
    value.strip().lower()
    for value in settings.paper_ocr_languages.split(",") if value.strip()
}
AUDIENCES = {"generalist", "practitioner", "expert"}
PART_STEPS = {"guide", "script", "audio"}


def _json_load(value: str | None, fallback):
    try:
        parsed = json.loads(value or "")
    except (TypeError, json.JSONDecodeError):
        return fallback
    return parsed if isinstance(parsed, type(fallback)) else fallback


def _canonical_hash(value: object) -> str:
    body = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _ocr_languages(value: str | list[str]) -> list[str]:
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
            raw = decoded if isinstance(decoded, list) else value.split(",")
        except json.JSONDecodeError:
            raw = value.split(",")
    else:
        raw = value
    languages = list(dict.fromkeys(str(item).strip().lower() for item in raw if str(item).strip()))
    unknown = set(languages) - OCR_LANGUAGES
    if unknown:
        raise HTTPException(
            422,
            "unsupported OCR language(s): " + ", ".join(sorted(unknown))
            + "; choose eng, spa, fra, or deu",
        )
    if not languages:
        raise HTTPException(422, "choose at least one OCR language")
    return languages


def _audiences(value: str | list[str]) -> list[str]:
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
            raw = decoded if isinstance(decoded, list) else value.split(",")
        except json.JSONDecodeError:
            raw = value.split(",")
    else:
        raw = value
    audiences = list(dict.fromkeys(
        str(item).strip().lower() for item in raw if str(item).strip()
    ))
    unknown = set(audiences) - AUDIENCES
    if unknown:
        raise HTTPException(
            422,
            "unsupported audience(s): " + ", ".join(sorted(unknown))
            + "; choose generalist, practitioner, or expert",
        )
    if not audiences:
        raise HTTPException(422, "choose at least one audience track")
    return audiences


def _paper_rows(session, project_id: int) -> tuple[Project, PaperSource]:
    project = session.get(Project, project_id)
    source = session.exec(
        select(PaperSource).where(PaperSource.project_id == project_id)
    ).first()
    if not project or project.source_type != "paper" or not source:
        raise HTTPException(404, "paper project was not found")
    return project, source


def _poor_pages(source: PaperSource) -> list[int]:
    report = _json_load(source.quality_report, {})
    values = report.get("poor_pages", [])
    pages: set[int] = set()
    for value in values if isinstance(values, list) else []:
        candidate = value.get("page") if isinstance(value, dict) else value
        try:
            if int(candidate) > 0:
                pages.add(int(candidate))
        except (TypeError, ValueError):
            continue
    page_rows = report.get("pages", [])
    for row in page_rows if isinstance(page_rows, list) else []:
        if not isinstance(row, dict) or str(row.get("grade", "")).upper() != "POOR":
            continue
        try:
            if int(row.get("page")) > 0:
                pages.add(int(row["page"]))
        except (TypeError, ValueError):
            continue
    if source.quality_grade.upper() == "POOR" and not pages and source.page_count == 1:
        pages.add(1)
    return sorted(pages)


def _acknowledgements(source: PaperSource) -> list[dict]:
    rows = _json_load(source.acknowledged_pages, [])
    return [row for row in rows if isinstance(row, dict)]


def _quality_report_payload(source: PaperSource) -> dict:
    report = _json_load(source.quality_report, {})
    pages = []
    for raw in report.get("pages", []) if isinstance(report.get("pages", []), list) else []:
        if not isinstance(raw, dict):
            continue
        row = dict(raw)
        if row.get("page") is None and row.get("page_number") is not None:
            row["page"] = row["page_number"]
        pages.append(row)
    report["pages"] = pages
    blockers = []
    for raw in report.get("blocked_reasons", []) if isinstance(report.get("blocked_reasons", []), list) else []:
        if not isinstance(raw, dict):
            continue
        row = dict(raw)
        if row.get("page") is None and row.get("page_number") is not None:
            row["page"] = row["page_number"]
        blockers.append(row)
    report["blocked_reasons"] = blockers
    return report


def _unacknowledged_poor_pages(source: PaperSource) -> list[int]:
    acknowledged = {
        int(row["page"])
        for row in _acknowledgements(source)
        if str(row.get("page", "")).isdigit() and str(row.get("reason", "")).strip()
    }
    return [page for page in _poor_pages(source) if page not in acknowledged]


def _remaining_quality_blockers(source: PaperSource) -> list[dict]:
    acknowledged = {
        int(row["page"])
        for row in _acknowledgements(source)
        if str(row.get("page", "")).isdigit() and str(row.get("reason", "")).strip()
    }
    report = _quality_report_payload(source)
    blockers = []
    for reason in report.get("blocked_reasons", []):
        if not isinstance(reason, dict):
            continue
        page = reason.get("page", reason.get("page_number"))
        try:
            if page is not None and int(page) in acknowledged:
                continue
        except (TypeError, ValueError):
            pass
        blockers.append(reason)
    if not blockers:
        blockers.extend({"kind": "page", "page": page, "grade": "POOR"}
                        for page in _unacknowledged_poor_pages(source))
    # Fail closed for a parser's document-level POOR signal even if an older
    # report omitted the structured blocker detail.
    if (
        report.get("analysis_blocked")
        and not blockers
        and source.quality_grade.upper() == "POOR"
        and not _poor_pages(source)
    ):
        blockers.append({"kind": "document", "grade": "POOR"})
    return blockers


def _source_payload(project: Project, source: PaperSource) -> dict:
    acknowledgements = _acknowledgements(source)
    poor = _poor_pages(source)
    return {
        "id": source.id,
        "project_id": source.project_id,
        "filename": source.original_filename,
        "original_filename": source.original_filename,
        "source_hash": source.source_hash,
        "sha256": source.source_hash,
        "relative_path": source.relative_path,
        "path": source.relative_path,
        "size_bytes": source.size_bytes,
        "page_count": source.page_count,
        "extracted_characters": source.extracted_characters,
        "character_count": source.extracted_characters,
        "ocr_languages": _json_load(source.ocr_languages, ["eng"]),
        "local_only": source.local_only,
        "privacy_locked": source.privacy_locked,
        "parser_version": source.parser_version,
        "parser_config_hash": source.parser_config_hash,
        "status": source.status,
        "quality_grade": source.quality_grade,
        "quality_report": _quality_report_payload(source),
        "poor_pages": poor,
        "acknowledged_pages": acknowledgements,
        "unacknowledged_poor_pages": _unacknowledged_poor_pages(source),
        "analysis_blocked": bool(_remaining_quality_blockers(source)),
        "cloud_sync_excluded": True,
        "error": source.error,
        "pdf_url": f"/api/papers/{project.id}/source",
        "limits": {
            "max_bytes": MAX_PAPER_BYTES,
            "max_pages": MAX_PAPER_PAGES,
            "max_extracted_characters": MAX_PAPER_CHARACTERS,
        },
        "created": source.created,
        "updated": source.updated,
    }


def _coverage_payload(source: PaperSource) -> dict:
    coverage = _json_load(source.coverage_report, {})
    topics = coverage.get("topics", [])
    if not isinstance(topics, list):
        topics = []
    coverage.setdefault("topics", topics)
    coverage.setdefault(
        "critical_total",
        sum(1 for topic in topics if isinstance(topic, dict) and topic.get("importance") == "critical"),
    )
    coverage.setdefault("critical_assigned", 0)
    coverage.setdefault("critical_omitted", 0)
    evidence_blocks = int(
        coverage.get("evidence_blocks")
        or coverage.get("total_evidence_blocks")
        or coverage.get("evidence_block_count")
        or 0
    )
    mapped_blocks = int(
        coverage.get("mapped_blocks")
        or coverage.get("mapped_evidence_blocks")
        or 0
    )
    pages_admitted = int(
        coverage.get("pages_admitted")
        or coverage.get("pages_with_evidence")
        or 0
    )
    coverage["evidence_blocks"] = evidence_blocks
    coverage["mapped_blocks"] = mapped_blocks
    coverage["pages_total"] = int(coverage.get("pages_total") or source.page_count or 0)
    coverage["pages_admitted"] = pages_admitted
    coverage.setdefault(
        "percent",
        round(100 * mapped_blocks / evidence_blocks) if evidence_blocks else 0,
    )
    coverage["analysis_blocked"] = bool(_remaining_quality_blockers(source))
    coverage["acknowledged_gaps"] = _acknowledgements(source)
    coverage["unacknowledged_poor_pages"] = _unacknowledged_poor_pages(source)
    return coverage


def _series_coverage(source: PaperSource, series: PaperSeries) -> dict:
    coverage = _coverage_payload(source)
    plan = _json_load(series.plan_json, {})
    topics = coverage.get("topics", [])
    if not topics:
        topics = plan.get("topics") or plan.get("critical_topics") or []
    plan_parts = [row for row in plan.get("parts", []) if isinstance(row, dict)]
    omissions = {
        str(row.get("topic_id")): row
        for row in plan.get("omissions", []) if isinstance(row, dict) and row.get("topic_id")
    }
    enriched = []
    for raw in topics if isinstance(topics, list) else []:
        if not isinstance(raw, dict):
            continue
        topic = dict(raw)
        topic_id = str(topic.get("id") or topic.get("topic_id") or "")
        topic_evidence = {str(value) for value in topic.get("evidence_ids", [])}
        assigned = None
        for part in plan_parts:
            explicit_topics = {str(value) for value in part.get("topics", [])}
            part_evidence = {
                str(value.get("evidence_id"))
                for value in part.get("evidence", []) if isinstance(value, dict)
                and value.get("role") == "primary"
            }
            if topic_id in explicit_topics or (topic_evidence and topic_evidence & part_evidence):
                assigned = part
                break
        omission = omissions.get(topic_id)
        topic["id"] = topic_id
        topic["assigned_part_id"] = assigned.get("id") if assigned else None
        topic["assigned_part"] = assigned.get("position") if assigned else None
        topic["omitted"] = omission is not None
        topic["omission_reason"] = omission.get("reason") if omission else None
        enriched.append(topic)
    critical = [topic for topic in enriched if topic.get("importance") == "critical"]
    major = [topic for topic in enriched if topic.get("importance") == "major"]
    coverage["topics"] = enriched
    coverage["critical_total"] = len(critical)
    coverage["critical_assigned"] = sum(bool(topic.get("assigned_part_id")) for topic in critical)
    coverage["critical_omitted"] = sum(bool(topic.get("omitted")) for topic in critical)
    coverage["major_total"] = len(major)
    coverage["major_assigned"] = sum(bool(topic.get("assigned_part_id")) for topic in major)
    covered = coverage["critical_assigned"] + coverage["critical_omitted"]
    coverage["percent"] = round(100 * covered / len(critical)) if critical else 100
    coverage["complete"] = covered == len(critical)
    return coverage


def _part_payload(session, part: PaperSeriesPart, *, include_evidence: bool = True) -> dict:
    payload = part.model_dump()
    payload["paper_series_id"] = part.series_id
    payload["structure_locked"] = _part_locked(part)
    payload["locked"] = _part_locked(part)
    if include_evidence:
        assignments = session.exec(
            select(PaperPartEvidence).where(PaperPartEvidence.part_id == part.id)
        ).all()
        evidence = []
        for assignment in assignments:
            chunk = session.get(PaperChunk, assignment.chunk_id)
            if not chunk:
                continue
            evidence.append({
                "evidence_id": chunk.evidence_id,
                "page": chunk.page_number,
                "section": chunk.section_path,
                "role": assignment.role,
                "importance": assignment.importance,
                "reason": assignment.reason,
            })
        payload["evidence"] = sorted(evidence, key=lambda row: (row["page"], row["evidence_id"]))
        payload["assignments"] = payload["evidence"]
        payload["evidence_ids"] = [row["evidence_id"] for row in payload["evidence"]]
    return payload


def _active_track_job(session, series_id: int) -> Job | None:
    """Return any queued/running job that can mutate an audience track.

    Series-wide and part-scoped jobs use separate database indexes so tracks
    can run independently.  Within one track they must still be serialized:
    scripts and immutable memory revisions form a single continuity chain.
    """
    return session.exec(select(Job).where(
        Job.paper_series_id == series_id,
        Job.status.in_(("queued", "running")),
    ).order_by(Job.created)).first()


def _current_memory_revision(session, series_id: int) -> PaperMemoryRevision | None:
    """Return the head of the currently valid, consecutive script chain.

    Old revisions remain immutable after an earlier part is regenerated, but
    stale future revisions must not be presented as the active series bible.
    """
    parts = session.exec(select(PaperSeriesPart).where(
        PaperSeriesPart.series_id == series_id
    ).order_by(PaperSeriesPart.position)).all()
    head = None
    for part in parts:
        if part.script_status not in {"done", "complete"}:
            break
        revision = session.exec(select(PaperMemoryRevision).where(
            PaperMemoryRevision.series_id == series_id,
            PaperMemoryRevision.part_id == part.id,
        ).order_by(PaperMemoryRevision.revision.desc())).first()
        if revision is None:
            break
        if head is not None and revision.parent_revision_id != head.id:
            break
        head = revision
    return head


def _series_payload(session, series: PaperSeries, *, detail: bool = True) -> dict:
    payload = series.model_dump()
    payload["plan"] = _json_load(series.plan_json, {})
    payload["omissions"] = payload["plan"].get("omissions", [])
    source = session.exec(select(PaperSource).where(
        PaperSource.project_id == series.project_id
    )).first()
    if source:
        payload["coverage"] = _series_coverage(source, series)
        payload["plan"].setdefault("topics", payload["coverage"].get("topics", []))
        payload["plan"].setdefault("coverage", payload["coverage"])
    parts = session.exec(
        select(PaperSeriesPart).where(PaperSeriesPart.series_id == series.id)
        .order_by(PaperSeriesPart.position)
    ).all()
    payload["parts"] = [_part_payload(session, part, include_evidence=detail) for part in parts]
    latest_memory = _current_memory_revision(session, series.id)
    payload["memory_revision"] = (
        {
            **latest_memory.model_dump(),
            "paper_series_id": latest_memory.series_id,
            "paper_part_id": latest_memory.part_id,
            "parent_id": latest_memory.parent_revision_id,
            "state": _json_load(latest_memory.state_json, {}),
        }
        if latest_memory else None
    )
    if detail:
        payload["artifacts"] = [
            artifact.model_dump()
            for artifact in session.exec(
                select(Artifact).where(Artifact.paper_series_id == series.id)
                .order_by(Artifact.created)
            ).all()
        ]
        payload["jobs"] = [
            job.model_dump()
            for job in session.exec(
                select(Job).where(Job.paper_series_id == series.id)
                .order_by(Job.created.desc())
            ).all()
        ]
    return payload


def _paper_payload(session, project: Project, source: PaperSource) -> dict:
    artifacts = session.exec(
        select(Artifact).where(Artifact.project_id == project.id)
        .order_by(Artifact.created)
    ).all()
    series = session.exec(
        select(PaperSeries).where(PaperSeries.project_id == project.id)
        .order_by(PaperSeries.created)
    ).all()
    jobs = session.exec(
        select(Job).where(Job.project_id == project.id).order_by(Job.created.desc())
    ).all()
    page_issues = []
    report = _quality_report_payload(source)
    for row in report.get("pages", []) if isinstance(report.get("pages", []), list) else []:
        if not isinstance(row, dict) or str(row.get("grade", "")).upper() != "POOR":
            continue
        page = row.get("page_number", row.get("page"))
        if page:
            page_issues.append({**row, "page": page})
    return {
        "project": project.model_dump(),
        "source": _source_payload(project, source),
        "quality": {
            "grade": source.quality_grade,
            "report": report,
            "poor_pages": _poor_pages(source),
            "page_issues": page_issues,
            "acknowledged_pages": _acknowledgements(source),
            "unacknowledged_poor_pages": _unacknowledged_poor_pages(source),
            "blocked": bool(_remaining_quality_blockers(source)),
        },
        "coverage": _coverage_payload(source),
        "artifacts": [artifact.model_dump() for artifact in artifacts],
        "shared_artifacts": [
            artifact.model_dump() for artifact in artifacts
            if artifact.paper_series_id is None and artifact.paper_part_id is None
        ],
        "series": [_series_payload(session, item, detail=False) for item in series],
        "jobs": [job.model_dump() for job in jobs],
    }


def _dispatch_job(session, job: Job, task_name: str, args: list) -> Job:
    """Commit the durable job before dispatch and fail it closed on broker errors."""
    session.add(job)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(409, f"{task_name} was started concurrently") from exc
    session.refresh(job)
    try:
        # Kept local so API imports do not load the parser/model task tree.
        from ..tasks.celery_app import celery

        result = celery.send_task(task_name, args=args)
    except Exception as exc:
        job.status = "error"
        job.error = f"could not dispatch to worker: {exc}"
        job.finished = utcnow()
        job.updated = utcnow()
        session.add(job)
        session.commit()
        raise HTTPException(
            503,
            "worker queue is unavailable; the paper was preserved and the job was not left queued",
        ) from exc
    job.celery_id = result.id
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


def _queue_job(
    session,
    *,
    project_id: int,
    task_name: str,
    task_args: list,
    series_id: int | None = None,
    part_id: int | None = None,
    options: dict | None = None,
) -> Job:
    active = session.exec(
        select(Job).where(
            Job.project_id == project_id,
            Job.task == task_name,
            Job.paper_series_id == series_id,
            Job.paper_part_id == part_id,
            Job.status.in_(("queued", "running")),
        )
    ).first()
    if active:
        raise HTTPException(409, f"{task_name} is already {active.status} for this scope")
    source = session.exec(
        select(PaperSource).where(PaperSource.project_id == project_id)
    ).first()
    if source and not source.privacy_locked:
        source.privacy_locked = True
        source.updated = utcnow()
        session.add(source)
    job = Job(
        project_id=project_id,
        paper_series_id=series_id,
        paper_part_id=part_id,
        task=task_name,
        options=json.dumps(options or {}, sort_keys=True),
    )
    session.add(job)
    session.flush()
    return _dispatch_job(session, job, task_name, [job.id, *task_args])


def _queue_paper_run_all(session, project_id: int, *, force: bool = False) -> Job:
    """Freeze and queue the extraction→analysis import pipeline."""
    active = session.exec(
        select(Job).where(
            Job.project_id == project_id,
            Job.paper_series_id == None,  # noqa: E711
            Job.paper_part_id == None,  # noqa: E711
            Job.status.in_(("queued", "running")),
        )
    ).first()
    if active:
        raise HTTPException(409, "wait for the active paper pipeline job to finish")
    source = session.exec(
        select(PaperSource).where(PaperSource.project_id == project_id)
    ).one()
    source.privacy_locked = True
    source.updated = utcnow()
    session.add(source)
    steps = ["paper_extract", "paper_analyze"]
    job = Job(
        project_id=project_id,
        task="run_all",
        status="queued",
        options=json.dumps({
            "profile": "paper",
            "steps": steps,
            "force_steps": steps if force else [],
        }, sort_keys=True),
    )
    session.add(job)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(409, "a paper pipeline was queued concurrently") from exc
    session.refresh(job)
    # Starts immediately when no other project run-all owns the serial slot.
    from ..tasks.orchestrate import maybe_start_next_run_all

    maybe_start_next_run_all()
    session.refresh(job)
    return job


def _new_project_slug(session, title: str) -> str:
    slug = library.make_slug(title)
    base, suffix = slug, 1
    while session.exec(select(Project).where(Project.slug == slug)).first():
        suffix += 1
        slug = f"{base}-{suffix}"
    return slug


def _verify_pdf(path: Path, size_bytes: int) -> None:
    if not size_bytes:
        raise HTTPException(400, "the uploaded PDF is empty")
    if size_bytes > MAX_PAPER_BYTES:
        raise HTTPException(413, "paper PDFs must be 250 MiB or smaller")
    with path.open("rb") as handle:
        if b"%PDF-" not in handle.read(1024):
            raise HTTPException(415, "the selected file is not a PDF")


def _create_paper_project(
    staged_path: Path,
    *,
    filename: str,
    title: str,
    size_bytes: int,
    source_hash: str,
    languages: list[str],
    audiences: list[str],
    local_only: bool,
) -> tuple[int, int]:
    display_title = title.strip() or Path(filename).stem or "Research paper"
    project_dir: Path | None = None
    sidecar_path: Path | None = None
    with get_session() as session:
        slug = _new_project_slug(session, display_title)
        project = Project(
            slug=slug,
            title=display_title,
            source="source/original.pdf",
            source_type="paper",
            status="new",
        )
        session.add(project)
        session.flush()
        pdf_rel = f"projects/{slug}/source/original.pdf"
        sidecar_rel = f"projects/{slug}/source.md"
        project.source = pdf_rel
        destination = library.lib_path(pdf_rel)
        project_dir = library.lib_path(f"projects/{slug}")
        sidecar_path = library.lib_path(sidecar_rel)
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.replace(staged_path, destination)
            source = PaperSource(
                project_id=project.id,
                original_filename=Path(filename).name,
                source_hash=source_hash,
                relative_path=pdf_rel,
                size_bytes=size_bytes,
                ocr_languages=json.dumps(languages),
                local_only=local_only,
                parser_config_hash=_canonical_hash({
                    "ocr_languages": languages,
                    "max_pages": MAX_PAPER_PAGES,
                    "max_extracted_characters": MAX_PAPER_CHARACTERS,
                    "local_only": local_only,
                }),
            )
            session.add(source)
            for audience in audiences:
                session.add(PaperSeries(
                    project_id=project.id,
                    audience=audience,
                    title=f"{display_title} — {audience.title()}",
                    target_minutes=settings.paper_target_minutes,
                    max_parts=settings.paper_max_parts,
                    plan_json=json.dumps({
                        "parts": [], "omissions": [], "topics": [],
                        "critical_topics": [],
                    }),
                ))
            artifact = Artifact(
                project_id=project.id,
                type="source_paper",
                title=f"Source PDF — {display_title}",
                path=sidecar_rel,
                media_path=pdf_rel,
                input_hash=source_hash,
                config_hash=source.parser_config_hash,
                provenance=json.dumps({
                    "source_hash": source_hash,
                    "filename": Path(filename).name,
                    "ocr_languages": languages,
                }, sort_keys=True),
                restricted=local_only,
                cloud_sync_excluded=True,
            )
            session.add(artifact)
            session.flush()
            body = (
                "# Immutable source PDF\n\n"
                f"Original filename: `{Path(filename).name}`  \n"
                f"SHA-256: `{source_hash}`  \n"
                "The source PDF is retained locally for page-grounded citations."
            )
            library._write_doc(sidecar_rel, {  # noqa: SLF001 - transactional library write
                "id": artifact.id,
                "type": artifact.type,
                "title": artifact.title,
                "project": slug,
                "project_id": project.id,
                "source_type": "paper",
                "media": pdf_rel,
                "source_hash": source_hash,
                "cloud_sync_excluded": True,
                "restricted": local_only or None,
            }, body)
            library.sync_fts(session, artifact, body)
            library.sync_search_chunks(session, artifact, body)
            session.commit()
            session.refresh(project)
            session.refresh(source)
            return project.id, source.id
        except Exception:
            session.rollback()
            if project_dir:
                shutil.rmtree(project_dir, ignore_errors=True)
            elif sidecar_path:
                sidecar_path.unlink(missing_ok=True)
            raise


def _staging_file(suffix: str = ".pdf") -> tuple[object, Path]:
    staging = settings.library_dir / ".paper-imports"
    staging.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w+b", prefix="paper-", suffix=suffix, dir=staging, delete=False
    )
    return handle, Path(handle.name)


@router.post("/upload")
async def upload_paper(
    file: UploadFile = File(...),
    title: str = Form(default=""),
    ocr_languages: str = Form(default='["eng"]'),
    audiences: str = Form(default='["generalist"]'),
    local_only: bool = Form(default=True),
    analyze: bool = Form(default=True),
):
    filename = Path(file.filename or "paper.pdf").name
    if Path(filename).suffix.lower() != ".pdf":
        raise HTTPException(415, "paper imports must be PDF files")
    languages = _ocr_languages(ocr_languages)
    selected_audiences = _audiences(audiences)
    handle, staged = _staging_file()
    size = 0
    digest = hashlib.sha256()
    try:
        with handle:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_PAPER_BYTES:
                    raise HTTPException(413, "paper PDFs must be 250 MiB or smaller")
                digest.update(chunk)
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        _verify_pdf(staged, size)
        project_id, _source_id = _create_paper_project(
            staged,
            filename=filename,
            title=title,
            size_bytes=size,
            source_hash=digest.hexdigest(),
            languages=languages,
            audiences=selected_audiences,
            local_only=local_only,
        )
        if analyze:
            with get_session() as session:
                job = _queue_paper_run_all(session, project_id)
        else:
            job = None
        with get_session() as session:
            project, source = _paper_rows(session, project_id)
            return {**_paper_payload(session, project, source), "job": job.model_dump() if job else None}
    finally:
        staged.unlink(missing_ok=True)
        await file.close()


class PaperImportRequest(BaseModel):
    path: str
    title: str | None = None
    ocr_languages: list[str] = Field(default_factory=lambda: ["eng"])
    audiences: list[Literal["generalist", "practitioner", "expert"]] = Field(
        default_factory=lambda: ["generalist"]
    )
    local_only: bool = True
    analyze: bool = True


@router.post("")
def import_local_paper(req: PaperImportRequest):
    languages = _ocr_languages(req.ocr_languages)
    selected_audiences = _audiences(req.audiences)
    mount = settings.host_media_mount.resolve()
    supplied = Path(req.path)
    candidate = supplied if supplied.is_absolute() else mount / supplied
    try:
        source_path = candidate.resolve(strict=True)
    except (FileNotFoundError, OSError) as exc:
        raise HTTPException(404, "mounted PDF path was not found") from exc
    try:
        source_path.relative_to(mount)
    except ValueError as exc:
        raise HTTPException(403, "local paper paths must be inside the mounted media directory") from exc
    if not source_path.is_file() or source_path.suffix.lower() != ".pdf":
        raise HTTPException(415, "paper imports must be PDF files")
    handle, staged = _staging_file()
    size = 0
    digest = hashlib.sha256()
    try:
        with handle, source_path.open("rb") as source_handle:
            while True:
                chunk = source_handle.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_PAPER_BYTES:
                    raise HTTPException(413, "paper PDFs must be 250 MiB or smaller")
                digest.update(chunk)
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        _verify_pdf(staged, size)
        project_id, _source_id = _create_paper_project(
            staged,
            filename=source_path.name,
            title=req.title or "",
            size_bytes=size,
            source_hash=digest.hexdigest(),
            languages=languages,
            audiences=selected_audiences,
            local_only=req.local_only,
        )
        if req.analyze:
            with get_session() as session:
                job = _queue_paper_run_all(session, project_id)
        else:
            job = None
        with get_session() as session:
            project, source = _paper_rows(session, project_id)
            return {**_paper_payload(session, project, source), "job": job.model_dump() if job else None}
    finally:
        staged.unlink(missing_ok=True)


@router.get("/{project_id}")
def paper_detail(project_id: int):
    with get_session() as session:
        project, source = _paper_rows(session, project_id)
        return _paper_payload(session, project, source)


@router.get("/{project_id}/source")
def paper_source_pdf(project_id: int):
    with get_session() as session:
        project, source = _paper_rows(session, project_id)
        path = library.lib_path(source.relative_path).resolve()
        root = settings.library_dir.resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise HTTPException(500, "stored paper path is outside the library") from exc
        if not path.is_file():
            raise HTTPException(404, "source PDF is missing")
        return FileResponse(
            path,
            media_type="application/pdf",
            filename=source.original_filename,
            content_disposition_type="inline",
            headers={"ETag": f'"{source.source_hash}"', "Cache-Control": "private, max-age=0"},
        )


@router.get("/{project_id}/evidence")
def paper_evidence(
    project_id: int,
    page: int | None = Query(default=None, ge=1, le=MAX_PAPER_PAGES),
    kind: str | None = None,
):
    with get_session() as session:
        _project, source = _paper_rows(session, project_id)
        query = select(PaperChunk).where(PaperChunk.source_id == source.id)
        if page is not None:
            query = query.where(PaperChunk.page_number == page)
        if kind:
            query = query.where(PaperChunk.kind == kind)
        chunks = session.exec(query.order_by(PaperChunk.chunk_index)).all()
        return [{
            **chunk.model_dump(),
            "bbox": _json_load(chunk.bbox, {}),
            "flags": _json_load(chunk.flags, []),
            "pdf_url": f"/api/papers/{project_id}/source#page={chunk.page_number}",
        } for chunk in chunks]


@router.post("/{project_id}/rerun-extraction")
def rerun_extraction(project_id: int):
    with get_session() as session:
        project, source = _paper_rows(session, project_id)
        if project.deleting:
            raise HTTPException(409, "project is being deleted")
        active_track = session.exec(select(Job).where(
            Job.project_id == project_id,
            Job.paper_series_id != None,  # noqa: E711
            Job.status.in_(("queued", "running")),
        )).first()
        if active_track:
            raise HTTPException(409, "wait for active audience-track work to finish")
        planned_tracks = session.exec(select(PaperSeries).where(
            PaperSeries.project_id == project_id,
            PaperSeries.plan_version > 0,
        )).all()
        if planned_tracks:
            # Extraction atomically replaces PaperChunk rows.  Approved/draft
            # plans refer to those rows and their stable evidence IDs, so a
            # rerun cannot silently strand assignments or preserve scripts
            # against a different parser result.  Empty pre-analysis audience
            # selections (plan_version=0) are safe and will be replanned.
            raise HTTPException(
                409,
                "delete planned audience tracks before rerunning extraction; "
                "the immutable source PDF will be retained",
            )
        source.status = "pending"
        source.error = ""
        source.updated = utcnow()
        session.add(source)
        job = _queue_paper_run_all(session, project_id, force=True)
        return job


@router.post("/{project_id}/analyze")
def analyze_paper(project_id: int):
    from ..paper import PaperAnalysisBlocked, require_analysis_ready

    with get_session() as session:
        project, source = _paper_rows(session, project_id)
        if project.deleting:
            raise HTTPException(409, "project is being deleted")
        try:
            require_analysis_ready(source)
        except PaperAnalysisBlocked as exc:
            raise HTTPException(409, str(exc)) from exc
        return _queue_job(
            session,
            project_id=project_id,
            task_name="paper_analyze",
            task_args=[project_id],
            options={"source_hash": source.source_hash},
        )


class PageAcknowledgement(BaseModel):
    page: int = Field(ge=1, le=MAX_PAPER_PAGES)
    reason: str = Field(min_length=3, max_length=1000)


class PageAcknowledgementRequest(BaseModel):
    pages: list[PageAcknowledgement] = Field(min_length=1)


@router.post("/{project_id}/acknowledgements")
@router.post("/{project_id}/acknowledge-pages", include_in_schema=False)
def acknowledge_pages(project_id: int, req: PageAcknowledgementRequest):
    with get_session() as session:
        project, source = _paper_rows(session, project_id)
        if source.status not in {"review_required", "ready_with_acknowledged_gaps", "ready"}:
            raise HTTPException(409, "extract the paper before acknowledging page gaps")
        poor = set(_poor_pages(source))
        unknown = sorted({row.page for row in req.pages} - poor)
        if unknown:
            raise HTTPException(422, f"page(s) are not named POOR pages: {unknown}")
        by_page = {int(row["page"]): row for row in _acknowledgements(source) if "page" in row}
        for row in req.pages:
            by_page[row.page] = {
                "page": row.page,
                "reason": row.reason.strip(),
                "created": utcnow().isoformat(),
            }
        source.acknowledged_pages = json.dumps(
            [by_page[page] for page in sorted(by_page)], sort_keys=True
        )
        # The acknowledgement ledger keeps the gap visible.  ``ready`` is the
        # engine's admitted status; it does not imply the acknowledged pages
        # became high-quality evidence.
        source.status = (
            "ready" if not _remaining_quality_blockers(source)
            else "review_required"
        )
        source.updated = utcnow()
        session.add(source)
        session.commit()
        session.refresh(source)
        payload = {
            "source": _source_payload(project, source),
            "quality": {
                "poor_pages": _poor_pages(source),
                "acknowledged_pages": _acknowledgements(source),
                "unacknowledged_poor_pages": _unacknowledged_poor_pages(source),
                "blocked": bool(_remaining_quality_blockers(source)),
            },
        }
    # Once the last named blocker is acknowledged, resume the paused analysis
    # when no root run is still winding down.  A dedicated /analyze endpoint is
    # also available if the prior run has not finished yet.
    analysis_job = None
    analysis_queue_error = None
    if not payload["quality"]["blocked"]:
        with get_session() as session:
            active = session.exec(
                select(Job).where(
                    Job.project_id == project_id,
                    Job.paper_series_id == None,  # noqa: E711
                    Job.paper_part_id == None,  # noqa: E711
                    Job.status.in_(("queued", "running")),
                )
            ).first()
            if not active:
                try:
                    analysis_job = _queue_job(
                        session,
                        project_id=project_id,
                        task_name="paper_analyze",
                        task_args=[project_id],
                        options={"acknowledged_pages": [row.page for row in req.pages]},
                    )
                except HTTPException as exc:
                    if exc.status_code != 503:
                        raise
                    # The acknowledgement is authoritative and already
                    # committed. Expose the queue issue without making the UI
                    # tell the user their review decision was lost.
                    analysis_queue_error = str(exc.detail)
    payload["analysis_job"] = analysis_job.model_dump() if analysis_job else None
    payload["analysis_queue_error"] = analysis_queue_error
    return payload


class PaperSeriesCreateRequest(BaseModel):
    audience: Literal["generalist", "practitioner", "expert"]
    title: str = ""
    target_minutes: int = Field(default=50, ge=40, le=60)
    user_guidance: str = Field(default="", max_length=10_000)
    auto_plan: bool = True


@router.post("/{project_id}/series")
def create_paper_series(project_id: int, req: PaperSeriesCreateRequest):
    with get_session() as session:
        project, source = _paper_rows(session, project_id)
        if project.deleting:
            raise HTTPException(409, "project is being deleted")
        existing = session.exec(
            select(PaperSeries).where(
                PaperSeries.project_id == project_id,
                PaperSeries.audience == req.audience,
            )
        ).first()
        if existing:
            raise HTTPException(409, f"the {req.audience} track already exists")
        series = PaperSeries(
            project_id=project_id,
            audience=req.audience,
            title=req.title.strip() or f"{project.title} — {req.audience.title()}",
            target_minutes=req.target_minutes,
            user_guidance=req.user_guidance.strip(),
            plan_json=json.dumps({"parts": [], "omissions": [], "critical_topics": []}),
        )
        session.add(series)
        try:
            session.commit()
        except IntegrityError as exc:
            session.rollback()
            raise HTTPException(409, f"the {req.audience} track was created concurrently") from exc
        session.refresh(series)
        # Tracks selected while extraction/analysis is still running stay at
        # plan_version=0; paper_analyze discovers and drafts them in one pass.
        # A track added later can be planned immediately.
        analysis_ready = session.exec(select(Artifact).where(
            Artifact.project_id == project_id,
            Artifact.paper_series_id == None,  # noqa: E711
            Artifact.paper_part_id == None,  # noqa: E711
            Artifact.type == "paper_coverage",
        )).first()
        root_active = session.exec(select(Job).where(
            Job.project_id == project_id,
            Job.paper_series_id == None,  # noqa: E711
            Job.paper_part_id == None,  # noqa: E711
            Job.status.in_(("queued", "running")),
        )).first()
        if req.auto_plan and analysis_ready and not root_active:
            job = _queue_job(
                session,
                project_id=project_id,
                series_id=series.id,
                task_name="paper_plan",
                task_args=[project_id, series.id],
                options={"audience": req.audience},
            )
        else:
            job = None
        payload = _series_payload(session, series)
        payload["job"] = job.model_dump() if job else None
        return payload


@router.get("/{project_id}/series")
def list_paper_series(project_id: int):
    with get_session() as session:
        _project, _source = _paper_rows(session, project_id)
        rows = session.exec(
            select(PaperSeries).where(PaperSeries.project_id == project_id)
            .order_by(PaperSeries.created)
        ).all()
        return [_series_payload(session, row, detail=False) for row in rows]


def _series_row(session, series_id: int) -> PaperSeries:
    series = session.get(PaperSeries, series_id)
    if not series:
        raise HTTPException(404, "paper series was not found")
    project = session.get(Project, series.project_id)
    if not project or project.source_type != "paper":
        raise HTTPException(404, "paper series was not found")
    return series


@series_router.get("/{series_id}")
def paper_series_detail(series_id: int):
    with get_session() as session:
        series = _series_row(session, series_id)
        payload = _series_payload(session, series)
        project = session.get(Project, series.project_id)
        source = session.exec(select(PaperSource).where(
            PaperSource.project_id == series.project_id
        )).one()
        memories = session.exec(
            select(PaperMemoryRevision).where(PaperMemoryRevision.series_id == series.id)
            .order_by(PaperMemoryRevision.revision)
        ).all()
        memory_payloads = [{
            **memory.model_dump(),
            "paper_series_id": memory.series_id,
            "paper_part_id": memory.part_id,
            "parent_id": memory.parent_revision_id,
            "state": _json_load(memory.state_json, {}),
            "active": bool(
                payload.get("memory_revision")
                and payload["memory_revision"].get("id") == memory.id
            ),
        } for memory in memories]
        return {
            "series": payload,
            "project": project.model_dump(),
            "source": _source_payload(project, source),
            "parts": payload["parts"],
            "coverage": payload.get("coverage", {}),
            "memory_revision": payload["memory_revision"],
            "memory_revisions": memory_payloads,
            "artifacts": payload.get("artifacts", []),
            "jobs": payload.get("jobs", []),
        }


class EvidenceAssignmentRequest(BaseModel):
    evidence_id: str = Field(min_length=1)
    role: Literal["primary", "bridge"] = "primary"
    importance: Literal["critical", "major", "supporting"] = "supporting"
    reason: str = Field(default="", max_length=1000)


class PaperPartPlanRequest(BaseModel):
    id: int | None = None
    position: int = Field(ge=1, le=5)
    title: str = Field(min_length=1, max_length=300)
    focus: str = Field(default="", max_length=5000)
    target_minutes: int = Field(default=50, ge=40, le=60)
    topics: list[str] = Field(default_factory=list)
    evidence: list[EvidenceAssignmentRequest] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    user_guidance: str = Field(default="", max_length=10_000)


class PaperOmissionRequest(BaseModel):
    evidence_id: str | None = None
    topic_id: str | None = None
    importance: Literal["critical", "major", "supporting"] = "supporting"
    demoted_from: Literal["critical", "major"] | None = None
    reason: str = Field(min_length=3, max_length=2000)


class PaperPlanUpdateRequest(BaseModel):
    expected_version: int = Field(ge=0)
    title: str | None = Field(default=None, max_length=300)
    target_minutes: int | None = Field(default=None, ge=40, le=60)
    parts: list[PaperPartPlanRequest] = Field(min_length=1, max_length=5)
    omissions: list[PaperOmissionRequest] = Field(default_factory=list)
    critical_topics: list[dict] | None = None
    user_guidance: str | None = Field(default=None, max_length=10_000)


def _part_locked(part: PaperSeriesPart) -> bool:
    return part.status == "complete" or part.script_status in {"done", "complete"}


def _part_assignment_signature(session, part: PaperSeriesPart) -> list[tuple]:
    result = []
    for assignment in session.exec(
        select(PaperPartEvidence).where(PaperPartEvidence.part_id == part.id)
    ).all():
        chunk = session.get(PaperChunk, assignment.chunk_id)
        if chunk:
            result.append((
                chunk.evidence_id,
                assignment.role,
                assignment.importance,
                assignment.reason,
            ))
    return sorted(result)


@series_router.put("/{series_id}/plan")
def update_paper_plan(series_id: int, req: PaperPlanUpdateRequest):
    with get_session() as session:
        series = _series_row(session, series_id)
        if series.plan_version != req.expected_version:
            raise HTTPException(
                409,
                f"plan version changed (current {series.plan_version}); reload before saving",
            )
        active = _active_track_job(session, series.id)
        if active:
            raise HTTPException(409, "wait for the active track job before editing its plan")
        hard_max = min(5, max(1, int(series.max_parts or 5)))
        if not 1 <= len(req.parts) <= hard_max:
            raise HTTPException(
                422, f"paper audience plans must contain 1-{hard_max} parts")
        positions = sorted(part.position for part in req.parts)
        if positions != list(range(1, len(req.parts) + 1)):
            raise HTTPException(422, "part positions must be consecutive starting at 1")

        source = session.exec(
            select(PaperSource).where(PaperSource.project_id == series.project_id)
        ).one()
        chunks = session.exec(
            select(PaperChunk).where(PaperChunk.source_id == source.id)
        ).all()
        chunks_by_evidence = {chunk.evidence_id: chunk for chunk in chunks}
        source_coverage = _coverage_payload(source)
        source_topics = [
            topic for topic in source_coverage.get("topics", [])
            if isinstance(topic, dict)
        ]
        evidence_importance: dict[str, str] = {}
        rank = {"supporting": 0, "major": 1, "critical": 2}
        for topic in source_topics:
            importance = str(topic.get("importance") or "supporting")
            if importance not in rank:
                importance = "supporting"
            for evidence_id in topic.get("evidence_ids", []):
                prior = evidence_importance.get(str(evidence_id), "supporting")
                if rank[importance] > rank[prior]:
                    evidence_importance[str(evidence_id)] = importance
        assignments_by_position: dict[int, list[EvidenceAssignmentRequest]] = {}
        for part in req.parts:
            assignments = list(part.evidence)
            assigned_ids = {item.evidence_id for item in assignments}
            for evidence_id in part.evidence_ids:
                if evidence_id not in assigned_ids:
                    assignments.append(EvidenceAssignmentRequest(
                        evidence_id=evidence_id,
                        role="primary",
                        importance=evidence_importance.get(evidence_id, "supporting"),
                    ))
                    assigned_ids.add(evidence_id)
            assignments_by_position[part.position] = assignments
        requested_evidence = {
            assignment.evidence_id
            for part in req.parts for assignment in assignments_by_position[part.position]
        }
        unknown_evidence = sorted(requested_evidence - set(chunks_by_evidence))
        if unknown_evidence:
            raise HTTPException(
                422,
                "unknown paper evidence ID(s): " + ", ".join(unknown_evidence[:10]),
            )
        primary_positions: dict[str, int] = {}
        bridge_counts: dict[str, int] = {}
        for part in req.parts:
            seen_in_part: set[str] = set()
            for assignment in assignments_by_position[part.position]:
                if assignment.evidence_id in seen_in_part:
                    raise HTTPException(
                        422,
                        f"{assignment.evidence_id} is assigned more than once in Part {part.position}",
                    )
                seen_in_part.add(assignment.evidence_id)
                if assignment.role == "primary":
                    prior = primary_positions.get(assignment.evidence_id)
                    if prior is not None:
                        raise HTTPException(
                            422,
                            f"{assignment.evidence_id} is primary in Parts {prior} and {part.position}",
                        )
                    primary_positions[assignment.evidence_id] = part.position
                else:
                    bridge_counts[assignment.evidence_id] = bridge_counts.get(assignment.evidence_id, 0) + 1
        overused = sorted(key for key, count in bridge_counts.items() if count > 2)
        if overused:
            raise HTTPException(
                422,
                "bridge evidence may appear in at most two parts: " + ", ".join(overused[:10]),
            )

        existing_parts = session.exec(
            select(PaperSeriesPart).where(PaperSeriesPart.series_id == series.id)
        ).all()
        existing_by_id = {part.id: part for part in existing_parts}
        requested_ids = {part.id for part in req.parts if part.id is not None}
        foreign_ids = requested_ids - set(existing_by_id)
        if foreign_ids:
            raise HTTPException(422, "one or more plan parts do not belong to this track")

        request_by_id = {part.id: part for part in req.parts if part.id is not None}
        for existing in existing_parts:
            requested = request_by_id.get(existing.id)
            if not _part_locked(existing):
                continue
            if not requested:
                raise HTTPException(409, f"completed Part {existing.position} cannot be removed")
            requested_signature = sorted((
                item.evidence_id,
                item.role,
                item.importance,
            ) for item in assignments_by_position[requested.position])
            existing_signature = sorted(
                item[:3] for item in _part_assignment_signature(session, existing)
            )
            if (
                requested.position != existing.position
                or requested.title != existing.title
                or requested.focus != existing.focus
                or requested.target_minutes != existing.target_minutes
                or requested_signature != existing_signature
            ):
                raise HTTPException(409, f"completed Part {existing.position} is structurally locked")

        removed = [part for part in existing_parts if part.id not in requested_ids]
        for part in removed:
            dependent = session.exec(
                select(Artifact).where(Artifact.paper_part_id == part.id)
            ).first() or session.exec(
                select(PaperMemoryRevision).where(PaperMemoryRevision.part_id == part.id)
            ).first()
            if dependent:
                raise HTTPException(409, f"Part {part.position} has generated output and cannot be removed")

        # Free unique (series, position) slots before applying a reorder.
        for part in existing_parts:
            if not _part_locked(part):
                part.position = -(part.id or 0) - 1
                session.add(part)
        session.flush()

        persisted: list[tuple[PaperSeriesPart, PaperPartPlanRequest]] = []
        changed_from: int | None = None
        for requested in sorted(req.parts, key=lambda item: item.position):
            part = existing_by_id.get(requested.id) if requested.id is not None else None
            if part is None:
                part = PaperSeriesPart(
                    series_id=series.id,
                    position=requested.position,
                    title=requested.title,
                    target_minutes=requested.target_minutes,
                )
                changed_from = min(changed_from or requested.position, requested.position)
            else:
                before = (
                    part.position, part.title, part.focus,
                    part.target_minutes, part.user_guidance,
                )
                after = (
                    requested.position, requested.title, requested.focus,
                    requested.target_minutes, requested.user_guidance,
                )
                if before != after:
                    changed_from = min(changed_from or requested.position, requested.position)
                requested_assignment_signature = sorted((
                    item.evidence_id,
                    item.role,
                    item.importance,
                    item.reason.strip(),
                ) for item in assignments_by_position[requested.position])
                if (_part_assignment_signature(session, part)
                        != requested_assignment_signature):
                    changed_from = min(
                        changed_from or requested.position, requested.position)
            part.position = requested.position
            part.title = requested.title.strip()
            part.focus = requested.focus.strip()
            part.target_minutes = requested.target_minutes
            part.user_guidance = requested.user_guidance.strip()
            part.updated = utcnow()
            session.add(part)
            session.flush()
            persisted.append((part, requested))

        for part in removed:
            for assignment in session.exec(
                select(PaperPartEvidence).where(PaperPartEvidence.part_id == part.id)
            ).all():
                session.delete(assignment)
            session.delete(part)
            changed_from = min(changed_from or max(part.position, 1), max(part.position, 1))
        session.flush()

        for part, requested in persisted:
            if _part_locked(part):
                continue
            for assignment in session.exec(
                select(PaperPartEvidence).where(PaperPartEvidence.part_id == part.id)
            ).all():
                session.delete(assignment)
            session.flush()
            for item in assignments_by_position[requested.position]:
                chunk = chunks_by_evidence[item.evidence_id]
                session.add(PaperPartEvidence(
                    part_id=part.id,
                    chunk_id=chunk.id,
                    role=item.role,
                    importance=item.importance,
                    reason=item.reason.strip(),
                ))

        if changed_from is not None:
            for part, _requested in persisted:
                if part.position >= changed_from and any(
                    status != "pending"
                    for status in (part.guide_status, part.script_status, part.audio_status)
                ):
                    part.stale = True
                    session.add(part)

        plan_parts = []
        for part, requested in persisted:
            assignments = assignments_by_position[requested.position]
            requested_topics = list(dict.fromkeys(requested.topics))
            if not requested_topics:
                assigned_ids = {item.evidence_id for item in assignments}
                requested_topics = [
                    str(topic.get("id") or topic.get("topic_id"))
                    for topic in source_topics
                    if (topic.get("id") or topic.get("topic_id"))
                    and assigned_ids & {str(value) for value in topic.get("evidence_ids", [])}
                ]
            plan_parts.append({
                "id": part.id,
                "position": part.position,
                "title": part.title,
                "focus": part.focus,
                "target_minutes": part.target_minutes,
                "topics": requested_topics,
                "evidence": [item.model_dump() for item in assignments],
                "evidence_ids": [item.evidence_id for item in assignments],
                "user_guidance": part.user_guidance,
            })
        omission_rows = []
        for item in req.omissions:
            row = item.model_dump()
            # Choosing "omit" for a critical topic plus recording the required
            # reason is the plan editor's explicit demotion action.
            if row.get("importance") == "critical" and not row.get("demoted_from"):
                row["demoted_from"] = "critical"
            omission_rows.append(row)
        prior_plan = _json_load(series.plan_json, {})
        topic_inventory = source_topics or [
            topic for topic in prior_plan.get("topics", [])
            if isinstance(topic, dict)
        ]
        topics_by_id = {
            str(topic.get("id") or topic.get("topic_id")): topic
            for topic in topic_inventory
            if topic.get("id") or topic.get("topic_id")
        }
        promoted_ids: set[str] = set()
        if req.critical_topics is not None:
            requested_ids = {
                str(topic.get("id") or topic.get("topic_id"))
                for topic in req.critical_topics if isinstance(topic, dict)
                and (topic.get("id") or topic.get("topic_id"))
            }
            unknown_topics = sorted(requested_ids - set(topics_by_id))
            if unknown_topics:
                raise HTTPException(
                    422,
                    "unknown critical topic ID(s): "
                    + ", ".join(unknown_topics[:10]),
                )
            promoted_ids = requested_ids
        critical_topics = []
        for topic_id, topic in topics_by_id.items():
            if topic.get("importance") == "critical" or topic_id in promoted_ids:
                # Evidence/text stay authoritative.  Clients may promote a
                # known topic, but cannot erase or rewrite a mapped critical
                # topic to bypass coverage approval.
                critical_topics.append({**topic, "importance": "critical"})
        if not critical_topics and not topics_by_id:
            critical_topics = [
                topic for topic in prior_plan.get("critical_topics", [])
                if isinstance(topic, dict)
            ]
        plan = {
            "parts": plan_parts,
            "omissions": omission_rows,
            "topics": source_topics or prior_plan.get("topics", []),
            "critical_topics": critical_topics,
        }
        if isinstance(prior_plan.get("analysis_lineage"), dict):
            plan["analysis_lineage"] = prior_plan["analysis_lineage"]
        series.title = req.title.strip() if req.title is not None else series.title
        series.target_minutes = req.target_minutes or series.target_minutes
        if req.user_guidance is not None:
            series.user_guidance = req.user_guidance.strip()
        series.plan_version += 1
        series.plan_json = json.dumps(plan, sort_keys=True)
        series.plan_hash = _canonical_hash({
            "source_hash": source.source_hash,
            "audience": series.audience,
            "target_minutes": series.target_minutes,
            "plan": plan,
        })
        series.status = "draft"
        series.approved_at = None
        series.updated = utcnow()
        session.add(series)
        session.commit()
        session.refresh(series)
        return _series_payload(session, series)


class VersionedRequest(BaseModel):
    expected_version: int = Field(ge=0)


def _critical_chunk_ids(chunks: list[PaperChunk]) -> set[str]:
    result: set[str] = set()
    for chunk in chunks:
        try:
            flags = json.loads(chunk.flags or "[]")
        except (TypeError, json.JSONDecodeError):
            flags = []
        if (
            isinstance(flags, list) and "critical" in flags
            or isinstance(flags, dict) and (
                flags.get("critical") is True or flags.get("importance") == "critical"
            )
        ):
            result.add(chunk.evidence_id)
    return result


@series_router.post("/{series_id}/approve")
def approve_paper_plan(series_id: int, req: VersionedRequest):
    with get_session() as session:
        series = _series_row(session, series_id)
        if series.plan_version != req.expected_version:
            raise HTTPException(409, "plan version changed; reload before approving")
        source = session.exec(
            select(PaperSource).where(PaperSource.project_id == series.project_id)
        ).one()
        if source.status not in {"ready", "ready_with_acknowledged_gaps"}:
            raise HTTPException(409, "paper extraction and review must finish before approval")
        unacknowledged = _unacknowledged_poor_pages(source)
        if unacknowledged:
            raise HTTPException(
                409,
                "acknowledge or replace POOR extraction page(s): "
                + ", ".join(map(str, unacknowledged)),
            )
        plan = _json_load(series.plan_json, {})
        parts = plan.get("parts", []) if isinstance(plan.get("parts", []), list) else []
        if not 1 <= len(parts) <= 5:
            raise HTTPException(422, "an approved track must contain one to five parts")
        omissions = plan.get("omissions", []) if isinstance(plan.get("omissions", []), list) else []
        for omission in omissions:
            if not isinstance(omission, dict) or not str(omission.get("reason", "")).strip():
                raise HTTPException(422, "every omitted item requires a reason")
        assigned_evidence = {
            item.get("evidence_id")
            for part in parts if isinstance(part, dict)
            for item in part.get("evidence", []) if isinstance(item, dict) and item.get("role") == "primary"
        }
        empty_parts = [
            int(part.get("position") or index)
            for index, part in enumerate(parts, 1) if isinstance(part, dict)
            and not any(
                isinstance(item, dict) and item.get("role") == "primary"
                and item.get("evidence_id")
                for item in part.get("evidence", [])
            )
        ]
        if empty_parts:
            raise HTTPException(
                422,
                "critical coverage is incomplete because every part requires "
                "primary evidence; empty Part(s): "
                + ", ".join(map(str, empty_parts)),
            )
        chunks = session.exec(
            select(PaperChunk).where(PaperChunk.source_id == source.id)
        ).all()
        critical_evidence = _critical_chunk_ids(chunks)
        critical_topics = plan.get("critical_topics", [])
        omitted_demotions = {
            item.get("evidence_id")
            for item in omissions if isinstance(item, dict)
            and item.get("demoted_from") == "critical"
            and str(item.get("reason", "")).strip()
        }
        topics_by_id = {
            str(item.get("id") or item.get("topic_id")): item
            for item in critical_topics if isinstance(item, dict)
            and (item.get("id") or item.get("topic_id"))
        }
        for omission in omissions:
            if not isinstance(omission, dict) or omission.get("demoted_from") != "critical":
                continue
            if not str(omission.get("reason", "")).strip():
                continue
            topic = topics_by_id.get(str(omission.get("topic_id") or ""), {})
            omitted_demotions.update(str(value) for value in topic.get("evidence_ids", []))
        missing_evidence = sorted(critical_evidence - assigned_evidence - omitted_demotions)

        required_topics = {
            str(item.get("id") or item.get("topic_id"))
            for item in critical_topics if isinstance(item, dict)
            and (item.get("id") or item.get("topic_id"))
            and item.get("importance", "critical") == "critical"
        }
        assigned_topics: set[str] = set()
        # Topic coverage must be grounded in a primary evidence assignment;
        # merely placing a topic id in plan JSON cannot satisfy approval.
        for item in critical_topics:
            if not isinstance(item, dict):
                continue
            topic_id = item.get("id") or item.get("topic_id")
            topic_evidence = {str(value) for value in item.get("evidence_ids", [])}
            if topic_id and (
                topic_evidence & assigned_evidence
                or not topic_evidence and any(
                    str(topic_id) in {
                        str(value) for value in part.get("topics", [])
                    }
                    for part in parts if isinstance(part, dict)
                )
            ):
                assigned_topics.add(str(topic_id))
        demoted_topics = {
            str(item.get("topic_id"))
            for item in omissions if isinstance(item, dict)
            and item.get("topic_id") and item.get("demoted_from") == "critical"
            and str(item.get("reason", "")).strip()
        }
        missing_topics = sorted(required_topics - assigned_topics - demoted_topics)

        all_topics = [
            item for item in plan.get("topics", [])
            if isinstance(item, dict) and (item.get("id") or item.get("topic_id"))
        ]
        omitted_topic_ids = {
            str(item.get("topic_id"))
            for item in omissions if isinstance(item, dict)
            and item.get("topic_id") and str(item.get("reason", "")).strip()
        }
        omitted_evidence = {
            str(item.get("evidence_id"))
            for item in omissions if isinstance(item, dict)
            and item.get("evidence_id") and str(item.get("reason", "")).strip()
        }
        topics_for_omissions = {
            str(item.get("id") or item.get("topic_id")): item
            for item in all_topics + [
                item for item in critical_topics if isinstance(item, dict)
            ]
            if item.get("id") or item.get("topic_id")
        }
        for topic_id in omitted_topic_ids | demoted_topics:
            topic = topics_for_omissions.get(topic_id, {})
            omitted_evidence.update(
                str(value) for value in topic.get("evidence_ids", [])
            )
        accounted_evidence = assigned_evidence | omitted_evidence
        covered_topic_ids: set[str] = set()
        for item in all_topics:
            topic_id = str(item.get("id") or item.get("topic_id"))
            topic_evidence = {str(value) for value in item.get("evidence_ids", [])}
            if topic_evidence and topic_evidence <= accounted_evidence:
                covered_topic_ids.add(topic_id)
            elif not topic_evidence and any(
                topic_id in {str(value) for value in part.get("topics", [])}
                for part in parts if isinstance(part, dict)
            ):
                covered_topic_ids.add(topic_id)
        missing_lower_topics = sorted(
            {str(item.get("id") or item.get("topic_id")) for item in all_topics}
            - covered_topic_ids - omitted_topic_ids
        )
        chunks_by_evidence = {chunk.evidence_id: chunk for chunk in chunks}
        acknowledged_pages = {
            int(row["page"])
            for row in _acknowledgements(source)
            if str(row.get("page", "")).isdigit()
        }
        gap_only_topics = []
        for item in critical_topics:
            if not isinstance(item, dict):
                continue
            topic_id = str(item.get("id") or item.get("topic_id") or "")
            if not topic_id or topic_id in demoted_topics:
                continue
            assigned_support = {
                str(value) for value in item.get("evidence_ids", [])
            } & assigned_evidence
            support_pages = {
                chunks_by_evidence[evidence_id].page_number
                for evidence_id in assigned_support
                if evidence_id in chunks_by_evidence
            }
            if support_pages and support_pages <= acknowledged_pages:
                gap_only_topics.append(topic_id)

        unaccounted_evidence = sorted(
            set(chunks_by_evidence) - assigned_evidence - omitted_evidence
        )
        if (missing_evidence or missing_topics or missing_lower_topics
                or unaccounted_evidence or gap_only_topics):
            details = []
            if missing_evidence:
                details.append("evidence " + ", ".join(missing_evidence[:10]))
            if missing_topics:
                details.append("topics " + ", ".join(missing_topics[:10]))
            if missing_lower_topics:
                details.append(
                    "unassigned/unomitted topics "
                    + ", ".join(missing_lower_topics[:10])
                )
            if unaccounted_evidence:
                details.append(
                    "evidence without a primary assignment or omission "
                    + ", ".join(unaccounted_evidence[:10])
                )
            if gap_only_topics:
                details.append(
                    "critical topics supported only by acknowledged POOR pages "
                    + ", ".join(gap_only_topics[:10])
                )
            raise HTTPException(
                422,
                "critical coverage or omission accounting is incomplete; "
                "assign each item or explicitly omit/demote it with a reason: "
                + "; ".join(details),
            )
        series.status = "approved"
        series.approved_at = utcnow()
        series.updated = utcnow()
        session.add(series)
        session.commit()
        session.refresh(series)
        return _series_payload(session, series)


@series_router.post("/{series_id}/run")
def run_paper_series(series_id: int):
    with get_session() as session:
        series = _series_row(session, series_id)
        if series.status not in {"approved", "running", "complete"}:
            raise HTTPException(409, "approve the audience plan before production")
        if not session.exec(
            select(PaperSeriesPart).where(PaperSeriesPart.series_id == series.id)
        ).first():
            raise HTTPException(409, "the audience plan has no parts")
        active = _active_track_job(session, series.id)
        if active:
            raise HTTPException(409, "wait for the active track job to finish")
        job = _queue_job(
            session,
            project_id=series.project_id,
            series_id=series.id,
            task_name="paper_series_run",
            task_args=[series.project_id, series.id],
            options={"plan_version": series.plan_version, "plan_hash": series.plan_hash},
        )
        return job


class FuturePartEditRequest(BaseModel):
    expected_version: int = Field(ge=0)
    title: str | None = Field(default=None, min_length=1, max_length=300)
    focus: str | None = Field(default=None, max_length=5000)
    user_guidance: str | None = Field(default=None, max_length=10_000)


@series_router.patch("/{series_id}/parts/{part_id}")
def edit_future_paper_part(series_id: int, part_id: int, req: FuturePartEditRequest):
    with get_session() as session:
        series = _series_row(session, series_id)
        if series.plan_version != req.expected_version:
            raise HTTPException(409, "plan version changed; reload before editing")
        active = _active_track_job(session, series.id)
        if active:
            raise HTTPException(409, "wait for the active track job before editing a part")
        part = session.get(PaperSeriesPart, part_id)
        if not part or part.series_id != series.id:
            raise HTTPException(404, "paper series part was not found")
        if _part_locked(part):
            raise HTTPException(409, "completed parts are structurally locked")
        if req.title is not None:
            part.title = req.title.strip()
        if req.focus is not None:
            part.focus = req.focus.strip()
        if req.user_guidance is not None:
            part.user_guidance = req.user_guidance.strip()
        part.updated = utcnow()
        session.add(part)
        following = session.exec(
            select(PaperSeriesPart).where(
                PaperSeriesPart.series_id == series.id,
                PaperSeriesPart.position >= part.position,
            )
        ).all()
        for candidate in following:
            if any(status != "pending" for status in (
                candidate.guide_status, candidate.script_status, candidate.audio_status
            )):
                candidate.stale = True
                session.add(candidate)
        plan = _json_load(series.plan_json, {})
        for row in plan.get("parts", []):
            if isinstance(row, dict) and row.get("id") == part.id:
                row.update({
                    "title": part.title,
                    "focus": part.focus,
                    "user_guidance": part.user_guidance,
                })
        series.plan_version += 1
        series.plan_json = json.dumps(plan, sort_keys=True)
        source = session.exec(
            select(PaperSource).where(PaperSource.project_id == series.project_id)
        ).one()
        series.plan_hash = _canonical_hash({
            "source_hash": source.source_hash,
            "audience": series.audience,
            "target_minutes": series.target_minutes,
            "plan": plan,
        })
        series.status = "draft"
        series.approved_at = None
        series.updated = utcnow()
        session.add(series)
        session.commit()
        session.refresh(series)
        return _series_payload(session, series)


@series_router.post("/{series_id}/parts/{part_id}/run/{step}")
def run_paper_part_step(series_id: int, part_id: int, step: str):
    if step not in PART_STEPS:
        raise HTTPException(422, "part step must be guide, script, or audio")
    with get_session() as session:
        series = _series_row(session, series_id)
        if series.status not in {"approved", "running", "complete"}:
            raise HTTPException(409, "approve the current plan before generating a part")
        active = _active_track_job(session, series.id)
        if active:
            raise HTTPException(409, "wait for the active track job to finish")
        part = session.get(PaperSeriesPart, part_id)
        if not part or part.series_id != series.id:
            raise HTTPException(404, "paper series part was not found")
        if step == "script" and part.position > 1:
            prior = session.exec(
                select(PaperSeriesPart).where(
                    PaperSeriesPart.series_id == series.id,
                    PaperSeriesPart.position == part.position - 1,
                )
            ).first()
            memory = session.exec(
                select(PaperMemoryRevision).where(
                    PaperMemoryRevision.series_id == series.id,
                    PaperMemoryRevision.part_id == (prior.id if prior else -1),
                )
            ).first()
            if not prior or prior.script_status not in {"done", "complete"} or not memory:
                raise HTTPException(409, "finalize the previous part script and memory first")
        if step == "audio" and part.script_status not in {"done", "complete"}:
            raise HTTPException(409, "generate the part script before audio")
        return _queue_job(
            session,
            project_id=series.project_id,
            series_id=series.id,
            part_id=part.id,
            task_name="paper_part_step",
            task_args=[series.project_id, series.id, part.id, step],
            options={"step": step, "plan_version": series.plan_version},
        )


@series_router.post("/{series_id}/parts/{part_id}/rebuild-following")
def rebuild_paper_part_and_following(series_id: int, part_id: int):
    with get_session() as session:
        series = _series_row(session, series_id)
        if series.status not in {"approved", "running", "complete"}:
            raise HTTPException(409, "approve the current plan before rebuilding")
        active = _active_track_job(session, series.id)
        if active:
            raise HTTPException(409, "wait for the active track job to finish")
        part = session.get(PaperSeriesPart, part_id)
        if not part or part.series_id != series.id:
            raise HTTPException(404, "paper series part was not found")
        return _queue_job(
            session,
            project_id=series.project_id,
            series_id=series.id,
            part_id=part.id,
            task_name="paper_rebuild_following",
            task_args=[series.project_id, series.id, part.id],
            options={"from_position": part.position, "plan_version": series.plan_version},
        )


@series_router.delete("/{series_id}")
def delete_paper_series(series_id: int):
    paths: list[Path] = []
    with get_session() as session:
        series = _series_row(session, series_id)
        active = session.exec(
            select(Job).where(
                Job.paper_series_id == series.id,
                Job.status.in_(("queued", "running")),
            )
        ).first()
        if active:
            raise HTTPException(409, "cancel the active track job before deleting it")
        artifacts = session.exec(
            select(Artifact).where(Artifact.paper_series_id == series.id)
        ).all()
        for artifact in artifacts:
            library.delete_search_chunks(session, artifact.id)
            session.exec(text("DELETE FROM artifact_fts WHERE artifact_id=:id").bindparams(id=artifact.id))
            session.exec(text("DELETE FROM artifacttag WHERE artifact_id=:id").bindparams(id=artifact.id))
            paths.append(library.lib_path(artifact.path))
            if artifact.media_path and not artifact.media_path.startswith("media:"):
                paths.append(library.lib_path(artifact.media_path))
            session.delete(artifact)
        parts = session.exec(
            select(PaperSeriesPart).where(PaperSeriesPart.series_id == series.id)
        ).all()
        part_ids = [part.id for part in parts]
        # One SQL statement safely removes a self-referencing revision chain;
        # row-at-a-time ORM deletes could encounter a parent before its child.
        session.exec(text(
            "DELETE FROM papermemoryrevision WHERE series_id=:series_id"
        ).bindparams(series_id=series.id))
        if part_ids:
            for assignment in session.exec(
                select(PaperPartEvidence).where(PaperPartEvidence.part_id.in_(part_ids))
            ).all():
                session.delete(assignment)
        for job in session.exec(
            select(Job).where(Job.paper_series_id == series.id)
        ).all():
            session.delete(job)
        session.flush()
        for part in parts:
            session.delete(part)
        session.flush()
        session.delete(series)
        session.commit()

    root = settings.library_dir.resolve()
    for path in paths:
        resolved = path.resolve()
        try:
            resolved.relative_to(root)
        except ValueError:
            continue
        try:
            resolved.unlink(missing_ok=True)
        except OSError:
            # DB deletion is authoritative; Library Health can report a file
            # that could not be cleaned up because another process held it.
            pass
    return {"ok": True, "source_retained": True}
