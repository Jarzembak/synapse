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
# The concurrency-one worker consuming the serial local-LLM queue; it runs
# the same task modules as the ordinary worker but only receives steps whose
# resolved provider is the bundled Ollama server (see common.step_queue).
LOCAL_LLM_WORKER = os.environ.get("SYNAPSE_LOCAL_LLM_WORKER", "").strip() == "1"

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
        media_storage,
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
    from ..task_names import MEDIA_AUTH_LEASE_TASK

    try:
        from .common import LOCAL_LLM_QUEUE

        paper_recovery = {"series": 0, "parts": 0}
        with get_session() as session:
            if PAPER_WORKER:
                owned_task = Job.task == "paper_extract"
            elif LOCAL_LLM_WORKER:
                # Only jobs dispatched to the serial local-LLM queue belong to
                # this worker; the ordinary worker's live jobs must survive a
                # restart here, and vice versa.
                owned_task = Job.queue == LOCAL_LLM_QUEUE
            else:
                owned_task = (
                    Job.task.not_in((
                        "paper_extract",
                        MEDIA_AUTH_LEASE_TASK,
                    ))
                    & (Job.queue != LOCAL_LLM_QUEUE)
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
            # Ghosts were never dispatched, so erroring them needs no
            # coordination with the worker that would have run them — the
            # always-present ordinary worker owns every non-paper ghost
            # regardless of the queue recorded on the row.
            ghosts = []
            if not LOCAL_LLM_WORKER:
                ghost_scope = (
                    Job.task == "paper_extract" if PAPER_WORKER
                    else Job.task.not_in((
                        "run_all",
                        "paper_extract",
                        MEDIA_AUTH_LEASE_TASK,
                    ))
                )
                ghosts = session.exec(select(Job).where(
                    Job.status == "queued", ghost_scope, Job.celery_id == "",
                )).all()
            for job in ghosts:
                job.status = "error"
                job.error = "interrupted before broker dispatch"
                job.updated = utcnow()
                session.add(job)
            if not PAPER_WORKER:
                from .paper_series import reconcile_interrupted_paper_jobs

                paper_recovery = reconcile_interrupted_paper_jobs(
                    session, stale)
            session.commit()
        if PAPER_WORKER:
            if stale or ghosts:
                logging.getLogger("synapse.pipeline").warning(
                    "reset %d running and %d undispatched paper job(s)",
                    len(stale), len(ghosts))
            return
        if LOCAL_LLM_WORKER:
            # Staging cleanup, media recovery, purge sweeps, and the run-all
            # kick remain the ordinary worker's startup duties.
            if stale:
                logging.getLogger("synapse.pipeline").warning(
                    "reset %d running local-LLM job(s) on worker start",
                    len(stale))
            if paper_recovery["series"] or paper_recovery["parts"]:
                logging.getLogger("synapse.pipeline").warning(
                    "reconciled %d interrupted paper series and %d part(s)",
                    paper_recovery["series"], paper_recovery["parts"])
            return
        from ..repository import cleanup_repository_staging

        cleanup_repository_staging()
        from ..media_storage import recover_interrupted_media_storage

        recover_interrupted_media_storage()
        from .cloud import enqueue_pending_privacy_purges

        enqueue_pending_privacy_purges()
        if stale or ghosts:
            logging.getLogger("synapse.pipeline").warning(
                "reset %d running and %d undispatched job(s) on worker start",
                len(stale), len(ghosts))
        if paper_recovery["series"] or paper_recovery["parts"]:
            logging.getLogger("synapse.pipeline").warning(
                "reconciled %d interrupted paper series and %d part(s)",
                paper_recovery["series"], paper_recovery["parts"])
        # Recovery is lease-based now, so safely continue the serial queue
        # instead of requiring a manual button after every deployment.
        from .orchestrate import maybe_start_next_run_all

        maybe_start_next_run_all()
    except Exception:  # never block worker startup on this
        logging.getLogger("synapse.pipeline").exception("orphaned-job reset failed")
