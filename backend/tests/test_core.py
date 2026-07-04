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
from app.llm import chunk_text, _strip_fences  # noqa: E402
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
