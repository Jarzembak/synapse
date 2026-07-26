from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlmodel import select, text

from .. import media_storage
from ..db import get_session
from ..models import Job, MediaStorageTarget, Project
from ..tasks.celery_app import celery
from ..tasks.common import set_job

router = APIRouter(
    prefix="/api/projects/{project_id}/media-storage",
    tags=["media-storage"],
)


class MediaPolicyUpdate(BaseModel):
    mode: str


def _status(project_id: int) -> dict:
    try:
        with get_session() as session:
            if not session.get(Project, project_id):
                raise HTTPException(404, "project not found")
            return media_storage.project_status(session, project_id)
    except media_storage.MediaStorageBusy as exc:
        raise HTTPException(409, str(exc)) from exc
    except media_storage.MediaStorageError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("")
def get_media_storage(project_id: int):
    return _status(project_id)


@router.put("")
def update_media_storage(project_id: int, req: MediaPolicyUpdate):
    try:
        from ..tasks import cloud

        # Legacy and cloud-primary publishers hold this same lock while
        # rechecking policy and mutating the remote. Policy changes therefore
        # cannot strand a payload staged under the previous mode.
        with cloud._remote_lock():
            with get_session() as session:
                media_storage.set_policy(session, project_id, req.mode)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except media_storage.MediaStorageBusy as exc:
        raise HTTPException(409, str(exc)) from exc
    except media_storage.MediaStorageError as exc:
        raise HTTPException(409, str(exc)) from exc
    return _status(project_id)


def _queue_action(project_id: int, action: str) -> dict:
    task_name = media_storage.ACTION_TASKS[action]
    with get_session() as session:
        session.exec(text("BEGIN IMMEDIATE"))
        project = session.get(Project, project_id)
        if not project:
            raise HTTPException(404, "project not found")
        if project.deleting:
            raise HTTPException(409, "project is being deleted")
        active = session.exec(select(Job).where(
            Job.project_id == project_id,
            Job.status.in_(("queued", "running")),
        )).first()
        if active:
            raise HTTPException(
                409, "wait for the active project job to finish before "
                     f"starting media {action}")
        if media_storage.active_media_transition(session, project_id):
            raise HTTPException(
                409,
                "wait for interrupted media storage state recovery before "
                f"starting media {action}",
            )
        policy = media_storage.get_policy(session, project_id)
        if action in {"sync", "evict"} and policy.mode != media_storage.CLOUD_PRIMARY:
            raise HTTPException(
                409, "enable cloud-primary media before starting this action")
        if action == "purge" and policy.mode != media_storage.KEEP_LOCAL:
            raise HTTPException(
                409, "switch to keep-local before purging remote media")
        if action in {"sync", "evict", "purge"}:
            try:
                target = session.get(
                    MediaStorageTarget, policy.storage_target_id)
                if not target:
                    raise media_storage.MediaStorageError(
                        "media storage target is missing")
                media_storage._assert_target_current(target)
            except media_storage.MediaStorageError as exc:
                raise HTTPException(409, str(exc)) from exc
        job = Job(project_id=project_id, task=task_name)
        session.add(job)
        session.commit()
        session.refresh(job)
        try:
            result = celery.send_task(task_name, args=[job.id, project_id])
            job.celery_id = result.id
            session.add(job)
            session.commit()
        except Exception as exc:
            set_job(
                session, job.id, status="error",
                error=f"could not dispatch: {exc}"[:2000])
            raise HTTPException(503, "worker queue is unavailable") from exc
        session.refresh(job)
        return job.model_dump()


@router.post("/sync")
def sync_media(project_id: int):
    """Upload and strongly verify eligible media; never remove local files."""
    return _queue_action(project_id, "sync")


@router.post("/evict")
def evict_media(project_id: int):
    """Free only durably verified media after project-idle and lease checks."""
    return _queue_action(project_id, "evict")


@router.post("/restore")
def restore_media(project_id: int):
    """Restore every absent, verified remote object to its canonical local path."""
    return _queue_action(project_id, "restore")


@router.post("/purge")
def purge_media(project_id: int):
    """Restore as needed, then safely remove this project's remote references."""
    return _queue_action(project_id, "purge")
