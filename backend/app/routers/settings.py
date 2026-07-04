from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlmodel import select, text

from ..config import ADVANCED_DEFAULTS, FUNCTION_DEFAULTS, advanced
from ..db import get_session
from ..models import Job, Tag
from ..settings_store import get_setting, set_setting
from ..tasks.prompts import DEFAULTS as PROMPT_DEFAULTS, PROMPT_LABELS

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


# --- advanced: prompt editor ---

@router.get("/prompts")
def get_prompts():
    out = {}
    for name, default in PROMPT_DEFAULTS.items():
        override = get_setting(f"prompt.{name}")
        out[name] = {
            "label": PROMPT_LABELS.get(name, name),
            "value": override or default,
            "modified": bool(override),
        }
    return out


class PromptOverride(BaseModel):
    value: str


@router.put("/prompts/{name}")
def set_prompt(name: str, req: PromptOverride):
    if name not in PROMPT_DEFAULTS:
        raise HTTPException(400, f"unknown prompt {name!r}")
    if req.value.strip() and req.value.strip() != PROMPT_DEFAULTS[name].strip():
        set_setting(f"prompt.{name}", req.value)
    else:
        set_setting(f"prompt.{name}", None)
    return {"ok": True}


@router.delete("/prompts/{name}")
def reset_prompt(name: str):
    if name not in PROMPT_DEFAULTS:
        raise HTTPException(400, f"unknown prompt {name!r}")
    set_setting(f"prompt.{name}", None)
    return {"ok": True, "default": PROMPT_DEFAULTS[name]}


# --- advanced: per-function generation params ---

@router.get("/params")
def get_params():
    return {fn: get_setting(f"params.{fn}") or {} for fn in FUNCTION_DEFAULTS}


class Params(BaseModel):
    temperature: float | None = None
    max_tokens: int | None = None


@router.put("/params/{function}")
def set_params(function: str, req: Params):
    if function not in FUNCTION_DEFAULTS:
        raise HTTPException(400, f"unknown function {function!r}")
    payload = {k: v for k, v in req.model_dump().items() if v is not None}
    set_setting(f"params.{function}", payload or None)
    return {"ok": True}


# --- advanced: audio / pipeline / asr knob groups ---

@router.get("/advanced")
def get_advanced():
    return {
        "groups": {g: advanced(g) for g in ADVANCED_DEFAULTS},
        "defaults": ADVANCED_DEFAULTS,
    }


class AdvancedGroup(BaseModel):
    values: dict


@router.put("/advanced/{group}")
def set_advanced(group: str, req: AdvancedGroup):
    if group not in ADVANCED_DEFAULTS:
        raise HTTPException(400, f"unknown group {group!r}")
    allowed = set(ADVANCED_DEFAULTS[group])
    values = {k: v for k, v in req.values.items() if k in allowed}
    set_setting(f"advanced.{group}", values or None)
    return {"ok": True}


# --- advanced: cloud storage ---

@router.get("/cloud")
def get_cloud():
    from ..tasks.cloud import FIELDS

    provider = get_setting("cloud.provider") or ""
    cfg = get_setting("cloud.config") or {}
    masked = {}
    if provider in FIELDS:
        for field, secret in FIELDS[provider].items():
            masked[field] = ("•set•" if cfg.get(field) else "") if secret \
                else cfg.get(field, "")
    return {
        "provider": provider,
        "providers": list(FIELDS),
        "fields": FIELDS.get(provider, {}),
        "all_fields": FIELDS,
        "config": masked,
        "remote_base": get_setting("cloud.remote_base") or "synapse",
        "auto": bool(get_setting("cloud.auto")),
        "last_sync": get_setting("cloud.last_sync"),
    }


class CloudConfig(BaseModel):
    provider: str
    config: dict[str, str] = {}
    remote_base: str = "synapse"
    auto: bool = False


@router.put("/cloud")
def set_cloud(req: CloudConfig):
    from ..tasks.cloud import FIELDS

    if req.provider and req.provider not in FIELDS:
        raise HTTPException(400, f"unknown provider {req.provider!r}")
    existing = get_setting("cloud.config") or {}
    merged = dict(existing) if req.provider == get_setting("cloud.provider") else {}
    for field, secret in FIELDS.get(req.provider, {}).items():
        incoming = req.config.get(field, "")
        if secret and incoming in ("", "•set•"):
            continue  # keep the stored secret unless a new one is supplied
        merged[field] = incoming
    set_setting("cloud.provider", req.provider or None)
    set_setting("cloud.config", merged or None)
    set_setting("cloud.remote_base", req.remote_base)
    set_setting("cloud.auto", req.auto)
    return {"ok": True}


@router.post("/cloud/sync")
def cloud_sync_now():
    if not get_setting("cloud.provider"):
        raise HTTPException(400, "configure a cloud provider first")
    from ..tasks.celery_app import celery

    with get_session() as session:
        job = Job(project_id=None, task="cloud_sync_all")
        session.add(job)
        session.commit()
        session.refresh(job)
        async_result = celery.send_task("cloud_sync_all", args=[job.id])
        job.celery_id = async_result.id
        session.add(job)
        session.commit()
        return job


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
    # cached project tag sets may hold the old name — recompute on next run
    from ..settings_store import delete_settings_prefix

    delete_settings_prefix("projtags.")
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
    # cached project tag sets may resurrect the deleted tag — invalidate them
    from ..settings_store import delete_settings_prefix

    delete_settings_prefix("projtags.")
    return {"ok": True}
