from __future__ import annotations

from ..db import get_session
from ..recovery import rebuild_from_vault
from .celery_app import celery
from .common import set_job, transition_job


@celery.task(name="rebuild_library")
def rebuild_library(job_id: int, prune_missing: bool = False):
    with get_session() as session:
        if not transition_job(session, job_id, {"queued"}, "running"):
            return
    try:
        with get_session() as session:
            result = rebuild_from_vault(
                session, prune_missing=prune_missing,
                on_progress=lambda message: set_job(session, job_id, progress=message),
            )
        with get_session() as session:
            transition_job(session, job_id, {"running"}, "done",
                           progress=f"reconciled {result['reconciled']} files")
    except Exception as exc:
        with get_session() as session:
            transition_job(session, job_id, {"running"}, "error", error=str(exc)[:2000])
        raise
