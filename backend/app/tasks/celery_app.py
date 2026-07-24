from __future__ import annotations

import os

from celery import Celery

from ..logging_setup import setup_logging

setup_logging()

from ..config import settings, validate_storage_roots  # noqa: E402

validate_storage_roots()

celery = Celery("vst", broker=settings.redis_url, backend=settings.redis_url)
celery.conf.task_track_started = True
celery.conf.worker_hijack_root_logger = False
celery.conf.task_soft_time_limit = 7 * 3600
celery.conf.task_time_limit = 8 * 3600
celery.conf.worker_cancel_long_running_tasks_on_connection_loss = True
# CPU-heavy PDF parsing/OCR is isolated to the concurrency-one paper worker;
# analysis and synthesis continue on the normal queue.
celery.conf.task_routes = {
    "paper_extract": {"queue": "paper"},
}
celery.conf.beat_schedule = {
    "scheduled-backup-check": {
        "task": "scheduled_backup_check",
        "schedule": 3600.0,
    },
    "repository-cloud-privacy-purge": {
        "task": "cloud_privacy_purge_sweep",
        "schedule": 3600.0,
    },
}

PAPER_WORKER = os.environ.get("SYNAPSE_PAPER_WORKER", "").strip() == "1"

from ..db import init_db  # noqa: E402

init_db()  # worker may start before the api; both are idempotent

# Import only the tasks consumed by this worker.  The dedicated parser image
# deliberately excludes media/ASR/TTS dependencies; analysis and all other
# paper-series tasks remain registered on the ordinary worker.
if PAPER_WORKER:
    from . import paper  # noqa: E402,F401
else:
    from . import (  # noqa: E402,F401
        ingest, transcribe, generate, repository, quickref, audio, cloud,
        orchestrate, backup, recovery, search, paper, paper_series, localmodels,
    )

from celery.signals import worker_ready  # noqa: E402


@worker_ready.connect
def _reset_orphaned_jobs(**_kwargs):
    """Reset only jobs owned by the queue of the worker that just started.

    The ordinary and parser workers run concurrently.  Treating every running
    row as orphaned when either worker restarts would corrupt live work on the
    other queue, so paper extraction is explicitly partitioned here.
    """
    import logging

    from sqlmodel import select

    from ..db import get_session
    from ..models import Job, utcnow

    try:
        with get_session() as session:
            owned_task = (
                Job.task == "paper_extract" if PAPER_WORKER
                else Job.task != "paper_extract"
            )
            stale = session.exec(select(Job).where(
                Job.status == "running", owned_task,
            )).all()
            for job in stale:
                job.status = "error"
                job.error = (job.error + "\n" if job.error else "") + \
                    "interrupted by a worker restart"
                job.updated = utcnow()
                session.add(job)
            # Planned child rows that never reached the broker are distinguishable
            # from durable queued Celery messages by their empty celery id.
            ghost_scope = (
                Job.task == "paper_extract" if PAPER_WORKER
                else Job.task.not_in(("run_all", "paper_extract"))
            )
            ghosts = session.exec(select(Job).where(
                Job.status == "queued", ghost_scope, Job.celery_id == "",
            )).all()
            for job in ghosts:
                job.status = "error"
                job.error = "interrupted before broker dispatch"
                job.updated = utcnow()
                session.add(job)
            session.commit()
        if PAPER_WORKER:
            if stale or ghosts:
                logging.getLogger("synapse.pipeline").warning(
                    "reset %d running and %d undispatched paper job(s)",
                    len(stale), len(ghosts))
            return
        from ..repository import cleanup_repository_staging

        cleanup_repository_staging()
        from .cloud import enqueue_pending_privacy_purges

        enqueue_pending_privacy_purges()
        if stale or ghosts:
            logging.getLogger("synapse.pipeline").warning(
                "reset %d running and %d undispatched job(s) on worker start",
                len(stale), len(ghosts))
        # Recovery is lease-based now, so safely continue the serial queue
        # instead of requiring a manual button after every deployment.
        from .orchestrate import maybe_start_next_run_all

        maybe_start_next_run_all()
    except Exception:  # never block worker startup on this
        logging.getLogger("synapse.pipeline").exception("orphaned-job reset failed")
