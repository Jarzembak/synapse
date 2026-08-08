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
from ..local_model_safety import (
    clear_inventory_cache,
    inspect_model,
    run_compatibility_benchmark,
)
from ..models import Job
from .celery_app import celery
from .common import set_job, transition_job

log = logging.getLogger("synapse.localmodels")


def _queue_automatic_benchmark(model: str) -> None:
    """Queue a non-fatal post-install compatibility check for chat models."""
    try:
        clear_inventory_cache()
        inventory, row = inspect_model(model, refresh=True)
        capabilities = set((row or {}).get("capabilities") or [])
        if inventory["ok"] and row and capabilities and "completion" not in capabilities:
            log.info(
                "ollama benchmark %s skipped: model does not advertise completion",
                model,
            )
            return
        with get_session() as session:
            job = Job(
                project_id=None,
                task="ollama_benchmark",
                progress=model,
                options=json.dumps({"automatic": True}),
            )
            session.add(job)
            session.commit()
            session.refresh(job)
            try:
                result = celery.send_task(
                    "ollama_benchmark",
                    args=[job.id, model],
                )
                job.celery_id = result.id
            except Exception as exc:
                job.status = "error"
                job.error = f"could not dispatch automatic benchmark: {exc}"[:2000]
            session.add(job)
            session.commit()
    except Exception:
        # Installation is complete and useful even if inspection, persistence,
        # or broker dispatch for this optional follow-up fails.
        log.warning(
            "could not queue automatic ollama benchmark for %s",
            model,
            exc_info=True,
        )


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
            # Ollama is a local transport boundary; HTTP(S)_PROXY must not
            # redirect Compose-internal hostnames (see llm._ollama)
            trust_env=False,
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
        _queue_automatic_benchmark(model)
    except Exception as e:
        with get_session() as session:
            transition_job(session, job_id, {"queued", "running"}, "error",
                           error=str(e)[:2000])
        log.error("ollama pull failed for %s: %s", model, e)
        raise


@celery.task(
    name="ollama_benchmark",
    soft_time_limit=120,
    time_limit=150,
)
def ollama_benchmark(job_id: int, model: str):
    """Run one small completion/JSON probe and persist it by model digest."""
    with get_session() as session:
        if not transition_job(
            session,
            job_id,
            {"queued"},
            "running",
            progress=f"{model}: checking compatibility",
        ):
            log.info(
                "ollama benchmark %s skipped: job %s is no longer queued",
                model,
                job_id,
            )
            return
    try:
        result = run_compatibility_benchmark(model)
        with get_session() as session:
            transition_job(
                session,
                job_id,
                {"running"},
                "done",
                progress=f"{model}: compatible",
            )
        log.info("benchmarked ollama model %s: %s", model, result)
    except Exception as exc:
        with get_session() as session:
            transition_job(
                session,
                job_id,
                {"queued", "running"},
                "error",
                error=str(exc)[:2000],
            )
        log.error("ollama benchmark failed for %s: %s", model, exc)
        raise
