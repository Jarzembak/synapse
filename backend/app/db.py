"""SQLite engine + FTS5 setup.

The DB is the search/relation index; markdown files under LIBRARY_DIR are the
source of truth for artifact *content*. `ArtifactFTS` is a contentless FTS5
table kept in sync from library.py whenever an artifact body is written.
"""
from __future__ import annotations

from sqlmodel import SQLModel, Session, create_engine, text

from .config import settings

settings.db_path.parent.mkdir(parents=True, exist_ok=True)
engine = create_engine(
    f"sqlite:///{settings.db_path}",
    connect_args={"check_same_thread": False},
)


def init_db() -> None:
    from . import models  # noqa: F401  ensure tables are registered

    SQLModel.metadata.create_all(engine)
    with engine.connect() as conn:
        conn.execute(
            text(
                "CREATE VIRTUAL TABLE IF NOT EXISTS artifact_fts "
                "USING fts5(title, body, artifact_id UNINDEXED, "
                "type UNINDEXED, project_id UNINDEXED)"
            )
        )
        conn.commit()


def get_session() -> Session:
    return Session(engine)
