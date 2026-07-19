"""Chunked FTS + optional Ollama embeddings for grounded retrieval."""
from __future__ import annotations

import math
import re
from array import array
from urllib.parse import quote

import httpx
from sqlmodel import Session, select, text

from . import library
from .config import settings
from .models import (
    Artifact, ChunkEmbedding, Project, RepositoryChunk, RepositoryFile,
    RepositorySnapshot, RepositorySource, SearchChunk,
)
from .settings_store import get_setting

DEFAULT_EMBED_MODEL = "nomic-embed-text"


def embedding_model() -> str:
    return get_setting("search.embedding_model", DEFAULT_EMBED_MODEL)


def embedding_provider() -> str:
    return get_setting("search.embedding_provider", "ollama")


def embed_texts(values: list[str], model: str | None = None, *,
                local_only: bool = False) -> list[list[float]]:
    if not values:
        return []
    selected_model = model or embedding_model()
    if local_only:
        from .llm import (require_local_ollama_endpoint,
                          validate_local_ollama_model)

        validate_local_ollama_model(selected_model)
        require_local_ollama_endpoint()
    if not local_only and embedding_provider() == "openai_compat":
        vectors = _embed_openai_compat(values, selected_model)
    else:
        # Restricted embeddings must not inherit an outbound proxy. Keep public
        # remote-Ollama compatibility while explicitly bypassing proxy env vars
        # for the local-only path.
        with httpx.Client(
                trust_env=not local_only, follow_redirects=False) as client:
            response = client.post(
                f"{settings.ollama_base_url}/api/embed",
                json={"model": selected_model, "input": values},
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
    artifact = session.get(Artifact, artifact_id)
    if not artifact:
        return 0
    project = session.get(Project, artifact.project_id) if artifact.project_id else None
    restricted = bool(
        library.artifact_is_restricted(session, artifact)
        or library.artifact_is_repository_derived(session, artifact)
        or (project and project.source_type == "github"))
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
        vectors = embed_texts(
            [chunk.body for chunk in batch], model, local_only=restricted)
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
                    *, model: str | None = None,
                    local_only: bool = False) -> list[int]:
    if not get_setting("search.semantic_enabled", False):
        return []
    model = model or embedding_model()
    query_vector = embed_texts([query], model, local_only=local_only)[0]
    rows = session.exec(
        select(ChunkEmbedding).where(ChunkEmbedding.model == model)
    ).all()
    scored = [(_cosine(query_vector, _unpack(row.vector)), row.chunk_id) for row in rows]
    scored.sort(reverse=True)
    return [chunk_id for score, chunk_id in scored[:limit] if score > 0]


def repository_fts_chunks(session: Session, query: str, project_id: int,
                          snapshot_id: int, limit: int = 100) -> list[int]:
    """Current-snapshot repository evidence matching *query* by exact text."""
    terms = [term for term in re.split(r"\s+", query.strip()) if term]
    if not terms:
        return []
    match = " ".join('"' + term.replace('"', '""') + '"' for term in terms)
    rows = session.exec(text(
        "SELECT chunk_id FROM repository_chunk_fts "
        "WHERE repository_chunk_fts MATCH :q AND project_id=:project_id "
        "AND snapshot_id=:snapshot_id "
        "ORDER BY rank LIMIT :limit"
    ).bindparams(
        q=match, project_id=project_id, snapshot_id=snapshot_id,
        limit=max(1, min(limit, 1000)),
    )).all()
    return [row[0] for row in rows]


def repository_results(session: Session, query: str, project_id: int,
                       limit: int = 12) -> list[dict]:
    """Render line-addressed results from a repository's active snapshot."""
    source = session.exec(
        select(RepositorySource).where(RepositorySource.project_id == project_id)
    ).first()
    if not source or not source.current_snapshot_id:
        return []
    snapshot = session.get(RepositorySnapshot, source.current_snapshot_id)
    project = session.get(Project, project_id)
    if not snapshot or not project:
        return []
    ranked = repository_fts_chunks(
        session, query, project_id, snapshot.id, max(limit * 8, 100))
    if not ranked:
        return []
    rank = {chunk_id: position for position, chunk_id in enumerate(ranked)}
    chunks = session.exec(
        select(RepositoryChunk).where(RepositoryChunk.id.in_(ranked))
    ).all()
    files = {
        file.id: file for file in session.exec(
            select(RepositoryFile).where(
                RepositoryFile.id.in_({chunk.file_id for chunk in chunks}),
                RepositoryFile.snapshot_id == snapshot.id,
            )
        ).all()
    }
    out: list[dict] = []
    for chunk in chunks:
        file = files.get(chunk.file_id)
        if not file:
            continue
        path = quote(file.path, safe="/")
        permalink = (
            f"{source.canonical_url}/blob/{snapshot.resolved_sha}/{path}"
            f"#L{chunk.start_line}-L{chunk.end_line}"
        )
        position = rank.get(chunk.id, len(rank))
        out.append({
            "chunk_id": f"repo:{chunk.id}",
            "artifact_id": None,
            "artifact_title": file.path,
            "artifact_type": "repository_source",
            "source_kind": "repository",
            "project_id": project_id,
            "project_title": project.title,
            "project_slug": project.slug,
            "media_artifact_id": None,
            "start_time": None,
            "path": file.path,
            "start_line": chunk.start_line,
            "end_line": chunk.end_line,
            "commit_sha": snapshot.resolved_sha,
            "source_url": permalink,
            "excerpt": chunk.body,
            "tags": [],
            "restricted": bool(source.local_only or source.is_private),
            "score": 1.4 / (60 + position),
        })
    out.sort(key=lambda item: item["score"], reverse=True)
    return out[:max(1, min(limit, 50))]


def hybrid_chunks(session: Session, query: str, limit: int = 12,
                  *, artifact_types: set[str] | None = None,
                  project_id: int | None = None,
                  tags: set[str] | None = None,
                  force_local_semantic: bool = False) -> list[dict]:
    exact = fts_chunks(session, query, max(limit * 8, 100))
    # A semantic query can itself contain private names/code. Keep it local for
    # every repository scope and for a global corpus that contains any GitHub
    # or sticky-restricted material, before retrieval reveals its candidates.
    scope_has_repository = bool(session.exec(select(Project.id).where(
        Project.source_type == "github",
        *( [Project.id == project_id] if project_id is not None else [] ),
    )).first())
    restricted_query = select(Artifact.id).where(
        (Artifact.restricted == True)  # noqa: E712
        | (Artifact.repository_derived == True)  # noqa: E712
    )
    if project_id is not None:
        restricted_query = restricted_query.where(Artifact.project_id == project_id)
    scope_has_restricted = bool(session.exec(restricted_query).first())
    try:
        semantic = semantic_chunks(
            session, query, max(limit * 8, 100),
            local_only=bool(
                force_local_semantic or scope_has_repository
                or scope_has_restricted
                or (project_id is not None
                    and library.project_is_restricted(session, project_id))),
        )
    except Exception:
        semantic = []  # FTS remains available when Ollama/model is offline.
    scores: dict[int, float] = {}
    for rank, chunk_id in enumerate(exact):
        scores[chunk_id] = scores.get(chunk_id, 0) + 1.25 / (60 + rank)
    for rank, chunk_id in enumerate(semantic):
        scores[chunk_id] = scores.get(chunk_id, 0) + 1.0 / (60 + rank)
    repo_out: list[dict] = []
    include_repo_source = not artifact_types or "repository_source" in artifact_types
    if project_id is not None and include_repo_source and not tags:
        repo_out = repository_results(session, query, project_id, limit)
    if not scores:
        return repo_out

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
            "source_kind": "artifact",
            "restricted": bool(
                library.artifact_is_restricted(session, artifact)
                or library.artifact_is_repository_derived(session, artifact)),
            "score": scores[chunk.id],
        })
    out.extend(repo_out)
    out.sort(key=lambda item: item["score"], reverse=True)
    return out[:max(1, min(limit, 50))]
