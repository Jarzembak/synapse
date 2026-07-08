from __future__ import annotations

from celery import Celery

from ..logging_setup import setup_logging

setup_logging()

from ..config import settings  # noqa: E402

celery = Celery("vst", broker=settings.redis_url, backend=settings.redis_url)
celery.conf.task_track_started = True
celery.conf.worker_hijack_root_logger = False

from ..db import init_db  # noqa: E402

init_db()  # worker may start before the api; both are idempotent

# Import task modules so the worker registers them.
from . import (  # noqa: E402,F401
    ingest, transcribe, generate, quickref, audio, cloud, orchestrate,
)

from celery.signals import worker_ready  # noqa: E402


@worker_ready.connect
def _reset_orphaned_jobs(**_kwargs):
    """A freshly-started worker has nothing running yet, so any Job still marked
    'running' is orphaned from a previous worker that died mid-task (celery
    early-acks; the task is not redelivered). Left as-is such a phantom blocks
    the serial run-all queue forever and hides the Continue button. Mark them
    failed so the queue is unblocked — recovery stays a manual Continue action,
    not an auto-resume. (Assumes a single worker, as docker-compose defines.)"""
    import logging

    from sqlmodel import select

    from ..db import get_session
    from ..models import Job, utcnow

    try:
        with get_session() as session:
            stale = session.exec(select(Job).where(Job.status == "running")).all()
            for job in stale:
                job.status = "error"
                job.error = (job.error + "\n" if job.error else "") + \
                    "interrupted by a worker restart"
                job.updated = utcnow()
                session.add(job)
            session.commit()
        if stale:
            logging.getLogger("synapse.pipeline").warning(
                "reset %d orphaned running job(s) on worker start", len(stale))
    except Exception:  # never block worker startup on this
        logging.getLogger("synapse.pipeline").exception("orphaned-job reset failed")
