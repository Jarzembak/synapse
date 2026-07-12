from __future__ import annotations

from datetime import datetime, timezone

from ..backup import create_backup
from ..db import get_session
from ..models import Job
from ..settings_store import get_setting, set_setting
from sqlmodel import select
from .celery_app import celery
from .common import set_job, transition_job


@celery.task(name="create_backup")
def backup_task(job_id: int, include_media: bool = True,
                include_repositories: bool = False):
    with get_session() as session:
        if not transition_job(session, job_id, {"queued"}, "running",
                              progress="snapshotting database and vault"):
            return
    try:
        path = create_backup(
            include_media=include_media,
            include_repositories=include_repositories,
        )
        set_setting("backup.last", {
            "status": "ok", "path": path.name,
            "at": datetime.now(timezone.utc).isoformat(),
        })
        with get_session() as session:
            transition_job(session, job_id, {"running"}, "done",
                           progress=f"created {path.name}")
    except Exception as exc:
        set_setting("backup.last", {
            "status": "error", "detail": str(exc)[:500],
            "at": datetime.now(timezone.utc).isoformat(),
        })
        with get_session() as session:
            transition_job(session, job_id, {"running"}, "error", error=str(exc)[:2000])
        raise


@celery.task(name="scheduled_backup_check")
def scheduled_backup_check():
    hours = int(get_setting("backup.schedule_hours", 0) or 0)
    if hours <= 0:
        return
    last = get_setting("backup.last") or {}
    try:
        last_at = datetime.fromisoformat(last.get("at", ""))
    except (TypeError, ValueError):
        last_at = None
    now = datetime.now(timezone.utc)
    if last_at and last_at.tzinfo is None:
        last_at = last_at.replace(tzinfo=timezone.utc)
    if last_at and (now - last_at).total_seconds() < hours * 3600:
        return
    with get_session() as session:
        if session.exec(select(Job).where(
            Job.status.in_(("queued", "running"))
        )).first():
            return
        job = Job(task="create_backup")
        session.add(job)
        session.commit()
        session.refresh(job)
        try:
            result = celery.send_task(
                "create_backup",
                args=[
                    job.id,
                    bool(get_setting("backup.include_media", True)),
                    bool(get_setting("backup.include_repositories", False)),
                ],
            )
            job.celery_id = result.id
            session.add(job)
            session.commit()
        except Exception as exc:
            set_job(session, job.id, status="error",
                    error=f"could not dispatch scheduled backup: {exc}")
            raise
