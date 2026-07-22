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
    # Any OpenAI-compatible server (LM Studio, llama.cpp, vLLM, LocalAI, …).
    # Include the /v1 suffix, e.g. http://host.docker.internal:1234/v1.
    openai_compat_base_url: str = ""
    openai_compat_api_key: str = ""
    anthropic_api_key: str = ""
    gemini_api_key: str = ""
    elevenlabs_api_key: str = ""
    allow_private_urls: bool = False

    library_dir: Path = Path("./data/library")
    media_dir: Path = Path("./data/media")
    host_media_mount: Path = Path("/host-media")
    db_path: Path = Path("./data/db/vst.sqlite3")
    backup_dir: Path = Path("./data/backups")
    repository_dir: Path = Path("./data/repositories")
    backup_encryption_key: str = ""
    settings_encryption_key: str = ""
    max_upload_bytes: int = 20 * 1024 * 1024 * 1024
    max_repository_download_bytes: int = 512 * 1024 * 1024
    max_repository_unpacked_bytes: int = 1024 * 1024 * 1024
    max_repository_files: int = 50_000
    # Archives may legitimately contain large binary assets; they are retained
    # but excluded from analysis by the much smaller text-file limit below.
    max_repository_file_bytes: int = 256 * 1024 * 1024
    max_repository_text_file_bytes: int = 5 * 1024 * 1024
    max_repository_indexed_bytes: int = 250 * 1024 * 1024
    repository_chunk_lines: int = 200
    repository_chunk_chars: int = 24_000
    repository_max_compression_ratio: int = 200
    repository_max_map_chunks: int = 64
    repository_max_map_input_chars: int = 800_000
    repository_local_model: str = "qwen3:8b"
    # Research-paper v1 intentionally fails at explicit, reviewable limits.
    # Inputs inside these limits are mapped completely; the paper pipeline
    # never converts an oversized source into a hidden prefix/sample.
    max_paper_upload_bytes: int = 250 * 1024 * 1024
    max_paper_pages: int = 500
    max_paper_extracted_chars: int = 5_000_000
    paper_ocr_languages: str = "eng,spa,fra,deu"
    paper_target_minutes: int = 50
    paper_min_minutes: int = 40
    paper_max_minutes: int = 60
    paper_max_parts: int = 5


settings = Settings()


def validate_storage_roots() -> None:
    """Raw repository snapshots must never sit inside a synced/backup tree."""
    repository = settings.repository_dir.resolve()
    protected = {
        "library": settings.library_dir.resolve(),
        "media": settings.media_dir.resolve(),
        "backups": settings.backup_dir.resolve(),
        "database": settings.db_path.resolve(),
    }
    for label, path in protected.items():
        overlap = (
            repository == path
            or repository in path.parents
            or path in repository.parents
        )
        if overlap:
            raise RuntimeError(
                f"REPOSITORY_DIR must not overlap the {label} storage path"
            )


# Providers: "ollama" (local, native API), "openai_compat" (any local
# OpenAI-compatible server), "anthropic", "gemini".
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
    "library_qa":     {"provider": "anthropic", "model": "claude-sonnet-5"},
    # Every repository job enforces the local boundary independently of these
    # defaults at the LLM call boundary, regardless of GitHub visibility.
    "repository_map":          {"provider": "ollama", "model": "qwen3:8b"},
    "repository_inventory":    {"provider": "ollama", "model": "qwen3:8b"},
    "repository_overview":     {"provider": "ollama", "model": "qwen3:8b"},
    "repository_usage":        {"provider": "ollama", "model": "qwen3:8b"},
    "repository_architecture": {"provider": "ollama", "model": "qwen3:8b"},
    "repository_expertise":    {"provider": "ollama", "model": "qwen3:8b"},
    "repository_environment":  {"provider": "ollama", "model": "qwen3:8b"},
    # Paper leaf maps are deliberately local by default. Cloud-enabled paper
    # projects may use the configured synthesis models, while project_scope()
    # forces every function back to the validated local model when local_only
    # is set on the paper source.
    "paper_map":       {"provider": "ollama",    "model": "qwen3:8b"},
    "paper_reduce":    {"provider": "anthropic", "model": "claude-sonnet-5"},
    "paper_synthesis": {"provider": "anthropic", "model": "claude-sonnet-5"},
    "paper_plan":      {"provider": "anthropic", "model": "claude-sonnet-5"},
    "paper_script":    {"provider": "anthropic", "model": "claude-sonnet-5"},
    "paper_memory":    {"provider": "ollama",    "model": "qwen3:8b"},
    # ASR + TTS are not chat providers; handled by their own tasks.
    "asr":            {"provider": "faster-whisper", "model": "distil-large-v3"},
    "tts":            {"provider": "piper",    "model": "en_US-ryan-medium"},
}

# Advanced tuning knobs (Settings → Advanced). Stored per-group in the Settings
# table under advanced.<group>; these are the shipping defaults.
ADVANCED_DEFAULTS: dict[str, dict] = {
    "audio": {
        "tts_speed": 1.0,        # Kokoro speaking rate multiplier
        "tts_gap": 0.4,          # seconds of silence between dialogue lines
        "tts_workers": 0,        # parallel TTS synthesis workers; 0 = auto
        "keep_intermediates": False,
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
    "local": {
        # Ollama loads models with a small default context window (4k in current
        # releases) and silently truncates anything longer — far below the ~24k-char
        # transcript chunks the correction pass sends. num_ctx is requested per
        # call, so no Modelfile edits are needed. Raising it raises RAM/VRAM use.
        "num_ctx": 16384,        # Ollama context window (tokens); ollama only
        "keep_alive": "5m",      # how long Ollama keeps the model loaded ("-1" = forever)
        "think": "auto",         # thinking models: auto = model default | on | off
        "timeout_seconds": 300,  # per-request timeout for local providers
        "json_mode": True,       # enforce native JSON output on structured steps
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
