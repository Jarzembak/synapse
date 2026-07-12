"""Shared helpers for pipeline tasks: job bookkeeping and artifact access."""
from __future__ import annotations

import functools
import logging
import traceback

from sqlalchemy import update
from sqlmodel import Session, select

from ..db import get_session
from ..context import current_job_id
from ..models import Artifact, Job, Project, utcnow
from .. import library

log = logging.getLogger("synapse.pipeline")
TERMINAL_JOB_STATES = {"done", "error", "canceled"}


class JobCanceled(RuntimeError):
    pass


def set_job(session: Session, job_id: int, *, force: bool = False, **fields) -> bool:
    """Update a job without allowing late workers to resurrect terminal state."""
    job = session.get(Job, job_id)
    if not job:
        return False
    requested = fields.get("status")
    if (not force and job.status in TERMINAL_JOB_STATES
            and requested is not None and requested != job.status):
        return False
    now = utcnow()
    if requested == "running" and job.started is None:
        fields.setdefault("started", now)
    if requested in TERMINAL_JOB_STATES:
        fields.setdefault("finished", now)
    for k, v in fields.items():
        setattr(job, k, v)
    job.updated = now
    if job.status in ("queued", "running"):
        job.heartbeat = now
    session.add(job)
    session.commit()
    return True


def transition_job(session: Session, job_id: int, from_states: set[str],
                   to_state: str, **fields) -> bool:
    """Atomically compare-and-set a job state."""
    now = utcnow()
    values = {**fields, "status": to_state, "updated": now, "heartbeat": now}
    if to_state == "running":
        values["started"] = now
    if to_state in TERMINAL_JOB_STATES:
        values["finished"] = now
    result = session.exec(
        update(Job).where(Job.id == job_id, Job.status.in_(from_states)).values(**values)
    )
    session.commit()
    return getattr(result, "rowcount", 0) == 1


def progress(job_id: int, message: str) -> None:
    with get_session() as session:
        job = session.get(Job, job_id)
        if job and job.status == "canceled":
            raise JobCanceled("job was canceled")
        if job and job.parent_job_id:
            parent = session.get(Job, job.parent_job_id)
            if not parent or parent.status != "running":
                raise JobCanceled("parent run is no longer active")
        if job and job.status in ("queued", "running"):
            set_job(session, job_id, progress=message)


def pipeline_task(fn):
    """Wrap a task in cancellation-safe, monotonic job transitions."""

    @functools.wraps(fn)
    def wrapper(job_id: int, project_id: int, *args, **kwargs):
        log.info("step %s starting (job=%s project=%s)", fn.__name__, job_id, project_id)
        with get_session() as session:
            job = session.get(Job, job_id)
            if not job:
                if job_id <= 0:
                    # Pure/test invocation: execute without persistence. Real
                    # database jobs always have a positive primary key.
                    return fn(job_id, project_id, *args, **kwargs)
                log.info("step %s skipped: job=%s was removed", fn.__name__, job_id)
                return None
            if job.parent_job_id:
                parent = session.get(Job, job.parent_job_id)
                if not parent or parent.status != "running":
                    set_job(session, job_id, status="canceled",
                            error="parent run is no longer active")
                    return None
            if not transition_job(session, job_id, {"queued"}, "running"):
                # Covers cancel-before-pickup and duplicate broker delivery.
                log.info("step %s skipped: job=%s is no longer queued",
                         fn.__name__, job_id)
                return None
        try:
            context_token = current_job_id.set(job_id)
            try:
                result = fn(job_id, project_id, *args, **kwargs)
            finally:
                current_job_id.reset(context_token)
            if fn.__name__ in {"ingest", "quickref"}:
                try:
                    from ..provenance import record_nonartifact_step

                    with get_session() as provenance_session:
                        project = provenance_session.get(Project, project_id)
                        if project and not project.deleting:
                            record_nonartifact_step(
                                provenance_session, project, fn.__name__)
                except Exception:
                    log.warning("could not record provenance for %s", fn.__name__,
                                exc_info=True)
            with get_session() as session:
                completed = transition_job(
                    session, job_id, {"running"}, "done", progress="complete")
                if not completed:
                    log.info("step %s finished after cancellation; state preserved",
                             fn.__name__)
                    return result
            log.info("step %s done (job=%s project=%s)", fn.__name__, job_id, project_id)
            return result
        except Exception as e:
            log.exception("step %s failed (job=%s project=%s)",
                          fn.__name__, job_id, project_id)
            with get_session() as session:
                transition_job(
                    session, job_id, {"queued", "running"}, "error",
                    error=f"{e}\n{traceback.format_exc()[-2000:]}",
                )
            raise

    return wrapper


def get_project(session: Session, project_id: int) -> Project:
    project = session.get(Project, project_id)
    if not project or project.deleting:
        raise ValueError(f"project {project_id} not found")
    return project


def artifact_body(session: Session, project_id: int, type: str) -> str:
    """Read the body of a project's artifact of the given type from disk."""
    art = session.exec(
        select(Artifact).where(
            Artifact.project_id == project_id, Artifact.type == type
        )
    ).first()
    if not art:
        raise ValueError(f"missing prerequisite artifact {type!r} — run that step first")
    _, body = library.read_doc(art.path)
    return body


def best_transcript(session: Session, project_id: int) -> str:
    """Corrected transcript if present, else raw."""
    try:
        return artifact_body(session, project_id, "corrected")
    except ValueError:
        return artifact_body(session, project_id, "transcript")


def auto_tag(project_id: int | None, artifact_id: int) -> None:
    """Fire-and-forget tagging of a freshly written artifact.

    Quick-ref docs are tagged individually from their own content; everything
    else triggers project-level tagging (one canonical set derived from the
    project's richest document, propagated to all of its artifacts — see
    generate.tag_project).
    """
    from .generate import tag_project, tag_task

    with get_session() as session:
        art = session.get(Artifact, artifact_id)
    if art and art.type.startswith("quickref_"):
        tag_task.delay(artifact_id)
    elif project_id:
        tag_project.delay(project_id)
