from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlmodel import select

from .config import SEED_TAGS, settings
from .db import get_session, init_db
from .models import Tag
from .routers import artifacts, jobs, projects, quickrefs
from .routers.settings import router as settings_router, tags_router


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


@app.get("/api/health")
def health():
    return {"ok": True}
