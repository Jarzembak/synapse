from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter
from sse_starlette.sse import EventSourceResponse
from sqlmodel import select

from ..db import get_session
from ..models import Job

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


def _serialize(job: Job) -> dict:
    return {
        "id": job.id, "project_id": job.project_id, "task": job.task,
        "status": job.status, "progress": job.progress, "error": job.error,
        "updated": job.updated.isoformat(),
    }


@router.get("")
def list_jobs(project_id: int | None = None, limit: int = 50):
    with get_session() as session:
        q = select(Job).order_by(Job.created.desc()).limit(limit)
        if project_id is not None:
            q = q.where(Job.project_id == project_id)
        return session.exec(q).all()


@router.get("/stream")
async def stream_jobs(project_id: int | None = None):
    """SSE: emits the active-job snapshot whenever it changes (1s poll)."""

    async def gen():
        last = ""
        while True:
            with get_session() as session:
                q = select(Job).where(Job.status.in_(("queued", "running")))
                if project_id is not None:
                    q = q.where(Job.project_id == project_id)
                active = [_serialize(j) for j in session.exec(q).all()]
                recent = session.exec(
                    select(Job).where(Job.status.in_(("done", "error")))
                    .order_by(Job.updated.desc()).limit(10)
                ).all()
                payload = json.dumps(
                    {"active": active, "recent": [_serialize(j) for j in recent]},
                    sort_keys=True,
                )
            if payload != last:
                last = payload
                yield {"event": "jobs", "data": payload}
            await asyncio.sleep(1)

    return EventSourceResponse(gen())
