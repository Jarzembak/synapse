"""Audio steps: podcast TTS (local Kokoro/Piper, Gemini cloud) and the
silence/off-topic trim of the source audio."""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import threading
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
from sqlmodel import select, text

from ..config import advanced, settings
from ..db import get_session
from .. import library, llm
from ..models import Artifact
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
log = logging.getLogger("synapse.audio")
# Piper: single-speaker ONNX voices pulled per-name from the rhasspy voice repo.
DEFAULT_PIPER_VOICES = {"HOST_A": "en_US-ryan-medium", "HOST_B": "en_US-amy-medium"}
PIPER_REPO = "https://huggingface.co/rhasspy/piper-voices/resolve/main"


def _tts_workers(default_auto: int) -> int:
    """Synthesis parallelism. 0 in settings = auto (per-engine default)."""
    n = int(advanced("audio").get("tts_workers", 0) or 0)
    if n > 0:
        return n
    return max(1, min(default_auto, os.cpu_count() or 1))


def _download(url: str, dest: Path) -> None:
    """Fetch to a private temp file and atomically rename on success.

    The temp name is unique per process+thread, so several cold fetchers of the
    same model (parallel TTS workers, or celery's 4 processes each starting a
    job) never share one .part file — each writes a COMPLETE file and renames it
    into place, so the promoted file is never interleaved or truncated. An
    interrupted download only leaves its own .part, which the finally cleans up."""
    if dest.exists():
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + f".{os.getpid()}.{threading.get_ident()}.part")
    try:
        with httpx.stream(
            "GET", url, follow_redirects=True,
            timeout=httpx.Timeout(900, connect=15),
        ) as r:
            r.raise_for_status()
            with open(tmp, "wb") as f:
                for chunk in r.iter_bytes():
                    f.write(chunk)
        try:
            tmp.replace(dest)
        except OSError:
            # lost a race to another worker (Windows raises on concurrent atomic
            # replace of the same target); the winner's file is already complete
            if not dest.exists():
                raise
    finally:
        tmp.unlink(missing_ok=True)


def _synthesize_lines(lines, tts_dir: Path, workers: int, on_progress,
                      render) -> list[str]:
    """Render each dialogue line to tts_dir/line_<i>.wav via `render(i, speaker,
    text, out_path)`, optionally in parallel, then return ordered ffmpeg concat
    entries interleaved with the shared gap. Progress is reported as lines
    complete; wav order is preserved by index regardless of completion order."""
    done = [0]
    lock = threading.Lock()

    def one(item):
        i, (speaker, text) = item
        out = tts_dir / f"line_{i:05d}.wav"
        render(i, speaker, text, out)
        with lock:
            done[0] += 1
            on_progress(f"synthesizing line {done[0]}/{len(lines)}")

    items = list(enumerate(lines))
    if workers > 1:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            list(ex.map(one, items))
    else:
        for item in items:
            one(item)

    entries: list[str] = []
    for i in range(len(lines)):
        entries.append(f"file 'line_{i:05d}.wav'")
        entries.append("file 'gap.wav'")
    return entries


def parse_script(body: str) -> list[tuple[str, str]]:
    """'HOST_A: text' lines → [(speaker, text)]."""
    out = []
    for line in body.splitlines():
        m = re.match(r"\s*(HOST_[AB])\s*:\s*(.+)", line)
        if m and m.group(2).strip():
            out.append((m.group(1), m.group(2).strip()))
    return out


def _kokoro_providers() -> None:
    """Pick the ONNX Runtime execution provider from the compute settings.
    kokoro-onnx honors the ONNX_PROVIDER env var; 'auto' uses CUDA only when the
    onnxruntime-gpu build actually exposes it (i.e. the GPU overlay)."""
    device = str(advanced("compute").get("kokoro_device", "auto"))
    if device == "cuda":
        os.environ["ONNX_PROVIDER"] = "CUDAExecutionProvider"
    elif device == "cpu":
        os.environ["ONNX_PROVIDER"] = "CPUExecutionProvider"
    else:  # auto
        try:
            import onnxruntime as ort

            avail = ort.get_available_providers()
        except Exception:
            avail = []
        os.environ["ONNX_PROVIDER"] = (
            "CUDAExecutionProvider" if "CUDAExecutionProvider" in avail
            else "CPUExecutionProvider")


def _kokoro_files() -> Path:
    """Ensure the Kokoro model files are present; returns their directory. Call
    on the main thread before spawning synthesis workers so threads only build
    sessions from files already on disk (never cold-download in parallel)."""
    model_dir = settings.media_dir / "models"
    for name, url in KOKORO_FILES.items():
        _download(url, model_dir / name)
    return model_dir


def _kokoro_model():
    from kokoro_onnx import Kokoro

    _kokoro_providers()
    model_dir = _kokoro_files()
    return Kokoro(str(model_dir / "kokoro-v1.0.onnx"), str(model_dir / "voices-v1.0.bin"))


def _write_gap(tts_dir: Path, rate: int) -> None:
    import numpy as np
    import soundfile as sf

    gap_seconds = max(0.05, float(advanced("audio")["tts_gap"]))
    sf.write(tts_dir / "gap.wav", np.zeros(int(rate * gap_seconds), dtype="float32"), rate)


def _concat_mp3(tts_dir: Path, entries: list[str], out: Path) -> Path:
    (tts_dir / "list.txt").write_text("\n".join(entries), encoding="utf-8")
    media.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0",
               "-i", str(tts_dir / "list.txt"), "-c:a", "libmp3lame", "-q:a", "4", str(out)])
    return out


def _tts_kokoro(lines, wd: Path, on_progress) -> Path:
    import soundfile as sf

    voices = {**DEFAULT_VOICES, **(get_setting("tts.voices") or {})}
    speed = float(advanced("audio")["tts_speed"])
    tts_dir = wd / "tts"
    tts_dir.mkdir(exist_ok=True)
    _write_gap(tts_dir, 24000)
    _kokoro_files()  # fetch on the main thread before any worker builds a model

    # one Kokoro per worker thread — the model is shareable but its English G2P
    # keeps per-call state, so a thread-local instance avoids any race. CPU
    # inference is already multi-core per call, so kokoro's auto default is 1;
    # the real CPU-parallel win is Piper, and the real kokoro win is the GPU.
    local = threading.local()

    def render(i, speaker, text, out):
        model = getattr(local, "kokoro", None)
        if model is None:
            model = local.kokoro = _kokoro_model()
        samples, rate = model.create(text, voice=voices[speaker], speed=speed)
        sf.write(out, samples, rate)

    entries = _synthesize_lines(lines, tts_dir, _tts_workers(1), on_progress, render)
    return _concat_mp3(tts_dir, entries, wd / "podcast.mp3")


def _piper_urls(voice: str) -> tuple[str, str]:
    """Repo URLs for a Piper voice by its rhasspy key, e.g. en_US-amy-medium →
    .../en/en_US/amy/medium/en_US-amy-medium.onnx[.json]."""
    lang_region, name, quality = voice.split("-")
    lang = lang_region.split("_")[0]
    base = f"{PIPER_REPO}/{lang}/{lang_region}/{name}/{quality}/{voice}"
    return base + ".onnx", base + ".onnx.json"


def _piper_files(voice: str) -> tuple[Path, Path]:
    onnx_url, cfg_url = _piper_urls(voice)
    model_dir = settings.media_dir / "models" / "piper"
    onnx, cfg = model_dir / f"{voice}.onnx", model_dir / f"{voice}.onnx.json"
    _download(onnx_url, onnx)
    _download(cfg_url, cfg)
    return onnx, cfg


def _piper_rate(cfg: Path) -> int:
    """A Piper voice's native sample rate from its .onnx.json (medium/high =
    22.05 kHz, low/x_low = 16 kHz). Falls back to the medium default."""
    try:
        return int(json.loads(cfg.read_text(encoding="utf-8"))["audio"]["sample_rate"])
    except Exception:
        return 22050


def _tts_piper(lines, wd: Path, on_progress) -> Path:
    voices = {**DEFAULT_PIPER_VOICES, **(get_setting("tts.piper_voices") or {})}
    files = {sp: _piper_files(voices[sp]) for sp in {s for s, _ in lines}}
    tts_dir = wd / "tts"
    tts_dir.mkdir(exist_ok=True)
    # match the gap to the voices' native rate so the concat demuxer sees
    # uniform stream params regardless of the chosen quality tier
    rate = max(_piper_rate(cfg) for _, cfg in files.values())
    _write_gap(tts_dir, rate)

    def render(i, speaker, text, out):
        onnx, cfg = files[speaker]
        proc = subprocess.run(
            ["piper", "--model", str(onnx), "--config", str(cfg),
             "--output_file", str(out)],
            input=text, capture_output=True, text=True, timeout=600,
        )
        if proc.returncode != 0 or not out.exists():
            raise RuntimeError(f"piper failed: {proc.stderr[-2000:]}")

    # each piper line is its own process, so parallelism is race-free
    entries = _synthesize_lines(lines, tts_dir, _tts_workers(4), on_progress, render)
    return _concat_mp3(tts_dir, entries, wd / "podcast.mp3")


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
        script_artifact = session.exec(
            select(Artifact).where(
                Artifact.project_id == project_id,
                Artifact.type == "podcast_script",
            )
        ).first()
        restricted = bool(
            (script_artifact and script_artifact.restricted)
            or (script_artifact and library.artifact_is_repository_derived(
                session, script_artifact))
            or library.project_is_restricted(session, project_id)
        )

    lines = parse_script(script)
    if not lines:
        raise RuntimeError("podcast script has no HOST_A:/HOST_B: lines")

    wd = media.workdir(project.slug)
    provider, model = llm.resolve_model("tts")
    # Gemini TTS receives the full generated script. Repository derivatives
    # must never leave the host, regardless of the global TTS
    # selection, so force the established local Piper path.
    if restricted and provider == "gemini":
        provider, model = "piper", "en_US-ryan-medium"
    on_progress = lambda m: progress(job_id, m)  # noqa: E731
    out: Path | None = None
    try:
        if provider == "gemini":
            out = _tts_gemini(lines, wd, model, on_progress)
        elif provider == "piper":
            out = _tts_piper(lines, wd, on_progress)
        else:
            out = _tts_kokoro(lines, wd, on_progress)

        _store_audio(project_id, project.slug, project.title, out,
                     type="podcast_audio", title_prefix="Podcast audio",
                     provider=provider, model=model,
                     note=f"Two-host podcast audio generated from "
                          f"{library.wikilink(f'projects/{project.slug}/podcast_script')}.")
    finally:
        if not advanced("audio").get("keep_intermediates", False):
            shutil.rmtree(wd / "tts", ignore_errors=True)
            if out:
                out.unlink(missing_ok=True)


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
    if not advanced("audio").get("keep_intermediates", False):
        out.unlink(missing_ok=True)


def _store_audio(project_id: int, slug: str, title: str, produced: Path, *,
                 type: str, title_prefix: str, provider: str, model: str,
                 note: str, extra: dict | None = None):
    """Copy a produced audio file into the library + write its sidecar artifact."""
    media_rel = f"projects/{slug}/{type}.mp3"
    dest = library.lib_path(media_rel)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + f".{os.getpid()}.tmp")
    previous = dest.read_bytes() if dest.exists() else None
    art = None
    try:
        with get_session() as session:
            # Keep a writer lease from payload publication through sidecar/DB
            # commit so backups and deletion cannot capture a half-published
            # audio artifact.
            session.exec(text("BEGIN IMMEDIATE"))
            try:
                shutil.copyfile(produced, tmp)
                os.replace(tmp, dest)
            finally:
                tmp.unlink(missing_ok=True)
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
    except Exception:
        # A canceled/deleted job can lose the guarded sidecar publication after
        # synthesis. Restore the previous payload (or remove the new one) so no
        # untracked private MP3 survives for backup or cloud discovery.
        if previous is None:
            dest.unlink(missing_ok=True)
        else:
            library._atomic_write_bytes(dest, previous)
        try:
            dest.parent.rmdir()
        except OSError:
            pass
        raise
    try:
        # Tagging is an asynchronous enhancement after publication. A broker
        # error must not roll back an already committed sidecar/DB row or make
        # the payload disappear underneath a queued cloud upload.
        auto_tag(project_id, art.id)
    except Exception:
        log.warning("could not queue tags for audio artifact %s", art.id,
                    exc_info=True)
