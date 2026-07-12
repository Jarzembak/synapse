"""Markdown library I/O.

Disk layout (LIBRARY_DIR is Obsidian-openable):
    projects/<project-slug>/<artifact>.md      one file per artifact
    projects/<project-slug>/<artifact>.mp3     binary payloads, sidecar .md holds metadata
    tools/<slug>.md                            cross-project quick-references
    techniques/<slug>.md                       (one folder per category: concepts/,
                                               technologies/, custom category dirs)
    .history/<library-relative-path>.<ts>.md   snapshots taken before quick-ref merges

Every write goes through write_artifact(), which persists frontmatter+body and
mirrors title/body into the SQLite FTS index. Disk is the source of truth for
content; the DB is the index.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import frontmatter
from slugify import slugify
from sqlmodel import Session, select, text

from .config import settings
from .models import Artifact, ChunkEmbedding, Job, Project, SearchChunk, Tag, ArtifactTag, utcnow

log = logging.getLogger(__name__)

ARTIFACT_TYPES = [
    "transcript", "corrected", "summary", "deepdive_claude", "deepdive_gemini",
    "deepdive_merged", "podcast_script", "podcast_audio", "trimmed_audio",
    "mindmap", "quickref_tool", "quickref_technique", "quickref_concept",
    "quickref_technology",  # plus quickref_<key> per custom category
    "source_video", "source_audio",
]


def lib_path(rel: str) -> Path:
    return settings.library_dir / rel


def resolve_media_path(media_path: str) -> Path:
    """Locate an artifact's binary payload.

    Values prefixed 'media:' live in MEDIA_DIR (large archived source files);
    unprefixed values are library-relative (podcast/trimmed audio).
    """
    if media_path.startswith("media:"):
        return settings.media_dir / media_path.removeprefix("media:")
    return lib_path(media_path)


def make_slug(name: str) -> str:
    return slugify(name)[:80] or "untitled"


def wikilink(rel_path: str) -> str:
    """Obsidian wikilink for a library-relative markdown path."""
    return f"[[{rel_path.removesuffix('.md')}]]"


def snapshot_history(rel: str) -> str | None:
    """Copy the current version of a file into .history/ before overwriting."""
    src = lib_path(rel)
    if not src.exists():
        return None
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    dest_rel = f".history/{rel}.{ts}.md"
    dest = lib_path(dest_rel)
    _atomic_write_bytes(dest, src.read_bytes())
    return dest_rel


def read_doc(rel: str) -> tuple[dict, str]:
    """Return (frontmatter dict, body) for a library-relative markdown path."""
    post = frontmatter.load(lib_path(rel))
    return dict(post.metadata), post.content


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """Replace *path* with *data* without exposing a partial file.

    The temporary file lives beside the destination, so ``os.replace`` stays
    on one filesystem and is atomic on every platform we support.  A failed
    write/replace leaves the prior destination intact and cleans up its temp.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        prior = path.stat()
    except FileNotFoundError:
        prior = None
    tmp: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            tmp = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        # NamedTemporaryFile defaults to 0600.  Keep an existing vault file's
        # permissions/ownership, and make new Markdown readable like the old
        # Path.write_text behavior (important for host-side Obsidian on Linux).
        tmp.chmod(stat.S_IMODE(prior.st_mode) if prior else 0o644)
        if prior is not None and hasattr(os, "chown"):
            try:
                os.chown(tmp, prior.st_uid, prior.st_gid)
            except PermissionError:
                pass
        os.replace(tmp, path)
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


def _write_doc(rel: str, meta: dict, body: str) -> None:
    path = lib_path(rel)
    post = frontmatter.Post(body, **meta)
    _atomic_write_bytes(path, frontmatter.dumps(post).encode("utf-8"))


def sync_fts(session: Session, artifact: Artifact, body: str) -> None:
    session.exec(text("DELETE FROM artifact_fts WHERE artifact_id = :id").bindparams(id=artifact.id))
    session.exec(
        text(
            "INSERT INTO artifact_fts(title, body, artifact_id, type, project_id) "
            "VALUES (:title, :body, :id, :type, :pid)"
        ).bindparams(
            title=artifact.title, body=body, id=artifact.id,
            type=artifact.type, pid=artifact.project_id or 0,
        )
    )


def _chunk_body(body: str, max_chars: int = 1800, overlap: int = 200) -> list[tuple[str, str]]:
    """Line-aware excerpts with a timestamp anchor when one is available."""
    if not body.strip():
        return []
    lines = body.splitlines(keepends=True)
    chunks: list[tuple[str, str]] = []
    current: list[str] = []
    size = 0
    for line in lines:
        if current and size + len(line) > max_chars:
            text_body = "".join(current).strip()
            match = re.search(r"\[(\d{2}:\d{2}:\d{2})\]", text_body)
            chunks.append((text_body, match.group(1) if match else ""))
            tail = text_body[-overlap:]
            current = [tail] if tail else []
            size = len(tail)
        current.append(line)
        size += len(line)
    if current:
        text_body = "".join(current).strip()
        match = re.search(r"\[(\d{2}:\d{2}:\d{2})\]", text_body)
        chunks.append((text_body, match.group(1) if match else ""))
    return chunks


def sync_search_chunks(session: Session, artifact: Artifact, body: str) -> None:
    """Replace retrieval chunks and their FTS rows in the artifact transaction."""
    existing = session.exec(
        select(SearchChunk).where(SearchChunk.artifact_id == artifact.id)
    ).all()
    for chunk in existing:
        session.exec(text("DELETE FROM chunk_fts WHERE chunk_id=:id").bindparams(id=chunk.id))
        for embedding in session.exec(
            select(ChunkEmbedding).where(ChunkEmbedding.chunk_id == chunk.id)
        ).all():
            session.delete(embedding)
        session.delete(chunk)
    session.flush()
    for index, (chunk_body, start_time) in enumerate(_chunk_body(body)):
        body_hash = hashlib.sha256(chunk_body.encode("utf-8")).hexdigest()
        chunk = SearchChunk(
            artifact_id=artifact.id, chunk_index=index, body=chunk_body,
            start_time=start_time, body_hash=body_hash,
        )
        session.add(chunk)
        session.flush()
        session.exec(text(
            "INSERT INTO chunk_fts(body, chunk_id, artifact_id) VALUES (:body, :cid, :aid)"
        ).bindparams(body=chunk_body, cid=chunk.id, aid=artifact.id))


def delete_search_chunks(session: Session, artifact_id: int) -> None:
    chunks = session.exec(
        select(SearchChunk).where(SearchChunk.artifact_id == artifact_id)
    ).all()
    for chunk in chunks:
        session.exec(text("DELETE FROM chunk_fts WHERE chunk_id=:id").bindparams(id=chunk.id))
        for embedding in session.exec(
            select(ChunkEmbedding).where(ChunkEmbedding.chunk_id == chunk.id)
        ).all():
            session.delete(embedding)
        session.delete(chunk)
    # Artifact deletion commonly follows immediately.  Without relationships
    # declared on these lightweight SQLModel tables, SQLAlchemy cannot infer
    # FK delete ordering, so force dependent rows out before the parent row.
    session.flush()


def write_artifact(
    session: Session,
    *,
    project_id: int | None,
    project_slug: str | None,
    type: str,
    title: str,
    body: str,
    rel_path: str | None = None,
    media_rel: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    extra_meta: dict | None = None,
    tags: list[str] | None = None,
) -> Artifact:
    """Create or update an artifact: markdown on disk + DB row + FTS index."""
    from .context import current_job_id

    publishing_job_id = current_job_id.get()
    if publishing_job_id:
        # Acquire SQLite's writer lock with a no-op compare-and-set. This
        # serializes artifact publication against the cancellation endpoint:
        # whichever transaction wins is observed by the other, so a provider
        # response that arrived after cancellation cannot slip a file through
        # the check/commit window.
        guard = session.exec(text(
            "UPDATE job SET heartbeat=heartbeat WHERE id=:id AND status='running'"
        ).bindparams(id=publishing_job_id))
        if getattr(guard, "rowcount", 0) != 1:
            session.rollback()
            raise RuntimeError("job was canceled before artifact publication")
        publishing_job = session.get(Job, publishing_job_id)
        if publishing_job.parent_job_id:
            parent = session.get(Job, publishing_job.parent_job_id)
            if not parent or parent.status != "running":
                session.rollback()
                raise RuntimeError("parent run ended before artifact publication")
    if project_id is not None:
        project = session.get(Project, project_id)
        if not project or project.deleting:
            raise ValueError("project no longer exists or is being deleted")
    if rel_path is None:
        rel_path = f"projects/{project_slug}/{type}.md"

    existing = session.exec(
        select(Artifact).where(Artifact.path == rel_path, Artifact.type == type)
    ).first()

    artifact = existing or Artifact(
        project_id=project_id, type=type, title=title, path=rel_path
    )
    artifact.title = title
    artifact.provider = provider or artifact.provider
    artifact.model = model or artifact.model
    artifact.media_path = media_rel or artifact.media_path
    artifact.updated = utcnow()
    session.add(artifact)
    document_path = lib_path(rel_path)
    previous_document = document_path.read_bytes() if document_path.exists() else None
    document_written = False
    try:
        # Allocate a new id without publishing a phantom Artifact row before
        # its Markdown has been written successfully.
        session.flush()
        from .provenance import capture_for_artifact

        input_hash, config_hash, provenance = capture_for_artifact(
            session, project_id, type)
        artifact.input_hash = input_hash
        artifact.config_hash = config_hash
        artifact.provenance = json.dumps(provenance, sort_keys=True)
        meta = {
            "id": artifact.id,
            "type": type,
            "title": title,
            "project": project_slug,
            "project_id": project_id,
            "created": artifact.created.isoformat(),
            "updated": artifact.updated.isoformat(),
            "provider": provider,
            "model": model,
            "input_hash": input_hash or None,
            "config_hash": config_hash or None,
            "provenance": provenance or None,
            "tags": tags or current_tags(session, artifact.id),
        }
        if media_rel:
            meta["media"] = media_rel
        meta.update(extra_meta or {})
        meta = {k: v for k, v in meta.items() if v is not None}
        _write_doc(rel_path, meta, body)
        document_written = True
        sync_fts(session, artifact, body)
        sync_search_chunks(session, artifact, body)
        session.commit()
    except Exception:
        session.rollback()
        if document_written:
            try:
                if previous_document is None:
                    document_path.unlink(missing_ok=True)
                else:
                    _atomic_write_bytes(document_path, previous_document)
            except Exception:
                log.exception("could not restore %s after index transaction failed", rel_path)
        raise
    session.refresh(artifact)
    _queue_cloud_sync(artifact)
    _queue_semantic_index(artifact)
    return artifact


def _queue_semantic_index(artifact: Artifact) -> None:
    from .settings_store import get_setting

    if not get_setting("search.semantic_enabled", False):
        return
    try:
        from .tasks.celery_app import celery

        celery.send_task("index_artifact_chunks", args=[artifact.id])
    except Exception:
        log.warning("could not queue semantic indexing for %s", artifact.path,
                    exc_info=True)


def _queue_cloud_sync(artifact: Artifact) -> None:
    """Best-effort: when cloud auto-sync is on, upload this artifact's files."""
    import logging

    from .settings_store import get_setting

    if not get_setting("cloud.auto"):
        return
    try:
        # lazy import — library.py is used by the API, which must not pull the
        # whole celery task tree at module import time
        from .tasks.celery_app import celery

        paths = [artifact.path] + ([artifact.media_path] if artifact.media_path else [])
        celery.send_task("cloud_sync_paths", args=[paths])
    except Exception as e:
        # a broker hiccup must never fail an artifact write — but leave a trace
        logging.getLogger(__name__).warning(
            "could not queue cloud sync for %s: %s", artifact.path, e)


def current_tags(session: Session, artifact_id: int | None) -> list[str]:
    if not artifact_id:
        return []
    rows = session.exec(
        select(Tag.name)
        .join(ArtifactTag, ArtifactTag.tag_id == Tag.id)
        .where(ArtifactTag.artifact_id == artifact_id)
    ).all()
    return sorted(rows)


def apply_tags(session: Session, artifact: Artifact, names: list[str]) -> None:
    """Replace an artifact's tags and rewrite its frontmatter tag list.

    Defensive on two fronts: LLM tag lists may contain names that slugify to
    the same tag ("Docker"/"docker"), and mid-loop autoflushes can persist a
    pending pair before the batch commits — both used to raise IntegrityError
    on the (artifact_id, tag_id) primary key.
    """
    clean: list[str] = []
    seen: set[str] = set()
    for name in names:
        norm = make_slug(name)
        if not norm or norm in seen:
            continue
        seen.add(norm)
        clean.append(norm)

    # Read before mutating the DB so a missing/corrupt source document cannot
    # leave a partially replaced relationship set.
    meta, body = read_doc(artifact.path)
    original = lib_path(artifact.path).read_bytes()
    doc_written = False
    try:
        tags_by_name: dict[str, Tag] = {}
        if clean:
            tags_by_name = {
                tag.name: tag
                for tag in session.exec(select(Tag).where(Tag.name.in_(clean))).all()
            }
        for norm in clean:
            if norm not in tags_by_name:
                tag = Tag(name=norm, kind="topic")
                session.add(tag)
                tags_by_name[norm] = tag

        # One flush assigns ids to every new Tag.  The old relationships and
        # their replacements then commit as one transaction (there are no
        # mid-loop commits that can expose a half-applied tag set).
        session.flush()
        session.exec(
            text("DELETE FROM artifacttag WHERE artifact_id = :id")
            .bindparams(id=artifact.id)
        )
        for norm in clean:
            session.add(ArtifactTag(
                artifact_id=artifact.id,
                tag_id=tags_by_name[norm].id,
            ))
        session.flush()

        meta["tags"] = sorted(clean)
        _write_doc(artifact.path, meta, body)
        doc_written = True
        session.commit()
        _queue_cloud_sync(artifact)
    except Exception:
        session.rollback()
        if doc_written:
            try:
                _atomic_write_bytes(lib_path(artifact.path), original)
            except Exception:
                log.exception("could not restore %s after tag transaction failed",
                              artifact.path)
        raise


def search_fts(session: Session, query: str, limit: int = 100) -> list[int]:
    """FTS5 match → artifact ids, rank order. Quotes each term for safety."""
    terms = [t for t in re.split(r"\s+", query.strip()) if t]
    if not terms:
        return []
    match = " ".join('"' + t.replace('"', '""') + '"' for t in terms)
    rows = session.exec(
        text(
            "SELECT artifact_id FROM artifact_fts WHERE artifact_fts MATCH :q "
            "ORDER BY rank LIMIT :n"
        ).bindparams(q=match, n=limit)
    ).all()
    return [r[0] for r in rows]


def parse_aliases(raw: str) -> list[str]:
    try:
        return json.loads(raw) if raw else []
    except json.JSONDecodeError:
        return []
