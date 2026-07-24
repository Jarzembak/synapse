"""Install Ollama models from the Settings UI.

Streams Ollama's /api/pull (newline-delimited JSON progress) into the Job row
so the Jobs page shows live download percentages. Models come from Ollama's
registry (ollama.com/library); a pull can be tens of gigabytes, so this runs
as a worker task rather than blocking an API request.
"""
from __future__ import annotations

import json
import logging
import time

import httpx

from ..config import settings
from ..db import get_session
from .celery_app import celery
from .common import set_job, transition_job

log = logging.getLogger("synapse.localmodels")


@celery.task(name="ollama_pull")
def ollama_pull(job_id: int, model: str):
    with get_session() as session:
        # CAS queued→running: a job canceled before pickup (or delivered
        # twice) must not download gigabytes anyway
        if not transition_job(session, job_id, {"queued"}, "running",
                              progress=f"{model}: starting"):
            log.info("ollama pull %s skipped: job %s is no longer queued",
                     model, job_id)
            return
    try:
        with httpx.stream(
            "POST", f"{settings.ollama_base_url}/api/pull",
            json={"model": model},
            # no read timeout: big models download for a long time between
            # progress lines; celery's task time limit is the backstop
            timeout=httpx.Timeout(None, connect=10),
        ) as response:
            if response.status_code >= 400:
                response.read()
                try:
                    detail = response.json().get("error") or response.text
                except ValueError:
                    detail = response.text
                raise RuntimeError(f"ollama returned {response.status_code}: "
                                   f"{detail[:500]}")
            last_update = 0.0
            for line in response.iter_lines():
                if not line.strip():
                    continue
                data = json.loads(line)
                if data.get("error"):
                    raise RuntimeError(data["error"])
                status = data.get("status", "")
                total, completed = data.get("total"), data.get("completed")
                if total and completed:
                    text = f"{model}: {status} {completed / total * 100:.0f}%"
                else:
                    text = f"{model}: {status}"
                # throttle DB writes — progress lines arrive many times a second
                now = time.monotonic()
                if now - last_update >= 2:
                    with get_session() as session:
                        # set_job refuses to touch a terminal row — a False
                        # return means the job was canceled: stop downloading
                        if not set_job(session, job_id, status="running",
                                       progress=text[:200]):
                            log.info("ollama pull %s aborted: job %s canceled",
                                     model, job_id)
                            return
                    last_update = now
        with get_session() as session:
            transition_job(session, job_id, {"running"}, "done",
                           progress=f"{model}: installed")
        log.info("pulled ollama model %s", model)
    except Exception as e:
        with get_session() as session:
            transition_job(session, job_id, {"queued", "running"}, "error",
                           error=str(e)[:2000])
        log.error("ollama pull failed for %s: %s", model, e)
        raise
