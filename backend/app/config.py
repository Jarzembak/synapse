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
    "summarize":      {"provider": "ollama",    "model": "qwen3:8b"},
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
