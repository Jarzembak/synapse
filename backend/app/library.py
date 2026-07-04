"""Markdown library I/O.

Disk layout (LIBRARY_DIR is Obsidian-openable):
    projects/<project-slug>/<artifact>.md      one file per artifact
    projects/<project-slug>/<artifact>.mp3     binary payloads, sidecar .md holds metadata
    tools/<slug>.md                            cross-project quick-references
    techniques/<slug>.md
    .history/<library-relative-path>.<ts>.md   snapshots taken before quick-ref merges

Every write goes through write_artifact(), which persists frontmatter+body and
mirrors title/body into the SQLite FTS index. Disk is the source of truth for
content; the DB is the index.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

import frontmatter
from slugify import slugify
from sqlmodel import Session, select, text

from .config import settings
from .models import Artifact, Tag, ArtifactTag, utcnow

ARTIFACT_TYPES = [
    "transcript", "corrected", "summary", "deepdive_claude", "deepdive_gemini",
    "deepdive_merged", "podcast_script", "podcast_audio", "trimmed_audio",
    "mindmap", "quickref_tool", "quickref_technique", "source_video", "source_audio",
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
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest_rel = f".history/{rel}.{ts}.md"
    dest = lib_path(dest_rel)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(src.read_bytes())
    return dest_rel


def read_doc(rel: str) -> tuple[dict, str]:
    """Return (frontmatter dict, body) for a library-relative markdown path."""
    post = frontmatter.load(lib_path(rel))
    return dict(post.metadata), post.content


def _write_doc(rel: str, meta: dict, body: str) -> None:
    path = lib_path(rel)
    path.parent.mkdir(parents=True, exist_ok=True)
    post = frontmatter.Post(body, **meta)
    path.write_text(frontmatter.dumps(post), encoding="utf-8")


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
    session.commit()
    session.refresh(artifact)

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
        "tags": tags or current_tags(session, artifact.id),
    }
    if media_rel:
        meta["media"] = media_rel
    meta.update(extra_meta or {})
    meta = {k: v for k, v in meta.items() if v is not None}
    _write_doc(rel_path, meta, body)

    sync_fts(session, artifact, body)
    session.commit()
    return artifact


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
    """Replace an artifact's tags and rewrite its frontmatter tag list."""
    session.exec(
        text("DELETE FROM artifacttag WHERE artifact_id = :id").bindparams(id=artifact.id)
    )
    clean = []
    for name in names:
        norm = make_slug(name)
        if not norm:
            continue
        tag = session.exec(select(Tag).where(Tag.name == norm)).first()
        if not tag:
            tag = Tag(name=norm, kind="topic")
            session.add(tag)
            session.commit()
            session.refresh(tag)
        session.add(ArtifactTag(artifact_id=artifact.id, tag_id=tag.id))
        clean.append(norm)
    session.commit()

    meta, body = read_doc(artifact.path)
    meta["tags"] = sorted(set(clean))
    _write_doc(artifact.path, meta, body)


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
