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
