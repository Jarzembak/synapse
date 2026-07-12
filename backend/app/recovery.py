"""Vault health checks and reconstruction of the SQLite search/index layer."""
from __future__ import annotations

import json
import re
import shutil
from collections import Counter
from pathlib import Path

from sqlmodel import Session, select, text

from . import library
from .config import settings
from .db import get_session
from .models import Artifact, ArtifactTag, Project, QuickRef, QuickRefSource, Tag


def recover_interrupted_deletions() -> dict:
    """Restore staged folders when a delete was interrupted before DB commit."""
    restored = removed = 0
    with get_session() as session:
        deleting = session.exec(select(Project).where(Project.deleting == True)).all()  # noqa: E712
        projects = {project.id: project for project in deleting}
        for project in deleting:
            for root in (settings.library_dir / "projects", settings.media_dir):
                staged = root / ".trash" / f"{project.slug}.delete-{project.id}"
                original = root / project.slug
                if staged.exists() and not original.exists():
                    staged.replace(original)
                    restored += 1
            project.deleting = False
            session.add(project)
        session.commit()

    # A crash after the DB commit can leave only the staged directory. It no
    # longer belongs to a project, so completing its removal is safe.
    for root in (settings.library_dir / "projects", settings.media_dir):
        trash = root / ".trash"
        if not trash.exists():
            continue
        for staged in trash.glob("*.delete-*"):
            try:
                project_id = int(staged.name.rsplit(".delete-", 1)[1])
            except (IndexError, ValueError):
                continue
            if project_id in projects:
                continue
            with get_session() as session:
                exists = session.get(Project, project_id)
            if exists is None:
                if staged.is_dir():
                    shutil.rmtree(staged, ignore_errors=True)
                else:
                    staged.unlink(missing_ok=True)
                removed += 1
    return {"restored": restored, "removed": removed}


def vault_files() -> list[Path]:
    if not settings.library_dir.exists():
        return []
    return sorted(
        path for path in settings.library_dir.rglob("*.md")
        if ".history" not in path.parts and ".trash" not in path.parts
    )


def health_report(session: Session) -> dict:
    files = {path.relative_to(settings.library_dir).as_posix() for path in vault_files()}
    artifacts = session.exec(select(Artifact)).all()
    paths = [artifact.path for artifact in artifacts]
    duplicates = [path for path, count in Counter(paths).items() if count > 1]
    project_ids = {project.id for project in session.exec(select(Project)).all()}
    fts_count = session.exec(text("SELECT COUNT(*) FROM artifact_fts")).one()[0]
    chunk_count = session.exec(text("SELECT COUNT(*) FROM searchchunk")).one()[0]
    version = session.exec(text(
        "SELECT COALESCE(MAX(version), 0) FROM schema_version"
    )).one()[0]
    return {
        "healthy": not (
            set(paths) - files or files - set(paths) or duplicates
            or fts_count != len(artifacts)
            or any(artifact.project_id and artifact.project_id not in project_ids
                   for artifact in artifacts)
        ),
        "schema_version": version,
        "files": len(files),
        "artifacts": len(artifacts),
        "fts_rows": fts_count,
        "fts_consistent": fts_count == len(artifacts),
        "search_chunks": chunk_count,
        "missing_files": sorted(set(paths) - files),
        "unindexed_files": sorted(files - set(paths)),
        "duplicate_paths": sorted(duplicates),
        "orphan_artifacts": sorted(
            artifact.id for artifact in artifacts
            if artifact.project_id and artifact.project_id not in project_ids
        ),
    }


def _project_for_doc(session: Session, rel: str, meta: dict) -> Project | None:
    parts = Path(rel).parts
    if len(parts) < 3 or parts[0] != "projects":
        return None
    slug = parts[1]
    project = session.exec(select(Project).where(Project.slug == slug)).first()
    if project:
        # File ordering is not semantic: a corrected transcript without source
        # metadata may be encountered before the raw transcript. Enrich that
        # placeholder when a later document carries the original source.
        explicit_source = meta.get("source_url") or meta.get("source")
        if explicit_source:
            project.source = str(explicit_source)
            project.source_type = (
                "url" if project.source.startswith(("http://", "https://"))
                else "local"
            )
        if meta.get("project_title"):
            project.title = str(meta["project_title"])
        session.add(project)
        return project
    source = str(meta.get("source_url") or meta.get("source") or slug)
    source_type = "url" if source.startswith(("http://", "https://")) else "local"
    title = str(meta.get("project_title") or meta.get("title") or slug)
    # Artifact titles commonly use "Type — Project"; retain the useful tail.
    if " — " in title:
        title = title.split(" — ", 1)[1]
    project = Project(slug=slug, title=title, source=source, source_type=source_type)
    session.add(project)
    session.flush()
    return project


def _quickref_for_doc(session: Session, rel: str, meta: dict, artifact_type: str) -> QuickRef:
    kind = artifact_type.removeprefix("quickref_")
    slug = Path(rel).stem
    ref = session.exec(
        select(QuickRef).where(QuickRef.kind == kind, QuickRef.slug == slug)
    ).first()
    aliases = meta.get("aliases") if isinstance(meta.get("aliases"), list) else []
    if not ref:
        ref = QuickRef(kind=kind, slug=slug, title=str(meta.get("title") or slug),
                       path=rel, aliases=json.dumps(aliases))
    else:
        ref.title = str(meta.get("title") or ref.title)
        ref.path = rel
        ref.aliases = json.dumps(aliases)
    session.add(ref)
    session.flush()
    return ref


def rebuild_from_vault(session: Session, *, prune_missing: bool = False,
                       on_progress=None) -> dict:
    files = vault_files()
    seen: set[tuple[str, str]] = set()
    repaired = 0
    for position, path in enumerate(files, 1):
        rel = path.relative_to(settings.library_dir).as_posix()
        try:
            meta, body = library.read_doc(rel)
        except Exception:
            continue
        artifact_type = str(meta.get("type") or "").strip()
        if not artifact_type:
            continue
        project = _project_for_doc(session, rel, meta)
        artifact = session.exec(
            select(Artifact).where(Artifact.path == rel, Artifact.type == artifact_type)
        ).first()
        if not artifact:
            artifact = Artifact(
                project_id=project.id if project else None,
                type=artifact_type, title=str(meta.get("title") or path.stem), path=rel,
            )
        artifact.project_id = project.id if project else artifact.project_id
        artifact.title = str(meta.get("title") or artifact.title)
        artifact.media_path = meta.get("media") or artifact.media_path
        artifact.provider = meta.get("provider") or artifact.provider
        artifact.model = meta.get("model") or artifact.model
        artifact.input_hash = str(meta.get("input_hash") or artifact.input_hash or "")
        artifact.config_hash = str(meta.get("config_hash") or artifact.config_hash or "")
        provenance = meta.get("provenance") or {}
        artifact.provenance = json.dumps(provenance, sort_keys=True)
        session.add(artifact)
        session.flush()
        library.sync_fts(session, artifact, body)
        library.sync_search_chunks(session, artifact, body)

        if artifact_type.startswith("quickref_"):
            ref = _quickref_for_doc(session, rel, meta, artifact_type)
            for slug in set(re.findall(r"\[\[projects/([^/]+)/deepdive_merged\]\]", body)):
                source_project = session.exec(
                    select(Project).where(Project.slug == slug)
                ).first()
                if source_project and not session.get(
                    QuickRefSource, (ref.id, source_project.id)
                ):
                    session.add(QuickRefSource(
                        quickref_id=ref.id, project_id=source_project.id))

        desired_tags = [str(value) for value in meta.get("tags", [])]
        session.exec(text(
            "DELETE FROM artifacttag WHERE artifact_id=:id"
        ).bindparams(id=artifact.id))
        for name in sorted({library.make_slug(value) for value in desired_tags if value}):
            tag = session.exec(select(Tag).where(Tag.name == name)).first()
            if not tag:
                tag = Tag(name=name)
                session.add(tag)
                session.flush()
            session.add(ArtifactTag(artifact_id=artifact.id, tag_id=tag.id))
        seen.add((rel, artifact_type))
        repaired += 1
        if on_progress:
            on_progress(f"reconciled {position}/{len(files)} files")
    if prune_missing:
        for artifact in session.exec(select(Artifact)).all():
            if (artifact.path, artifact.type) not in seen:
                library.delete_search_chunks(session, artifact.id)
                session.exec(text(
                    "DELETE FROM artifact_fts WHERE artifact_id=:id"
                ).bindparams(id=artifact.id))
                session.exec(text(
                    "DELETE FROM artifacttag WHERE artifact_id=:id"
                ).bindparams(id=artifact.id))
                session.delete(artifact)
    session.commit()
    return {"reconciled": repaired, "pruned": prune_missing}
