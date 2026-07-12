"""Paginated library retrieval, hybrid snippets, and grounded Q&A."""
from __future__ import annotations

from collections import Counter, defaultdict

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlmodel import select

from .. import library, llm
from ..db import get_session
from ..models import Artifact, ArtifactTag, ChunkEmbedding, Job, Project, SearchChunk, Tag
from ..search import embedding_model, hybrid_chunks
from ..settings_store import get_setting
from ..tasks.celery_app import celery
from ..tasks.common import set_job
from ..tasks.prompts import get_prompt

router = APIRouter(prefix="/api/library", tags=["search"])


def _tags_for(session, artifact_ids: list[int]) -> dict[int, list[str]]:
    out: dict[int, list[str]] = defaultdict(list)
    if not artifact_ids:
        return out
    rows = session.exec(
        select(ArtifactTag.artifact_id, Tag.name)
        .join(Tag, Tag.id == ArtifactTag.tag_id)
        .where(ArtifactTag.artifact_id.in_(artifact_ids))
    ).all()
    for artifact_id, name in rows:
        out[artifact_id].append(name)
    return {key: sorted(values) for key, values in out.items()}


@router.get("/query")
def query_library(
    q: str = "", title: str = "", type: str = "", tag: str = "",
    project_id: int | None = None, project: str = "",
    sort: str = "relevance", order: str = "desc", offset: int = 0, limit: int = 50,
):
    """Server-side pagination without the former silent 500-artifact ceiling."""
    offset = max(0, offset)
    limit = max(1, min(limit, 100))
    with get_session() as session:
        statement = select(Artifact)
        ranked: list[int] = []
        if q.strip():
            ranked = library.search_fts(session, q, limit=10_000)
            if not ranked:
                return {"items": [], "total": 0, "offset": offset, "limit": limit,
                        "facets": {"types": {}, "projects": {}, "tags": {}}}
            statement = statement.where(Artifact.id.in_(ranked))
        types = {value for value in type.split(",") if value}
        if title.strip():
            statement = statement.where(Artifact.title.contains(title.strip()))
        if types:
            statement = statement.where(Artifact.type.in_(types))
        if project_id is not None:
            statement = statement.where(Artifact.project_id == project_id)
        elif project:
            matched_project = session.exec(
                select(Project).where(Project.slug == project)
            ).first()
            if not matched_project:
                return {"items": [], "total": 0, "offset": offset, "limit": limit,
                        "facets": {"types": {}, "projects": {}, "tags": {}}}
            statement = statement.where(Artifact.project_id == matched_project.id)
        requested_tags = {value for value in tag.split(",") if value}
        if requested_tags:
            statement = (statement.join(ArtifactTag, ArtifactTag.artifact_id == Artifact.id)
                         .join(Tag, Tag.id == ArtifactTag.tag_id)
                         .where(Tag.name.in_(requested_tags)).distinct())
        artifacts = list(session.exec(statement).all())
        rank = {artifact_id: index for index, artifact_id in enumerate(ranked)}
        reverse = order != "asc"
        if q.strip() and sort == "relevance":
            artifacts.sort(key=lambda artifact: rank.get(artifact.id, 10**9))
            if order == "asc":
                artifacts.reverse()
        else:
            key = {
                "title": lambda artifact: artifact.title.lower(),
                "type": lambda artifact: artifact.type,
                "created": lambda artifact: artifact.created,
                "updated": lambda artifact: artifact.updated,
            }.get(sort, lambda artifact: artifact.updated)
            artifacts.sort(key=key, reverse=reverse)

        projects = {project.id: project for project in session.exec(select(Project)).all()}
        all_ids = [artifact.id for artifact in artifacts]
        tag_map = _tags_for(session, all_ids)
        type_counts = Counter(artifact.type for artifact in artifacts)
        project_counts = Counter(
            projects[artifact.project_id].slug for artifact in artifacts
            if artifact.project_id in projects
        )
        tag_counts = Counter(
            name for artifact_id in all_ids for name in tag_map.get(artifact_id, [])
        )
        page = artifacts[offset:offset + limit]
        items = [{
            **artifact.model_dump(),
            "project_slug": projects.get(artifact.project_id).slug
            if artifact.project_id in projects else None,
            "tags": tag_map.get(artifact.id, []),
        } for artifact in page]
        return {
            "items": items, "total": len(artifacts), "offset": offset, "limit": limit,
            "facets": {
                "types": dict(type_counts), "projects": dict(project_counts),
                "tags": dict(tag_counts),
            },
        }


@router.get("/hybrid")
def hybrid_search(q: str, type: str = "", tag: str = "",
                  project_id: int | None = None, limit: int = 12):
    if not q.strip():
        return {"results": [], "semantic_enabled": bool(
            get_setting("search.semantic_enabled", False))}
    with get_session() as session:
        results = hybrid_chunks(
            session, q, limit,
            artifact_types={value for value in type.split(",") if value} or None,
            project_id=project_id,
            tags={value for value in tag.split(",") if value} or None,
        )
    return {"results": results,
            "semantic_enabled": bool(get_setting("search.semantic_enabled", False)),
            "embedding_model": embedding_model()}


class AskRequest(BaseModel):
    question: str
    type: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    project_id: int | None = None
    limit: int = 8


@router.post("/ask")
def ask_library(req: AskRequest):
    question = req.question.strip()
    if not question:
        raise HTTPException(400, "question is required")
    with get_session() as session:
        sources = hybrid_chunks(
            session, question, max(1, min(req.limit, 12)),
            artifact_types=set(req.type) or None, project_id=req.project_id,
            tags=set(req.tags) or None,
        )
    if not sources:
        return {
            "answer": "I couldn't find enough library material to answer that question.",
            "sources": [], "grounded": True,
        }
    context = []
    public_sources = []
    for index, source in enumerate(sources, 1):
        marker = f"S{index}"
        where = source.get("project_title") or "Shared library"
        timestamp = f" at {source['start_time']}" if source.get("start_time") else ""
        context.append(
            f"[{marker}] {where} — {source['artifact_title']}{timestamp}\n"
            f"{source['excerpt']}"
        )
        public_sources.append({**source, "marker": marker})
    answer = llm.complete(
        "library_qa", get_prompt("library_qa"),
        f"QUESTION:\n{question}\n\nLIBRARY EXCERPTS:\n\n" + "\n\n---\n\n".join(context),
    ).strip()
    if not answer:
        raise HTTPException(502, "the configured answer model returned no text")
    return {"answer": answer, "sources": public_sources, "grounded": True}


@router.post("/reindex")
def rebuild_index():
    with get_session() as session:
        existing = session.exec(
            select(Job).where(Job.task == "rebuild_search",
                              Job.status.in_(("queued", "running")))
        ).first()
        if existing:
            raise HTTPException(409, "search rebuild is already active")
        job = Job(task="rebuild_search")
        session.add(job)
        session.commit()
        session.refresh(job)
        try:
            result = celery.send_task("rebuild_search", args=[job.id])
            job.celery_id = result.id
            session.add(job)
            session.commit()
        except Exception as exc:
            set_job(session, job.id, status="error", error=f"could not dispatch: {exc}")
            raise HTTPException(503, "worker queue is unavailable")
        session.refresh(job)
        return job


@router.get("/index/status")
def index_status():
    with get_session() as session:
        chunks = len(session.exec(select(SearchChunk.id)).all())
        embeddings = len(session.exec(
            select(ChunkEmbedding.chunk_id).where(ChunkEmbedding.model == embedding_model())
        ).all())
    return {
        "chunks": chunks, "embeddings": embeddings,
        "semantic_enabled": bool(get_setting("search.semantic_enabled", False)),
        "embedding_model": embedding_model(),
    }
