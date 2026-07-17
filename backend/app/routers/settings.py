from __future__ import annotations

import re

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlmodel import select, text

from ..config import ADVANCED_DEFAULTS, FUNCTION_DEFAULTS, advanced, settings
from ..db import get_session
from ..models import Job, Tag
from ..settings_store import get_setting, set_setting
from ..tasks.prompts import DEFAULTS as PROMPT_DEFAULTS, PROMPT_LABELS

router = APIRouter(prefix="/api/settings", tags=["settings"])

PROVIDERS = ["ollama", "openai_compat", "anthropic", "gemini"]
FUNCTION_PROVIDERS = {
    **{name: PROVIDERS for name in FUNCTION_DEFAULTS},
    "asr": ["faster-whisper", "gemini"],
    "tts": ["piper", "kokoro", "gemini"],
}

# Ollama keep-alive: a bare number ("300" seconds, "-1" keep forever — sent as
# JSON numbers, the only form Ollama gives those semantics) or a Go duration
# ("5m", "24h", "1h30m", "500ms").
KEEP_ALIVE_RE = re.compile(
    r"^-?(\d+(\.\d+)?|(\d+(\.\d+)?(ns|us|µs|ms|s|m|h))+)$")


@router.get("/models")
def get_models():
    out = {}
    for fn, default in FUNCTION_DEFAULTS.items():
        out[fn] = get_setting(f"model.{fn}") or default
    return {
        "functions": out, "providers": PROVIDERS, "defaults": FUNCTION_DEFAULTS,
        "provider_options": FUNCTION_PROVIDERS,
    }


@router.get("/local-models")
def local_models():
    """Installed models on each local server, for the model-matrix dropdowns.
    Best-effort: an unreachable server reports ok=False rather than erroring."""
    out: dict[str, dict] = {}
    try:
        response = httpx.get(f"{settings.ollama_base_url}/api/tags", timeout=3)
        response.raise_for_status()
        names = sorted(item.get("name", "") for item in response.json().get("models", []))
        out["ollama"] = {"configured": True, "ok": True,
                         "models": [name for name in names if name], "detail": ""}
    except Exception as exc:
        out["ollama"] = {"configured": True, "ok": False, "models": [],
                         "detail": str(exc)[:300]}
    base = (settings.openai_compat_base_url or "").rstrip("/")
    if not base:
        out["openai_compat"] = {"configured": False, "ok": False, "models": [],
                                "detail": "OPENAI_COMPAT_BASE_URL is not set"}
    else:
        try:
            headers = {}
            if settings.openai_compat_api_key:
                headers["Authorization"] = f"Bearer {settings.openai_compat_api_key}"
            response = httpx.get(f"{base}/models", headers=headers, timeout=3)
            response.raise_for_status()
            names = sorted(item.get("id", "") for item in response.json().get("data", []))
            out["openai_compat"] = {"configured": True, "ok": True,
                                    "models": [name for name in names if name],
                                    "detail": ""}
        except Exception as exc:
            out["openai_compat"] = {"configured": True, "ok": False, "models": [],
                                    "detail": str(exc)[:300]}
    return out


class ModelOverride(BaseModel):
    provider: str
    model: str


@router.put("/models/{function}")
def set_model(function: str, req: ModelOverride):
    if function not in FUNCTION_DEFAULTS:
        raise HTTPException(400, f"unknown function {function!r}")
    provider, model = req.provider.strip(), req.model.strip()
    if provider not in FUNCTION_PROVIDERS[function]:
        raise HTTPException(
            400, f"provider {provider!r} is not valid for {function}; "
                 f"choose from {', '.join(FUNCTION_PROVIDERS[function])}")
    if not model:
        raise HTTPException(400, "model cannot be blank")
    set_setting(f"model.{function}", {"provider": provider, "model": model})
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
        "piper": get_setting("tts.piper_voices", {"HOST_A": "en_US-ryan-medium", "HOST_B": "en_US-amy-medium"}),
        "gemini": get_setting("tts.gemini_voices", {"HOST_A": "Charon", "HOST_B": "Kore"}),
    }


class Voices(BaseModel):
    kokoro: dict[str, str] | None = None
    piper: dict[str, str] | None = None
    gemini: dict[str, str] | None = None


@router.put("/voices")
def set_voices(req: Voices):
    if req.kokoro:
        set_setting("tts.voices", req.kokoro)
    if req.piper:
        set_setting("tts.piper_voices", req.piper)
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
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_tokens: int | None = Field(default=None, ge=1, le=200_000)


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


def _validated_advanced(group: str, values: dict) -> dict:
    clean = {key: value for key, value in values.items()
             if key in ADVANCED_DEFAULTS[group]}
    numeric = {
        ("audio", "tts_speed"): (0.25, 3.0),
        ("audio", "tts_gap"): (0.05, 10.0),
        ("audio", "tts_workers"): (0, 32),
        ("audio", "trim_db"): (-100, 0),
        ("audio", "trim_silence"): (0.1, 60),
        ("pipeline", "chunk_chars"): (1_000, 500_000),
        ("pipeline", "podcast_segments"): (0, 100),
        ("pipeline", "max_tags"): (1, 50),
        ("local", "num_ctx"): (1_024, 262_144),
        ("local", "timeout_seconds"): (30, 3_600),
    }
    enums = {
        ("pipeline", "deepdive_depth"): {"concise", "standard", "exhaustive"},
        ("compute", "whisper_device"): {"auto", "cpu", "cuda"},
        ("compute", "whisper_compute_type"): {
            "auto", "int8", "int8_float16", "float16"},
        ("compute", "kokoro_device"): {"auto", "cpu", "cuda"},
        ("local", "think"): {"auto", "on", "off"},
    }
    booleans = {
        ("pipeline", "allow_new_tags"), ("asr", "vad"),
        ("audio", "keep_intermediates"), ("local", "json_mode"),
    }
    for key, value in clean.items():
        rule = numeric.get((group, key))
        if rule:
            if not isinstance(value, (int, float)) or isinstance(value, bool) \
                    or not rule[0] <= value <= rule[1]:
                raise HTTPException(422, f"{key} must be between {rule[0]} and {rule[1]}")
        allowed = enums.get((group, key))
        if allowed and value not in allowed:
            raise HTTPException(422, f"{key} must be one of {', '.join(sorted(allowed))}")
        if (group, key) in booleans and not isinstance(value, bool):
            raise HTTPException(422, f"{key} must be true or false")
    if group == "asr" and not isinstance(clean.get("language", ""), str):
        raise HTTPException(422, "language must be a string")
    if group == "local" and "keep_alive" in clean:
        keep_alive = clean["keep_alive"]
        if not isinstance(keep_alive, str) or \
                (keep_alive and not KEEP_ALIVE_RE.match(keep_alive)):
            raise HTTPException(
                422, 'keep_alive must be an Ollama duration like "5m", "24h", '
                     '"0", or "-1" (blank = server default)')
    return clean


@router.put("/advanced/{group}")
def set_advanced(group: str, req: AdvancedGroup):
    if group not in ADVANCED_DEFAULTS:
        raise HTTPException(400, f"unknown group {group!r}")
    values = _validated_advanced(group, req.values)
    set_setting(f"advanced.{group}", values or None)
    return {"ok": True}


# --- pipeline profiles / search / backup ---


@router.get("/profiles")
def get_profiles():
    from ..tasks.orchestrate import pipeline_profiles

    return pipeline_profiles()


class ProfileConfig(BaseModel):
    label: str
    description: str = ""
    steps: list[str]


@router.put("/profiles/{key}")
def save_profile(key: str, req: ProfileConfig):
    from .. import library
    from ..tasks.orchestrate import BUILTIN_PROFILES, STEP_NAMES

    clean_key = library.make_slug(key)
    if clean_key in BUILTIN_PROFILES:
        raise HTTPException(400, "built-in profiles cannot be overwritten")
    unknown = set(req.steps) - STEP_NAMES
    if unknown:
        raise HTTPException(422, f"unknown step(s): {', '.join(sorted(unknown))}")
    if not req.label.strip() or not req.steps:
        raise HTTPException(422, "profile label and at least one step are required")
    profiles = get_setting("pipeline.profiles") or {}
    profiles[clean_key] = {
        "label": req.label.strip(), "description": req.description.strip(),
        "steps": list(dict.fromkeys(req.steps)),
    }
    set_setting("pipeline.profiles", profiles)
    return {"key": clean_key, **profiles[clean_key], "custom": True}


@router.delete("/profiles/{key}")
def delete_profile(key: str):
    from ..tasks.orchestrate import BUILTIN_PROFILES

    if key in BUILTIN_PROFILES:
        raise HTTPException(400, "built-in profiles cannot be deleted")
    profiles = get_setting("pipeline.profiles") or {}
    if key not in profiles:
        raise HTTPException(404)
    del profiles[key]
    set_setting("pipeline.profiles", profiles or None)
    return {"ok": True}


class SearchConfig(BaseModel):
    semantic_enabled: bool = False
    embedding_provider: str = "ollama"
    embedding_model: str = "nomic-embed-text"


@router.get("/search")
def get_search_settings():
    return {
        "semantic_enabled": bool(get_setting("search.semantic_enabled", False)),
        "embedding_provider": get_setting("search.embedding_provider", "ollama"),
        "embedding_model": get_setting("search.embedding_model", "nomic-embed-text"),
    }


@router.put("/search")
def save_search_settings(req: SearchConfig):
    if req.embedding_provider not in ("ollama", "openai_compat"):
        raise HTTPException(422, "embedding provider must be ollama or openai_compat")
    model = req.embedding_model.strip()
    if req.semantic_enabled and not model:
        raise HTTPException(422, "an embedding model is required")
    set_setting("search.semantic_enabled", req.semantic_enabled)
    set_setting("search.embedding_provider", req.embedding_provider)
    set_setting("search.embedding_model", model or "nomic-embed-text")
    return {"ok": True, **req.model_dump()}


class BackupConfig(BaseModel):
    retention: int = Field(default=5, ge=1, le=100)
    schedule_hours: int = Field(default=0, ge=0, le=24 * 30)
    include_media: bool = True


@router.get("/backup")
def get_backup_settings():
    return {
        "retention": get_setting("backup.retention", 5),
        "schedule_hours": get_setting("backup.schedule_hours", 0),
        "include_media": get_setting("backup.include_media", True),
        "last": get_setting("backup.last"),
    }


@router.put("/backup")
def save_backup_settings(req: BackupConfig):
    for key, value in req.model_dump().items():
        set_setting(f"backup.{key}", value)
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
        # idempotent: two concurrent full syncs both upload files the other
        # hasn't finished yet, and cloud backends like Google Drive allow
        # same-name duplicates — so return the in-flight sync instead of racing
        # a second one.
        existing = session.exec(
            select(Job).where(Job.task == "cloud_sync_all",
                              Job.status.in_(("queued", "running")))
            .order_by(Job.created)
        ).first()
        if existing:
            return existing.model_dump()
        job = Job(project_id=None, task="cloud_sync_all")
        session.add(job)
        session.commit()
        session.refresh(job)
        try:
            async_result = celery.send_task("cloud_sync_all", args=[job.id])
            job.celery_id = async_result.id
        except Exception as e:
            # never leave a 'queued' row with no task behind it — the idempotency
            # guard would then return this ghost forever and block real syncs
            job.status = "error"
            job.error = f"could not dispatch: {e}"[:2000]
            session.add(job)
            session.commit()
            raise HTTPException(503, f"could not queue sync: {e}")
        session.add(job)
        session.commit()
        session.refresh(job)
        # return a dict, not the ORM row — its attributes expire after commit and
        # the session closes before FastAPI serializes, which yielded an empty {}
        return job.model_dump()


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
