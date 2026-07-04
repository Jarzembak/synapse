"""Step 1: acquire the source media (download URL or copy local file)."""
from __future__ import annotations

import shutil
from pathlib import Path

from ..db import get_session
from .celery_app import celery
from .common import get_project, pipeline_task, progress
from . import media


def cookies_path(project_slug: str) -> Path:
    return media.workdir(project_slug) / "cookies.txt"


@celery.task(name="ingest")
@pipeline_task
def ingest(job_id: int, project_id: int):
    with get_session() as session:
        project = get_project(session, project_id)

    wd = media.workdir(project.slug)
    audio = wd / "source.m4a"

    if project.source_type == "url":
        progress(job_id, "downloading audio with yt-dlp")
        import yt_dlp

        opts = {
            "format": "bestaudio/best",
            "outtmpl": str(wd / "source.%(ext)s"),
            "postprocessors": [
                {"key": "FFmpegExtractAudio", "preferredcodec": "m4a"}
            ],
            "quiet": True,
        }
        ck = cookies_path(project.slug)
        if ck.exists():
            opts["cookiefile"] = str(ck)
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(project.source, download=True)
            title = info.get("title") or project.title
    else:
        progress(job_id, "copying local file")
        src = media.resolve_local_source(project.source)
        if src.suffix.lower() in {".m4a", ".mp3", ".wav", ".flac", ".ogg", ".opus"}:
            shutil.copy(src, wd / f"source{src.suffix.lower()}")
            audio = wd / f"source{src.suffix.lower()}"
        else:
            progress(job_id, "extracting audio track with ffmpeg")
            media.extract_audio(src, audio)
        title = project.title

    with get_session() as session:
        project = get_project(session, project_id)
        if title and project.title.startswith("(pending"):
            project.title = title
        project.status = "ingested"
        session.add(project)
        session.commit()
    return str(audio)


def source_audio(project_slug: str) -> Path:
    wd = media.workdir(project_slug)
    for ext in (".m4a", ".mp3", ".wav", ".flac", ".ogg", ".opus"):
        p = wd / f"source{ext}"
        if p.exists():
            return p
    raise FileNotFoundError("no ingested audio — run the ingest step first")
