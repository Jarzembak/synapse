from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, HTTPException
from sse_starlette.sse import EventSourceResponse
from sqlmodel import select

from ..db import get_session
from ..models import Job, Project
from ..tasks.celery_app import celery
from ..tasks.orchestrate import STEP_LABELS

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


def _label(task: str) -> str:
    if task == "run_all":
        return "Run all steps"
    return STEP_LABELS.get(task, task)


def _project_titles(session) -> dict[int, str]:
    return {p.id: p.title for p in session.exec(select(Project)).all()}


def _serialize(job: Job, titles: dict[int, str]) -> dict:
    return {
        "id": job.id, "project_id": job.project_id, "task": job.task,
        "task_label": _label(job.task),
        "project_title": titles.get(job.project_id or 0, ""),
        "status": job.status, "progress": job.progress, "error": job.error,
        "created": job.created.isoformat(),
        "updated": job.updated.isoformat(),
    }


@router.get("")
def list_jobs(project_id: int | None = None, limit: int = 100):
    with get_session() as session:
        titles = _project_titles(session)
        q = select(Job).order_by(Job.created.desc()).limit(limit)
        if project_id is not None:
            q = q.where(Job.project_id == project_id)
        return [_serialize(j, titles) for j in session.exec(q).all()]


@router.post("/{job_id}/cancel")
def cancel_job(job_id: int):
    """Cancel a queued or running job. Queued jobs simply drop out of the
    queue; a dispatched celery task is revoked; a running run-all's
    orchestrator notices the status and stops on its next poll."""
    with get_session() as session:
        job = session.get(Job, job_id)
        if not job:
            raise HTTPException(404)
        if job.status not in ("queued", "running"):
            raise HTTPException(409, f"job is already {job.status}")
        if job.celery_id:
            try:
                # terminate=True stops a task that's already executing, not just
                # one still waiting in the queue.
                celery.control.revoke(job.celery_id, terminate=True)
            except Exception:
                pass
        job.status = "canceled"
        job.error = "canceled by user"
        session.add(job)
        session.commit()
    return {"ok": True}


@router.get("/stream")
async def stream_jobs(project_id: int | None = None):
    """SSE: emits the active + recent job snapshot whenever it changes."""

    async def gen():
        last = ""
        while True:
            with get_session() as session:
                titles = _project_titles(session)
                aq = select(Job).where(Job.status.in_(("queued", "running"))) \
                    .order_by(Job.created)
                if project_id is not None:
                    aq = aq.where(Job.project_id == project_id)
                active = [_serialize(j, titles) for j in session.exec(aq).all()]
                rq = select(Job).where(Job.status.in_(("done", "error", "canceled"))) \
                    .order_by(Job.updated.desc()).limit(25)
                if project_id is not None:
                    rq = rq.where(Job.project_id == project_id)
                recent = [_serialize(j, titles) for j in session.exec(rq).all()]
                payload = json.dumps({"active": active, "recent": recent}, sort_keys=True)
            if payload != last:
                last = payload
                yield {"event": "jobs", "data": payload}
            await asyncio.sleep(1)

    return EventSourceResponse(gen())
