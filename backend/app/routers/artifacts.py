from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlmodel import select

from ..db import get_session
from ..models import Artifact, Project, Tag, ArtifactTag
from .. import library

router = APIRouter(prefix="/api", tags=["artifacts"])

MIME_BY_EXT = {
    ".mp4": "video/mp4", ".webm": "video/webm", ".mkv": "video/x-matroska",
    ".m4a": "audio/mp4", ".mp3": "audio/mpeg", ".wav": "audio/wav",
    ".flac": "audio/flac", ".ogg": "audio/ogg", ".opus": "audio/opus",
}


def media_mime(filename: str) -> str:
    from pathlib import PurePath

    return MIME_BY_EXT.get(PurePath(filename).suffix.lower(), "application/octet-stream")


@router.get("/artifacts/{artifact_id}")
def get_artifact(artifact_id: int):
    with get_session() as session:
        art = session.get(Artifact, artifact_id)
        if not art:
            raise HTTPException(404)
        try:
            meta, body = library.read_doc(art.path)
        except FileNotFoundError:
            raise HTTPException(410, "artifact file missing from library")
        project = session.get(Project, art.project_id) if art.project_id else None
        return {
            "artifact": art,
            "meta": meta,
            "body": body,
            "tags": library.current_tags(session, art.id),
            "project": project,
        }


class TagUpdate(BaseModel):
    tags: list[str]


@router.put("/artifacts/{artifact_id}/tags")
def set_tags(artifact_id: int, req: TagUpdate):
    with get_session() as session:
        art = session.get(Artifact, artifact_id)
        if not art:
            raise HTTPException(404)
        library.apply_tags(session, art, req.tags)
        return {"tags": library.current_tags(session, art.id)}


@router.get("/library/search")
def search_library(
    q: str = "",
    type: str = "",
    tag: str = "",
    project_id: int | None = None,
    sort: str = "updated",   # updated | created | title | type
    order: str = "desc",
    limit: int = 200,
):
    with get_session() as session:
        stmt = select(Artifact)
        if q.strip():
            ids = library.search_fts(session, q, limit=500)
            if not ids:
                return []
            stmt = stmt.where(Artifact.id.in_(ids))
        if type:
            stmt = stmt.where(Artifact.type.in_(type.split(",")))
        if project_id is not None:
            stmt = stmt.where(Artifact.project_id == project_id)
        if tag:
            stmt = (stmt.join(ArtifactTag, ArtifactTag.artifact_id == Artifact.id)
                        .join(Tag, Tag.id == ArtifactTag.tag_id)
                        .where(Tag.name.in_(tag.split(","))))
        col = {"updated": Artifact.updated, "created": Artifact.created,
               "title": Artifact.title, "type": Artifact.type}.get(sort, Artifact.updated)
        stmt = stmt.order_by(col.desc() if order == "desc" else col.asc()).limit(limit)
        arts = session.exec(stmt).all()

        projects = {p.id: p.slug for p in session.exec(select(Project)).all()}
        return [
            {**a.model_dump(), "project_slug": projects.get(a.project_id),
             "tags": library.current_tags(session, a.id)}
            for a in arts
        ]


@router.get("/media/{artifact_id}")
def get_media(artifact_id: int):
    """Serve an artifact's binary payload (mp3) from the library volume."""
    with get_session() as session:
        art = session.get(Artifact, artifact_id)
        if not art or not art.media_path:
            raise HTTPException(404)
        path = library.resolve_media_path(art.media_path)
        if not path.exists():
            raise HTTPException(410)
        return FileResponse(path, media_type=media_mime(path.name), filename=path.name)
