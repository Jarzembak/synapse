"""Step 2: transcript — site captions when available, else ASR.

Transcript body format (used downstream by trim + correction):
    [HH:MM:SS] text
"""
from __future__ import annotations

import re
from pathlib import Path

from ..config import settings
from ..db import get_session
from .. import library, llm
from .celery_app import celery
from .common import auto_tag, get_project, pipeline_task, progress
from .ingest import cookies_path, source_audio
from . import media


def ts(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def parse_vtt(text: str) -> str:
    """WebVTT → '[HH:MM:SS] line' transcript, cue dedupe included.

    YouTube auto-captions repeat rolling text across cues; consecutive
    duplicate lines are dropped.
    """
    out: list[str] = []
    last_line = None
    cue_time = None
    for raw in text.splitlines():
        line = raw.strip()
        m = re.match(r"(\d+):(\d{2}):(\d{2})[.,]\d+\s+--\>", line) or re.match(
            r"(\d{2}):(\d{2})[.,]\d+\s+--\>", line
        )
        if m:
            g = m.groups()
            if len(g) == 3:
                cue_time = f"{int(g[0]):02d}:{g[1]}:{g[2]}"
            else:
                cue_time = f"00:{g[0]}:{g[1]}"
            continue
        if not line or line == "WEBVTT" or line.startswith(("Kind:", "Language:", "NOTE")):
            continue
        if line.isdigit():
            continue
        clean = re.sub(r"<[^>]+>", "", line).strip()
        if not clean or clean == last_line:
            continue
        out.append(f"[{cue_time or '00:00:00'}] {clean}")
        last_line = clean
    return "\n".join(out)


def fetch_site_captions(url: str, project_slug: str) -> str | None:
    import yt_dlp

    wd = media.workdir(project_slug)
    opts = {
        "skip_download": True,
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitleslangs": ["en.*", "en"],
        "subtitlesformat": "vtt",
        "outtmpl": str(wd / "captions.%(ext)s"),
        "quiet": True,
    }
    ck = cookies_path(project_slug)
    if ck.exists():
        opts["cookiefile"] = str(ck)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    except Exception as e:
        import logging

        logging.getLogger(__name__).info(
            "no site captions for %s (falling back to ASR): %s", url, e)
        return None
    vtts = sorted(wd.glob("captions*.vtt"))
    if not vtts:
        return None
    parsed = parse_vtt(vtts[0].read_text(encoding="utf-8", errors="replace"))
    return parsed or None


def _whisper_configs(device: str, compute_type: str) -> list[tuple[str, str]]:
    """Ordered (device, compute_type) attempts, most-preferred first.

    Auto GPU deliberately prefers float16 over int8: on Blackwell (RTX 50xx,
    sm_120) faster-whisper's int8 path raises CUBLAS_STATUS_NOT_SUPPORTED
    during inference, and float16 is the documented workaround while staying
    valid on all CUDA GPUs. cpu/int8 is always the final safety net.
    """
    if device == "cpu":
        ct = "int8" if compute_type in ("auto", "float16", "int8_float16") else compute_type
        return [("cpu", ct)]
    configs: list[tuple[str, str]] = []
    if device in ("cuda", "auto"):
        if compute_type == "auto":
            configs += [("cuda", "float16"), ("cuda", "int8_float16"), ("cuda", "int8")]
        else:
            configs.append(("cuda", compute_type))
            if compute_type != "float16":
                configs.append(("cuda", "float16"))  # Blackwell-safe retry
    # always fall back to CPU so a GPU that's absent/unsupported still works
    configs.append(("cpu", "int8"))
    # de-dupe preserving order
    seen, out = set(), []
    for c in configs:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def whisper_transcribe(audio: Path, on_progress) -> str:
    import logging

    from faster_whisper import WhisperModel

    from ..config import advanced

    log = logging.getLogger(__name__)
    asr = advanced("asr")
    compute = advanced("compute")
    device = compute.get("whisper_device") or "auto"
    compute_type = compute.get("whisper_compute_type") or "auto"
    _, model_name = llm.resolve_model("asr")

    # int8 on Blackwell fails during INFERENCE, not model load, so each config
    # is attempted through a full transcription before moving to the next.
    configs = _whisper_configs(device, compute_type)
    last_err: Exception | None = None
    for i, (dev, ct) in enumerate(configs):
        try:
            on_progress(f"loading whisper {model_name} (device={dev}, compute={ct})")
            model = WhisperModel(model_name, device=dev, compute_type=ct)
            segments, info = model.transcribe(
                str(audio),
                vad_filter=bool(asr.get("vad", True)),
                language=(asr.get("language") or None),
            )
            total = info.duration or 1
            lines = []
            for seg in segments:  # inference happens here — Blackwell int8 dies here
                lines.append(f"[{ts(seg.start)}] {seg.text.strip()}")
                on_progress(f"transcribing {int(seg.end / total * 100)}%")
            return "\n".join(lines)
        except (ValueError, RuntimeError) as e:
            last_err = e
            log.warning("whisper failed on device=%s compute=%s (%s)%s",
                        dev, ct, e,
                        "; trying next config" if i + 1 < len(configs) else "")
    raise RuntimeError(f"all whisper configs failed; last error: {last_err}")


def gemini_transcribe(audio: Path) -> str:
    from google import genai

    client = genai.Client(api_key=settings.gemini_api_key)
    uploaded = client.files.upload(file=str(audio))
    _, model_name = llm.resolve_model("deepdive_gemini")
    resp = client.models.generate_content(
        model=model_name,
        contents=[
            "Transcribe this audio verbatim. Prefix each paragraph with its "
            "start timestamp as [HH:MM:SS]. Output only the transcript.",
            uploaded,
        ],
    )
    return resp.text or ""


@celery.task(name="transcribe")
@pipeline_task
def transcribe(job_id: int, project_id: int):
    with get_session() as session:
        project = get_project(session, project_id)

    body = None
    source = "site captions"
    if project.source_type == "url":
        progress(job_id, "checking for site captions")
        body = fetch_site_captions(project.source, project.slug)

    if not body:
        asr_provider, _ = llm.resolve_model("asr")
        audio = source_audio(project.slug)
        if asr_provider == "gemini":
            progress(job_id, "transcribing with Gemini")
            body = gemini_transcribe(audio)
            source = "gemini-asr"
        else:
            progress(job_id, "transcribing with faster-whisper")
            body = whisper_transcribe(audio, lambda msg: progress(job_id, msg))
            source = "faster-whisper"

    if not body or not body.strip():
        raise RuntimeError("transcription produced no text")

    with get_session() as session:
        project = get_project(session, project_id)
        art = library.write_artifact(
            session,
            project_id=project_id,
            project_slug=project.slug,
            type="transcript",
            title=f"Transcript — {project.title}",
            body=body,
            extra_meta={"transcript_source": source, "source_url": project.source},
        )
        project.status = "transcribed"
        session.add(project)
        session.commit()
        auto_tag(project_id, art.id)
