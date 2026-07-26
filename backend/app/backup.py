"""Consistent, verifiable backups of the vault, database, and archived media."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .config import settings
from .settings_store import get_setting

MAGIC = b"SYNAPSE-BACKUP-1\0"


def _encrypt(source: Path, destination: Path, secret: str) -> None:
    key = hashlib.sha256(secret.encode("utf-8")).digest()
    nonce = os.urandom(12)
    encryptor = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
    with source.open("rb") as src, destination.open("wb") as dst:
        dst.write(MAGIC + nonce)
        while chunk := src.read(1024 * 1024):
            dst.write(encryptor.update(chunk))
        dst.write(encryptor.finalize())
        dst.write(encryptor.tag)


def _decrypt(source: Path, destination: Path, secret: str) -> None:
    size = source.stat().st_size
    with source.open("rb") as src:
        if src.read(len(MAGIC)) != MAGIC:
            raise ValueError("not a Synapse encrypted backup")
        nonce = src.read(12)
        src.seek(size - 16)
        tag = src.read(16)
        src.seek(len(MAGIC) + 12)
        remaining = size - len(MAGIC) - 12 - 16
        decryptor = Cipher(
            algorithms.AES(hashlib.sha256(secret.encode("utf-8")).digest()),
            modes.GCM(nonce, tag),
        ).decryptor()
        with destination.open("wb") as dst:
            while remaining:
                chunk = src.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise ValueError("truncated encrypted backup")
                remaining -= len(chunk)
                dst.write(decryptor.update(chunk))
            dst.write(decryptor.finalize())


def _sqlite_snapshot(destination: Path) -> None:
    source = sqlite3.connect(settings.db_path)
    target = sqlite3.connect(destination)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()


def _copy_regular_file(source: Path, destination: Path) -> None:
    if source.is_symlink() or not source.is_file():
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def _snapshot_contains_restricted_data(
    database: Path, *, include_repositories: bool = False,
) -> bool:
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        tables = {
            str(row[0]) for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }

        def columns(table: str) -> set[str]:
            if table not in tables:
                return set()
            return {
                str(row[1]) for row in connection.execute(
                    f'PRAGMA table_info("{table}")'
                ).fetchall()
            }

        source_columns = columns("repositorysource")
        source_predicates = [
            f"{name} = 1" for name in ("is_private", "local_only")
            if name in source_columns
        ]
        source = (connection.execute(
            "SELECT 1 FROM repositorysource WHERE "
            + " OR ".join(source_predicates) + " LIMIT 1"
        ).fetchone() if source_predicates else None)
        paper_columns = columns("papersource")
        paper_source = (connection.execute(
            "SELECT 1 FROM papersource WHERE local_only = 1 LIMIT 1"
        ).fetchone() if "local_only" in paper_columns else None)
        artifact = (connection.execute(
            "SELECT 1 FROM artifact WHERE restricted = 1 LIMIT 1"
        ).fetchone() if "restricted" in columns("artifact") else None)
        tag = (connection.execute(
            "SELECT 1 FROM tag WHERE restricted = 1 LIMIT 1"
        ).fetchone() if "restricted" in columns("tag") else None)
        repository_secret = None
        if include_repositories:
            repository_secret = (connection.execute(
                "SELECT 1 FROM repositoryfile WHERE restricted = 1 LIMIT 1"
            ).fetchone() if "restricted" in columns("repositoryfile") else None)
            repository_secret = repository_secret or (connection.execute(
                "SELECT 1 FROM repositorysnapshot "
                "WHERE secret_finding_count > 0 LIMIT 1"
            ).fetchone() if "secret_finding_count" in columns(
                "repositorysnapshot") else None)
        return bool(source or paper_source or artifact or tag or repository_secret)
    finally:
        connection.close()


def _archive_member_for_media_locator(locator: str) -> str:
    normalized = str(locator or "").replace("\\", "/")
    label = "library"
    relative = normalized
    if normalized.startswith("media:"):
        label = "media"
        relative = normalized.removeprefix("media:")
    relative = relative.lstrip("/")
    parts = relative.split("/")
    if not relative or any(part in ("", ".", "..") for part in parts):
        return ""
    return f"{label}/{relative}"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _staged_payload_lookup(root: Path):
    cache: dict[str, tuple[int, str] | None] = {}

    def lookup(member: str) -> tuple[int, str] | None:
        if member in cache:
            return cache[member]
        if not member.startswith(("library/", "media/")):
            cache[member] = None
            return None
        path = root / Path(member)
        if not path.is_file() or path.is_symlink():
            cache[member] = None
            return None
        result = (path.stat().st_size, _sha256_file(path))
        cache[member] = result
        return result

    return lookup


def _archive_payload_lookup(archive: zipfile.ZipFile):
    cache: dict[str, tuple[int, str] | None] = {}

    def lookup(member: str) -> tuple[int, str] | None:
        if member in cache:
            return cache[member]
        try:
            info = archive.getinfo(member)
        except KeyError:
            cache[member] = None
            return None
        if info.is_dir():
            cache[member] = None
            return None
        digest = hashlib.sha256()
        with archive.open(info) as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
        result = (info.file_size, digest.hexdigest())
        cache[member] = result
        return result

    return lookup


def _cloud_primary_media_dependencies(
    database: Path, *, archived_payload,
) -> dict:
    """Describe media bytes that the database expects to remain in cloud storage.

    The manifest deliberately contains only restoration identifiers and the
    storage target's non-secret identity. Credentials remain in the encrypted
    settings store contained in the database snapshot.
    """
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        tables = {
            str(row[0]) for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "mediaobject" not in tables:
            return {"count": 0, "objects": [], "storage_targets": []}

        media_columns = {
            str(row[1]) for row in connection.execute(
                'PRAGMA table_info("mediaobject")'
            ).fetchall()
        }
        required_media_columns = {
            "id", "project_id", "artifact_id", "role", "storage_target_id",
            "local_path", "remote_key", "state", "size_bytes", "sha256",
            "remote_size_bytes", "remote_sha256",
        }
        if not required_media_columns <= media_columns:
            raise ValueError(
                "media object table is missing fields required for backup disclosure"
            )

        project_slugs: dict[int, str] = {}
        if "project" in tables:
            project_columns = {
                str(row[1]) for row in connection.execute(
                    'PRAGMA table_info("project")'
                ).fetchall()
            }
            if {"id", "slug"} <= project_columns:
                project_slugs = {
                    int(row["id"]): str(row["slug"])
                    for row in connection.execute(
                        'SELECT id, slug FROM "project"'
                    ).fetchall()
                }

        targets: dict[int, dict] = {}
        if "mediastoragetarget" in tables:
            target_columns = {
                str(row[1]) for row in connection.execute(
                    'PRAGMA table_info("mediastoragetarget")'
                ).fetchall()
            }
            required_target_columns = {
                "id", "identity_hash", "provider", "remote_base",
            }
            if required_target_columns <= target_columns:
                targets = {
                    int(row["id"]): {
                        "identity_hash": str(row["identity_hash"] or ""),
                        "provider": str(row["provider"] or ""),
                        "remote_base": str(row["remote_base"] or ""),
                    }
                    for row in connection.execute(
                        "SELECT id, identity_hash, provider, remote_base "
                        "FROM mediastoragetarget"
                    ).fetchall()
                }

        objects: list[dict] = []
        referenced_targets: dict[str, dict] = {}
        rows = connection.execute(
            "SELECT id, project_id, artifact_id, role, storage_target_id, "
            "local_path, remote_key, state, size_bytes, sha256, "
            "remote_size_bytes, remote_sha256 "
            "FROM mediaobject WHERE remote_key != '' ORDER BY id"
        ).fetchall()
        for row in rows:
            archive_member = _archive_member_for_media_locator(row["local_path"])
            payload = archived_payload(archive_member) if archive_member else None
            local_hash = str(row["sha256"] or "")
            remote_hash = str(row["remote_sha256"] or "")
            local_size = int(row["size_bytes"] or 0)
            remote_size = int(row["remote_size_bytes"] or 0)
            archived_is_verified = (
                str(row["state"] or "") == "verified"
                and payload is not None
                and bool(local_hash)
                and local_hash == remote_hash
                and local_size > 0
                and local_size == remote_size
                and payload == (remote_size, remote_hash)
            )
            if archived_is_verified:
                continue
            target_id = (
                int(row["storage_target_id"])
                if row["storage_target_id"] is not None else None
            )
            target = targets.get(target_id) if target_id is not None else None
            target_identity = target["identity_hash"] if target else ""
            if target:
                referenced_targets[target_identity] = target
            project_id = int(row["project_id"])
            objects.append({
                "media_object_id": int(row["id"]),
                "project_id": project_id,
                "project_slug": project_slugs.get(project_id, ""),
                "artifact_id": (
                    int(row["artifact_id"])
                    if row["artifact_id"] is not None else None
                ),
                "role": str(row["role"] or ""),
                "state": str(row["state"] or ""),
                "remote_key": str(row["remote_key"] or ""),
                "sha256": remote_hash or local_hash,
                "size_bytes": remote_size or local_size,
                "storage_target_identity_hash": target_identity,
            })
        return {
            "count": len(objects),
            "objects": objects,
            "storage_targets": sorted(
                referenced_targets.values(),
                key=lambda item: item["identity_hash"],
            ),
        }
    finally:
        connection.close()


def _dependency_contract_complete(dependencies: dict) -> bool:
    objects = dependencies.get("objects")
    targets = dependencies.get("storage_targets")
    count = dependencies.get("count")
    if (not isinstance(count, int) or count < 0
            or not isinstance(objects, list) or not isinstance(targets, list)
            or count != len(objects)):
        return False
    target_identities = {
        target.get("identity_hash")
        for target in targets
        if (
            isinstance(target, dict)
            and isinstance(target.get("identity_hash"), str)
            and target.get("identity_hash")
            and isinstance(target.get("provider"), str)
            and target.get("provider")
            and isinstance(target.get("remote_base"), str)
            and target.get("remote_base")
        )
    }
    if len(target_identities) != len(targets):
        return False
    return all(
        isinstance(item, dict)
        and isinstance(item.get("media_object_id"), int)
        and isinstance(item.get("project_id"), int)
        and isinstance(item.get("remote_key"), str)
        and bool(item.get("remote_key"))
        and isinstance(item.get("storage_target_identity_hash"), str)
        and item.get("storage_target_identity_hash") in target_identities
        for item in objects
    )


def _validate_unencrypted_policy(session) -> None:
    from sqlmodel import select

    from .models import Artifact, PaperSource, RepositorySource, Tag

    private_source = session.exec(select(RepositorySource).where(
        (RepositorySource.is_private == True)  # noqa: E712
        | (RepositorySource.local_only == True)  # noqa: E712
    )).first()
    private_paper = session.exec(select(PaperSource).where(
        PaperSource.local_only == True  # noqa: E712
    )).first()
    restricted_artifact = session.exec(select(Artifact).where(
        Artifact.restricted == True  # noqa: E712
    )).first()
    restricted_tag = session.exec(select(Tag).where(
        Tag.restricted == True  # noqa: E712
    )).first()
    if private_source or private_paper or restricted_artifact or restricted_tag:
        raise ValueError(
            "BACKUP_ENCRYPTION_KEY is required while private or local-only "
            "source analysis exists because the backup contains source-derived data"
        )


def _source_files(*, include_media: bool, include_repositories: bool):
    if settings.library_dir.exists():
        for path in settings.library_dir.rglob("*"):
            if (path.is_file() and not path.is_symlink()
                    and ".trash" not in path.parts and ".staging" not in path.parts):
                yield "library", settings.library_dir, path
    if include_media and settings.media_dir.exists():
        seen: set[Path] = set()
        for pattern in (
            "*/source_video.*", "*/source_audio.*", "*/source.*", "*/uploaded.*",
        ):
            for path in settings.media_dir.glob(pattern):
                if path not in seen and path.is_file() and not path.is_symlink():
                    seen.add(path)
                    yield "media", settings.media_dir, path
    if include_repositories and settings.repository_dir.exists():
        for path in settings.repository_dir.rglob("*"):
            if (path.is_file() and not path.is_symlink()
                    and ".trash" not in path.parts and ".staging" not in path.parts):
                yield "repositories", settings.repository_dir, path


def _storage_fingerprint(*, include_media: bool,
                         include_repositories: bool) -> dict[str, tuple[int, int]]:
    result: dict[str, tuple[int, int]] = {}
    for label, root, path in _source_files(
            include_media=include_media,
            include_repositories=include_repositories):
        stat_result = path.stat()
        relative = path.relative_to(root).as_posix()
        result[f"{label}/{relative}"] = (
            stat_result.st_size, stat_result.st_mtime_ns)
    return result


def _stage_backup_files(root: Path, *, include_media: bool,
                        include_repositories: bool) -> None:
    """Copy one optimistic filesystem snapshot for later validation."""
    for label, source_root, path in _source_files(
            include_media=include_media,
            include_repositories=include_repositories):
        rel = path.relative_to(source_root)
        _copy_regular_file(path, root / label / rel)


def create_backup(*, include_media: bool = True,
                  include_repositories: bool = False) -> Path:
    if include_repositories and not settings.backup_encryption_key:
        # A public repository can contain a secret that has not yet reached its
        # incremental scanner row. Raw snapshots therefore always require
        # encryption, independent of current visibility/classification.
        raise ValueError(
            "BACKUP_ENCRYPTION_KEY is required when repository snapshots are included"
        )
    settings.backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    with tempfile.TemporaryDirectory(dir=settings.backup_dir) as tmpdir:
        tmp = Path(tmpdir)
        db_snapshot = tmp / "synapse.sqlite3"
        staged = tmp / "staged"
        # Copy optimistically, then validate file identity and snapshot SQLite
        # under a short writer lease. This gives a coherent point in time
        # without holding SQLite's only writer while copying multi-GB media.
        from sqlmodel import select, text

        from .db import get_session
        from .models import Job

        stable = False
        for _attempt in range(3):
            shutil.rmtree(staged, ignore_errors=True)
            db_snapshot.unlink(missing_ok=True)
            before = _storage_fingerprint(
                include_media=include_media,
                include_repositories=include_repositories)
            _stage_backup_files(
                staged,
                include_media=include_media,
                include_repositories=include_repositories)
            with get_session() as session:
                session.exec(text("BEGIN IMMEDIATE"))
                try:
                    from . import media_storage

                    active = session.exec(select(Job).where(
                        Job.status.in_(("queued", "running")),
                        Job.task != "create_backup",
                    )).first()
                    if active:
                        raise ValueError(
                            "wait for active processing jobs to finish before creating a backup"
                        )
                    media_storage.assert_no_transient_media(
                        session, action="creating a backup")
                    after = _storage_fingerprint(
                        include_media=include_media,
                        include_repositories=include_repositories)
                    if before != after:
                        continue
                    if not settings.backup_encryption_key:
                        _validate_unencrypted_policy(session)
                    _sqlite_snapshot(db_snapshot)
                    stable = True
                finally:
                    session.rollback()
            if stable:
                break
        if not stable:
            raise RuntimeError(
                "library files changed repeatedly while the backup was staged; try again"
            )
        if (not settings.backup_encryption_key
                and _snapshot_contains_restricted_data(
                    db_snapshot, include_repositories=include_repositories)):
            raise ValueError(
                "BACKUP_ENCRYPTION_KEY is required because the database "
                "snapshot contains private, local-only, or restricted raw "
                "source data"
            )
        archive = tmp / f"synapse-{stamp}.zip"
        includes = ["database", "library"]
        if include_media:
            includes.append("archived_media")
        if include_repositories:
            includes.append("repository_snapshots")
        manifest = {
            "format": 1,
            "created": datetime.now(timezone.utc).isoformat(),
            "includes": includes,
        }
        if include_media:
            manifest["external_dependencies"] = {
                "cloud_primary_media": _cloud_primary_media_dependencies(
                    db_snapshot,
                    archived_payload=_staged_payload_lookup(staged),
                ),
            }
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED,
                             compresslevel=6, allowZip64=True) as zf:
            zf.writestr("manifest.json", json.dumps(manifest, indent=2))
            zf.write(db_snapshot, "database/synapse.sqlite3")
            library_root = staged / "library"
            if library_root.exists():
                for path in library_root.rglob("*"):
                    if (path.is_file() and ".trash" not in path.parts
                            and ".staging" not in path.parts):
                        rel = path.relative_to(library_root).as_posix()
                        zf.write(path, f"library/{rel}")
            media_root = staged / "media"
            if include_media and media_root.exists():
                for pattern in (
                    "*/source_video.*", "*/source_audio.*", "*/source.*", "*/uploaded.*",
                ):
                    for path in media_root.glob(pattern):
                        if path.is_file():
                            rel = path.relative_to(media_root).as_posix()
                            zf.write(path, f"media/{rel}")
            repository_root = staged / "repositories"
            if include_repositories and repository_root.exists():
                for path in repository_root.rglob("*"):
                    if (path.is_file() and ".trash" not in path.parts
                            and ".staging" not in path.parts):
                        rel = path.relative_to(repository_root).as_posix()
                        zf.write(path, f"repositories/{rel}")
        if settings.backup_encryption_key:
            encrypted = tmp / f"synapse-{stamp}.zip.enc"
            _encrypt(archive, encrypted, settings.backup_encryption_key)
            destination = settings.backup_dir / encrypted.name
            os.replace(encrypted, destination)
        else:
            destination = settings.backup_dir / archive.name
            os.replace(archive, destination)

    retention = max(1, int(get_setting("backup.retention", 5)))
    backups = sorted(list_backups(), key=lambda item: item.stat().st_mtime, reverse=True)
    for old in backups[retention:]:
        old.unlink(missing_ok=True)
    return destination


def list_backups() -> list[Path]:
    if not settings.backup_dir.exists():
        return []
    return sorted(
        [*settings.backup_dir.glob("synapse-*.zip"),
         *settings.backup_dir.glob("synapse-*.zip.enc")],
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )


def _verify_database(archive: zipfile.ZipFile) -> str:
    """Run SQLite's full integrity check on the database inside an archive."""
    extracted: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False) as handle:
            extracted = Path(handle.name)
            with archive.open("database/synapse.sqlite3") as source:
                shutil.copyfileobj(source, handle, length=1024 * 1024)
        try:
            connection = sqlite3.connect(f"file:{extracted}?mode=ro", uri=True)
            try:
                rows = connection.execute("PRAGMA integrity_check").fetchall()
            finally:
                connection.close()
        except sqlite3.DatabaseError as exc:
            return f"error: {exc}"
        messages = [str(row[0]) for row in rows]
        return "ok" if messages == ["ok"] else "; ".join(messages[:10])
    except KeyError:
        return "missing database/synapse.sqlite3"
    finally:
        if extracted is not None:
            extracted.unlink(missing_ok=True)


def _verify_cloud_primary_media(
    archive: zipfile.ZipFile, manifest: dict, includes: list,
) -> dict:
    """Validate the declared remote-media dependency against the snapshot."""
    if "archived_media" not in includes:
        return {
            "applicable": False,
            "valid": True,
            "dependency_count": 0,
            "requires_remote": False,
            "remote_availability_checked": False,
            "message": "Archived media was not selected for this backup.",
        }

    extracted: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False) as handle:
            extracted = Path(handle.name)
            with archive.open("database/synapse.sqlite3") as source:
                shutil.copyfileobj(source, handle, length=1024 * 1024)
        expected = _cloud_primary_media_dependencies(
            extracted,
            archived_payload=_archive_payload_lookup(archive),
        )
    except (KeyError, sqlite3.DatabaseError, ValueError) as exc:
        return {
            "applicable": True,
            "valid": False,
            "dependency_count": 0,
            "requires_remote": False,
            "remote_availability_checked": False,
            "message": f"Could not validate cloud-primary media dependencies: {exc}",
        }
    finally:
        if extracted is not None:
            extracted.unlink(missing_ok=True)

    external = manifest.get("external_dependencies")
    declared = (
        external.get("cloud_primary_media")
        if isinstance(external, dict) else None
    )
    # Backups written before cloud-primary storage existed remain valid when
    # their database contains no cloud-only media. Any real dependency must be
    # declared explicitly.
    declaration_matches = (
        declared == expected
        if declared is not None
        else expected["count"] == 0
    )
    contract_complete = _dependency_contract_complete(expected)
    valid = declaration_matches and contract_complete
    count = expected["count"]
    if not valid:
        message = (
            "The cloud-primary media dependency declaration does not match "
            "the database snapshot or is incomplete."
        )
    elif count:
        message = (
            f"The archive depends on {count} cloud-primary media object"
            f"{'' if count == 1 else 's'} at the recorded remote target"
            f"{'' if len(expected['storage_targets']) == 1 else 's'}. "
            "Remote availability and credentials were not tested."
        )
    else:
        message = "All selected media bytes are contained in the archive."
    return {
        "applicable": True,
        "valid": valid,
        "dependency_count": count,
        "requires_remote": count > 0,
        "remote_availability_checked": False,
        "storage_targets": expected["storage_targets"],
        "message": message,
    }


def verify_backup(path: Path) -> dict:
    temporary: Path | None = None
    try:
        archive = path
        if path.suffix == ".enc":
            if not settings.backup_encryption_key:
                raise ValueError("BACKUP_ENCRYPTION_KEY is required to verify this backup")
            handle = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
            handle.close()
            temporary = Path(handle.name)
            _decrypt(path, temporary, settings.backup_encryption_key)
            archive = temporary
        with zipfile.ZipFile(archive) as zf:
            bad = zf.testzip()
            manifest = json.loads(zf.read("manifest.json"))
            raw_includes = (
                manifest.get("includes", []) if isinstance(manifest, dict) else []
            )
            includes = raw_includes if isinstance(raw_includes, list) else []
            manifest_valid = (
                isinstance(manifest, dict)
                and manifest.get("format") == 1
                and all(isinstance(item, str) for item in includes)
                and "database" in includes
                and "library" in includes
            )
            database_integrity = (
                _verify_database(zf) if bad is None
                else f"not checked: bad ZIP member {bad}"
            )
            cloud_primary_media = (
                _verify_cloud_primary_media(zf, manifest, includes)
                if bad is None and manifest_valid and database_integrity == "ok"
                else {
                    "applicable": "archived_media" in includes,
                    "valid": False,
                    "dependency_count": 0,
                    "requires_remote": False,
                    "remote_availability_checked": False,
                    "message": (
                        "Cloud-primary media dependencies were not checked "
                        "because archive verification failed."
                    ),
                }
            )
            return {
                "valid": (bad is None and manifest_valid
                          and database_integrity == "ok"
                          and cloud_primary_media["valid"]),
                "bad_file": bad,
                "manifest": manifest,
                "manifest_valid": manifest_valid,
                "database_integrity": database_integrity,
                "cloud_primary_media": cloud_primary_media,
                "media_self_contained": (
                    cloud_primary_media["applicable"]
                    and cloud_primary_media["valid"]
                    and not cloud_primary_media["requires_remote"]
                ),
                "files": len(zf.infolist()),
            }
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)
