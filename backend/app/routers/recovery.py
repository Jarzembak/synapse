from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlmodel import select

from ..db import get_session
from ..models import Job
from ..recovery import health_report
from ..tasks.celery_app import celery
from ..tasks.common import set_job

router = APIRouter(prefix="/api/library", tags=["recovery"])


@router.get("/health")
def library_health():
    with get_session() as session:
        return health_report(session)


class RepairRequest(BaseModel):
    prune_missing: bool = False


@router.post("/repair")
def repair_library(req: RepairRequest):
    with get_session() as session:
        active = session.exec(
            select(Job).where(Job.task == "rebuild_library",
                              Job.status.in_(("queued", "running")))
        ).first()
        if active:
            raise HTTPException(409, "library repair is already active")
        job = Job(task="rebuild_library")
        session.add(job)
        session.commit()
        session.refresh(job)
        try:
            result = celery.send_task("rebuild_library", args=[job.id, req.prune_missing])
            job.celery_id = result.id
            session.add(job)
            session.commit()
        except Exception as exc:
            set_job(session, job.id, status="error", error=f"could not dispatch: {exc}")
            raise HTTPException(503, "worker queue is unavailable")
        session.refresh(job)
        return job
