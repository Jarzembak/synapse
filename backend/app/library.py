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
from .models import (Artifact, ArtifactTag, ChunkEmbedding, Job, Project,
                     QuickRef, QuickRefSource, SearchChunk, Tag, utcnow)

log = logging.getLogger(__name__)

ARTIFACT_TYPES = [
    "transcript", "corrected", "summary", "repo_inventory", "repo_usage",
    "repo_architecture", "repo_expertise", "repo_environment",
    "deepdive_claude", "deepdive_gemini",
    "deepdive_merged", "podcast_script", "podcast_audio", "trimmed_audio",
    "mindmap", "quickref_tool", "quickref_technique", "quickref_concept",
    "quickref_technology",  # plus quickref_<key> per custom category
    "source_video", "source_audio", "source_paper", "paper_extraction_report",
    "paper_coverage", "paper_argument_map", "paper_mindmap",
    "paper_quick_references",
    "paper_overview", "paper_methods", "paper_evidence", "paper_prerequisites",
    "paper_critique", "paper_deepdive_explanatory",
    "paper_deepdive_methodology", "paper_study_guide",
    "paper_part_guide", "paper_part_script", "paper_part_audio",
]


def lib_path(rel: str) -> Path:
    return settings.library_dir / rel


_MARKDOWN_IMAGE_RE = re.compile(
    r"!\[(?P<alt>[^\]\r\n]*)\]\s*\([^\)\r\n]*\)", re.IGNORECASE)
_MARKDOWN_IMAGE_REF_RE = re.compile(
    r"!\[(?P<alt>[^\]\r\n]*)\]\s*\[[^\]\r\n]*\]", re.IGNORECASE)
_EVIDENCE_COMMENT_RE = re.compile(
    r"(<!--(?:(?:E|P):[A-Za-z0-9_.:-]+|"
    r"SEGMENT_EVIDENCE:[A-Za-z0-9_.:,-]+)-->)")
_FENCE_OPEN_RE = re.compile(
    r"^(?P<indent> {0,3})(?P<fence>`{3,}|~{3,})[^\r\n]*(?P<ending>\r?\n)?$")


def sanitize_restricted_markdown(body: str) -> str:
    """Remove auto-loading markup from private-derived model output.

    Repository text is adversarial input. Even a local model can be induced to
    emit an image URL containing private data; Markdown viewers such as a web
    browser or Obsidian may fetch it automatically. Restricted documents retain
    ordinary links (which require an explicit click) but never active embeds.
    """
    replacement = lambda match: (  # noqa: E731
        f"[Image omitted for local-only safety: {match.groupdict().get('alt') or 'image'}]"
    )
    clean = _MARKDOWN_IMAGE_RE.sub(replacement, body)
    clean = _MARKDOWN_IMAGE_REF_RE.sub(replacement, clean)
    output: list[str] = []
    active_fence: tuple[str, int] | None = None
    for line in clean.splitlines(keepends=True):
        if active_fence is not None:
            marker, minimum = active_fence
            closing = re.match(
                rf"^ {{0,3}}{re.escape(marker)}{{{minimum},}}[ \t]*(?:\r?\n)?$",
                line,
            )
            output.append(line)
            if closing:
                active_fence = None
            continue
        opening = _FENCE_OPEN_RE.match(line)
        if opening:
            fence = opening.group("fence")
            ending = opening.group("ending") or ""
            # Obsidian plugins can execute info strings such as dataviewjs.
            # All restricted fenced blocks are deliberately inert text.
            output.append(f"{opening.group('indent')}{fence}text{ending}")
            active_fence = (fence[0], len(fence))
            continue
        # Escape angle brackets directly rather than matching whole tags: raw
        # HTML may legally span lines (`<img\nsrc=...>`). Exact evidence
        # comments are the sole active-looking syntax preserved.
        inert = "".join(
            segment if _EVIDENCE_COMMENT_RE.fullmatch(segment)
            else segment.replace("<", "&lt;").replace(">", "&gt;")
            for segment in _EVIDENCE_COMMENT_RE.split(line)
        )
        # This catches escaped-alt CommonMark images, reference images and
        # Obsidian embeds that balanced-regex image matchers cannot parse. The
        # `$=` rewrite also disables Dataview inline JavaScript outside fences.
        output.append(inert.replace("![", "&#33;[").replace("$=", "&#36;="))
    return "".join(output)


def project_is_restricted(session: Session, project_id: int | None) -> bool:
    if project_id is None:
        return False
    project = session.get(Project, project_id)
    if not project:
        return False
    if project.source_type == "paper":
        try:
            from .models import PaperSource

            source = session.exec(select(PaperSource).where(
                PaperSource.project_id == project_id
            )).first()
            # A paper without its policy row is incomplete and must fail closed.
            return source is None or bool(source.local_only)
        except Exception:
            return True
    if project.source_type != "github":
        return False
    try:
        from .repository import repository_source_for_project

        source = repository_source_for_project(session, project_id)
        # Missing policy for a GitHub project fails closed.
        return source is None or bool(source.local_only or source.is_private)
    except Exception:
        return True


def artifact_is_restricted(session: Session, artifact: Artifact) -> bool:
    return bool(getattr(artifact, "restricted", False) or
                project_is_restricted(session, artifact.project_id))


def artifact_is_cloud_excluded(session: Session, artifact: Artifact) -> bool:
    """Whether an artifact must never be copied to configured cloud storage."""
    return bool(getattr(artifact, "cloud_sync_excluded", False)
                or artifact_is_restricted(session, artifact)
                or artifact_is_repository_derived(session, artifact))


def artifact_is_repository_derived(session: Session, artifact: Artifact) -> bool:
    """Whether any GitHub project contributed to this artifact."""
    if bool(getattr(artifact, "repository_derived", False)):
        return True
    project = session.get(Project, artifact.project_id) if artifact.project_id else None
    if project and project.source_type == "github":
        return True
    ref_ids = session.exec(select(QuickRef.id).where(
        QuickRef.path == artifact.path
    )).all()
    if not ref_ids:
        return False
    return bool(session.exec(
        select(Project.id)
        .join(QuickRefSource, QuickRefSource.project_id == Project.id)
        .where(
            QuickRefSource.quickref_id.in_(ref_ids),
            Project.source_type == "github",
        )
    ).first())


def mark_project_restricted(session: Session, project_id: int) -> int:
    """Apply sticky DB privacy before any best-effort vault rewriting."""
    artifacts = session.exec(
        select(Artifact).where(Artifact.project_id == project_id)
    ).all()
    # Global quick references are attributed to their most recent contributor
    # in Artifact.project_id, but may contain material from several projects.
    # If any contributor becomes private, the complete merged document must be
    # sticky-private even when another public project wrote it most recently.
    contributed_paths = session.exec(
        select(QuickRef.path)
        .join(QuickRefSource, QuickRefSource.quickref_id == QuickRef.id)
        .where(QuickRefSource.project_id == project_id)
    ).all()
    if contributed_paths:
        contributed = session.exec(
            select(Artifact).where(Artifact.path.in_(contributed_paths))
        ).all()
        known_ids = {artifact.id for artifact in artifacts}
        artifacts.extend(
            artifact for artifact in contributed if artifact.id not in known_ids)
    marked = 0
    for artifact in artifacts:
        changed = bool(
            not getattr(artifact, "restricted", False)
            or not getattr(artifact, "repository_derived", False))
        artifact.restricted = True
        artifact.repository_derived = True
        session.add(artifact)
        marked += int(changed)
    if artifacts:
        artifact_ids = [artifact.id for artifact in artifacts if artifact.id is not None]
        tag_ids = session.exec(
            select(ArtifactTag.tag_id).where(ArtifactTag.artifact_id.in_(artifact_ids))
        ).all()
        for tag in session.exec(select(Tag).where(Tag.id.in_(tag_ids))).all() if tag_ids else []:
            tag.restricted = True
            session.add(tag)
    session.flush()
    return marked


def sanitize_project_artifacts(project_id: int) -> None:
    """Best-effort vault hardening after the DB privacy transaction commits."""
    from .db import get_session

    with get_session() as session:
        artifact_ids = list(session.exec(
            select(Artifact.id).where(Artifact.project_id == project_id)
        ).all())
        contributed_paths = session.exec(
            select(QuickRef.path)
            .join(QuickRefSource, QuickRefSource.quickref_id == QuickRef.id)
            .where(QuickRefSource.project_id == project_id)
        ).all()
        if contributed_paths:
            artifact_ids.extend(session.exec(
                select(Artifact.id).where(Artifact.path.in_(contributed_paths))
            ).all())
        artifact_ids = list(dict.fromkeys(artifact_ids))
    for artifact_id in artifact_ids:
        try:
            with get_session() as session:
                artifact = session.get(Artifact, artifact_id)
                if not artifact:
                    continue
                path = lib_path(artifact.path)
                try:
                    meta, body = read_doc(artifact.path)
                    sanitized = sanitize_restricted_markdown(body)
                    meta["restricted"] = True
                    meta["repository_derived"] = True
                    _write_doc(artifact.path, meta, sanitized)
                except Exception:
                    # If frontmatter is corrupt, sanitize the complete text so
                    # an external Markdown viewer still cannot auto-load data.
                    raw = path.read_text(encoding="utf-8")
                    sanitized = sanitize_restricted_markdown(raw)
                    _atomic_write_bytes(path, sanitized.encode("utf-8"))
                sync_fts(session, artifact, sanitized)
                sync_search_chunks(session, artifact, sanitized)
                history_base = lib_path(f".history/{artifact.path}")
                history_parent = history_base.parent
                history_prefix = history_base.name + "."
                if history_parent.is_dir():
                    for history_path in history_parent.iterdir():
                        if (not history_path.is_file() or history_path.is_symlink()
                                or not history_path.name.startswith(history_prefix)):
                            continue
                        history_rel = history_path.relative_to(
                            settings.library_dir).as_posix()
                        try:
                            history_meta, history_body = read_doc(history_rel)
                            history_meta["restricted"] = True
                            history_meta["repository_derived"] = True
                            _write_doc(
                                history_rel, history_meta,
                                sanitize_restricted_markdown(history_body))
                        except Exception:
                            history_raw = history_path.read_text(encoding="utf-8")
                            _atomic_write_bytes(
                                history_path,
                                sanitize_restricted_markdown(history_raw).encode("utf-8"),
                            )
                session.commit()
                _queue_semantic_index(artifact)
        except Exception:
            # DB policy is already committed and remains the application/cloud
            # boundary. Library Health reports any unreadable vault file.
            log.exception("could not sanitize restricted artifact id=%s", artifact_id)


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
    paper_series_id: int | None = None,
    paper_part_id: int | None = None,
    cloud_sync_excluded: bool = False,
    input_hash_override: str | None = None,
    config_hash_override: str | None = None,
    provenance_override: dict | None = None,
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
    project_restricted = False
    project_repository_derived = False
    if project_id is not None:
        project = session.get(Project, project_id)
        if not project or project.deleting:
            raise ValueError("project no longer exists or is being deleted")
        if project.source_type == "github":
            project_repository_derived = True
            try:
                from .repository import repository_source_for_project

                source = repository_source_for_project(session, project_id)
                # Missing policy metadata fails closed.  Once restricted, an
                # artifact remains restricted even if a later contributor is
                # public (important for shared quick-reference paths).
                project_restricted = source is None or bool(
                    getattr(source, "local_only", False)
                    or getattr(source, "is_private", getattr(source, "private", False)))
                if not project_restricted:
                    from . import llm

                    project_restricted = llm._repository_local_only()
            except Exception:
                project_restricted = True
        elif project.source_type == "paper":
            project_restricted = project_is_restricted(session, project_id)
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
    if hasattr(artifact, "paper_series_id"):
        artifact.paper_series_id = paper_series_id
    if hasattr(artifact, "paper_part_id"):
        artifact.paper_part_id = paper_part_id
    if hasattr(artifact, "cloud_sync_excluded"):
        artifact.cloud_sync_excluded = bool(
            getattr(artifact, "cloud_sync_excluded", False)
            or cloud_sync_excluded or type == "source_paper")
    artifact.repository_derived = bool(
        getattr(artifact, "repository_derived", False)
        or project_repository_derived)
    if hasattr(artifact, "restricted"):
        artifact.restricted = bool(
            getattr(artifact, "restricted", False) or project_restricted)
    if getattr(artifact, "restricted", False):
        body = sanitize_restricted_markdown(body)
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
        if input_hash_override is not None:
            input_hash = input_hash_override
        if config_hash_override is not None:
            config_hash = config_hash_override
        if provenance_override is not None:
            provenance = provenance_override
        artifact.input_hash = input_hash
        artifact.config_hash = config_hash
        artifact.provenance = json.dumps(provenance, sort_keys=True)
        meta = {
            "id": artifact.id,
            "type": type,
            "title": title,
            "project": project_slug,
            "project_id": project_id,
            "project_title": project.title if project_id is not None else None,
            "source_type": project.source_type if project_id is not None else None,
            "source_url": project.source if project_id is not None else None,
            "created": artifact.created.isoformat(),
            "updated": artifact.updated.isoformat(),
            "provider": provider,
            "model": model,
            "input_hash": input_hash or None,
            "config_hash": config_hash or None,
            "provenance": provenance or None,
            "restricted": getattr(artifact, "restricted", False) or None,
            "repository_derived": (
                getattr(artifact, "repository_derived", False) or None),
            "cloud_sync_excluded": (
                getattr(artifact, "cloud_sync_excluded", False) or None),
            "paper_series_id": getattr(artifact, "paper_series_id", None),
            "paper_part_id": getattr(artifact, "paper_part_id", None),
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

    if (getattr(artifact, "restricted", False)
            or getattr(artifact, "cloud_sync_excluded", False)
            or not get_setting("cloud.auto")):
        return
    try:
        from .db import get_session

        with get_session() as session:
            stored = session.get(Artifact, artifact.id) if artifact.id else None
            if stored is None or artifact_is_cloud_excluded(session, stored):
                return
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
    # Serialize the final DB/file publication with project deletion. If this
    # no-op guarded UPDATE wins, deletion waits and subsequently stages the
    # tagged file; if deletion already fenced the project, no path is touched.
    if artifact.project_id is not None:
        guard = session.exec(text(
            "UPDATE project SET deleting=deleting "
            "WHERE id=:id AND deleting=0"
        ).bindparams(id=artifact.project_id))
        if getattr(guard, "rowcount", 0) != 1:
            session.rollback()
            raise RuntimeError("project was deleted before tag publication")

    artifact_sensitive = bool(
        getattr(artifact, "restricted", False)
        or artifact_is_repository_derived(session, artifact))
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
                tag = Tag(
                    name=norm,
                    kind="topic",
                    restricted=artifact_sensitive,
                )
                session.add(tag)
                tags_by_name[norm] = tag
            elif artifact_sensitive:
                # Privacy provenance is sticky even if the private artifact is
                # later deleted or the same term is used by public projects.
                tags_by_name[norm].restricted = True
                session.add(tags_by_name[norm])

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
