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

router = APIRouter(prefix="/api/projects", tags=["projects"])

# step name → (celery task name, human label); order defines the pipeline board
STEPS: list[tuple[str, str]] = [
    ("ingest", "Ingest media"),
    ("transcribe", "Transcript"),
    ("correct", "Correction pass"),
    ("summarize", "Summary"),
    ("deepdive_claude", "Deep dive (Claude)"),
    ("deepdive_gemini", "Deep dive (Gemini)"),
    ("merge", "Merge deep dives"),
    ("quickref", "Quick-references"),
    ("podcast_script", "Podcast script"),
    ("tts", "Podcast audio"),
    ("trim", "Trim audio"),
    ("mindmap", "Mind map"),
]
STEP_NAMES = {s for s, _ in STEPS}


class ProjectCreate(BaseModel):
    source: str
    source_type: str  # "url" | "local"
    title: str | None = None


@router.post("")
def create_project(req: ProjectCreate):
    if req.source_type not in ("url", "local"):
        raise HTTPException(400, "source_type must be 'url' or 'local'")
    title = req.title or (f"(pending: {req.source[:60]})" if req.source_type == "url"
                          else req.source.rsplit("/", 1)[-1])
    slug = library.make_slug(req.title or req.source.rsplit("/", 1)[-1].rsplit("?", 1)[0])
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
        return {
            "project": project,
            "artifacts": artifacts,
            "steps": [
                {
                    "name": name,
                    "label": label,
                    "job": latest.get(name),
                    "artifact": next((a for a in artifacts if a.type == name or
                                      (name == "transcribe" and a.type == "transcript") or
                                      (name == "correct" and a.type == "corrected") or
                                      (name == "summarize" and a.type == "summary") or
                                      (name == "merge" and a.type == "deepdive_merged") or
                                      (name == "tts" and a.type == "podcast_audio") or
                                      (name == "trim" and a.type == "trimmed_audio")), None),
                }
                for name, label in STEPS
            ],
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
        job = Job(project_id=project_id, task=step)
        session.add(job)
        session.commit()
        session.refresh(job)
        async_result = celery.send_task(step, args=[job.id, project_id])
        job.celery_id = async_result.id
        session.add(job)
        session.commit()
        return job


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
    """Remove the project's DB rows. Library files stay on disk by design."""
    from sqlmodel import text

    with get_session() as session:
        project = session.get(Project, project_id)
        if not project:
            raise HTTPException(404)
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
    return {"ok": True}
