"""SQLite engine + FTS5 setup.

The DB is the search/relation index; markdown files under LIBRARY_DIR are the
source of truth for artifact *content*. `ArtifactFTS` is a contentless FTS5
table kept in sync from library.py whenever an artifact body is written.
"""
from __future__ import annotations

import logging

from sqlalchemy import event
from sqlalchemy.exc import IntegrityError
from sqlmodel import SQLModel, Session, create_engine, text

from .config import settings

settings.db_path.parent.mkdir(parents=True, exist_ok=True)
engine = create_engine(
    f"sqlite:///{settings.db_path}",
    connect_args={"check_same_thread": False, "timeout": 15},
    pool_pre_ping=True,
)

log = logging.getLogger(__name__)


@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_connection, _connection_record) -> None:
    """Integrity and multi-process behavior for the API + Celery workers."""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA busy_timeout=15000")
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.close()


def _columns(conn, table: str) -> set[str]:
    return {row[1] for row in conn.exec_driver_sql(f'PRAGMA table_info("{table}")')}


def _add_column(conn, table: str, name: str, ddl: str) -> None:
    if name not in _columns(conn, table):
        conn.exec_driver_sql(f'ALTER TABLE "{table}" ADD COLUMN "{name}" {ddl}')


def _migrate(conn) -> None:
    """Idempotent migrations for databases persisted across app upgrades."""
    conn.exec_driver_sql(
        "CREATE TABLE IF NOT EXISTS schema_version "
        "(version INTEGER NOT NULL, applied TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )
    _add_column(conn, "project", "deleting", "BOOLEAN NOT NULL DEFAULT 0")
    for name in ("input_hash", "config_hash"):
        _add_column(conn, "artifact", name, "VARCHAR NOT NULL DEFAULT ''")
    _add_column(conn, "artifact", "provenance", "VARCHAR NOT NULL DEFAULT '{}'")
    _add_column(conn, "artifact", "restricted", "BOOLEAN NOT NULL DEFAULT 0")
    _add_column(conn, "artifact", "repository_derived", "BOOLEAN NOT NULL DEFAULT 0")
    _add_column(conn, "tag", "restricted", "BOOLEAN NOT NULL DEFAULT 0")
    _add_column(conn, "job", "parent_job_id", "INTEGER")
    _add_column(conn, "job", "options", "VARCHAR NOT NULL DEFAULT '{}'")
    for name in ("started", "finished", "heartbeat"):
        _add_column(conn, "job", name, "DATETIME")
    # Development builds may already have an early repository schema.  Keep
    # these additive migrations idempotent so those databases remain usable.
    if _columns(conn, "repositorysource"):
        _add_column(conn, "repositorysource", "local_only", "BOOLEAN NOT NULL DEFAULT 1")
        conn.exec_driver_sql("UPDATE repositorysource SET local_only=1")
        _add_column(conn, "repositorysource", "pending_sha", "VARCHAR NOT NULL DEFAULT ''")
        _add_column(conn, "repositorysource", "current_snapshot_id", "INTEGER")
        _add_column(conn, "repositorysource", "description", "VARCHAR NOT NULL DEFAULT ''")
        _add_column(conn, "repositorysource", "include_paths", "VARCHAR NOT NULL DEFAULT '[]'")
        _add_column(conn, "repositorysource", "exclude_paths", "VARCHAR NOT NULL DEFAULT '[]'")
        _add_column(conn, "repositorysource", "coverage_preview", "VARCHAR NOT NULL DEFAULT '{}'")
        _add_column(conn, "repositorysource", "cloud_purge_pending",
                    "BOOLEAN NOT NULL DEFAULT 0")
    if _columns(conn, "repositorysnapshot"):
        _add_column(conn, "repositorysnapshot", "archive_bytes", "INTEGER NOT NULL DEFAULT 0")
        _add_column(conn, "repositorysnapshot", "facts", "VARCHAR NOT NULL DEFAULT '{}'")
        _add_column(conn, "repositorysnapshot", "secret_finding_count", "INTEGER NOT NULL DEFAULT 0")
        _add_column(conn, "repositorysnapshot", "scanner_version", "VARCHAR NOT NULL DEFAULT '1'")
        _add_column(conn, "repositorysnapshot", "scan_config_hash", "VARCHAR NOT NULL DEFAULT ''")
        _add_column(conn, "repositorysnapshot", "omitted_links", "VARCHAR NOT NULL DEFAULT '[]'")
    if _columns(conn, "repositoryfile"):
        _add_column(conn, "repositoryfile", "symlink", "BOOLEAN NOT NULL DEFAULT 0")
    if _columns(conn, "repositorychunk"):
        _add_column(conn, "repositorychunk", "content_hash", "VARCHAR NOT NULL DEFAULT ''")
        _add_column(conn, "repositorychunk", "summary_text", "VARCHAR NOT NULL DEFAULT ''")
        _add_column(conn, "repositorychunk", "summary_json", "VARCHAR NOT NULL DEFAULT '{}'")
        _add_column(conn, "repositorychunk", "summary_config_hash", "VARCHAR NOT NULL DEFAULT ''")

    # Backfill the durable origin marker while contributor/project lineage is
    # still available. This closes the upgrade case where deleting a GitHub
    # project later detaches a shared quick-reference from its source rows.
    if _columns(conn, "artifact") and "repository_derived" in _columns(conn, "artifact"):
        if _columns(conn, "project"):
            conn.exec_driver_sql(
                "UPDATE artifact SET repository_derived=1 WHERE project_id IN "
                "(SELECT id FROM project WHERE source_type='github')"
            )
        if (_columns(conn, "quickref") and _columns(conn, "quickrefsource")
                and _columns(conn, "project")):
            conn.exec_driver_sql(
                "UPDATE artifact SET repository_derived=1 WHERE path IN ("
                "SELECT q.path FROM quickref q JOIN quickrefsource qs "
                "ON qs.quickref_id=q.id JOIN project p ON p.id=qs.project_id "
                "WHERE p.source_type='github')"
            )

    indexes = [
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_artifact_path_type ON artifact(path, type)",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_quickref_kind_slug ON quickref(kind, slug)",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_active_project_task "
        "ON job(project_id, task) WHERE status IN ('queued','running')",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_running_run_all "
        "ON job((1)) WHERE task='run_all' AND status='running'",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_search_chunk_position "
        "ON searchchunk(artifact_id, chunk_index)",
        "CREATE INDEX IF NOT EXISTS ix_job_status_updated ON job(status, updated)",
        "CREATE INDEX IF NOT EXISTS ix_llmcall_created_function "
        "ON llmcall(created, function)",
        "CREATE INDEX IF NOT EXISTS ix_artifact_restricted ON artifact(restricted)",
        "CREATE INDEX IF NOT EXISTS ix_artifact_repository_derived "
        "ON artifact(repository_derived)",
        "CREATE INDEX IF NOT EXISTS ix_tag_restricted ON tag(restricted)",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_repository_source_project "
        "ON repositorysource(project_id)",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_repository_snapshot_source_sha "
        "ON repositorysnapshot(source_id, resolved_sha)",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_repository_file_snapshot_path "
        "ON repositoryfile(snapshot_id, path)",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_repository_chunk_file_position "
        "ON repositorychunk(file_id, chunk_index)",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_repository_chunk_evidence "
        "ON repositorychunk(evidence_id)",
        "CREATE INDEX IF NOT EXISTS ix_repository_file_snapshot_excluded "
        "ON repositoryfile(snapshot_id, excluded)",
        "CREATE INDEX IF NOT EXISTS ix_repository_chunk_body_hash "
        "ON repositorychunk(body_hash)",
        "CREATE INDEX IF NOT EXISTS ix_repository_source_cloud_purge "
        "ON repositorysource(cloud_purge_pending)",
    ]
    for ddl in indexes:
        try:
            conn.exec_driver_sql(ddl)
        except IntegrityError:
            log.warning(
                "integrity index deferred because existing rows conflict; "
                "Library Health can report/repair this: %s", ddl)
    current = conn.exec_driver_sql(
        "SELECT COALESCE(MAX(version), 0) FROM schema_version"
    ).scalar()
    if current < 1:
        conn.exec_driver_sql("INSERT INTO schema_version(version) VALUES (1)")
        current = 1
    if current < 2:
        conn.exec_driver_sql("INSERT INTO schema_version(version) VALUES (2)")


def init_db() -> None:
    from . import models  # noqa: F401  ensure tables are registered

    SQLModel.metadata.create_all(engine)
    with engine.begin() as conn:
        _migrate(conn)
        conn.execute(
            text(
                "CREATE VIRTUAL TABLE IF NOT EXISTS artifact_fts "
                "USING fts5(title, body, artifact_id UNINDEXED, "
                "type UNINDEXED, project_id UNINDEXED)"
            )
        )
        conn.execute(
            text(
                "CREATE VIRTUAL TABLE IF NOT EXISTS chunk_fts "
                "USING fts5(body, chunk_id UNINDEXED, artifact_id UNINDEXED)"
            )
        )
        conn.execute(
            text(
                "CREATE VIRTUAL TABLE IF NOT EXISTS repository_chunk_fts "
                "USING fts5(body, chunk_id UNINDEXED, file_id UNINDEXED, "
                "snapshot_id UNINDEXED, project_id UNINDEXED)"
            )
        )


def get_session() -> Session:
    return Session(engine)
