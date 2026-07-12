"""Background maintenance for chunk and semantic search indexes."""
from __future__ import annotations

import logging

from sqlmodel import select

from .. import library
from ..db import get_session
from ..models import Artifact
from ..search import index_artifact
from ..settings_store import get_setting
from .celery_app import celery
from .common import set_job, transition_job

log = logging.getLogger(__name__)


@celery.task(name="index_artifact_chunks", autoretry_for=(OSError,),
             retry_backoff=True, retry_jitter=True, max_retries=3)
def index_artifact_chunks(artifact_id: int):
    with get_session() as session:
        if not session.get(Artifact, artifact_id):
            return 0
        return index_artifact(session, artifact_id)


@celery.task(name="rebuild_search")
def rebuild_search(job_id: int):
    with get_session() as session:
        if not transition_job(session, job_id, {"queued"}, "running"):
            return
        artifact_ids = session.exec(select(Artifact.id).order_by(Artifact.id)).all()
    semantic = bool(get_setting("search.semantic_enabled", False))
    indexed = 0
    try:
        for position, artifact_id in enumerate(artifact_ids, 1):
            with get_session() as session:
                artifact = session.get(Artifact, artifact_id)
                if not artifact:
                    continue
                try:
                    _meta, body = library.read_doc(artifact.path)
                except FileNotFoundError:
                    continue
                library.sync_fts(session, artifact, body)
                library.sync_search_chunks(session, artifact, body)
                session.commit()
                if semantic:
                    indexed += index_artifact(session, artifact.id)
                set_job(session, job_id,
                        progress=f"indexed {position}/{len(artifact_ids)} artifacts")
        with get_session() as session:
            transition_job(session, job_id, {"running"}, "done",
                           progress=f"complete; {indexed} semantic chunks")
    except Exception as exc:
        log.exception("search rebuild failed")
        with get_session() as session:
            transition_job(session, job_id, {"running"}, "error", error=str(exc)[:2000])
        raise
