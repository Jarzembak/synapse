"""Step 1: acquire the source media (download URL or copy local file), and the
optional archive step that keeps a full local copy of URL sources."""
from __future__ import annotations

import shutil
from pathlib import Path

from ..db import get_session
from .. import library
from ..settings_store import get_setting
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


def video_format_string(max_height: int) -> str:
    """yt-dlp format selector honoring the configured resolution cap (0 = best)."""
    if max_height and max_height > 0:
        return f"bestvideo[height<={max_height}]+bestaudio/best[height<={max_height}]/best"
    return "bestvideo+bestaudio/best"


@celery.task(name="download")
@pipeline_task
def download(job_id: int, project_id: int):
    """Archive the URL source locally: full video (mp4) + audio-only copy,
    both registered as browsable library artifacts."""
    with get_session() as session:
        project = get_project(session, project_id)

    if project.source_type != "url":
        raise ValueError("local sources are already on disk — nothing to download")

    import yt_dlp

    wd = media.workdir(project.slug)
    max_height = int(get_setting("download.max_height", 1080))

    def hook(d):
        if d.get("status") == "downloading":
            pct = d.get("_percent_str", "").strip()
            if pct:
                progress(job_id, f"downloading video {pct}")
        elif d.get("status") == "finished":
            progress(job_id, "download finished, merging streams")

    progress(job_id, "downloading video with yt-dlp")
    opts = {
        "format": video_format_string(max_height),
        "merge_output_format": "mp4",
        "outtmpl": str(wd / "source_video.%(ext)s"),
        "progress_hooks": [hook],
        "quiet": True,
    }
    ck = cookies_path(project.slug)
    if ck.exists():
        opts["cookiefile"] = str(ck)
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(project.source, download=True)

    videos = sorted(wd.glob("source_video.*"))
    if not videos:
        raise RuntimeError("yt-dlp reported success but no video file was produced")
    video = videos[0]

    # audio-only copy: reuse the ingested audio when present, else extract
    audio = wd / "source_audio.m4a"
    if not audio.exists():
        try:
            existing = source_audio(project.slug)
            if existing.suffix == ".m4a":
                shutil.copy(existing, audio)
            else:
                media.extract_audio(existing, audio)
        except FileNotFoundError:
            progress(job_id, "extracting audio track from video")
            media.extract_audio(video, audio)

    source_meta = {
        "source_url": project.source,
        "uploader": info.get("uploader"),
        "upload_date": info.get("upload_date"),
        "duration_seconds": info.get("duration"),
    }
    with get_session() as session:
        library.write_artifact(
            session,
            project_id=project_id,
            project_slug=project.slug,
            type="source_video",
            title=f"Source video — {project.title}",
            body=f"Archived video download of `{project.source}`.",
            rel_path=f"projects/{project.slug}/source_video.md",
            media_rel=f"media:{project.slug}/{video.name}",
            extra_meta={
                **source_meta,
                "resolution": f"{info.get('width', '?')}x{info.get('height', '?')}",
                "filesize_bytes": video.stat().st_size,
                "max_height_setting": max_height,
            },
        )
        library.write_artifact(
            session,
            project_id=project_id,
            project_slug=project.slug,
            type="source_audio",
            title=f"Source audio — {project.title}",
            body=f"Archived audio-only copy of `{project.source}`.",
            rel_path=f"projects/{project.slug}/source_audio.md",
            media_rel=f"media:{project.slug}/{audio.name}",
            extra_meta={**source_meta, "filesize_bytes": audio.stat().st_size},
        )
