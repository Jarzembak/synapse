from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlmodel import select, text

from ..config import FUNCTION_DEFAULTS
from ..db import get_session
from ..models import Tag
from ..settings_store import get_setting, set_setting

router = APIRouter(prefix="/api/settings", tags=["settings"])

PROVIDERS = ["ollama", "anthropic", "gemini"]


@router.get("/models")
def get_models():
    out = {}
    for fn, default in FUNCTION_DEFAULTS.items():
        out[fn] = get_setting(f"model.{fn}") or default
    return {"functions": out, "providers": PROVIDERS, "defaults": FUNCTION_DEFAULTS}


class ModelOverride(BaseModel):
    provider: str
    model: str


@router.put("/models/{function}")
def set_model(function: str, req: ModelOverride):
    if function not in FUNCTION_DEFAULTS:
        raise HTTPException(400, f"unknown function {function!r}")
    set_setting(f"model.{function}", {"provider": req.provider, "model": req.model})
    return {"ok": True}


@router.get("/glossary")
def get_glossary():
    return {"terms": get_setting("glossary", [])}


class Glossary(BaseModel):
    terms: list[str]


@router.put("/glossary")
def set_glossary(req: Glossary):
    set_setting("glossary", sorted({t.strip() for t in req.terms if t.strip()}))
    return {"ok": True}


@router.get("/voices")
def get_voices():
    return {
        "kokoro": get_setting("tts.voices", {"HOST_A": "am_michael", "HOST_B": "af_heart"}),
        "gemini": get_setting("tts.gemini_voices", {"HOST_A": "Charon", "HOST_B": "Kore"}),
    }


class Voices(BaseModel):
    kokoro: dict[str, str] | None = None
    gemini: dict[str, str] | None = None


@router.put("/voices")
def set_voices(req: Voices):
    if req.kokoro:
        set_setting("tts.voices", req.kokoro)
    if req.gemini:
        set_setting("tts.gemini_voices", req.gemini)
    return {"ok": True}


@router.get("/download")
def get_download():
    return {"max_height": get_setting("download.max_height", 1080)}


class DownloadPrefs(BaseModel):
    max_height: int  # 0 = best available


@router.put("/download")
def set_download(req: DownloadPrefs):
    if req.max_height < 0:
        raise HTTPException(400, "max_height must be >= 0 (0 = best)")
    set_setting("download.max_height", req.max_height)
    return {"ok": True}


# --- tag vocabulary management ---

tags_router = APIRouter(prefix="/api/tags", tags=["tags"])


@tags_router.get("")
def list_tags():
    with get_session() as session:
        tags = session.exec(select(Tag).order_by(Tag.name)).all()
        counts = dict(session.exec(
            text("SELECT tag_id, COUNT(*) FROM artifacttag GROUP BY tag_id")
        ).all())
        return [{**t.model_dump(), "count": counts.get(t.id, 0)} for t in tags]


class TagCreate(BaseModel):
    name: str
    kind: str = "topic"


@tags_router.post("")
def create_tag(req: TagCreate):
    from .. import library

    with get_session() as session:
        name = library.make_slug(req.name)
        if session.exec(select(Tag).where(Tag.name == name)).first():
            raise HTTPException(409, "tag exists")
        tag = Tag(name=name, kind=req.kind)
        session.add(tag)
        session.commit()
        session.refresh(tag)
        return tag


class TagRename(BaseModel):
    name: str


@tags_router.put("/{tag_id}")
def rename_tag(tag_id: int, req: TagRename):
    """Rename propagates: merges into an existing tag of the new name if any."""
    from .. import library
    from ..models import Artifact

    with get_session() as session:
        tag = session.get(Tag, tag_id)
        if not tag:
            raise HTTPException(404)
        new_name = library.make_slug(req.name)
        existing = session.exec(select(Tag).where(Tag.name == new_name)).first()
        if existing and existing.id != tag_id:
            session.exec(text(
                "UPDATE OR IGNORE artifacttag SET tag_id = :new WHERE tag_id = :old"
            ).bindparams(new=existing.id, old=tag_id))
            session.exec(text("DELETE FROM artifacttag WHERE tag_id = :old")
                         .bindparams(old=tag_id))
            session.delete(tag)
            keep = existing
        else:
            tag.name = new_name
            session.add(tag)
            keep = tag
        session.commit()

        # rewrite frontmatter tag lists of affected artifacts
        ids = [r[0] for r in session.exec(
            text("SELECT artifact_id FROM artifacttag WHERE tag_id = :id")
            .bindparams(id=keep.id)
        ).all()]
        for aid in ids:
            art = session.get(Artifact, aid)
            if art:
                library.apply_tags(session, art, library.current_tags(session, aid))
    return {"ok": True}


@tags_router.delete("/{tag_id}")
def delete_tag(tag_id: int):
    with get_session() as session:
        tag = session.get(Tag, tag_id)
        if not tag:
            raise HTTPException(404)
        session.exec(text("DELETE FROM artifacttag WHERE tag_id = :id").bindparams(id=tag_id))
        session.delete(tag)
        session.commit()
    return {"ok": True}
