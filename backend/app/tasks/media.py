"""ffmpeg / path helpers shared by ingest, tts and trim."""
from __future__ import annotations

import subprocess
from pathlib import Path

from ..config import settings


def workdir(project_slug: str) -> Path:
    d = settings.media_dir / project_slug
    d.mkdir(parents=True, exist_ok=True)
    return d


def run(cmd: list[str], *, timeout: int = 6 * 3600) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed: {proc.stderr[-2000:]}")


def extract_audio(src: Path, dest: Path) -> Path:
    run(["ffmpeg", "-y", "-i", str(src), "-vn", "-ac", "1", "-ar", "24000", str(dest)])
    return dest


def duration_seconds(path: Path) -> float:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True,
        timeout=60,
    )
    try:
        return float(proc.stdout.strip())
    except ValueError:
        return 0.0


def resolve_local_source(rel: str) -> Path:
    """Validate a user-supplied path against the read-only host media mount."""
    base = settings.host_media_mount.resolve()
    candidate = (base / rel).resolve()
    # is_relative_to enforces a path-component boundary; a bare str.startswith
    # would also accept a sibling like /srv/media-private for base /srv/media.
    if not candidate.is_relative_to(base):
        raise ValueError("path escapes the media mount")
    if not candidate.exists():
        raise FileNotFoundError(f"{rel} not found under the host media directory")
    return candidate


def resolve_uploaded_source(project_slug: str, filename: str) -> Path:
    """Resolve a browser upload inside this project's private work directory."""
    base = workdir(project_slug).resolve()
    candidate = (base / Path(filename).name).resolve()
    if not candidate.is_relative_to(base) or not candidate.is_file():
        raise FileNotFoundError("uploaded source is missing")
    return candidate
