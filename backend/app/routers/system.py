"""System resource monitor: host CPU/RAM (psutil), GPU (nvidia-smi), and which
models Ollama currently has resident (its /api/ps), streamed over SSE.

Served from the API container, but psutil reads the host-wide /proc, so CPU/RAM
reflect the whole box — including whatever the worker's TTS/ASR step is doing.
GPU stats need nvidia-smi in this container (the GPU overlay grants it); without
it the gpus list is simply empty, which itself answers 'is the GPU in use?'.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess

import httpx
import psutil
from fastapi import APIRouter
from sse_starlette.sse import EventSourceResponse

from ..config import settings
from ..db import get_session
from ..models import Job, LLMCall, Project
from ..tasks.celery_app import celery
from sqlmodel import select, text

router = APIRouter(prefix="/api/system", tags=["system"])


def _num(s: str) -> float | None:
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _gpus() -> list[dict]:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return []
    try:
        proc = subprocess.run(
            [exe, "--query-gpu=index,name,utilization.gpu,memory.used,"
                  "memory.total,temperature.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=3,
        )
        if proc.returncode != 0:
            return []
        gpus = []
        for line in proc.stdout.strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 6:
                continue
            idx, name, util, used, total, temp = parts[:6]
            gpus.append({
                "index": int(idx) if idx.isdigit() else 0,
                "name": name,
                "util_percent": _num(util),
                "mem_used_mb": _num(used),
                "mem_total_mb": _num(total),
                "temp_c": _num(temp),
            })
        return gpus
    except Exception:
        return []


def _ollama_models() -> list[dict]:
    """Resident models from Ollama's /api/ps, with a GPU/CPU/hybrid tag derived
    from how much of each model is loaded into VRAM."""
    try:
        r = httpx.get(f"{settings.ollama_base_url}/api/ps", timeout=1.5)
        r.raise_for_status()
        out = []
        for m in r.json().get("models", []):
            size = m.get("size") or 0
            vram = m.get("size_vram") or 0
            if vram <= 0:
                where = "cpu"
            elif vram >= size:
                where = "gpu"
            else:
                where = "hybrid"
            out.append({
                "name": m.get("name") or m.get("model") or "?",
                "size_mb": round(size / 1_048_576),
                "vram_mb": round(vram / 1_048_576),
                "processor": where,
            })
        return out
    except Exception:
        return []


def _snapshot(cpu_interval: float | None = None) -> dict:
    vm = psutil.virtual_memory()
    per_core = psutil.cpu_percent(interval=cpu_interval, percpu=True)
    try:
        disk = shutil.disk_usage(settings.library_dir)
        disk_stats = {
            "used_mb": round(disk.used / 1_048_576),
            "total_mb": round(disk.total / 1_048_576),
            "percent": round(disk.used / disk.total * 100, 1) if disk.total else 0,
        }
    except OSError:
        disk_stats = None
    with get_session() as session:
        active_jobs = len(session.exec(
            select(Job.id).where(Job.status.in_(("queued", "running")))
        ).all())
    return {
        "cpu_percent": round(sum(per_core) / len(per_core), 1) if per_core else 0.0,
        "cpu_per_core": [round(c, 1) for c in per_core],
        "cpu_count": psutil.cpu_count(),
        "mem_used_mb": round(vm.used / 1_048_576),
        "mem_total_mb": round(vm.total / 1_048_576),
        "mem_percent": vm.percent,
        "gpus": _gpus(),
        "ollama_models": _ollama_models(),
        "disk": disk_stats,
        "active_jobs": active_jobs,
    }


def _preflight() -> dict:
    checks: list[dict] = []
    repository_projects_exist = False

    def add(name: str, ok: bool, detail: str, required: bool = True):
        checks.append({"name": name, "ok": ok, "detail": detail, "required": required})

    try:
        with get_session() as session:
            session.exec(text("SELECT 1")).one()
            repository_projects_exist = bool(session.exec(select(Project.id).where(
                Project.source_type == "github"
            )).first())
        add("database", True, "SQLite is writable and reachable")
    except Exception as exc:
        add("database", False, str(exc))
    try:
        import redis

        redis.Redis.from_url(settings.redis_url, socket_timeout=2).ping()
        add("redis", True, "queue broker is reachable")
    except Exception as exc:
        add("redis", False, str(exc))
    try:
        replies = celery.control.ping(timeout=1)
        add("worker", bool(replies), f"{len(replies)} worker(s) responding")
    except Exception as exc:
        add("worker", False, str(exc))
    for command in ("ffmpeg", "ffprobe", "rclone"):
        path = shutil.which(command)
        add(command, bool(path), path or "not installed", required=command != "rclone")
    try:
        response = httpx.get(f"{settings.ollama_base_url}/api/tags", timeout=2)
        response.raise_for_status()
        models = [item.get("name", "") for item in response.json().get("models", [])]
        add("ollama", True, f"{len(models)} model(s) installed", required=False)
        from ..search import embedding_model

        embed = embedding_model()
        add("embedding model", any(name == embed or name.startswith(embed + ":") for name in models),
            f"{embed} {'is installed' if any(name == embed or name.startswith(embed + ':') for name in models) else 'must be pulled'}",
            required=False)
        from ..repository import repository_local_model

        try:
            repo_model = repository_local_model()
            repo_model_installed = any(
                name == repo_model or name.startswith(repo_model + ":")
                for name in models)
            add(
                "repository model",
                repo_model_installed,
                f"{repo_model} "
                f"{'is installed' if repo_model_installed else 'must be pulled before repository analysis'}",
                required=repository_projects_exist,
            )
        except ValueError as exc:
            add(
                "repository model", False, str(exc),
                required=repository_projects_exist)
    except Exception as exc:
        add("ollama", False, str(exc), required=False)
        if repository_projects_exist:
            add("repository model", False, "Ollama is unreachable", required=True)
    add("Anthropic key", bool(settings.anthropic_api_key),
        "configured" if settings.anthropic_api_key else "not configured", required=False)
    add("Gemini key", bool(settings.gemini_api_key),
        "configured" if settings.gemini_api_key else "not configured", required=False)
    try:
        disk = shutil.disk_usage(settings.library_dir)
        free_gb = disk.free / (1024 ** 3)
        add("disk space", free_gb >= 2, f"{free_gb:.1f} GB free")
    except OSError as exc:
        add("disk space", False, str(exc))
    return {
        "ready": all(check["ok"] for check in checks if check["required"]),
        "checks": checks,
    }


@router.get("/stats")
async def stats():
    # small blocking sample so a one-shot poll returns a real CPU number
    return await asyncio.to_thread(_snapshot, 0.2)


@router.get("/preflight")
async def preflight():
    return await asyncio.to_thread(_preflight)


@router.get("/usage")
def usage(limit: int = 500):
    with get_session() as session:
        rows = session.exec(
            select(LLMCall).order_by(LLMCall.created.desc()).limit(max(1, min(limit, 5000)))
        ).all()
    grouped: dict[str, dict] = {}
    for row in rows:
        key = f"{row.function}:{row.provider}/{row.model}"
        item = grouped.setdefault(key, {
            "function": row.function, "provider": row.provider, "model": row.model,
            "calls": 0, "errors": 0, "input_tokens": 0, "output_tokens": 0,
            "duration_seconds": 0.0,
        })
        item["calls"] += 1
        item["errors"] += row.status == "error"
        item["input_tokens"] += row.input_tokens
        item["output_tokens"] += row.output_tokens
        item["duration_seconds"] = round(item["duration_seconds"] + row.duration_seconds, 3)
    return {"summary": list(grouped.values()),
            "recent": [row.model_dump() for row in rows[:50]]}


@router.get("/stream")
async def stream():
    async def gen():
        # Each tick takes its own 2s blocking CPU sample (interval > 0), which is
        # self-contained — unlike interval=None it doesn't read/write psutil's
        # process-global baseline, so concurrent System tabs can't skew each
        # other's readings. The 2s sample also paces the stream.
        while True:
            snap = await asyncio.to_thread(_snapshot, 2.0)
            yield {"event": "system", "data": json.dumps(snap)}

    return EventSourceResponse(gen())
