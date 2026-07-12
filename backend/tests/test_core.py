"""Unit tests for the pure logic: VTT parsing, chunking, span math, frontmatter."""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# point storage at a temp sandbox before app modules import settings
os.environ.setdefault("LIBRARY_DIR", str(Path(__file__).parent / "_lib"))
os.environ.setdefault("MEDIA_DIR", str(Path(__file__).parent / "_media"))
os.environ.setdefault("DB_PATH", str(Path(__file__).parent / "_db" / "test.sqlite3"))

from app.tasks.transcribe import parse_vtt, ts  # noqa: E402
from app.tasks.audio import hms_to_s, keep_spans, parse_script  # noqa: E402
from app.tasks.ingest import video_format_string  # noqa: E402
from app.llm import chunk_text, _strip_fences  # noqa: E402
from app.routers.artifacts import media_mime  # noqa: E402
from app import library  # noqa: E402


VTT = """WEBVTT
Kind: captions
Language: en

00:00:01.000 --> 00:00:04.000
hello and <b>welcome</b>

00:00:04.000 --> 00:00:06.000
hello and welcome

00:01:10.500 --> 00:01:12.000
let's run nmap
"""


def test_parse_vtt_dedupes_and_timestamps():
    out = parse_vtt(VTT)
    lines = out.splitlines()
    assert lines[0] == "[00:00:01] hello and welcome"
    assert len([l for l in lines if "hello and welcome" in l]) == 1
    assert "[00:01:10] let's run nmap" in lines


def test_ts_format():
    assert ts(3661) == "01:01:01"
    assert ts(0) == "00:00:00"


def test_hms_roundtrip():
    assert hms_to_s("01:01:01") == 3661


def test_keep_spans_complement():
    remove = [
        {"start": "00:00:00", "end": "00:01:00", "reason": "intro"},
        {"start": "00:05:00", "end": "00:06:00", "reason": "sponsor"},
    ]
    keeps = keep_spans(remove, total=600)
    assert keeps == [(60.0, 300.0), (360.0, 600.0)]


def test_keep_spans_rejects_garbage():
    keeps = keep_spans([{"start": "xx", "end": "00:01:00"},
                        {"start": "00:02:00", "end": "00:01:00"}], total=600)
    assert keeps == [(0.0, 600.0)]


def test_parse_script():
    script = "# Title\nHOST_A: hey there\nnot dialogue\nHOST_B: hi!\n"
    assert parse_script(script) == [("HOST_A", "hey there"), ("HOST_B", "hi!")]


def test_chunk_text_overlap():
    body = "\n".join(f"line {i} " + "x" * 50 for i in range(1000))
    chunks = chunk_text(body, max_chars=5000, overlap=200)
    assert all(len(c) <= 5600 for c in chunks)
    assert "".join(c for c in [chunks[0]])  # non-empty
    # overlap: the first line of chunk 2 appears near the end of chunk 1
    first_line_c2 = chunks[1].splitlines()[0]
    assert first_line_c2 in chunks[0]


def test_strip_fences():
    assert _strip_fences('```json\n{"a": 1}\n```') == '{"a": 1}'
    assert _strip_fences('Sure! {"a": 1}') == '{"a": 1}'


def test_frontmatter_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(library.settings, "library_dir", tmp_path)
    rel = "projects/demo/summary.md"
    library._write_doc(rel, {"type": "summary", "tags": ["nmap"]}, "Body **here**.")
    meta, body = library.read_doc(rel)
    assert meta["type"] == "summary"
    assert meta["tags"] == ["nmap"]
    assert body == "Body **here**."


def test_atomic_doc_write_preserves_old_file_on_replace_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(library.settings, "library_dir", tmp_path)
    rel = "projects/demo/summary.md"
    library._write_doc(rel, {"type": "summary"}, "old body")

    def fail_replace(*_args):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(library.os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated replace failure"):
        library._write_doc(rel, {"type": "summary"}, "new body")

    _, body = library.read_doc(rel)
    assert body == "old body"
    assert not list((tmp_path / "projects" / "demo").glob(".summary.md.*.tmp"))


def test_snapshot_history(tmp_path, monkeypatch):
    monkeypatch.setattr(library.settings, "library_dir", tmp_path)
    rel = "tools/nmap.md"
    library._write_doc(rel, {"title": "nmap"}, "v1")
    snap = library.snapshot_history(rel)
    assert snap and library.lib_path(snap).read_text(encoding="utf-8").endswith("v1")
    assert library.snapshot_history("tools/does-not-exist.md") is None


def test_make_slug():
    assert library.make_slug("Nmap NSE!") == "nmap-nse"
    assert library.make_slug("") == "untitled"


def test_whisper_config_chain():
    from app.tasks.transcribe import _whisper_configs

    # auto GPU prefers float16 (Blackwell-safe) before int8, ends on cpu net
    assert _whisper_configs("auto", "auto") == [
        ("cuda", "float16"), ("cuda", "int8_float16"), ("cuda", "int8"), ("cpu", "int8")]
    # explicit cuda int8 retries float16 before falling to cpu
    assert _whisper_configs("cuda", "int8") == [
        ("cuda", "int8"), ("cuda", "float16"), ("cpu", "int8")]
    # cpu never emits gpu-only float16 kernels
    assert _whisper_configs("cpu", "float16") == [("cpu", "int8")]
    assert _whisper_configs("cpu", "int8") == [("cpu", "int8")]


def test_gemini_asr_uses_asr_model_and_deletes_upload(monkeypatch, tmp_path):
    import types

    from app import llm
    from app.tasks.transcribe import gemini_transcribe

    calls = {"deleted": []}
    uploaded = types.SimpleNamespace(name="files/asr-upload")

    class Files:
        def upload(self, *, file):
            calls["uploaded"] = file
            return uploaded

        def delete(self, *, name):
            calls["deleted"].append(name)

    class Models:
        def generate_content(self, *, model, contents):
            calls["model"] = model
            calls["contents"] = contents
            return types.SimpleNamespace(text="[00:00:00] hello")

    client = types.SimpleNamespace(files=Files(), models=Models())
    genai = types.SimpleNamespace(Client=lambda **_kwargs: client)
    google = types.ModuleType("google")
    google.genai = genai
    monkeypatch.setitem(sys.modules, "google", google)
    monkeypatch.setattr(llm, "resolve_model",
                        lambda fn: ("gemini", "gemini-asr-model") if fn == "asr"
                        else (_ for _ in ()).throw(AssertionError(fn)))

    audio = tmp_path / "speech.mp3"
    audio.write_bytes(b"audio")
    assert gemini_transcribe(audio) == "[00:00:00] hello"
    assert calls["model"] == "gemini-asr-model"
    assert calls["uploaded"] == str(audio)
    assert calls["deleted"] == ["files/asr-upload"]


def test_gemini_asr_deletes_upload_when_generation_fails(monkeypatch, tmp_path):
    import types

    from app import llm
    from app.tasks.transcribe import gemini_transcribe

    deleted = []
    uploaded = types.SimpleNamespace(name="files/failed-asr-upload")
    files = types.SimpleNamespace(
        upload=lambda **_kwargs: uploaded,
        delete=lambda *, name: deleted.append(name),
    )

    def fail_generation(**_kwargs):
        raise RuntimeError("generation failed")

    client = types.SimpleNamespace(
        files=files,
        models=types.SimpleNamespace(generate_content=fail_generation),
    )
    google = types.ModuleType("google")
    google.genai = types.SimpleNamespace(Client=lambda **_kwargs: client)
    monkeypatch.setitem(sys.modules, "google", google)
    monkeypatch.setattr(llm, "resolve_model", lambda _fn: ("gemini", "asr-model"))

    with pytest.raises(RuntimeError, match="generation failed"):
        gemini_transcribe(tmp_path / "speech.mp3")
    assert deleted == ["files/failed-asr-upload"]


def test_cloud_config_replace_is_atomic(tmp_path, monkeypatch):
    from app.tasks import cloud

    db_path = tmp_path / "db" / "test.sqlite3"
    conf = db_path.parent / "rclone.conf"
    conf.parent.mkdir(parents=True)
    conf.write_text("old config", encoding="utf-8")
    values = {
        "cloud.provider": "drive",
        "cloud.config": {"token": '{"access_token":"new"}'},
    }
    monkeypatch.setattr(cloud.settings, "db_path", db_path)
    monkeypatch.setattr(cloud, "get_setting", lambda key: values.get(key))
    monkeypatch.setattr(cloud.os, "replace",
                        lambda *_args: (_ for _ in ()).throw(OSError("replace failed")))

    with pytest.raises(OSError, match="replace failed"):
        cloud._conf_path()
    assert conf.read_text(encoding="utf-8") == "old config"
    assert not list(conf.parent.glob(".rclone.conf.*.tmp"))


def test_cloud_sync_paths_reports_uploaded_and_skipped(tmp_path, monkeypatch):
    from app.tasks import cloud

    library_dir = tmp_path / "library"
    media_dir = tmp_path / "media"
    library_dir.mkdir()
    (library_dir / "one.md").write_text("one", encoding="utf-8")
    media_file = media_dir / "demo" / "source.mp3"
    media_file.parent.mkdir(parents=True)
    media_file.write_bytes(b"audio")

    calls = []
    records = []
    monkeypatch.setattr(cloud.settings, "library_dir", library_dir)
    monkeypatch.setattr(cloud.settings, "media_dir", media_dir)
    monkeypatch.setattr(cloud, "_dest", lambda sub: f"remote:{sub}")
    monkeypatch.setattr(cloud, "_rclone", lambda args: calls.append(args))
    monkeypatch.setattr(cloud, "_record", lambda status, detail: records.append((status, detail)))

    result = cloud.sync_paths.run([
        "one.md", "one.md", "missing.md", "media:demo/source.mp3",
    ])
    assert result == {"uploaded": 2, "skipped": 2}
    assert len(calls) == 2
    assert records == [("ok", "uploaded 2 file(s); skipped 2 file(s)")]


def test_video_format_string():
    assert video_format_string(1080) == \
        "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best"
    assert video_format_string(0) == "bestvideo+bestaudio/best"


def test_resolve_media_path():
    lib = library.resolve_media_path("projects/demo/podcast_audio.mp3")
    assert lib == library.settings.library_dir / "projects/demo/podcast_audio.mp3"
    med = library.resolve_media_path("media:demo/source_video.mp4")
    assert med == library.settings.media_dir / "demo/source_video.mp4"


def test_media_mime():
    assert media_mime("source_video.mp4") == "video/mp4"
    assert media_mime("source_audio.m4a") == "audio/mp4"
    assert media_mime("podcast_audio.mp3") == "audio/mpeg"
    assert media_mime("weird.xyz") == "application/octet-stream"
