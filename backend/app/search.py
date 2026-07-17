"""Chunked FTS + optional Ollama embeddings for grounded retrieval."""
from __future__ import annotations

import math
import re
from array import array

import httpx
from sqlmodel import Session, select, text

from . import library
from .config import settings
from .models import Artifact, ChunkEmbedding, Project, SearchChunk
from .settings_store import get_setting

DEFAULT_EMBED_MODEL = "nomic-embed-text"


def embedding_model() -> str:
    return get_setting("search.embedding_model", DEFAULT_EMBED_MODEL)


def embedding_provider() -> str:
    return get_setting("search.embedding_provider", "ollama")


def embed_texts(values: list[str], model: str | None = None) -> list[list[float]]:
    if not values:
        return []
    if embedding_provider() == "openai_compat":
        vectors = _embed_openai_compat(values, model or embedding_model())
    else:
        response = httpx.post(
            f"{settings.ollama_base_url}/api/embed",
            json={"model": model or embedding_model(), "input": values},
            timeout=httpx.Timeout(180, connect=5),
        )
        response.raise_for_status()
        vectors = response.json().get("embeddings") or []
    if len(vectors) != len(values) or any(not vector for vector in vectors):
        raise RuntimeError("embedding provider returned an unexpected response")
    return [[float(value) for value in vector] for vector in vectors]


def _embed_openai_compat(values: list[str], model: str) -> list[list[float]]:
    """OpenAI-style /embeddings on the configured local server (LM Studio, …)."""
    base = (settings.openai_compat_base_url or "").rstrip("/")
    if not base:
        raise RuntimeError(
            "embedding provider openai_compat needs OPENAI_COMPAT_BASE_URL in .env")
    headers = {}
    if settings.openai_compat_api_key:
        headers["Authorization"] = f"Bearer {settings.openai_compat_api_key}"
    response = httpx.post(
        f"{base}/embeddings", headers=headers,
        json={"model": model, "input": values},
        timeout=httpx.Timeout(180, connect=5),
    )
    response.raise_for_status()
    rows = response.json().get("data") or []
    rows.sort(key=lambda item: item.get("index", 0))
    return [row.get("embedding") or [] for row in rows]


def _pack(vector: list[float]) -> bytes:
    return array("f", vector).tobytes()


def _unpack(raw: bytes) -> array:
    out = array("f")
    out.frombytes(raw)
    return out


def _cosine(left, right) -> float:
    if len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    lnorm = math.sqrt(sum(a * a for a in left))
    rnorm = math.sqrt(sum(b * b for b in right))
    return dot / (lnorm * rnorm) if lnorm and rnorm else 0.0


def index_artifact(session: Session, artifact_id: int, *, model: str | None = None) -> int:
    model = model or embedding_model()
    chunks = session.exec(
        select(SearchChunk).where(SearchChunk.artifact_id == artifact_id)
        .order_by(SearchChunk.chunk_index)
    ).all()
    pending: list[SearchChunk] = []
    for chunk in chunks:
        existing = session.get(ChunkEmbedding, (chunk.id, model))
        if not existing or existing.body_hash != chunk.body_hash:
            pending.append(chunk)
    if not pending:
        return 0
    indexed = 0
    for start in range(0, len(pending), 32):
        batch = pending[start:start + 32]
        vectors = embed_texts([chunk.body for chunk in batch], model)
        for chunk, vector in zip(batch, vectors):
            row = session.get(ChunkEmbedding, (chunk.id, model)) or ChunkEmbedding(
                chunk_id=chunk.id, model=model, dimensions=len(vector),
                vector=b"", body_hash=chunk.body_hash,
            )
            row.dimensions = len(vector)
            row.vector = _pack(vector)
            row.body_hash = chunk.body_hash
            session.add(row)
            indexed += 1
        session.commit()
    return indexed


def fts_chunks(session: Session, query: str, limit: int = 100) -> list[int]:
    terms = [term for term in re.split(r"\s+", query.strip()) if term]
    if not terms:
        return []
    match = " ".join('"' + term.replace('"', '""') + '"' for term in terms)
    rows = session.exec(text(
        "SELECT chunk_id FROM chunk_fts WHERE chunk_fts MATCH :q "
        "ORDER BY rank LIMIT :limit"
    ).bindparams(q=match, limit=max(1, min(limit, 1000)))).all()
    return [row[0] for row in rows]


def semantic_chunks(session: Session, query: str, limit: int = 100,
                    *, model: str | None = None) -> list[int]:
    if not get_setting("search.semantic_enabled", False):
        return []
    model = model or embedding_model()
    query_vector = embed_texts([query], model)[0]
    rows = session.exec(
        select(ChunkEmbedding).where(ChunkEmbedding.model == model)
    ).all()
    scored = [(_cosine(query_vector, _unpack(row.vector)), row.chunk_id) for row in rows]
    scored.sort(reverse=True)
    return [chunk_id for score, chunk_id in scored[:limit] if score > 0]


def hybrid_chunks(session: Session, query: str, limit: int = 12,
                  *, artifact_types: set[str] | None = None,
                  project_id: int | None = None,
                  tags: set[str] | None = None) -> list[dict]:
    exact = fts_chunks(session, query, max(limit * 8, 100))
    try:
        semantic = semantic_chunks(session, query, max(limit * 8, 100))
    except Exception:
        semantic = []  # FTS remains available when Ollama/model is offline.
    scores: dict[int, float] = {}
    for rank, chunk_id in enumerate(exact):
        scores[chunk_id] = scores.get(chunk_id, 0) + 1.25 / (60 + rank)
    for rank, chunk_id in enumerate(semantic):
        scores[chunk_id] = scores.get(chunk_id, 0) + 1.0 / (60 + rank)
    if not scores:
        return []

    chunks = session.exec(
        select(SearchChunk).where(SearchChunk.id.in_(list(scores)))
    ).all()
    artifacts = {
        artifact.id: artifact for artifact in session.exec(
            select(Artifact).where(Artifact.id.in_({chunk.artifact_id for chunk in chunks}))
        ).all()
    }
    projects = {project.id: project for project in session.exec(select(Project)).all()}
    media_by_project: dict[int, Artifact] = {}
    project_ids = {artifact.project_id for artifact in artifacts.values() if artifact.project_id}
    if project_ids:
        priority = {"source_video": 0, "source_audio": 1, "trimmed_audio": 2}
        candidates = session.exec(
            select(Artifact).where(
                Artifact.project_id.in_(project_ids),
                Artifact.type.in_(tuple(priority)),
            )
        ).all()
        for candidate in sorted(candidates, key=lambda item: priority[item.type]):
            if candidate.media_path and candidate.project_id not in media_by_project:
                media_by_project[candidate.project_id] = candidate
    out: list[dict] = []
    for chunk in chunks:
        artifact = artifacts.get(chunk.artifact_id)
        if not artifact:
            continue
        if artifact_types and artifact.type not in artifact_types:
            continue
        if project_id is not None and artifact.project_id != project_id:
            continue
        artifact_tags = set(library.current_tags(session, artifact.id))
        if tags and not artifact_tags.intersection(tags):
            continue
        project = projects.get(artifact.project_id)
        out.append({
            "chunk_id": chunk.id,
            "artifact_id": artifact.id,
            "artifact_title": artifact.title,
            "artifact_type": artifact.type,
            "project_id": artifact.project_id,
            "project_title": project.title if project else None,
            "project_slug": project.slug if project else None,
            "media_artifact_id": (
                media_by_project[artifact.project_id].id
                if artifact.project_id in media_by_project else None
            ),
            "start_time": chunk.start_time or None,
            "excerpt": chunk.body,
            "tags": sorted(artifact_tags),
            "score": scores[chunk.id],
        })
    out.sort(key=lambda item: item["score"], reverse=True)
    return out[:max(1, min(limit, 50))]
