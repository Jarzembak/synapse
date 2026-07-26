from __future__ import annotations

import json

from .. import media_storage
from ..db import get_session
from .celery_app import celery
from .common import transition_job


def _run(job_id: int, operation, *, running: str):
    with get_session() as session:
        if not transition_job(
                session, job_id, {"queued"}, "running", progress=running):
            # Cancellation before pickup and duplicate broker delivery are both
            # terminal for this delivery. Most importantly, an evict operation
            # must never run after its Job row has been canceled.
            return None
    try:
        result = operation()
        with get_session() as session:
            transition_job(
                session, job_id, {"running"}, "done",
                progress=json.dumps(result, sort_keys=True))
        return result
    except Exception as exc:
        with get_session() as session:
            transition_job(
                session, job_id, {"running"}, "error",
                error=str(exc)[:2000])
        raise


@celery.task(name="media_storage_sync")
def sync_project(job_id: int, project_id: int):
    return _run(
        job_id,
        lambda: media_storage.sync_project_media(project_id),
        running="uploading and verifying media",
    )


@celery.task(name="media_storage_evict")
def evict_project(job_id: int, project_id: int):
    return _run(
        job_id,
        lambda: media_storage.evict_project_media(project_id, job_id=job_id),
        running="freeing verified local media",
    )


@celery.task(name="media_storage_restore")
def restore_project(job_id: int, project_id: int):
    return _run(
        job_id,
        lambda: media_storage.restore_project_media(project_id),
        running="restoring media from cloud",
    )


@celery.task(name="media_storage_purge")
def purge_project(job_id: int, project_id: int):
    return _run(
        job_id,
        lambda: media_storage.purge_project_remote_media(
            project_id, job_id=job_id),
        running="purging verified remote media",
    )
