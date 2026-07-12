from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from ..db import get_session
from ..models import Artifact, Job, Project
from .. import library
from ..config import settings
from ..security import validate_source_url
from ..tasks import media
from ..tasks.celery_app import celery
from ..tasks.ingest import cookies_path

from ..tasks.orchestrate import (  # noqa: E402
    STEPS, STEP_LABELS, STEP_NAMES, STEP_OUTPUT, applicable_steps,
    missing_deps, pipeline_profiles, step_done, step_stale, transitive_dependents,
    RUN_DEPS,
)

router = APIRouter(prefix="/api/projects", tags=["projects"])


def _step_label(task: str) -> str:
    return "Run all steps" if task == "run_all" else STEP_LABELS.get(task, task)


def _list_step_done(step: str, artifact_types: set[str], done_tasks: set[str]) -> bool:
    """DB-only step-completion for the projects list — no filesystem access.

    The canonical step_done() stats the media dir for `ingest` (and even
    mkdir's it) and issues a fresh query for `quickref`; doing that per project
    on a ~1/sec-polled list is both a side effect and an N+1. Here `ingest` is
    proven by a succeeded ingest job or the existence of a transcript (which
    can't exist without ingested audio), and `quickref` by a succeeded job."""
    if step == "ingest":
        return "ingest" in done_tasks or "transcript" in artifact_types
    if step == "quickref":
        return "quickref" in done_tasks
    return STEP_OUTPUT[step] in artifact_types


def _project_progress(project: Project, artifact_types: set[str],
                      jobs: list[Job]) -> dict:
    """Derived pipeline status for the projects list, computed from the step
    graph (the raw Project.status field only ever tracks ingest/transcribe).

    `jobs` must be this project's jobs ordered oldest-first (by updated).
    Precedence: running (a job is queued/running) > canceled (the last run was
    canceled) > failed (it errored) > complete (all applicable steps done) >
    partial > new.
    """
    applicable = applicable_steps(project)
    done_tasks = {j.task for j in jobs if j.status == "done"}
    done = sum(1 for s in applicable
               if _list_step_done(s, artifact_types, done_tasks))
    total = len(applicable)

    active = [j for j in jobs if j.status in ("queued", "running")]
    latest = jobs[-1] if jobs else None
    status, detail = "new", None
    if active:
        run = next((j for j in active if j.status == "running"), active[0])
        # name a concrete step rather than the run-all wrapper when possible
        step = next((j for j in active
                     if j.status == "running" and j.task in STEP_NAMES), None) or run
        status = "running"
        detail = _step_label(step.task)
    elif latest is not None and (
            latest.status == "canceled"
            or (latest.status == "error" and "cancel" in (latest.error or "").lower())):
        # a user cancellation aborts leftover step jobs into 'error' rows tagged
        # 'run-all canceled' — surface that as canceled, not a pipeline failure
        status = "canceled"
    elif latest is not None and latest.status == "error":
        status = "failed"
        # name the most recent errored step that isn't since completed
        errored = next((j for j in reversed(jobs)
                        if j.status == "error" and j.task in STEP_NAMES
                        and not _list_step_done(j.task, artifact_types, done_tasks)), None)
        detail = _step_label(errored.task) if errored else None
    elif total and done >= total:
        status = "complete"
    elif done > 0:
        status = "partial"

    return {
        "done": done,
        "total": total,
        "status": status,
        "detail": detail,
        "last_activity": latest.updated.isoformat() if latest else None,
    }


class ProjectCreate(BaseModel):
    source: str
    source_type: str  # "url" | "local" ("upload" is created by /upload)
    title: str | None = None


@router.post("")
def create_project(req: ProjectCreate):
    if req.source_type not in ("url", "local"):
        raise HTTPException(400, "source_type must be 'url' or 'local'")
    source = req.source.strip()
    if not source:
        raise HTTPException(400, "source is required")
    if req.source_type == "url":
        try:
            source = validate_source_url(
                source, allow_private=settings.allow_private_urls)
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    # URL with no explicit title → derive "<author/podcast> - <title>" from the
    # site metadata (no download). Falls back to a pending placeholder that the
    # ingest step later replaces once it fetches the real metadata.
    resolved = (req.title or "").strip() or None
    if resolved is None and req.source_type == "url":
        from ..tasks.ingest import combined_title, fetch_url_metadata

        resolved = combined_title(fetch_url_metadata(source))

    if resolved:
        title = slug_seed = resolved
    elif req.source_type == "url":
        title = f"(pending: {source[:60]})"
        slug_seed = source.rsplit("/", 1)[-1].rsplit("?", 1)[0] or "video"
    else:
        title = slug_seed = source.rsplit("/", 1)[-1]

    slug = library.make_slug(slug_seed)
    with get_session() as session:
        base, n = slug, 1
        while session.exec(select(Project).where(Project.slug == slug)).first():
            n += 1
            slug = f"{base}-{n}"
        project = Project(slug=slug, title=title, source=source,
                          source_type=req.source_type)
        session.add(project)
        session.commit()
        session.refresh(project)
        return project


_MEDIA_SUFFIXES = {
    ".mp3", ".m4a", ".wav", ".flac", ".ogg", ".opus", ".aac",
    ".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v", ".mpeg", ".mpg",
}


@router.post("/upload")
async def upload_project(request: Request, filename: str, title: str = ""):
    """Create a project from a browser upload without exposing host paths."""
    original = Path(filename or "upload").name
    suffix = Path(original).suffix.lower()
    if suffix not in _MEDIA_SUFFIXES:
        raise HTTPException(415, "choose a common audio or video file")
    try:
        declared_size = int(request.headers.get("content-length", "0"))
    except ValueError:
        declared_size = 0
    if declared_size > settings.max_upload_bytes:
        raise HTTPException(413, "the upload is larger than this Synapse instance allows")

    staging = settings.media_dir / ".uploads"
    staging.mkdir(parents=True, exist_ok=True)
    tmp_path: Path | None = None
    total = 0
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix="upload-", suffix=suffix, dir=staging, delete=False,
        ) as handle:
            tmp_path = Path(handle.name)
            async for chunk in request.stream():
                total += len(chunk)
                if total > settings.max_upload_bytes:
                    raise HTTPException(
                        413,
                        f"upload exceeds the {settings.max_upload_bytes // (1024 ** 3)} GB limit",
                    )
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        if not total:
            raise HTTPException(400, "the uploaded file is empty")

        display_title = title.strip() or Path(original).stem
        slug = library.make_slug(display_title)
        with get_session() as session:
            base, n = slug, 1
            while session.exec(select(Project).where(Project.slug == slug)).first():
                n += 1
                slug = f"{base}-{n}"
            project = Project(
                slug=slug, title=display_title, source=f"uploaded{suffix}",
                source_type="upload",
            )
            session.add(project)
            session.commit()
            session.refresh(project)

        destination = media.workdir(slug) / f"uploaded{suffix}"
        try:
            os.replace(tmp_path, destination)
            tmp_path = None
        except Exception:
            with get_session() as session:
                failed = session.get(Project, project.id)
                if failed:
                    session.delete(failed)
                    session.commit()
            shutil.rmtree(destination.parent, ignore_errors=True)
            raise
        return project
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)


class ProjectRename(BaseModel):
    title: str


@router.patch("/{project_id}")
def rename_project(project_id: int, req: ProjectRename):
    """Rename a project (display title only — the on-disk slug and its library
    files are left untouched, so nothing is orphaned)."""
    new_title = req.title.strip()
    if not new_title:
        raise HTTPException(400, "title cannot be empty")
    with get_session() as session:
        project = session.get(Project, project_id)
        if not project:
            raise HTTPException(404)
        if project.deleting:
            raise HTTPException(409, "project is being deleted")
        project.title = new_title
        session.add(project)
        session.commit()
        session.refresh(project)
        return project


@router.get("")
def list_projects():
    from collections import defaultdict

    with get_session() as session:
        projects = session.exec(select(Project).order_by(Project.created.desc())).all()
        # batch the per-project inputs so the list is a fixed handful of queries
        arts_by_pid: dict[int, set[str]] = defaultdict(set)
        for pid, typ in session.exec(select(Artifact.project_id, Artifact.type)).all():
            arts_by_pid[pid].add(typ)
        jobs_by_pid: dict[int, list[Job]] = defaultdict(list)
        for job in session.exec(select(Job).order_by(Job.updated)).all():
            jobs_by_pid[job.project_id].append(job)
        return [
            {**p.model_dump(),
             "progress": _project_progress(
                 p, arts_by_pid.get(p.id, set()), jobs_by_pid.get(p.id, []))}
            for p in projects
        ]


@router.get("/steps")
def list_steps():
    return [{"name": name, "label": label} for name, label in STEPS]


@router.get("/{project_id}")
def get_project(project_id: int):
    with get_session() as session:
        project = session.get(Project, project_id)
        if not project:
            raise HTTPException(404)
        artifacts = session.exec(
            select(Artifact).where(Artifact.project_id == project_id)
        ).all()
        jobs = session.exec(
            select(Job).where(Job.project_id == project_id).order_by(Job.created.desc())
        ).all()
        # latest job per step for the pipeline board
        latest: dict[str, Job] = {}
        for job in reversed(jobs):
            latest[job.task] = job

        artifact_types = {a.type for a in artifacts}
        applicable = set(applicable_steps(project))
        steps = []
        remaining = 0
        for name, label in STEPS:
            job = latest.get(name)
            missing = missing_deps(session, project, name, artifact_types)
            done = step_done(session, project, name, artifact_types)
            stale = done and step_stale(session, project, name)
            not_applicable = name not in applicable
            if not done and not not_applicable:
                remaining += 1
            steps.append({
                "name": name,
                "label": label,
                "job": job,
                "missing": missing,        # unmet prerequisite step labels
                "blocked": bool(missing),
                "done": done,
                "stale": stale,
                "not_applicable": not_applicable,
                "artifact": next((a for a in artifacts if a.type == name or
                                  (name == "download" and a.type == "source_video") or
                                  (name == "transcribe" and a.type == "transcript") or
                                  (name == "correct" and a.type == "corrected") or
                                  (name == "summarize" and a.type == "summary") or
                                  (name == "merge" and a.type == "deepdive_merged") or
                                  (name == "tts" and a.type == "podcast_audio") or
                                  (name == "trim" and a.type == "trimmed_audio")), None),
            })
        run_all_job = next(
            (j for j in jobs if j.task == "run_all" and j.status in ("queued", "running")),
            None,
        )
        any_active = any(j.status in ("queued", "running") for j in jobs)
        return {
            "project": project,
            "artifacts": artifacts,
            "steps": steps,
            "remaining": remaining,
            "run_all_active": run_all_job is not None,
            "run_all_state": run_all_job.status if run_all_job else None,
            "any_active": any_active,
            "profiles": pipeline_profiles(),
        }


@router.post("/{project_id}/run/{step}")
def run_step(project_id: int, step: str):
    if step not in STEP_NAMES:
        raise HTTPException(400, f"unknown step {step!r}")
    with get_session() as session:
        project = session.get(Project, project_id)
        if not project:
            raise HTTPException(404)
        if project.deleting:
            raise HTTPException(409, "project is being deleted")
        if step not in applicable_steps(project):
            raise HTTPException(409, f"{step} does not apply to this project")
        running = session.exec(
            select(Job).where(Job.project_id == project_id, Job.task == step,
                              Job.status.in_(("queued", "running")))
        ).first()
        if running:
            raise HTTPException(409, f"{step} is already {running.status}")
        missing = missing_deps(session, project, step)
        if missing:
            raise HTTPException(409, f"{step} requires: {', '.join(missing)}")
        job = Job(project_id=project_id, task=step)
        session.add(job)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            raise HTTPException(409, f"{step} was started concurrently")
        session.refresh(job)
        try:
            async_result = celery.send_task(step, args=[job.id, project_id])
        except Exception as exc:
            job.status = "error"
            job.error = f"could not dispatch to worker: {exc}"
            session.add(job)
            session.commit()
            raise HTTPException(
                503, "worker queue is unavailable; the job was not left queued")
        job.celery_id = async_result.id
        session.add(job)
        session.commit()
        session.refresh(job)  # commit expires attributes → would serialize as {}
        return job


class RunAllRequest(BaseModel):
    profile: str = "full"
    steps: list[str] | None = None
    force_steps: list[str] = Field(default_factory=list)


@router.post("/{project_id}/run_all")
def run_all(project_id: int, req: RunAllRequest | None = None):
    """Queue every remaining step for this project. Runs immediately if no
    other project's run-all is active, otherwise waits its turn (run-alls are
    serial — each holds a worker slot for its whole run). Individual steps
    within the run still go concurrent where dependencies allow."""
    from ..tasks.orchestrate import maybe_start_next_run_all

    with get_session() as session:
        project = session.get(Project, project_id)
        if not project:
            raise HTTPException(404)
        if project.deleting:
            raise HTTPException(409, "project is being deleted")
        existing = session.exec(
            select(Job).where(Job.project_id == project_id, Job.task == "run_all",
                              Job.status.in_(("queued", "running")))
        ).first()
        if existing:
            raise HTTPException(409, f"run-all is already {existing.status} for this project")
        options = (req or RunAllRequest()).model_dump()
        profiles = pipeline_profiles()
        if options.get("steps") is not None and not options["steps"]:
            raise HTTPException(400, "steps cannot be empty when explicitly provided")
        if options.get("steps") is None and options["profile"] not in profiles:
            raise HTTPException(400, f"unknown pipeline profile {options['profile']!r}")
        for key in ("steps", "force_steps"):
            unknown = set(options.get(key) or []) - STEP_NAMES
            if unknown:
                raise HTTPException(400, f"unknown step(s): {', '.join(sorted(unknown))}")
        job = Job(project_id=project_id, task="run_all", status="queued",
                  options=json.dumps(options))
        session.add(job)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            raise HTTPException(409, "a project run was queued concurrently")
        job_id = job.id

    maybe_start_next_run_all()  # starts now if nothing else is running
    with get_session() as session:
        return session.get(Job, job_id)


@router.post("/{project_id}/rerun/{step}")
def rerun_affected(project_id: int, step: str):
    """Force one step and every downstream consumer into one run attempt."""
    if step not in STEP_NAMES:
        raise HTTPException(400, f"unknown step {step!r}")
    affected = {step} | transitive_dependents(step, RUN_DEPS)
    return run_all(project_id, RunAllRequest(
        profile="affected", steps=sorted(affected), force_steps=sorted(affected)))


@router.post("/{project_id}/reset_jobs")
def reset_jobs(project_id: int):
    """Recovery hatch: mark all queued/running jobs as error (e.g. after a
    worker crash left them stranded, blocking re-runs with 409s)."""
    from ..tasks.common import transition_job
    from ..tasks.orchestrate import cancel_children, maybe_start_next_run_all

    with get_session() as session:
        if not session.get(Project, project_id):
            raise HTTPException(404)
        stuck = session.exec(
            select(Job).where(Job.project_id == project_id,
                              Job.status.in_(("queued", "running")))
        ).all()
        for job in stuck:
            transition_job(session, job.id, {"queued", "running"}, "canceled",
                           error="manually canceled as stuck")
            if job.task == "run_all":
                cancel_children(job.id, "parent run manually canceled")
            if job.celery_id:
                try:
                    celery.control.revoke(job.celery_id, terminate=True)
                except Exception:
                    pass
        maybe_start_next_run_all()
        return {"reset": len(stuck), "canceled": len(stuck)}


@router.post("/{project_id}/cookies")
async def upload_cookies(project_id: int, file: UploadFile):
    with get_session() as session:
        project = session.get(Project, project_id)
        if not project:
            raise HTTPException(404)
        if project.deleting:
            raise HTTPException(409, "project is being deleted")
    payload = await file.read(2_000_001)
    if len(payload) > 2_000_000:
        raise HTTPException(413, "cookies.txt must be 2 MB or smaller")
    text_payload = payload.decode("utf-8", errors="replace")
    if "# Netscape HTTP Cookie File" not in text_payload and "\t" not in text_payload:
        raise HTTPException(400, "expected a Netscape-format cookies.txt file")
    dest = cookies_path(project.slug)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(f".tmp.{os.getpid()}")
    tmp.write_bytes(payload)
    try:
        tmp.chmod(0o600)
        tmp.replace(dest)
        dest.chmod(0o600)
    finally:
        tmp.unlink(missing_ok=True)
    return {"ok": True}


@router.delete("/{project_id}")
def delete_project(project_id: int):
    """Fence work, stage files, then transactionally remove project state."""
    import shutil

    from sqlmodel import text

    with get_session() as session:
        project = session.get(Project, project_id)
        if not project:
            raise HTTPException(404)
        active = session.exec(
            select(Job).where(Job.project_id == project_id,
                              Job.status.in_(("queued", "running")))
        ).first()
        if active:
            raise HTTPException(409, "cancel active jobs before deleting this project")
        project.deleting = True
        session.add(project)
        session.commit()
        slug = project.slug

    paths = [settings.library_dir / "projects" / slug, settings.media_dir / slug]
    staged: list[tuple] = []
    try:
        for path in paths:
            if not path.exists():
                continue
            trash = path.parent / ".trash" / f"{slug}.delete-{project_id}"
            trash.parent.mkdir(parents=True, exist_ok=True)
            if trash.exists():
                shutil.rmtree(trash)
            path.replace(trash)
            staged.append((path, trash))
    except OSError as exc:
        for original, trash in reversed(staged):
            if trash.exists() and not original.exists():
                trash.replace(original)
        with get_session() as session:
            project = session.get(Project, project_id)
            if project:
                project.deleting = False
                session.add(project)
                session.commit()
        raise HTTPException(500, f"could not stage project files for deletion: {exc}")

    try:
        with get_session() as session:
            project = session.get(Project, project_id)
            prefix = f"projects/{slug}/"
            own = session.exec(
                select(Artifact).where(Artifact.project_id == project_id,
                                       Artifact.path.startswith(prefix))
            ).all()
            shared = session.exec(
                select(Artifact).where(Artifact.project_id == project_id,
                                       ~Artifact.path.startswith(prefix))
            ).all()
            for art in shared:
                art.project_id = None
                session.add(art)
            for art in own:
                chunk_ids = [r[0] for r in session.exec(text(
                    "SELECT id FROM searchchunk WHERE artifact_id=:id"
                ).bindparams(id=art.id)).all()]
                for chunk_id in chunk_ids:
                    session.exec(text(
                        "DELETE FROM chunkembedding WHERE chunk_id=:id"
                    ).bindparams(id=chunk_id))
                    session.exec(text(
                        "DELETE FROM chunk_fts WHERE chunk_id=:id"
                    ).bindparams(id=chunk_id))
                session.exec(text(
                    "DELETE FROM searchchunk WHERE artifact_id=:id"
                ).bindparams(id=art.id))
                session.exec(text(
                    "DELETE FROM artifact_fts WHERE artifact_id=:id"
                ).bindparams(id=art.id))
                session.exec(text(
                    "DELETE FROM artifacttag WHERE artifact_id=:id"
                ).bindparams(id=art.id))
                session.delete(art)
            session.exec(text(
                "DELETE FROM job WHERE project_id=:id"
            ).bindparams(id=project_id))
            session.exec(text(
                "DELETE FROM quickrefsource WHERE project_id=:id"
            ).bindparams(id=project_id))
            session.delete(project)
            session.commit()
    except Exception:
        for original, trash in reversed(staged):
            if trash.exists() and not original.exists():
                trash.replace(original)
        with get_session() as session:
            project = session.get(Project, project_id)
            if project:
                project.deleting = False
                session.add(project)
                session.commit()
        raise

    for _original, trash in staged:
        shutil.rmtree(trash, ignore_errors=True)
    from ..settings_store import delete_settings_prefix, set_setting

    set_setting(f"projtags.{project_id}", None)
    delete_settings_prefix(f"step_signature.{project_id}.")
    return {"ok": True}
