# Synapse

A self-hosted, containerized web app for turning any video or audio source — a
local file, or a URL from YouTube, Vimeo, Udemy, or similar — into a permanent,
searchable knowledge library. Point it at a talk once; it produces a transcript,
a corrected transcript, a summary, two independent deep dives that get merged
into one, a growing set of per-tool/per-technique quick-reference docs, a
two-host podcast script with generated audio, a trimmed audio-only copy, and an
interactive mind map. Everything accumulates in one browsable, taggable,
full-text-searchable library instead of living as scattered files.

It supports both local models (via Ollama, CPU-friendly by default) and
frontier APIs (Claude, Gemini), configurable **per pipeline step**, so you can
run cheaply on local hardware and reserve API spend for the steps that need it.

- [How it works](#how-it-works)
- [Quick start](#quick-start)
- [Using the app](#using-the-app)
- [The library on disk](#the-library-on-disk)
- [Configuring models](#configuring-models)
- [Local files, cookies, and remote GPUs](#local-files-cookies-and-remote-gpus)
- [Development](#development)
- [Troubleshooting](#troubleshooting)
- [Current limitations](#current-limitations)

## How it works

### Architecture

Five containers, orchestrated by `docker-compose.yml`:

| Service | Role |
|---|---|
| `frontend` | nginx serving the built React SPA; proxies `/api` to `api` |
| `api` | FastAPI — REST endpoints, SSE job stream, reads/writes the library |
| `worker` | Celery worker — runs every pipeline step (this is where yt-dlp, ffmpeg, faster-whisper, Kokoro, and all LLM calls actually execute) |
| `redis` | Celery broker + result backend |
| `ollama` | Local model server (OpenAI-compatible API) for any step configured to use a local model |

`api` and `worker` share the same backend image and codebase; `api` only serves
HTTP/SSE, `worker` only consumes the job queue, so a slow transcription or LLM
call never blocks the UI.

### The pipeline

A "project" is one video/audio source. Its pipeline is twelve independent
steps, run in order from the project's pipeline board — each writes an
artifact you can open immediately, and each can be re-run on its own (e.g. if
you edit the glossary and want to re-run correction, or swap the deep-dive
model and regenerate just that step):

1. **Ingest** — downloads the source with `yt-dlp` (best audio track) or copies/extracts audio from a local file.
2. **Transcript** — tries the site's own captions first (manual subtitles preferred, auto-captions as fallback, parsed from WebVTT with rolling-caption dedup). If none exist, falls back to ASR: **faster-whisper** locally on CPU, or Gemini's native audio transcription if you've configured that step to use Gemini.
3. **Correction pass** — an LLM re-reads the transcript and fixes transcription errors only (misheard words, mangled shell commands, wrong acronyms, garbled product names) using your glossary of known-correct terms. It does not summarize or edit for style — meaning and structure are preserved.
4. **Summary** — a short (150–250 word) summary of the video.
5. **Deep dive (Claude)** and **6. Deep dive (Gemini)** — two independent, structured deep-dive documents generated from the corrected transcript. Both are instructed to focus on **core concepts, tools, and technologies**, and critically: any procedural content in the source (a step-by-step tutorial, a walkthrough of a methodology, a config recipe) must be captured **in full** — every step, every command, the reasoning behind it, and the expected result — never compressed into a summary sentence.
7. **Merge** — an LLM combines both deep dives into one unified document: redundant material is deduplicated (keeping the clearer telling of each point), unique content from each is folded in, and the **union of all procedures is preserved** — two procedures are only merged if they describe the literal same steps.
8. **Quick-references** — reads the merged deep dive, identifies every tool and technique discussed in substance, and for each one either creates a new quick-reference doc or **merges new material into an existing one** if that tool/technique has appeared before (matching handles name variants — e.g. "Nmap", "nmap NSE" — by showing the LLM the existing doc index and letting it match or justify a new entry; matched variants are recorded as aliases). Before any merge, the previous version of the doc is snapshotted so you can view or revert it later.
9. **Podcast script** — a long two-host script (`HOST_A` / `HOST_B`) covering the merged deep dive, written as an outline first (so it has real structure and covers every segment) then expanded segment by segment for natural, technically accurate dialogue.
10. **Podcast audio** — text-to-speech of that script. Local default is **Kokoro** (fast, CPU-friendly, runs per line then stitches with ffmpeg); Gemini's native multi-speaker TTS is available as a cloud alternative.
11. **Trim audio** — takes the original source audio, has an LLM identify off-topic spans from the timestamped transcript (intro chatter, sponsor reads, subscribe requests, tangents — conservatively, keeping anything it's unsure about), cuts those spans out with ffmpeg, and removes silence.
12. **Mind map** — an LLM turns the merged deep dive into a topic graph (concepts, tools, techniques, technologies as nodes, with labeled relationships), rendered as a clickable, pannable diagram; clicking a node shows its description and links straight to its quick-reference doc if one exists.

Every artifact written by any step is also passed through **auto-tagging**: an
LLM proposes tags from a shared, editable vocabulary (only inventing a new tag
when nothing existing fits), so tags stay consistent across your whole library
instead of drifting into synonyms.

## Quick start

Requires Docker (Docker Desktop on Windows/Mac, or Docker Engine on Linux) —
everything else runs inside containers.

```bash
cp .env.example .env
```

Edit `.env`:

```bash
ANTHROPIC_API_KEY=sk-ant-...      # needed for the Claude deep dive, merge, quick-refs, podcast script, mind map (defaults)
GEMINI_API_KEY=...                # needed for the Gemini deep dive (default), optional Gemini ASR/TTS
```

Both keys are optional independently — if you leave a key blank, just don't
run the steps assigned to that provider (or reassign those steps to your local
Ollama model in **Settings**; see [Configuring models](#configuring-models)).

```bash
docker compose up --build
```

First-time setup, in another terminal, once the containers are up:

```bash
# pull the default local model (used for correction, summary, tagging, and trim-span detection)
docker compose exec ollama ollama pull qwen3:8b
```

Open **http://localhost:8080**.

That's it — everything else (Kokoro's TTS model weights, faster-whisper's ASR
model) downloads automatically on first use of that step and is cached in a
Docker volume, so subsequent runs are fast.

## Using the app

**Projects** is where you start: paste a URL (YouTube, Vimeo, Udemy, or
anything [yt-dlp supports](https://github.com/yt-dlp/yt-dlp)) or give a local
file path, and a project is created. Opening a project shows its **pipeline
board** — twelve step cards you run in order (each is disabled while
running/queued; a card turns green once its artifact exists, red on error with
the full error message expandable inline). Progress updates live via
server-sent events, so you can watch "transcribing 43%" or "writing segment
4/11" without refreshing. Each completed step links straight to its artifact.

**Library** is the home page and the point of the whole app: every artifact
from every project, in one searchable list. Full-text search hits the SQLite
FTS5 index (so searching for a command or a specific phrase from a transcript
works, not just titles); filter by type, tag, or project, and sort by date,
title, or type. Click through to render markdown, play audio, or open the
mind map.

**Quick-refs** is the accumulating tool/technique library — separate from the
per-project pipeline because these documents are *cross-project*: the nmap
quick-ref started from your first networking video keeps growing every time
nmap comes up again. Each entry shows which videos contributed to it, its
alias list, and its version history (view or one-click revert any prior
snapshot).

**Settings** holds everything that changes how the pipeline behaves:
per-function model selection, the correction glossary, TTS voice choices, and
the tag vocabulary (add/rename/delete — renaming merges into an existing tag
of the new name if one exists, and propagates to every artifact's frontmatter).

## The library on disk

Every artifact is a markdown file with YAML frontmatter, plus an SQLite index
for search/sort/relations. **The markdown files are the source of truth for
content; the database is just an index** — you can grep, `git`, sync, or back
up `data/library/` directly, and it's openable as a second **Obsidian vault**
(quick-refs and deep dives cross-link with `[[wikilinks]]`).

```
data/library/
├── projects/<project-slug>/
│   ├── transcript.md            # raw transcript, [HH:MM:SS]-timestamped
│   ├── corrected.md             # after the correction pass
│   ├── summary.md
│   ├── deepdive_claude.md
│   ├── deepdive_gemini.md
│   ├── deepdive_merged.md
│   ├── podcast_script.md
│   ├── podcast_audio.md         # sidecar metadata; audio itself is podcast_audio.mp3
│   ├── podcast_audio.mp3
│   ├── trimmed_audio.md / .mp3
│   └── mindmap.md               # topic-graph JSON in a code fence
├── tools/<tool-slug>.md         # cross-project quick-references
├── techniques/<technique-slug>.md
└── .history/                    # timestamped snapshots taken before every quick-ref merge
```

Frontmatter on every file includes `type`, `title`, `project`, `created`,
`updated`, `provider`, `model`, and `tags`; quick-refs additionally track
`aliases` (name variants matched to this doc).

Raw downloaded/working media (source audio, yt-dlp temp files, cookies) lives
separately under `data/media/`, and is not part of the searchable library.

## Configuring models

Every LLM-driven step has an independent provider/model setting in
**Settings → Model matrix**. Providers:

- **ollama** — local, via the bundled `ollama` container (or point `OLLAMA_BASE_URL` in `.env` at a bigger box on your network — see below). Default for correction, summary, trim-span detection, and tagging (`qwen3:8b`).
- **anthropic** — Claude API. Default for the Claude deep dive, the merge, quick-references, the podcast script, and the mind map (all `claude-sonnet-5`; swap in `claude-opus-4-8` for more depth on any of these, or `claude-haiku-4-5` to cut cost on correction/summary/tagging).
- **gemini** — Gemini API. Default for the Gemini deep dive (`gemini-3.5-flash`); can also be assigned to ASR (native audio transcription) or TTS (native multi-speaker speech) if you'd rather not run those locally.

Changing a dropdown takes effect on the *next run* of that step — nothing
needs restarting. There's no requirement to use both frontier providers; if
you only have an Anthropic key, for example, reassign the Gemini deep-dive
step to `anthropic`/`claude-sonnet-5` (you'll get two Claude passes merged
into one instead of a Claude+Gemini cross-check — still useful, just not the
default two-perspective design) or to `ollama` if you'd rather keep it fully
local.

## Local files, cookies, and remote GPUs

**Local file input:** set `HOST_MEDIA_DIR` in `.env` to a folder on your host
machine (default is `./data/media`, i.e. inside the project checkout). It's
mounted read-only into the containers at `/host-media`. When creating a
project with source type "local file", give a path **relative to that
directory** — e.g. if `HOST_MEDIA_DIR=D:\Videos` and your file is
`D:\Videos\talks\recon.mp4`, enter `talks/recon.mp4`.

**Authenticated sites (Udemy, etc.):** on the project detail page, upload a
`cookies.txt` (Netscape format, exportable with any browser cookie-export
extension) before running **Ingest** or **Transcript** — yt-dlp uses it for
that project's requests.

**Bigger local models on other hardware:** if you have a GPU box elsewhere on
your network already running Ollama, set `OLLAMA_BASE_URL` in `.env` to its
address (e.g. `http://10.0.0.5:11434`) instead of the bundled container. Every
step assigned to the `ollama` provider then runs there — no code changes,
just point Settings at bigger model names (e.g. `qwen3:30b-a3b-instruct`)
once they're pulled on that box.

## Development

Backend tests (pure Python, no Docker needed):

```bash
cd backend
pip install -r requirements.txt
pytest tests -q
```

Frontend dev server with hot reload (proxies `/api` to `localhost:8000`, so
run the backend separately or via `docker compose up api redis ollama worker`
first):

```bash
cd frontend
npm install
npm run dev
```

Rebuilding after backend/frontend code changes: `docker compose up --build`.
Wiping all data (library, media, database, Ollama/Whisper model caches) to
start fresh: `docker compose down -v` (destructive — confirm you want to lose
the library before running this).

## Troubleshooting

- **A step fails immediately with "missing prerequisite artifact"** — steps depend on earlier ones (e.g. the merge step needs both deep dives). Run the pipeline board top to bottom.
- **Transcription is slow** — faster-whisper on CPU is realistic for CPU-only hardware but not fast; for long videos, consider assigning the `asr` function to `gemini` in Settings instead.
- **A frontier-model step errors with an auth/key message** — check the corresponding `_API_KEY` in `.env` and that you restarted (`docker compose up`) after editing it.
- **yt-dlp fails on a URL** — the site may need cookies (see above) or may not be supported; check `docker compose logs worker` for the underlying yt-dlp error.
- **JSON-producing steps (trim spans, mind map, quick-ref matching) occasionally fail** — local models are more prone to malformed JSON than frontier ones; the app retries automatically, but if a local model consistently fails structured-output steps, assign those specific functions to a frontier provider instead.

## Current limitations

- ElevenLabs is listed as a future TTS option in the design but isn't wired
  into the TTS step yet — the two working TTS providers today are Kokoro
  (local) and Gemini (cloud). `ELEVENLABS_API_KEY` in `.env.example` is a
  placeholder for that later addition.
- No authentication — this is designed to run on a trusted local network for a
  single user, not to be exposed to the internet.
- No GPU passthrough is configured in `docker-compose.yml` (matching the
  CPU-only hardware this was built for); if you later add a GPU, faster
  local ASR (Parakeet) and better local dialogue TTS (Dia2/VibeVoice) become
  worth adding.
