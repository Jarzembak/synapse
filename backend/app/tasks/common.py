"""Shared helpers for pipeline tasks: job bookkeeping and artifact access."""
from __future__ import annotations

import functools
import logging
import traceback

from sqlmodel import Session, select

from ..db import get_session
from ..models import Artifact, Job, Project, utcnow
from .. import library

log = logging.getLogger("synapse.pipeline")


def set_job(session: Session, job_id: int, **fields) -> None:
    job = session.get(Job, job_id)
    if not job:
        return
    for k, v in fields.items():
        setattr(job, k, v)
    job.updated = utcnow()
    session.add(job)
    session.commit()


def progress(job_id: int, message: str) -> None:
    with get_session() as session:
        set_job(session, job_id, progress=message)


def pipeline_task(fn):
    """Wrap a task body with job status transitions + error capture."""

    @functools.wraps(fn)
    def wrapper(job_id: int, project_id: int, *args, **kwargs):
        log.info("step %s starting (job=%s project=%s)", fn.__name__, job_id, project_id)
        with get_session() as session:
            job = session.get(Job, job_id)
            if job and job.status == "canceled":
                # user canceled between enqueue and pickup — honor it instead
                # of resurrecting the job to running/done
                log.info("step %s skipped: job=%s already canceled", fn.__name__, job_id)
                return None
            set_job(session, job_id, status="running")
        try:
            result = fn(job_id, project_id, *args, **kwargs)
            with get_session() as session:
                job = session.get(Job, job_id)
                if job and job.status == "canceled":
                    log.info("step %s finished but job=%s was canceled — leaving canceled",
                             fn.__name__, job_id)
                    return result
                set_job(session, job_id, status="done", progress="complete")
            log.info("step %s done (job=%s project=%s)", fn.__name__, job_id, project_id)
            return result
        except Exception as e:  # surface the real error to the UI
            log.exception("step %s failed (job=%s project=%s)", fn.__name__, job_id, project_id)
            with get_session() as session:
                set_job(
                    session, job_id, status="error",
                    error=f"{e}\n{traceback.format_exc()[-2000:]}",
                )
            raise

    return wrapper


def get_project(session: Session, project_id: int) -> Project:
    project = session.get(Project, project_id)
    if not project:
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
