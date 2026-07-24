from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlmodel import select

from .logging_setup import setup_logging

setup_logging()

from .config import SEED_TAGS, settings, validate_storage_roots  # noqa: E402
from .db import get_session, init_db  # noqa: E402
from .models import Tag  # noqa: E402
from .routers import (  # noqa: E402
    artifacts, backup, jobs, logs, projects, quickrefs, recovery, repositories,
    search, system, papers,
)
from .routers.settings import router as settings_router, tags_router  # noqa: E402


@asynccontextmanager
async def lifespan(app: FastAPI):
    validate_storage_roots()
    settings.library_dir.mkdir(parents=True, exist_ok=True)
    settings.media_dir.mkdir(parents=True, exist_ok=True)
    settings.repository_dir.mkdir(parents=True, exist_ok=True)
    init_db()
    from .repository import cleanup_repository_staging

    cleanup_repository_staging()
    from .recovery import recover_interrupted_deletions

    recover_interrupted_deletions()
    from .tasks.cloud import enqueue_pending_privacy_purges

    enqueue_pending_privacy_purges()
    with get_session() as session:
        if not session.exec(select(Tag)).first():
            for name, kind in SEED_TAGS:
                session.add(Tag(name=name, kind=kind))
            session.commit()
    yield


app = FastAPI(title="Synapse", lifespan=lifespan)
app.include_router(projects.router)
app.include_router(repositories.router)
app.include_router(papers.router)
app.include_router(papers.series_router)
app.include_router(jobs.router)
app.include_router(artifacts.router)
app.include_router(quickrefs.router)
app.include_router(search.router)
app.include_router(recovery.router)
app.include_router(backup.router)
app.include_router(settings_router)
app.include_router(tags_router)
app.include_router(logs.router)
app.include_router(system.router)


@app.get("/api/health")
def health():
    return {"ok": True}
