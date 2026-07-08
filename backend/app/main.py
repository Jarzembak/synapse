from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlmodel import select

from .logging_setup import setup_logging

setup_logging()

from .config import SEED_TAGS, settings  # noqa: E402
from .db import get_session, init_db  # noqa: E402
from .models import Tag  # noqa: E402
from .routers import artifacts, jobs, logs, projects, quickrefs, system  # noqa: E402
from .routers.settings import router as settings_router, tags_router  # noqa: E402


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.library_dir.mkdir(parents=True, exist_ok=True)
    settings.media_dir.mkdir(parents=True, exist_ok=True)
    init_db()
    with get_session() as session:
        if not session.exec(select(Tag)).first():
            for name, kind in SEED_TAGS:
                session.add(Tag(name=name, kind=kind))
            session.commit()
    yield


app = FastAPI(title="Synapse", lifespan=lifespan)
app.include_router(projects.router)
app.include_router(jobs.router)
app.include_router(artifacts.router)
app.include_router(quickrefs.router)
app.include_router(settings_router)
app.include_router(tags_router)
app.include_router(logs.router)
app.include_router(system.router)


@app.get("/api/health")
def health():
    return {"ok": True}
