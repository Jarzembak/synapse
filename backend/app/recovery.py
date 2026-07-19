"""Vault health checks and reconstruction of the SQLite search/index layer."""
from __future__ import annotations

import json
import logging
import re
import shutil
from collections import Counter
from pathlib import Path

from sqlmodel import Session, select, text

from . import library
from .config import settings
from .db import get_session
from .models import (
    Artifact, ArtifactTag, Project, QuickRef, QuickRefSource, RepositoryChunk,
    RepositoryFile, RepositorySnapshot, RepositorySource, Tag,
)
from .settings_store import get_setting

log = logging.getLogger(__name__)


def recover_interrupted_deletions() -> dict:
    """Restore staged folders when a delete was interrupted before DB commit."""
    restored = removed = failed = 0
    with get_session() as session:
        deleting = session.exec(select(Project).where(Project.deleting == True)).all()  # noqa: E712
        for project in deleting:
            repository_source = session.exec(
                select(RepositorySource).where(
                    RepositorySource.project_id == project.id)
            ).first()
            for root in (
                settings.library_dir / "projects",
                settings.library_dir / ".history" / "projects",
                settings.media_dir,
                settings.repository_dir,
            ):
                storage_key = (
                    str(repository_source.id)
                    if root == settings.repository_dir and repository_source
                    else project.slug
                )
                original = root / storage_key
                trash = root / ".trash"
                candidates = sorted(trash.glob(
                    f"{project.slug}.delete-{project.id}*")) if trash.exists() else []
                if candidates and not original.exists():
                    candidates[0].replace(original)
                    restored += 1
            project.deleting = False
            session.add(project)
        session.commit()

    # A crash after the DB commit can leave only the staged directory. It no
    # longer belongs to a project, so completing its removal is safe.
    for root in (
        settings.library_dir / "projects",
        settings.library_dir / ".history" / "projects",
        settings.media_dir,
        settings.repository_dir,
    ):
        trash = root / ".trash"
        if not trash.exists():
            continue
        for staged in trash.glob("*.delete-*"):
            try:
                if staged.is_dir():
                    shutil.rmtree(staged)
                else:
                    staged.unlink(missing_ok=True)
                removed += 1
            except OSError:
                failed += 1
                log.exception("secure deletion cleanup remains pending for %s", staged)
    return {"restored": restored, "removed": removed, "failed": failed}


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
    projects = session.exec(select(Project)).all()
    project_ids = {project.id for project in projects}
    github_project_ids = {project.id for project in projects if project.source_type == "github"}
    fts_count = session.exec(text("SELECT COUNT(*) FROM artifact_fts")).one()[0]
    chunk_count = session.exec(text("SELECT COUNT(*) FROM searchchunk")).one()[0]
    repository_chunk_count = len(session.exec(select(RepositoryChunk.id)).all())
    repository_fts_count = session.exec(
        text("SELECT COUNT(*) FROM repository_chunk_fts")
    ).one()[0]
    repository_sources = session.exec(select(RepositorySource)).all()
    repository_snapshots = session.exec(select(RepositorySnapshot)).all()
    repository_files = session.exec(select(RepositoryFile)).all()
    repository_source_ids = {source.id for source in repository_sources}
    repository_snapshot_ids = {snapshot.id for snapshot in repository_snapshots}
    repository_snapshots_by_id = {snapshot.id: snapshot for snapshot in repository_snapshots}
    repository_file_ids = {file.id for file in repository_files}
    source_project_ids = {source.project_id for source in repository_sources}

    def missing_snapshot_directory(snapshot: RepositorySnapshot) -> bool:
        if snapshot.status != "ready" or not snapshot.relative_path:
            return False
        root = settings.repository_dir.resolve()
        candidate = (root / snapshot.relative_path).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            return True
        return not candidate.is_dir()

    repository_orphans = (
        [f"project-source:{project_id}" for project_id in github_project_ids
         if project_id not in source_project_ids]
        + [f"source:{source.id}" for source in repository_sources
         if source.project_id not in project_ids]
        + [f"source-current:{source.id}" for source in repository_sources
           if source.current_snapshot_id
           and source.current_snapshot_id not in repository_snapshot_ids]
        + [f"source-current-owner:{source.id}" for source in repository_sources
           if source.current_snapshot_id
           and source.current_snapshot_id in repository_snapshots_by_id
           and repository_snapshots_by_id[source.current_snapshot_id].source_id != source.id]
        + [f"snapshot:{snapshot.id}" for snapshot in repository_snapshots
           if snapshot.source_id not in repository_source_ids]
        + [f"file:{file.id}" for file in repository_files
           if file.snapshot_id not in repository_snapshot_ids]
        + [f"chunk:{chunk.id}" for chunk in session.exec(select(RepositoryChunk)).all()
           if chunk.file_id not in repository_file_ids]
        + [f"snapshot-directory:{snapshot.id}" for snapshot in repository_snapshots
           if missing_snapshot_directory(snapshot)]
    )
    version = session.exec(text(
        "SELECT COALESCE(MAX(version), 0) FROM schema_version"
    )).one()[0]
    return {
        "healthy": not (
            set(paths) - files or files - set(paths) or duplicates
            or fts_count != len(artifacts)
            or repository_fts_count != repository_chunk_count
            or repository_orphans
            or any(artifact.project_id and artifact.project_id not in project_ids
                   for artifact in artifacts)
        ),
        "schema_version": version,
        "files": len(files),
        "artifacts": len(artifacts),
        "fts_rows": fts_count,
        "fts_consistent": fts_count == len(artifacts),
        "search_chunks": chunk_count,
        "repository_chunks": repository_chunk_count,
        "repository_fts_rows": repository_fts_count,
        "repository_fts_consistent": repository_fts_count == repository_chunk_count,
        "repository_orphans": sorted(repository_orphans),
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
            project.source_type = str(meta.get("source_type") or (
                "url" if project.source.startswith(("http://", "https://"))
                else "local"
            ))
        if meta.get("project_title"):
            project.title = str(meta["project_title"])
        session.add(project)
        return project
    source = str(meta.get("source_url") or meta.get("source") or slug)
    source_type = str(meta.get("source_type") or (
        "url" if source.startswith(("http://", "https://")) else "local"
    ))
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


def _repository_source_for_doc(session: Session, project: Project | None,
                               meta: dict) -> RepositorySource | None:
    if not project or project.source_type != "github":
        return None
    existing = session.exec(select(RepositorySource).where(
        RepositorySource.project_id == project.id)).first()
    if existing:
        return existing
    try:
        from .repository import GITHUB_CREDENTIAL_KEY, parse_github_url

        parsed = parse_github_url(str(meta.get("source_url") or project.source))
    except Exception:
        return None
    provenance = meta.get("provenance") if isinstance(meta.get("provenance"), dict) else {}
    signature = (((provenance.get("config") or {}).get("repository") or {})
                 .get("source") or {})
    include_paths = signature.get("include_paths") or "[]"
    exclude_paths = signature.get("exclude_paths") or "[]"
    if not isinstance(include_paths, str):
        include_paths = json.dumps(include_paths)
    if not isinstance(exclude_paths, str):
        exclude_paths = json.dumps(exclude_paths)
    # Vault-only recovery cannot re-query mutable GitHub visibility without
    # credentials/network. Fail closed: every recovered GitHub source stays
    # local-only until it is explicitly re-imported and inspected again.
    restricted = True
    commit_sha = str(meta.get("commit_sha") or signature.get("resolved_sha") or "")
    source = RepositorySource(
        project_id=project.id,
        owner=parsed.owner,
        repository=parsed.repository,
        canonical_url=parsed.canonical_url,
        requested_ref=str(meta.get("requested_ref") or signature.get("requested_ref") or commit_sha),
        default_branch="",
        is_private=restricted,
        local_only=restricted,
        credential_ref=GITHUB_CREDENTIAL_KEY if restricted else "",
        include_paths=include_paths,
        exclude_paths=exclude_paths,
        pending_sha=commit_sha,
        cloud_purge_pending=bool(
            get_setting("cloud.provider") or get_setting("cloud.last_sync")),
    )
    session.add(source)
    session.flush()
    return source


def rebuild_repository_fts(session: Session, *, on_progress=None) -> int:
    """Rebuild the disposable repository-code FTS mirror from durable rows."""
    session.exec(text("DELETE FROM repository_chunk_fts"))
    files = {file.id: file for file in session.exec(select(RepositoryFile)).all()}
    snapshots = {
        snapshot.id: snapshot
        for snapshot in session.exec(select(RepositorySnapshot)).all()
    }
    sources = {
        source.id: source for source in session.exec(select(RepositorySource)).all()
    }
    chunks = session.exec(select(RepositoryChunk).order_by(RepositoryChunk.id)).all()
    indexed = 0
    for position, chunk in enumerate(chunks, 1):
        file = files.get(chunk.file_id)
        snapshot = snapshots.get(file.snapshot_id) if file else None
        source = sources.get(snapshot.source_id) if snapshot else None
        if not file or not snapshot or not source:
            continue
        session.exec(text(
            "INSERT INTO repository_chunk_fts"
            "(body, chunk_id, file_id, snapshot_id, project_id) "
            "VALUES (:body, :chunk_id, :file_id, :snapshot_id, :project_id)"
        ).bindparams(
            body=chunk.body, chunk_id=chunk.id, file_id=file.id,
            snapshot_id=snapshot.id, project_id=source.project_id,
        ))
        indexed += 1
        if on_progress and position % 250 == 0:
            on_progress(f"reindexed repository evidence {position}/{len(chunks)}")
    return indexed


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
        _repository_source_for_doc(session, project, meta)
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
        artifact.repository_derived = bool(
            meta.get("repository_derived")
            or (project is not None and project.source_type == "github")
            or getattr(artifact, "repository_derived", False))
        if hasattr(artifact, "restricted"):
            artifact.restricted = bool(
                meta.get("restricted")
                or (project is not None and project.source_type == "github")
                or getattr(artifact, "restricted", False))
        artifact.input_hash = str(meta.get("input_hash") or artifact.input_hash or "")
        artifact.config_hash = str(meta.get("config_hash") or artifact.config_hash or "")
        provenance = meta.get("provenance") or {}
        artifact.provenance = json.dumps(provenance, sort_keys=True)
        session.add(artifact)
        session.flush()
        if artifact.restricted:
            body = library.sanitize_restricted_markdown(body)
            meta["restricted"] = True
            if artifact.repository_derived:
                meta["repository_derived"] = True
            library._write_doc(rel, meta, body)
        library.sync_fts(session, artifact, body)
        library.sync_search_chunks(session, artifact, body)

        project_local_quickref = bool(meta.get("project_local")) or rel.startswith(
            "projects/")
        if artifact_type.startswith("quickref_") and not project_local_quickref:
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
                tag = Tag(name=name, restricted=bool(artifact.restricted))
                session.add(tag)
                session.flush()
            elif artifact.restricted:
                tag.restricted = True
                session.add(tag)
            session.add(ArtifactTag(artifact_id=artifact.id, tag_id=tag.id))
        seen.add((rel, artifact_type))
        repaired += 1
        if on_progress:
            on_progress(f"reconciled {position}/{len(files)} files")

    # Resolve contributor wikilinks only after every project has been rebuilt;
    # alphabetic vault ordering can place concepts/tools before projects.
    for ref in session.exec(select(QuickRef)).all():
        try:
            _meta, ref_body = library.read_doc(ref.path)
        except Exception:
            continue
        for slug in set(re.findall(
                r"\[\[projects/([^/]+)/deepdive_merged\]\]", ref_body)):
            source_project = session.exec(select(Project).where(
                Project.slug == slug
            )).first()
            if source_project and not session.get(
                    QuickRefSource, (ref.id, source_project.id)):
                session.add(QuickRefSource(
                    quickref_id=ref.id, project_id=source_project.id))
    session.flush()

    # Global quick references carry contributor lineage separately from their
    # last-writer Artifact.project_id. If any recovered contributor is a
    # fail-closed GitHub source, make and sanitize the merged canonical doc.
    restricted_project_ids = session.exec(select(RepositorySource.project_id).where(
        (RepositorySource.is_private == True)  # noqa: E712
        | (RepositorySource.local_only == True)  # noqa: E712
    )).all()
    if restricted_project_ids:
        restricted_ref_ids = session.exec(select(QuickRefSource.quickref_id).where(
            QuickRefSource.project_id.in_(restricted_project_ids)
        )).all()
        refs = session.exec(select(QuickRef).where(
            QuickRef.id.in_(restricted_ref_ids)
        )).all() if restricted_ref_ids else []
        for ref in refs:
            artifact = session.exec(select(Artifact).where(
                Artifact.path == ref.path
            )).first()
            if not artifact:
                continue
            artifact.restricted = True
            artifact.repository_derived = True
            session.add(artifact)
            meta, body = library.read_doc(artifact.path)
            body = library.sanitize_restricted_markdown(body)
            meta["restricted"] = True
            meta["repository_derived"] = True
            library._write_doc(artifact.path, meta, body)
            library.sync_fts(session, artifact, body)
            library.sync_search_chunks(session, artifact, body)
            tag_ids = session.exec(select(ArtifactTag.tag_id).where(
                ArtifactTag.artifact_id == artifact.id
            )).all()
            for tag in session.exec(select(Tag).where(
                    Tag.id.in_(tag_ids))).all() if tag_ids else []:
                tag.restricted = True
                session.add(tag)
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
    rebuild_repository_fts(session, on_progress=on_progress)
    session.commit()
    try:
        from .tasks.cloud import enqueue_pending_privacy_purges

        enqueue_pending_privacy_purges()
    except Exception:
        log.exception("could not queue recovered cloud privacy purge")
    return {"reconciled": repaired, "pruned": prune_missing}
