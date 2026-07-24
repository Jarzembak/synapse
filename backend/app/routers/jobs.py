from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, HTTPException
from sse_starlette.sse import EventSourceResponse
from sqlmodel import select

from ..db import get_session
from ..models import Job, Project
from ..tasks.celery_app import celery
from ..tasks.common import transition_job
from ..tasks.orchestrate import STEP_LABELS, cancel_children, maybe_start_next_run_all

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


NONSTEP_LABELS = {
    "run_all": "Run all steps",
    "cloud_sync_all": "Cloud sync — everything",
    "ollama_pull": "Install local model",
    "create_backup": "Create backup",
    "rebuild_library": "Rebuild index from vault",
    "rebuild_search": "Rebuild search index",
}


def _label(task: str) -> str:
    return NONSTEP_LABELS.get(task) or STEP_LABELS.get(task, task)


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
        "started": job.started.isoformat() if job.started else None,
        "finished": job.finished.isoformat() if job.finished else None,
        "parent_job_id": job.parent_job_id,
        "connected": True,
    }


@router.get("")
def list_jobs(project_id: int | None = None, limit: int = 100):
    with get_session() as session:
        titles = _project_titles(session)
        q = select(Job).order_by(Job.created.desc()).limit(max(1, min(limit, 1000)))
        if project_id is not None:
            q = q.where(Job.project_id == project_id)
        return [_serialize(j, titles) for j in session.exec(q).all()]


@router.post("/continue")
def continue_queue():
    """Resume the whole-project run-all queue if it stalled.

    Run-alls execute one at a time and auto-chain, but that hand-off is
    event-driven — if the worker restarts mid-run (crash, redeploy, reboot) the
    chain breaks and queued jobs wait forever. This kicks the next one; it's a
    no-op if one is already running or nothing is queued."""
    with get_session() as session:
        running = session.exec(
            select(Job).where(Job.task == "run_all", Job.status == "running")
        ).first()
        queued = len(session.exec(
            select(Job).where(Job.task == "run_all", Job.status == "queued")
        ).all())
    maybe_start_next_run_all()
    return {"already_running": running is not None, "queued": queued}


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
        # Fence database state before revocation so a task picked up in the
        # small race window sees a terminal state and exits without publishing.
        transition_job(session, job.id, {"queued", "running"}, "canceled",
                       error="canceled by user")
        if job.task == "run_all":
            cancel_children(job.id, "run-all canceled by user")
        if job.celery_id:
            try:
                # terminate=True stops a task that's already executing, not just
                # one still waiting in the queue.
                celery.control.revoke(job.celery_id, terminate=True)
            except Exception:
                pass
    # Canceling the active orchestrator should immediately release the serial
    # queue even if the revoked task never reaches its finally block.
    maybe_start_next_run_all()
    return {"ok": True}


@router.get("/stream")
async def stream_jobs(project_id: int | None = None):
    """SSE: emits the active + recent job snapshot whenever it changes."""

    async def gen():
        last = ""
        idle_ticks = 0
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
            idle_ticks += 1
            # emit on change, and at least every ~15s as a heartbeat so the
            # client can tell a live-but-idle stream from a dead one
            if payload != last or idle_ticks >= 15:
                last = payload
                idle_ticks = 0
                yield {"event": "jobs", "data": payload}
            await asyncio.sleep(1)

    return EventSourceResponse(gen())
