from __future__ import annotations

from fastapi import APIRouter, HTTPException, UploadFile
from pydantic import BaseModel
from sqlmodel import select

from ..db import get_session
from ..models import Artifact, Job, Project
from .. import library
from ..tasks import media
from ..tasks.celery_app import celery
from ..tasks.ingest import cookies_path

from ..tasks.orchestrate import (  # noqa: E402
    STEPS, STEP_NAMES, applicable_steps, missing_deps, step_done,
)

router = APIRouter(prefix="/api/projects", tags=["projects"])


class ProjectCreate(BaseModel):
    source: str
    source_type: str  # "url" | "local"
    title: str | None = None


@router.post("")
def create_project(req: ProjectCreate):
    if req.source_type not in ("url", "local"):
        raise HTTPException(400, "source_type must be 'url' or 'local'")

    # URL with no explicit title → derive "<author/podcast> - <title>" from the
    # site metadata (no download). Falls back to a pending placeholder that the
    # ingest step later replaces once it fetches the real metadata.
    resolved = (req.title or "").strip() or None
    if resolved is None and req.source_type == "url":
        from ..tasks.ingest import combined_title, fetch_url_metadata

        resolved = combined_title(fetch_url_metadata(req.source))

    if resolved:
        title = slug_seed = resolved
    elif req.source_type == "url":
        title = f"(pending: {req.source[:60]})"
        slug_seed = req.source.rsplit("/", 1)[-1].rsplit("?", 1)[0] or "video"
    else:
        title = slug_seed = req.source.rsplit("/", 1)[-1]

    slug = library.make_slug(slug_seed)
    with get_session() as session:
        base, n = slug, 1
        while session.exec(select(Project).where(Project.slug == slug)).first():
            n += 1
            slug = f"{base}-{n}"
        project = Project(slug=slug, title=title, source=req.source,
                          source_type=req.source_type)
        session.add(project)
        session.commit()
        session.refresh(project)
        return project


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
        project.title = new_title
        session.add(project)
        session.commit()
        session.refresh(project)
        return project


@router.get("")
def list_projects():
    with get_session() as session:
        projects = session.exec(select(Project).order_by(Project.created.desc())).all()
        return projects


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
        }


@router.post("/{project_id}/run/{step}")
def run_step(project_id: int, step: str):
    if step not in STEP_NAMES:
        raise HTTPException(400, f"unknown step {step!r}")
    with get_session() as session:
        project = session.get(Project, project_id)
        if not project:
            raise HTTPException(404)
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
        session.commit()
        session.refresh(job)
        async_result = celery.send_task(step, args=[job.id, project_id])
        job.celery_id = async_result.id
        session.add(job)
        session.commit()
        session.refresh(job)  # commit expires attributes → would serialize as {}
        return job


@router.post("/{project_id}/run_all")
def run_all(project_id: int):
    """Queue every remaining step for this project. Runs immediately if no
    other project's run-all is active, otherwise waits its turn (run-alls are
    serial — each holds a worker slot for its whole run). Individual steps
    within the run still go concurrent where dependencies allow."""
    from ..tasks.orchestrate import maybe_start_next_run_all

    with get_session() as session:
        project = session.get(Project, project_id)
        if not project:
            raise HTTPException(404)
        existing = session.exec(
            select(Job).where(Job.project_id == project_id, Job.task == "run_all",
                              Job.status.in_(("queued", "running")))
        ).first()
        if existing:
            raise HTTPException(409, f"run-all is already {existing.status} for this project")
        job = Job(project_id=project_id, task="run_all", status="queued")
        session.add(job)
        session.commit()
        job_id = job.id

    maybe_start_next_run_all()  # starts now if nothing else is running
    with get_session() as session:
        return session.get(Job, job_id)


@router.post("/{project_id}/reset_jobs")
def reset_jobs(project_id: int):
    """Recovery hatch: mark all queued/running jobs as error (e.g. after a
    worker crash left them stranded, blocking re-runs with 409s)."""
    with get_session() as session:
        if not session.get(Project, project_id):
            raise HTTPException(404)
        stuck = session.exec(
            select(Job).where(Job.project_id == project_id,
                              Job.status.in_(("queued", "running")))
        ).all()
        for job in stuck:
            job.status = "error"
            job.error = "manually reset"
            session.add(job)
        session.commit()
        return {"reset": len(stuck)}


@router.post("/{project_id}/cookies")
async def upload_cookies(project_id: int, file: UploadFile):
    with get_session() as session:
        project = session.get(Project, project_id)
        if not project:
            raise HTTPException(404)
    dest = cookies_path(project.slug)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(await file.read())
    return {"ok": True}


@router.delete("/{project_id}")
def delete_project(project_id: int):
    """Permanently delete a project: its DB rows AND its on-disk artifacts and
    downloaded media. Cross-project quick-reference docs it contributed to are
    kept (only the contribution link is removed)."""
    import shutil

    from sqlmodel import text

    from ..config import settings

    with get_session() as session:
        project = session.get(Project, project_id)
        if not project:
            raise HTTPException(404)
        slug = project.slug
        for art in session.exec(select(Artifact).where(Artifact.project_id == project_id)).all():
            session.exec(text("DELETE FROM artifact_fts WHERE artifact_id = :id")
                         .bindparams(id=art.id))
            session.exec(text("DELETE FROM artifacttag WHERE artifact_id = :id")
                         .bindparams(id=art.id))
            session.delete(art)
        session.exec(text("DELETE FROM job WHERE project_id = :id").bindparams(id=project_id))
        session.exec(text("DELETE FROM quickrefsource WHERE project_id = :id")
                     .bindparams(id=project_id))
        session.delete(project)
        session.commit()

    # remove this project's own directories only — never the shared
    # tools/techniques/concepts quick-ref trees
    for path in (settings.library_dir / "projects" / slug,
                 settings.media_dir / slug):
        shutil.rmtree(path, ignore_errors=True)

    from ..settings_store import set_setting

    set_setting(f"projtags.{project_id}", None)  # drop the cached tag marker
    return {"ok": True}
