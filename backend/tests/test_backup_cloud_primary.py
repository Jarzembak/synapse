from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
import zipfile
from contextlib import contextmanager

from app import backup
from app import db as database


class _NoActiveJobsResult:
    def first(self):
        return None


class _NoActiveJobsSession:
    def exec(self, _statement):
        return _NoActiveJobsResult()

    def rollback(self):
        return None


@contextmanager
def _no_active_jobs_session():
    yield _NoActiveJobsSession()


def _cloud_only_database(
    path, *, verified_payload: bytes, conflicting_payload: bytes,
) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE marker(value TEXT NOT NULL);
            INSERT INTO marker VALUES ('cloud-primary-backup');

            CREATE TABLE project(
                id INTEGER PRIMARY KEY,
                slug TEXT NOT NULL
            );
            INSERT INTO project(id, slug) VALUES (7, 'remote-course');

            CREATE TABLE mediastoragetarget(
                id INTEGER PRIMARY KEY,
                identity_hash TEXT NOT NULL,
                provider TEXT NOT NULL,
                remote_base TEXT NOT NULL
            );
            INSERT INTO mediastoragetarget(
                id, identity_hash, provider, remote_base
            ) VALUES (
                3, 'target-identity-hash', 's3', 'synapse/media'
            );

            CREATE TABLE mediaobject(
                id INTEGER PRIMARY KEY,
                project_id INTEGER NOT NULL,
                artifact_id INTEGER,
                role TEXT NOT NULL,
                storage_target_id INTEGER,
                local_path TEXT NOT NULL,
                remote_key TEXT NOT NULL,
                state TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                sha256 TEXT NOT NULL,
                remote_size_bytes INTEGER NOT NULL,
                remote_sha256 TEXT NOT NULL
            );
            INSERT INTO mediaobject(
                id, project_id, artifact_id, role, storage_target_id,
                local_path, remote_key, state, size_bytes, sha256,
                remote_size_bytes, remote_sha256
            ) VALUES (
                41, 7, 13, 'source_video', 3,
                'media:remote-course/source_video.mp4',
                'media-objects/ab/cdef.mp4', 'cloud_only',
                2048, 'local-sha256', 2048, 'remote-sha256'
            );
            INSERT INTO mediaobject(
                id, project_id, artifact_id, role, storage_target_id,
                local_path, remote_key, state, size_bytes, sha256,
                remote_size_bytes, remote_sha256
            ) VALUES (
                42, 7, 14, 'source_audio', 3,
                'media:remote-course/source_audio.mp3',
                'media-objects/ab/error.mp3', 'error',
                1024, 'error-local-sha256', 1024, 'error-remote-sha256'
            );
            """
        )
        verified_hash = hashlib.sha256(verified_payload).hexdigest()
        connection.execute(
            """
            INSERT INTO mediaobject(
                id, project_id, artifact_id, role, storage_target_id,
                local_path, remote_key, state, size_bytes, sha256,
                remote_size_bytes, remote_sha256
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                43, 7, 15, "source_video", 3,
                "media:remote-course/source.mp4",
                "media-objects/ab/included.mp4", "verified",
                len(verified_payload), verified_hash,
                len(verified_payload), verified_hash,
            ),
        )
        connection.execute(
            """
            INSERT INTO mediaobject(
                id, project_id, artifact_id, role, storage_target_id,
                local_path, remote_key, state, size_bytes, sha256,
                remote_size_bytes, remote_sha256
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                44, 7, 16, "source_video", 3,
                "media:remote-course/uploaded.mp4",
                "media-objects/ab/conflicting.mp4", "error",
                len(conflicting_payload),
                hashlib.sha256(conflicting_payload).hexdigest(),
                4096, "conflicting-remote-sha256",
            ),
        )
        connection.commit()


def test_cloud_only_media_is_declared_without_hydration_and_verified(
    tmp_path, monkeypatch,
):
    db_path = tmp_path / "synapse.sqlite3"
    library_dir = tmp_path / "library"
    media_dir = tmp_path / "media"
    backup_dir = tmp_path / "backups"
    library_dir.mkdir()
    media_dir.mkdir()
    (library_dir / "notes.md").write_text("portable notes", encoding="utf-8")
    verified_payload = b"included locally"
    conflicting_payload = b"conflicting local bytes"
    included_media = media_dir / "remote-course" / "source.mp4"
    included_media.parent.mkdir()
    included_media.write_bytes(verified_payload)
    conflicting_media = media_dir / "remote-course" / "uploaded.mp4"
    conflicting_media.write_bytes(conflicting_payload)
    _cloud_only_database(
        db_path,
        verified_payload=verified_payload,
        conflicting_payload=conflicting_payload,
    )

    monkeypatch.setattr(backup.settings, "db_path", db_path)
    monkeypatch.setattr(backup.settings, "library_dir", library_dir)
    monkeypatch.setattr(backup.settings, "media_dir", media_dir)
    monkeypatch.setattr(backup.settings, "backup_dir", backup_dir)
    monkeypatch.setattr(backup.settings, "backup_encryption_key", "test-backup-key")
    monkeypatch.setattr(backup, "get_setting", lambda _key, default=None: default)
    monkeypatch.setattr(database, "get_session", _no_active_jobs_session)

    created = backup.create_backup(include_media=True)
    report = backup.verify_backup(created)

    assert report["valid"] is True
    assert report["media_self_contained"] is False
    assert report["cloud_primary_media"] == {
        "applicable": True,
        "valid": True,
        "dependency_count": 3,
        "requires_remote": True,
        "remote_availability_checked": False,
        "storage_targets": [{
            "identity_hash": "target-identity-hash",
            "provider": "s3",
            "remote_base": "synapse/media",
        }],
        "message": (
            "The archive depends on 3 cloud-primary media objects at the "
            "recorded remote target. Remote availability and credentials "
            "were not tested."
        ),
    }
    dependency = report["manifest"]["external_dependencies"][
        "cloud_primary_media"
    ]
    assert dependency["count"] == 3
    assert dependency["objects"] == [
        {
            "media_object_id": 41,
            "project_id": 7,
            "project_slug": "remote-course",
            "artifact_id": 13,
            "role": "source_video",
            "state": "cloud_only",
            "remote_key": "media-objects/ab/cdef.mp4",
            "sha256": "remote-sha256",
            "size_bytes": 2048,
            "storage_target_identity_hash": "target-identity-hash",
        },
        {
            "media_object_id": 42,
            "project_id": 7,
            "project_slug": "remote-course",
            "artifact_id": 14,
            "role": "source_audio",
            "state": "error",
            "remote_key": "media-objects/ab/error.mp3",
            "sha256": "error-remote-sha256",
            "size_bytes": 1024,
            "storage_target_identity_hash": "target-identity-hash",
        },
        {
            "media_object_id": 44,
            "project_id": 7,
            "project_slug": "remote-course",
            "artifact_id": 16,
            "role": "source_video",
            "state": "error",
            "remote_key": "media-objects/ab/conflicting.mp4",
            "sha256": "conflicting-remote-sha256",
            "size_bytes": 4096,
            "storage_target_identity_hash": "target-identity-hash",
        },
    ]
    assert dependency["storage_targets"] == [{
        "identity_hash": "target-identity-hash",
        "provider": "s3",
        "remote_base": "synapse/media",
    }]
    assert included_media.read_bytes() == verified_payload
    assert conflicting_media.read_bytes() == conflicting_payload
    assert not (media_dir / "remote-course" / "source_video.mp4").exists()
    assert not (media_dir / "remote-course" / "source_audio.mp3").exists()

    # The dependency declaration is part of archive validity. A manifest that
    # understates it must not pass verification even when the ZIP and SQLite
    # payloads are otherwise intact.
    tampered_manifest = copy.deepcopy(report["manifest"])
    tampered_manifest["external_dependencies"]["cloud_primary_media"]["count"] = 0
    tampered = backup_dir / "tampered.zip"
    with zipfile.ZipFile(tampered, "w") as archive:
        archive.writestr("manifest.json", json.dumps(tampered_manifest))
        archive.write(db_path, "database/synapse.sqlite3")
        archive.writestr("library/notes.md", "portable notes")
        archive.write(
            included_media,
            "media/remote-course/source.mp4",
        )
        archive.write(
            conflicting_media,
            "media/remote-course/uploaded.mp4",
        )

    tampered_report = backup.verify_backup(tampered)
    assert tampered_report["valid"] is False
    assert tampered_report["database_integrity"] == "ok"
    assert tampered_report["cloud_primary_media"]["valid"] is False
    assert tampered_report["cloud_primary_media"]["dependency_count"] == 3
