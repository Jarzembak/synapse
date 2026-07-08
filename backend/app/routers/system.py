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
    return {
        "cpu_percent": round(sum(per_core) / len(per_core), 1) if per_core else 0.0,
        "cpu_per_core": [round(c, 1) for c in per_core],
        "cpu_count": psutil.cpu_count(),
        "mem_used_mb": round(vm.used / 1_048_576),
        "mem_total_mb": round(vm.total / 1_048_576),
        "mem_percent": vm.percent,
        "gpus": _gpus(),
        "ollama_models": _ollama_models(),
    }


@router.get("/stats")
async def stats():
    # small blocking sample so a one-shot poll returns a real CPU number
    return await asyncio.to_thread(_snapshot, 0.2)


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
