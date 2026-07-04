from __future__ import annotations

from celery import Celery

from ..config import settings

celery = Celery("vst", broker=settings.redis_url, backend=settings.redis_url)
celery.conf.task_track_started = True
celery.conf.worker_hijack_root_logger = False

from ..db import init_db  # noqa: E402

init_db()  # worker may start before the api; both are idempotent

# Import task modules so the worker registers them.
from . import ingest, transcribe, generate, quickref, audio, cloud  # noqa: E402,F401
