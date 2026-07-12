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


def create_backup(*, include_media: bool = True) -> Path:
    settings.backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    with tempfile.TemporaryDirectory(dir=settings.backup_dir) as tmpdir:
        tmp = Path(tmpdir)
        db_snapshot = tmp / "synapse.sqlite3"
        _sqlite_snapshot(db_snapshot)
        archive = tmp / f"synapse-{stamp}.zip"
        manifest = {
            "format": 1,
            "created": datetime.now(timezone.utc).isoformat(),
            "includes": ["database", "library"] + (["archived_media"] if include_media else []),
        }
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED,
                             compresslevel=6, allowZip64=True) as zf:
            zf.writestr("manifest.json", json.dumps(manifest, indent=2))
            zf.write(db_snapshot, "database/synapse.sqlite3")
            if settings.library_dir.exists():
                for path in settings.library_dir.rglob("*"):
                    if path.is_file() and ".trash" not in path.parts:
                        rel = path.relative_to(settings.library_dir).as_posix()
                        zf.write(path, f"library/{rel}")
            if include_media and settings.media_dir.exists():
                for pattern in (
                    "*/source_video.*", "*/source_audio.*", "*/source.*", "*/uploaded.*",
                ):
                    for path in settings.media_dir.glob(pattern):
                        if path.is_file():
                            rel = path.relative_to(settings.media_dir).as_posix()
                            zf.write(path, f"media/{rel}")
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
            includes = manifest.get("includes", []) if isinstance(manifest, dict) else []
            manifest_valid = (
                isinstance(manifest, dict)
                and manifest.get("format") == 1
                and "database" in includes
                and "library" in includes
            )
            database_integrity = (
                _verify_database(zf) if bad is None
                else f"not checked: bad ZIP member {bad}"
            )
            return {
                "valid": (bad is None and manifest_valid
                          and database_integrity == "ok"),
                "bad_file": bad,
                "manifest": manifest,
                "manifest_valid": manifest_valid,
                "database_integrity": database_integrity,
                "files": len(zf.infolist()),
            }
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)
