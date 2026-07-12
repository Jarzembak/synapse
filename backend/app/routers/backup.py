from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlmodel import select

from ..backup import list_backups, verify_backup
from ..config import settings
from ..db import get_session
from ..models import Job
from ..settings_store import get_setting
from ..tasks.celery_app import celery
from ..tasks.common import set_job

router = APIRouter(prefix="/api/backups", tags=["backups"])


def _serialize(path):
    return {
        "name": path.name, "size": path.stat().st_size,
        "updated": path.stat().st_mtime,
        "encrypted": path.suffix == ".enc",
    }


@router.get("")
def backups():
    return {
        "backups": [_serialize(path) for path in list_backups()],
        "encryption_configured": bool(settings.backup_encryption_key),
    }


class BackupRequest(BaseModel):
    include_media: bool | None = None


@router.post("")
def start_backup(req: BackupRequest):
    include_media = (bool(get_setting("backup.include_media", True))
                     if req.include_media is None else req.include_media)
    with get_session() as session:
        active = session.exec(
            select(Job).where(Job.task == "create_backup",
                              Job.status.in_(("queued", "running")))
        ).first()
        if active:
            raise HTTPException(409, "a backup is already active")
        job = Job(task="create_backup")
        session.add(job)
        session.commit()
        session.refresh(job)
        try:
            result = celery.send_task("create_backup", args=[job.id, include_media])
            job.celery_id = result.id
            session.add(job)
            session.commit()
        except Exception as exc:
            set_job(session, job.id, status="error", error=f"could not dispatch: {exc}")
            raise HTTPException(503, "worker queue is unavailable")
        session.refresh(job)
        return job


def _path(name: str):
    if "/" in name or "\\" in name or ".." in name:
        raise HTTPException(404)
    path = settings.backup_dir / name
    if path not in list_backups():
        raise HTTPException(404)
    return path


@router.get("/{name}/verify")
def verify(name: str):
    try:
        return verify_backup(_path(name))
    except Exception as exc:
        raise HTTPException(422, f"backup verification failed: {exc}")


@router.get("/{name}")
def download(name: str):
    path = _path(name)
    return FileResponse(path, filename=path.name, media_type="application/octet-stream")
