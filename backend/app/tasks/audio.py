"""Audio steps: podcast TTS (local Kokoro default, Gemini/ElevenLabs cloud) and
the silence/off-topic trim of the source audio."""
from __future__ import annotations

import json
import re
from pathlib import Path

import httpx

from ..config import advanced, settings
from ..db import get_session
from .. import library, llm
from ..settings_store import get_setting
from .celery_app import celery
from .common import artifact_body, auto_tag, get_project, pipeline_task, progress
from .ingest import source_audio
from .prompts import get_prompt
from . import media

KOKORO_FILES = {
    "kokoro-v1.0.onnx":
        "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx",
    "voices-v1.0.bin":
        "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin",
}
DEFAULT_VOICES = {"HOST_A": "am_michael", "HOST_B": "af_heart"}


def parse_script(body: str) -> list[tuple[str, str]]:
    """'HOST_A: text' lines → [(speaker, text)]."""
    out = []
    for line in body.splitlines():
        m = re.match(r"\s*(HOST_[AB])\s*:\s*(.+)", line)
        if m and m.group(2).strip():
            out.append((m.group(1), m.group(2).strip()))
    return out


def _kokoro_model():
    from kokoro_onnx import Kokoro

    model_dir = settings.media_dir / "models"
    model_dir.mkdir(parents=True, exist_ok=True)
    for name, url in KOKORO_FILES.items():
        dest = model_dir / name
        if not dest.exists():
            with httpx.stream("GET", url, follow_redirects=True, timeout=None) as r:
                r.raise_for_status()
                with open(dest, "wb") as f:
                    for chunk in r.iter_bytes():
                        f.write(chunk)
    return Kokoro(str(model_dir / "kokoro-v1.0.onnx"), str(model_dir / "voices-v1.0.bin"))


def _tts_kokoro(lines, wd: Path, on_progress) -> Path:
    import numpy as np
    import soundfile as sf

    voices = {**DEFAULT_VOICES, **(get_setting("tts.voices") or {})}
    tuning = advanced("audio")
    kokoro = _kokoro_model()
    tts_dir = wd / "tts"
    tts_dir.mkdir(exist_ok=True)

    concat_list = tts_dir / "list.txt"
    gap = tts_dir / "gap.wav"
    gap_seconds = max(0.05, float(tuning["tts_gap"]))
    sf.write(gap, np.zeros(int(24000 * gap_seconds), dtype="float32"), 24000)

    entries = []
    for i, (speaker, text) in enumerate(lines):
        on_progress(f"synthesizing line {i + 1}/{len(lines)}")
        samples, rate = kokoro.create(text, voice=voices[speaker],
                                      speed=float(tuning["tts_speed"]))
        line_wav = tts_dir / f"line_{i:05d}.wav"
        sf.write(line_wav, samples, rate)
        entries.append(f"file '{line_wav.name}'")
        entries.append(f"file '{gap.name}'")
    concat_list.write_text("\n".join(entries), encoding="utf-8")

    out = wd / "podcast.mp3"
    media.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_list),
               "-c:a", "libmp3lame", "-q:a", "4", str(out)])
    return out


def _tts_gemini(lines, wd: Path, model: str, on_progress) -> Path:
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=settings.gemini_api_key)
    voices = get_setting("tts.gemini_voices") or {"HOST_A": "Charon", "HOST_B": "Kore"}

    # chunk the script to stay inside per-request limits
    chunks: list[str] = []
    cur: list[str] = []
    size = 0
    for speaker, text in lines:
        entry = f"{speaker}: {text}"
        if size + len(entry) > 3500 and cur:
            chunks.append("\n".join(cur))
            cur, size = [], 0
        cur.append(entry)
        size += len(entry)
    if cur:
        chunks.append("\n".join(cur))

    tts_dir = wd / "tts"
    tts_dir.mkdir(exist_ok=True)
    entries = []
    for i, chunk in enumerate(chunks):
        on_progress(f"synthesizing chunk {i + 1}/{len(chunks)} (Gemini TTS)")
        resp = client.models.generate_content(
            model=model,
            contents=f"TTS the following two-host conversation:\n{chunk}",
            config=types.GenerateContentConfig(
                response_modalities=["AUDIO"],
                speech_config=types.SpeechConfig(
                    multi_speaker_voice_config=types.MultiSpeakerVoiceConfig(
                        speaker_voice_configs=[
                            types.SpeakerVoiceConfig(
                                speaker=sp,
                                voice_config=types.VoiceConfig(
                                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                                        voice_name=voices[sp])),
                            )
                            for sp in ("HOST_A", "HOST_B")
                        ]
                    )
                ),
            ),
        )
        pcm = resp.candidates[0].content.parts[0].inline_data.data
        raw = tts_dir / f"chunk_{i:04d}.pcm"
        raw.write_bytes(pcm)
        wav = tts_dir / f"chunk_{i:04d}.wav"
        media.run(["ffmpeg", "-y", "-f", "s16le", "-ar", "24000", "-ac", "1",
                   "-i", str(raw), str(wav)])
        entries.append(f"file 'chunk_{i:04d}.wav'")

    (tts_dir / "list.txt").write_text("\n".join(entries), encoding="utf-8")
    out = wd / "podcast.mp3"
    media.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0",
               "-i", str(tts_dir / "list.txt"), "-c:a", "libmp3lame", "-q:a", "4", str(out)])
    return out


@celery.task(name="tts")
@pipeline_task
def tts(job_id: int, project_id: int):
    with get_session() as session:
        project = get_project(session, project_id)
        script = artifact_body(session, project_id, "podcast_script")

    lines = parse_script(script)
    if not lines:
        raise RuntimeError("podcast script has no HOST_A:/HOST_B: lines")

    wd = media.workdir(project.slug)
    provider, model = llm.resolve_model("tts")
    if provider == "gemini":
        out = _tts_gemini(lines, wd, model, lambda m: progress(job_id, m))
    else:
        out = _tts_kokoro(lines, wd, lambda m: progress(job_id, m))

    _store_audio(project_id, project.slug, project.title, out,
                 type="podcast_audio", title_prefix="Podcast audio",
                 provider=provider, model=model,
                 note=f"Two-host podcast audio generated from "
                      f"{library.wikilink(f'projects/{project.slug}/podcast_script')}.")


def hms_to_s(t: str) -> float:
    h, m, s = (int(x) for x in t.split(":"))
    return h * 3600 + m * 60 + s


def keep_spans(remove: list[dict], total: float) -> list[tuple[float, float]]:
    """Complement of the (validated, sorted) remove spans over [0, total]."""
    spans = []
    for r in remove:
        try:
            a, b = hms_to_s(r["start"]), hms_to_s(r["end"])
        except (KeyError, ValueError):
            continue
        if 0 <= a < b <= total + 1:
            spans.append((a, min(b, total)))
    spans.sort()
    keep: list[tuple[float, float]] = []
    cursor = 0.0
    for a, b in spans:
        if a > cursor:
            keep.append((cursor, a))
        cursor = max(cursor, b)
    if cursor < total:
        keep.append((cursor, total))
    return [(a, b) for a, b in keep if b - a > 0.2]


@celery.task(name="trim")
@pipeline_task
def trim(job_id: int, project_id: int):
    with get_session() as session:
        project = get_project(session, project_id)
        transcript = artifact_body(session, project_id, "transcript")

    src = source_audio(project.slug)
    wd = media.workdir(project.slug)
    total = media.duration_seconds(src)

    progress(job_id, "finding off-topic spans")
    provider, model = llm.resolve_model("trim_spans")
    result = llm.complete_json("trim_spans", get_prompt("trim_spans"), transcript[:120000])
    removed = result.get("remove", [])
    keeps = keep_spans(removed, total)

    progress(job_id, "cutting and removing silence with ffmpeg")
    tuning = advanced("audio")
    silence = (f"silenceremove=stop_periods=-1"
               f":stop_duration={float(tuning['trim_silence'])}"
               f":stop_threshold={int(tuning['trim_db'])}dB")
    if keeps and len(keeps) < 200:
        parts = []
        filters = []
        for i, (a, b) in enumerate(keeps):
            filters.append(f"[0:a]atrim={a}:{b},asetpts=PTS-STARTPTS[k{i}]")
            parts.append(f"[k{i}]")
        filters.append(f"{''.join(parts)}concat=n={len(keeps)}:v=0:a=1[cat]")
        graph = ";".join(filters) + f";[cat]{silence}[out]"
    else:
        graph = f"[0:a]{silence}[out]"

    out = wd / "trimmed.mp3"
    media.run(["ffmpeg", "-y", "-i", str(src), "-filter_complex", graph,
               "-map", "[out]", "-c:a", "libmp3lame", "-q:a", "4", str(out)])

    reasons = "\n".join(
        f"- `{r.get('start')}–{r.get('end')}` {r.get('reason', '')}" for r in removed
    ) or "- (none — only silence was removed)"
    _store_audio(project_id, project.slug, project.title, out,
                 type="trimmed_audio", title_prefix="Trimmed audio",
                 provider=provider, model=model,
                 note=f"Source audio with silence and off-topic spans removed.\n\n"
                      f"## Removed spans\n{reasons}",
                 extra={"original_seconds": total,
                        "trimmed_seconds": media.duration_seconds(out)})


def _store_audio(project_id: int, slug: str, title: str, produced: Path, *,
                 type: str, title_prefix: str, provider: str, model: str,
                 note: str, extra: dict | None = None):
    """Copy a produced audio file into the library + write its sidecar artifact."""
    media_rel = f"projects/{slug}/{type}.mp3"
    dest = library.lib_path(media_rel)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(produced.read_bytes())

    with get_session() as session:
        art = library.write_artifact(
            session,
            project_id=project_id,
            project_slug=slug,
            type=type,
            title=f"{title_prefix} — {title}",
            body=note,
            media_rel=media_rel,
            provider=provider,
            model=model,
            extra_meta={"duration_seconds": media.duration_seconds(dest), **(extra or {})},
        )
        auto_tag(project_id, art.id)  # inherits the project's canonical tag set
