"""Environment + per-function model configuration.

Every pipeline function has a default (provider, model). Any of these can be
overridden at runtime via the Settings table (see `settings` helpers), so
switching a function from a local Ollama model to a frontier API is a UI change,
not a code change.
"""
from __future__ import annotations

import os
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    redis_url: str = "redis://localhost:6379/0"
    ollama_base_url: str = "http://localhost:11434"
    anthropic_api_key: str = ""
    gemini_api_key: str = ""
    elevenlabs_api_key: str = ""

    library_dir: Path = Path("./data/library")
    media_dir: Path = Path("./data/media")
    host_media_mount: Path = Path("/host-media")
    db_path: Path = Path("./data/db/vst.sqlite3")


settings = Settings()

# Providers: "ollama" (local, OpenAI-compatible), "anthropic", "gemini".
# The value here is the *shipping default*; the Settings table can override it.
FUNCTION_DEFAULTS: dict[str, dict[str, str]] = {
    "correct":        {"provider": "ollama",    "model": "qwen3:8b"},
    "summarize":      {"provider": "anthropic", "model": "claude-sonnet-5"},
    "deepdive_claude": {"provider": "anthropic", "model": "claude-sonnet-5"},
    "deepdive_gemini": {"provider": "gemini",    "model": "gemini-3.5-flash"},
    "merge":          {"provider": "anthropic", "model": "claude-sonnet-5"},
    "quickref":       {"provider": "anthropic", "model": "claude-sonnet-5"},
    "podcast_script": {"provider": "anthropic", "model": "claude-sonnet-5"},
    "trim_spans":     {"provider": "ollama",    "model": "qwen3:8b"},
    "mindmap":        {"provider": "anthropic", "model": "claude-sonnet-5"},
    "tag":            {"provider": "ollama",    "model": "qwen3:8b"},
    # ASR + TTS are not chat providers; handled by their own tasks.
    "asr":            {"provider": "faster-whisper", "model": "distil-large-v3"},
    "tts":            {"provider": "kokoro",   "model": "kokoro-82m"},
}

# Advanced tuning knobs (Settings → Advanced). Stored per-group in the Settings
# table under advanced.<group>; these are the shipping defaults.
ADVANCED_DEFAULTS: dict[str, dict] = {
    "audio": {
        "tts_speed": 1.0,        # Kokoro speaking rate multiplier
        "tts_gap": 0.4,          # seconds of silence between dialogue lines
        "tts_workers": 0,        # parallel TTS synthesis workers; 0 = auto
        "trim_db": -40,          # silenceremove threshold (dBFS)
        "trim_silence": 1.5,     # minimum silence duration to remove (s)
    },
    "pipeline": {
        "chunk_chars": 24000,    # transcript chunk size for the correction pass
        "deepdive_depth": "standard",   # concise | standard | exhaustive
        "podcast_segments": 0,   # target segment count; 0 = model decides
        "max_tags": 8,           # tags per artifact
        "allow_new_tags": True,  # let the tagger extend the vocabulary
    },
    "asr": {
        "vad": True,             # voice-activity-detection filter
        "language": "",          # language hint; "" = auto-detect
    },
    "compute": {
        # faster-whisper device: "auto" picks CUDA when available, else CPU.
        # GPU use additionally requires the docker-compose.gpu.yml overlay
        # (NVIDIA runtime + CUDA libraries in the worker image).
        "whisper_device": "auto",        # auto | cpu | cuda
        "whisper_compute_type": "auto",  # auto | int8 | int8_float16 | float16
        # Kokoro TTS ONNX Runtime execution provider. "auto" uses CUDA only when
        # onnxruntime exposes it; the shipped images use CPU onnxruntime (see the
        # Dockerfile note), so this is CPU in practice — Piper is the fast path.
        "kokoro_device": "auto",         # auto | cpu | cuda
    },
}


def advanced(group: str) -> dict:
    """Effective advanced settings for a group: defaults + stored overrides."""
    from .settings_store import get_setting

    merged = dict(ADVANCED_DEFAULTS[group])
    merged.update(get_setting(f"advanced.{group}") or {})
    return merged


# Seed tag vocabulary (cyber / sysadmin oriented). Users can extend/edit in UI.
SEED_TAGS: list[tuple[str, str]] = [
    ("networking", "domain"), ("kubernetes", "tech"), ("ansible", "tool"),
    ("vmware", "tech"), ("forensics", "domain"), ("red-team", "domain"),
    ("blue-team", "domain"), ("cloud", "domain"), ("linux", "tech"),
    ("windows", "tech"), ("scripting", "domain"), ("containers", "tech"),
    ("firewall", "tech"), ("active-directory", "tech"), ("wireshark", "tool"),
    ("nmap", "tool"), ("docker", "tool"), ("terraform", "tool"),
    ("incident-response", "technique"), ("threat-hunting", "technique"),
    ("penetration-testing", "technique"), ("hardening", "technique"),
]
