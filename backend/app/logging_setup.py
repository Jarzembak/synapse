"""Central logging for both processes (api + worker).

One format, two destinations: stdout (captured by `docker compose logs`) and a
rotating file per service under LOG_DIR (a shared volume, so all services'
logs land in one host directory — data/logs/). Configuration is env-driven:

    LOG_LEVEL   DEBUG|INFO|WARNING|ERROR   (default INFO)
    LOG_DIR     directory for rotating log files; unset = stdout only
    LOG_NAME    service name used in the filename (synapse-<name>.log)

Called once at import time by app.main (api) and app.tasks.celery_app
(worker); safe to call repeatedly.
"""
from __future__ import annotations

import logging
import logging.handlers
import os
from pathlib import Path

FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"

_configured = False


def setup_logging() -> None:
    global _configured
    if _configured:
        return
    _configured = True

    root = logging.getLogger()
    level = os.environ.get("LOG_LEVEL", "INFO").strip().upper()
    if level not in logging.getLevelNamesMapping():
        level = "INFO"  # a typo in .env must not crash-loop both services
    root.setLevel(level)
    fmt = logging.Formatter(FORMAT)

    if not root.handlers:  # don't double-log if something configured us first
        stream = logging.StreamHandler()
        stream.setFormatter(fmt)
        root.addHandler(stream)

    # Quiet chatty third-party per-request logging so our own pipeline logs stay
    # readable — httpx logs a line for EVERY request (the system monitor polls
    # Ollama every 2s, which would otherwise flood the api log).
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    log_dir = os.environ.get("LOG_DIR", "")
    if log_dir:
        try:
            Path(log_dir).mkdir(parents=True, exist_ok=True)
            name = os.environ.get("LOG_NAME", "app")
            fh = logging.handlers.RotatingFileHandler(
                Path(log_dir) / f"synapse-{name}.log",
                maxBytes=5_000_000, backupCount=3, encoding="utf-8",
            )
            fh.setFormatter(fmt)
            root.addHandler(fh)
        except OSError as e:
            logging.getLogger(__name__).warning(
                "could not open log directory %r: %s", log_dir, e)


def log_file_for(service: str) -> Path | None:
    """Path of a service's current log file, if file logging is configured."""
    log_dir = os.environ.get("LOG_DIR", "")
    if not log_dir:
        return None
    return Path(log_dir) / f"synapse-{service}.log"
