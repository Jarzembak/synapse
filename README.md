# Synapse

A self-hosted, containerized web app for turning any video or audio source — a
local file, or a URL from YouTube, Vimeo, Udemy, or similar — into a permanent,
searchable knowledge library. Point it at a talk once; it produces a transcript,
a corrected transcript, a summary, two independent deep dives that get merged
into one, a growing set of quick-reference docs (tools, techniques, concepts,
technologies, or categories you define yourself), a two-host podcast script
with generated audio, a trimmed audio-only copy, and an interactive mind map.
Everything accumulates in one browsable, taggable library with exact and
semantic search, source-grounded Q&A, timestamp playback links, and an optional
push to your own cloud storage instead of living as scattered files.

Vibe coded by Fable and Sol and inspired by Jeff McJunkin's methodology.

It supports both local models — the bundled Ollama (CPU-friendly by default)
or any OpenAI-compatible server you already run, like LM Studio, llama.cpp,
or vLLM — and frontier APIs (Claude, Gemini), configurable **per pipeline
step**, so you can run cheaply on local hardware and reserve API spend for
the steps that need it.
Nearly every behavior — the prompts each step sends its model, generation
temperature, audio pacing, tagging rules — is tunable from **Settings → Advanced**
without touching code.

- [How it works](#how-it-works)
- [Quick start](#quick-start)
- [Using the app](#using-the-app)
- [Themes](#themes)
- [Advanced settings](#advanced-settings)
- [Cloud storage](#cloud-storage)
- [Backups and encryption](#backups-and-encryption)
- [The library on disk](#the-library-on-disk)
- [Configuring models](#configuring-models)
- [Local files, cookies, and remote GPUs](#local-files-cookies-and-remote-gpus)
- [Network exposure](#network-exposure)
- [Development](#development)
- [Logging](#logging)
- [Troubleshooting](#troubleshooting)
- [Current limitations](#current-limitations)

## How it works

### Architecture

Six containers, orchestrated by `docker-compose.yml`:

| Service | Role |
|---|---|
| `frontend` | nginx serving the built React SPA; proxies `/api` to `api` |
| `api` | FastAPI — REST endpoints, SSE job stream, reads/writes the library |
| `worker` | Celery worker — runs every pipeline step (this is where yt-dlp, ffmpeg, faster-whisper, Kokoro/Piper, and all LLM calls actually execute) |
| `beat` | Celery scheduler — checks hourly whether a configured backup is due |
| `redis` | Celery broker + result backend |
| `ollama` | Local model server for any step configured to use a local model (Synapse can also use an OpenAI-compatible server you run yourself — see [Configuring models](#configuring-models)) |

`api`, `worker`, and `beat` share the same backend image and codebase; `api`
serves HTTP/SSE, `worker` consumes jobs, and `beat` only schedules periodic
checks, so a slow transcription or LLM call never blocks the UI.

### The pipeline

A "project" is one video/audio source. Its pipeline is thirteen independent
steps, run in order from the project's pipeline board — each writes an
artifact you can open immediately, and each can be re-run on its own (e.g. if
you edit the glossary and want to re-run correction, or swap the deep-dive
model and regenerate just that step):

1. **Ingest** — downloads the source with `yt-dlp` (best audio track) or copies/extracts audio from a local file.
2. **Download & keep media** *(optional, URL sources only)* — archives the source permanently: the full video (best quality up to your configured resolution cap, merged to mp4) **and** an audio-only copy. Both are stored under `data/media/<project>/` and registered as library artifacts, so they're searchable, playable in the browser (the video player supports seeking), and downloadable. Local-file projects show "already local" instead.
3. **Transcript** — tries the site's own captions first (manual subtitles preferred, auto-captions as fallback, parsed from WebVTT with rolling-caption dedup). If none exist, falls back to ASR: **faster-whisper** locally on CPU, or Gemini's native audio transcription if you've configured that step to use Gemini.
4. **Correction pass** — an LLM re-reads the transcript and fixes transcription errors only (misheard words, mangled shell commands, wrong acronyms, garbled product names) using your glossary of known-correct terms. It does not summarize or edit for style — meaning and structure are preserved.
5. **Summary** — a short (150–250 word) summary of the video.
6. **Deep dive (Claude)** and **7. Deep dive (Gemini)** — two independent, structured deep-dive documents generated from the corrected transcript. Both are instructed to focus on **core concepts, tools, and technologies**, and critically: any procedural content in the source (a step-by-step tutorial, a walkthrough of a methodology, a config recipe) must be captured **in full** — every step, every command, the reasoning behind it, and the expected result — never compressed into a summary sentence.
8. **Merge** — an LLM combines both deep dives into one unified document: redundant material is deduplicated (keeping the clearer telling of each point), unique content from each is folded in, and the **union of all procedures is preserved** — two procedures are only merged if they describe the literal same steps.
9. **Quick-references** — reads the merged deep dive, identifies every entity discussed in substance under one of four built-in kinds — **tool**, **technique**, **concept**, **technology** — or any category you've defined yourself (see [Quick-refs categories](#using-the-app) below), and for each one either creates a new quick-reference doc or **merges new material into an existing one** if it has appeared before (matching handles name variants — e.g. "Nmap", "nmap NSE" — by showing the LLM the existing doc index and letting it match or justify a new entry; matched variants are recorded as aliases). Each kind gets a deliberately different document shape: a **tool** doc is a user-friendly instruction manual (what it is, getting started, core usage, examples, gotchas) for something you actually run; a **technique** doc is a step-by-step recipe for one specific task (goal, prerequisites, numbered steps with exact commands, verification); a **concept** doc is a crisp explainer for an idea or principle you understand rather than execute (definition, why it matters, how it works, related tools/techniques); a **technology** doc is a primer on a platform/protocol/standard (what it is, key pieces, how it works, how it's worked with). Custom categories use the doc prompt you write for them. Before any merge, the previous version of the doc is snapshotted so you can view or revert it later.
10. **Podcast script** — a long two-host script (`HOST_A` / `HOST_B`) covering the merged deep dive, written as an outline first (so it has real structure and covers every segment) then expanded segment by segment for natural, technically accurate dialogue.
11. **Podcast audio** — text-to-speech of that script. Two local providers: **Piper** (fast, CPU-friendly, recommended default) and **Kokoro** (also CPU today — see [Current limitations](#current-limitations)); each renders line-by-line, optionally in parallel, then stitches with ffmpeg. Gemini's native multi-speaker TTS is available as a cloud alternative.
12. **Trim audio** — takes the original source audio, has an LLM identify off-topic spans from the timestamped transcript (intro chatter, sponsor reads, subscribe requests, tangents — conservatively, keeping anything it's unsure about), cuts those spans out with ffmpeg, and removes silence.
13. **Mind map** — an LLM turns the merged deep dive into a topic graph (concepts, tools, techniques, technologies as nodes, with labeled relationships), rendered as a clickable, pannable diagram; clicking a node shows its description and links straight to its quick-reference doc if one exists.

Every artifact is also **auto-tagged** from a shared, editable vocabulary.
Tagging is project-level: one LLM call reads the project's richest document
(the merged deep dive once it exists, else the corrected/raw transcript) and
the resulting tag set is propagated to *all* of that project's artifacts —
so metadata-only artifacts like the archived video or the podcast MP3 inherit
accurate tags instead of being guessed at from their (nearly empty) own
content. The set is recomputed automatically when a richer document appears.
Quick-reference docs are the exception: they're cross-project and are tagged
individually from their own content. Proposed tags are automatically
sanitized before they reach the vocabulary — a small local model occasionally
loops a token (`apis-apis-apis…`) or emits a run-on phrase; degenerate
repeats are collapsed and over-long/multi-word junk is dropped, while
existing tags you created on purpose are always trusted as-is.

Every generated artifact also records the exact upstream content signature,
model, prompt, and relevant settings that produced it. If a source, glossary,
prompt, model, voice, or tuning value changes, the project board marks affected
outputs **update available** all the way down the dependency graph. A profile
run automatically rebuilds only missing or stale work. Built-in profiles cover
Full production, Research library, Quick notes, and Audio edition; custom
profiles can be assembled in Settings. “Re-run downstream” deliberately
rebuilds one step and every output that consumes it. Summary/deep-dive prompts
preserve `[HH:MM:SS]` source citations and label model-added background context.

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
# pull the default local model (used for correction, tagging, and trim-span detection)
docker compose exec ollama ollama pull qwen3:8b
# optional: enable meaning-based Hybrid search in Settings, then pull its model
docker compose exec ollama ollama pull nomic-embed-text
```

Open **http://localhost:8080**.

The default Compose stack binds the web app to loopback only. Synapse has no
authentication, so it is not reachable from other devices unless you
explicitly opt in; see [Network exposure](#network-exposure).

That's it — everything else (the TTS provider's voice files, whether Piper or
Kokoro; faster-whisper's ASR model) downloads automatically on first use of
that step and is cached, so subsequent runs are fast.

## Using the app

**Projects** is where you start: paste a URL (YouTube, Vimeo, Udemy, or
anything [yt-dlp supports](https://github.com/yt-dlp/yt-dlp)), upload an audio
or video file directly in the browser, or give a mounted local-file path. The
projects list shows each one's
**derived pipeline status** — a colored chip (New / Partial / Running /
Complete / Failed / Canceled), a `done/total` step count with a thin progress
bar, the active or failed step's name, and last-activity time — computed live
from the step graph rather than a static field, and refreshed automatically
over the job stream. Opening a project shows its **pipeline board** —
thirteen step cards you run in order (each is disabled while running/queued;
a card turns green once its artifact exists, red on error with the full error
message expandable inline). Choose a built-in or custom pipeline profile at
the top; completed cards show when an update is available after their inputs
or settings change, and can rebuild only themselves or their entire downstream
branch. Progress updates live via server-sent events, so
you can watch "transcribing 43%" or "writing segment 4/11" without
refreshing. Each completed step links straight to its artifact.

**Library** is the home page and the point of the whole app: every artifact
from every project, in a server-paginated, sortable, filterable list without a
silent result ceiling. **Exact** mode uses SQLite FTS5, so commands and quoted
transcript phrases work, not just titles. **Hybrid** mode retrieves line-aware
excerpts and optionally blends exact ranking with local Ollama embeddings, so
conceptual searches can find relevant passages that use different wording.
Query, mode, type/tag/project filters, ordering, and page are stored in the URL,
making a filtered view bookmarkable. The **Ask your library** panel retrieves
supporting excerpts first and requires the configured answer model to cite
them as `[S1]`, `[S2]`, etc.; it refuses to fill gaps from general knowledge.
Every citation keeps the excerpt visible, and timestamped sources offer a
**play @ HH:MM:SS** link that opens the project's source media at that moment.

**Quick-refs** is the accumulating tool/technique/concept/technology
library — separate from the per-project pipeline because these documents are
*cross-project*: the nmap quick-ref started from your first networking video
keeps growing every time nmap comes up again. The left sidebar holds the
controls — a search box (matches name, alias, or tag), toggle buttons per
category, a sort dropdown (name / recently updated / most sources), and a tag
cloud with counts (documents are tagged the same way library artifacts are).
The main area lays out **one column per category** side by side (🔧 Tools /
🎯 Techniques / 💡 Concepts / ⚙️ Technologies, plus any custom categories you
add), so you see everything at a glance instead of scrolling one long list.
Clicking a doc swaps the columns for its detail view — contributing videos,
alias list, tags, version history (view or one-click revert any prior
snapshot), and a delete-doc action — with a back button to return to the
columns.

You aren't limited to the four built-in kinds: **Settings → Quick-ref
categories** lets you define your own (a custom label/icon/library-folder,
a description that tells the entity-extraction step what belongs in it, and
a doc-writing prompt of your own). New categories are picked up automatically
by extraction; adding one shows a reminder banner naming the other prompts —
deep dive, entity extraction, mind map — worth reviewing in the prompt editor
so they actually surface that kind of material. A category's key and folder
are fixed once created (existing docs never orphan), and one with existing
docs can't be deleted until those docs are removed first.

**Jobs** is a live view of the whole job queue across every project: what's
running, what's queued, and recent history, all streamed over SSE. Whole-
project "run all" jobs execute one at a time and auto-chain to the next
queued project; individual steps run concurrently as worker capacity frees
up. Any queued or running job can be canceled. Cancellation is fenced in the
database before worker revocation, so a late provider response cannot publish
or resurrect canceled work. On worker restart, orphaned rows are marked
interrupted and the oldest durable whole-project run resumes automatically;
**Continue queue** remains as a manual recovery control.

**System** combines the live resource monitor (CPU, memory, library disk,
active jobs, GPU/VRAM, and resident Ollama models) with operational checks.
It tests the database, broker, worker, media tools, Ollama/embedding model,
optional provider keys, and free space; shows local per-function model-call
counts, errors, tokens and duration; reports vault/index integrity; can rebuild
the SQLite search layer from Markdown; and creates, verifies, downloads, and
lists backup snapshots.

**Logs** tails the api/worker log files in the browser — no `docker compose
logs` needed. Toggle between services, filter by minimum level (all / info+ /
warning+ / error-only — multi-line tracebacks stay grouped under their
error), filter by text, choose how many lines to tail, watch it live (polls
every 2s) or freeze it, and download the current tail.

**Settings** holds everything that changes how the pipeline behaves:
provider-compatible per-function model selection, Piper/Kokoro/Gemini host
voices, built-in and custom pipeline profiles, optional semantic indexing,
backup schedule/retention/media policy, desktop completion notifications, the
correction glossary, download resolution, tag vocabulary, quick-ref categories,
and an **Advanced** section covering prompts, generation parameters,
audio/pipeline/ASR/compute tuning, and cloud storage — see below.

## Themes

A dropdown in the top-right corner of the nav bar switches the whole UI
between six themes: **Light**, **Dark**, **Cyberpunk** (neon magenta/cyan on
near-black), **Synthwave** (purple/pink/orange), **Terminal** (green CRT with
scanlines), and **Amber CRT**. Your choice is saved in the browser
(`localStorage`) and applied instantly with no reload; the mind map, markdown
rendering, and code blocks all follow the active theme.

## Advanced settings

**Settings → Advanced** exposes seven collapsible groups for fine-tuning
pipeline behavior beyond the model matrix:

- **Prompt editor** — the exact system prompt sent to the model for every
  pipeline step (correction, both deep-dive generators, the merge, entity
  extraction, each quick-ref template including any custom category's own
  prompt, podcast outline/segments, trim-span detection, mind map, tagging)
  in an editable textarea. A "modified" badge marks any prompt you've changed
  from the shipped default; "reset to default" clears your edit and reverts
  instantly. Edits take effect the next time that step runs — nothing needs
  restarting.
- **Generation parameters** — per-function temperature and max-output-tokens
  overrides, for when you want a specific step more deterministic (lower
  temperature) or more creative, or need to raise the output ceiling for an
  unusually long transcript.
- **Audio tuning** — TTS speaking speed and the pause length inserted between
  dialogue lines, a parallel-workers count for TTS synthesis (0 = auto; each
  Piper line renders in its own process so this speeds it up directly, while
  Kokoro's win comes mainly from the GPU — see below), plus the trim step's
  silence threshold (dB) and minimum silence duration to cut.
- **Pipeline behavior** — the correction pass's chunk size (characters per
  LLM call), deep-dive depth (concise / standard / exhaustive), a target
  podcast segment count (0 = let the model decide), the max tags applied per
  artifact, and whether the auto-tagger is allowed to invent new vocabulary
  tags or must only choose from existing ones.
- **ASR options** — toggle the voice-activity-detection filter (disable if
  faster-whisper is dropping quiet words) and set a language hint for
  non-English sources (blank = auto-detect). The Whisper model size itself
  (tiny → large) is set in the Model matrix, on the `asr` row.
- **Compute** — GPU vs CPU device selection for faster-whisper (`auto` picks
  CUDA when available) and its compute type, plus a Kokoro-device setting
  that's effectively CPU-only today regardless of the GPU overlay — see
  [Current limitations](#current-limitations) for why.
- **Cloud storage** — see the next section.

## Cloud storage

Synapse can push every artifact it produces to your own cloud storage,
either automatically as each one is written or on demand. This runs on
[rclone](https://rclone.org/) inside the worker container, which is why one
integration covers five very different backends — no separate SDK or app
registration burden per provider beyond what's described below.

**What syncs:** the entire `data/library/` tree (transcripts, deep dives,
quick-refs, podcast script/audio, mind maps — everything) plus the archived
`source_video`/`source_audio` files from `data/media/<project>/` if you've
run the Download & keep media step. Working files (yt-dlp temp files,
cookies, the transcription-only audio copy) are never uploaded.

**When it runs:** turn on **auto-upload** and each artifact is queued for
upload the moment its pipeline step finishes writing it — no separate click
per file. Independently, the **"Sync everything now"** button does a full
pass over the whole library and archived media at once, useful for backfilling
artifacts that existed before you configured cloud storage, or for a periodic
full resync. Clicking it again while one is already in flight returns the
same in-progress sync rather than starting a second, overlapping one. On
Google Drive specifically — the one backend of the five that allows multiple
files with the same name in a folder — a full sync finishes with a dedupe
pass that folds any same-name duplicates back down to the newest copy, so
the remote self-heals rather than accumulating dupes from any past race.
There's no scheduled/bidirectional sync — Synapse only ever pushes up.

### Setup, step by step

1. Open **Settings → Advanced → Cloud storage** and pick a **Provider** from
   the dropdown. Below are the exact fields for each:

   **S3-compatible** (AWS S3, self-hosted MinIO, Backblaze B2, Wasabi, or
   anything else that speaks the S3 API):
   - `endpoint` — the S3 API URL (e.g. `https://s3.us-east-1.amazonaws.com`
     for AWS, `https://minio.yourdomain.com` for a self-hosted MinIO, or your
     B2/Wasabi endpoint)
   - `bucket` — the bucket name (create it in your provider's console first)
   - `access_key_id` / `secret_access_key` — from your provider (IAM user for
     AWS, an access key you generate in MinIO's console, an application key
     for B2, etc.)
   - `region` — optional; leave blank for MinIO/most non-AWS providers

   **WebDAV** (Nextcloud, ownCloud, or any generic WebDAV server):
   - `url` — your WebDAV endpoint, e.g. for Nextcloud:
     `https://your-nextcloud.example.com/remote.php/dav/files/<your-username>`
   - `vendor` — `nextcloud` or `owncloud` (blank defaults to `nextcloud`)
   - `user` — your Nextcloud/ownCloud username
   - `password` — an **app password**, not your login password: in
     Nextcloud, go to Settings → Security → "Create new app password"

   **Google Drive / Dropbox / OneDrive** (OAuth-based — no Synapse-side app
   registration, but you generate a token once using rclone itself):
   1. Install rclone on any machine with a web browser (`https://rclone.org/downloads/` —
      this can be your everyday laptop, it doesn't need to be near Synapse).
   2. Run `rclone authorize "drive"` (or `"dropbox"` / `"onedrive"`) in a
      terminal. It opens your browser, asks you to sign in and approve
      access, then prints a block of JSON to the terminal like
      `{"access_token":"...","token_type":"Bearer",...}`.
   3. Copy that entire JSON block and paste it into the **token** field in
      Synapse's Settings.
   4. Google Drive only: `root_folder_id` is optional — leave blank to sync
      into "My Drive"'s root, or paste a folder ID to sync into a specific
      existing folder. OneDrive only: `drive_type` is `personal` (default) or
      `business`.
   5. These tokens expire; if a sync starts failing after a long time, repeat
      steps 1–3 to get a fresh token.

2. Set **Remote base folder** (default `synapse`) — the top-level folder name
   created inside your bucket/Drive/Nextcloud where everything lands
   (`<remote_base>/library/...` and `<remote_base>/media/...`).
3. Check **auto-upload each artifact when it's produced** if you want zero
   manual steps going forward, or leave it off and rely on manual syncs.
4. Click **Save cloud settings**.
5. Click **Sync everything now** to do an initial full backfill of whatever's
   already in your library. Progress shows in the job ticker (top-right of
   the nav) same as any pipeline step; a status line under the cloud section
   shows the result and timestamp of the last sync attempt, including the
   error message if one failed.

Secrets you enter are **masked** as soon as you save them — the API never
echoes a saved `secret_access_key`, `password`, or `token` back to the
browser (you'll see `•set•` instead). To rotate a credential, just type the
new value over the masked field and save again; leaving it as `•set•` or
blank keeps the previously stored value.

## Backups and encryption

Synapse can create a consistent ZIP containing an SQLite snapshot and the
entire Markdown library, with archived/browser-uploaded source media optionally included.
Backups live in `data/backups/`, which is shared by the API and worker. The
lightweight `beat` service checks once per hour and queues a backup when the
configured interval is due; scheduling is disabled by default. Retention
defaults to the five newest archives. Configure the policy in **Settings →
Backups**, then create, verify, or download snapshots under **System → Backups**.
Verification checks the archive CRC and runs SQLite's own integrity check on
the contained database snapshot.

Set `BACKUP_ENCRYPTION_KEY` in `.env` before creating backups if the archive
should be encrypted. Use a long, random value, store it in a password manager,
and keep it for as long as any encrypted archive exists—there is no recovery
path if it is lost. Leaving it blank creates ordinary, unencrypted ZIP files.

`SETTINGS_ENCRYPTION_KEY` protects saved cloud credentials. If it is blank,
Synapse generates `data/db/.settings.key` instead. For a portable disaster
recovery setup, set and retain the environment value; otherwise secure a
separate copy of `.settings.key`, because the database inside a Synapse backup
does not contain that key. The backup directory is on the same host by
default, so copy verified archives to another device or storage provider.

The Markdown vault remains independently recoverable: **System → Library
integrity → Rebuild index from vault** reconstructs projects, artifacts,
quick-reference relationships, tags, FTS rows, and retrieval chunks. It does
not overwrite Markdown. Project deletion stages folders before its database
transaction; startup automatically restores or finishes any staging left by a
power loss between those operations.

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
│   ├── source_video.md          # sidecar for the archived download; the video/audio
│   ├── source_audio.md          #   files themselves live in data/media/<slug>/
│   └── mindmap.md               # topic-graph JSON in a code fence
├── tools/<tool-slug>.md         # cross-project quick-references — instruction manuals
├── techniques/<technique-slug>.md  #   — step-by-step recipes
├── concepts/<concept-slug>.md      #   — explainers
├── technologies/<technology-slug>.md  #   — platform/protocol primers
├── <custom-category-folder>/       #   — one folder per category you define yourself
└── .history/                    # timestamped snapshots taken before every quick-ref merge
```

Frontmatter on every file includes `type`, `title`, `project`, `created`,
`updated`, `provider`, `model`, `tags`, and provenance hashes/details for the
effective inputs and configuration; quick-refs additionally track
`aliases` (name variants matched to this doc). A quick-ref's artifact `type`
is `quickref_<category-key>` — `quickref_tool`, `quickref_technology`,
`quickref_<your-custom-key>`, and so on.

Working media and archived downloads live separately under `data/media/<slug>/`:
the ingest step's working audio, yt-dlp temp files, and cookies, plus — once
you've run **Download & keep media** — the permanent `source_video.mp4` and
`source_audio.m4a`. Keeping the large binaries out of `data/library/` means
your Obsidian-openable vault stays lightweight, while the sidecar `.md` files
above keep the downloads searchable and playable from the Library UI. Back up
`data/media/` too if the archived videos matter to you.

## Configuring models

Every LLM-driven step has an independent provider/model setting in
**Settings → Model matrix**. Providers:

- **ollama** — local, via the bundled `ollama` container (or point `OLLAMA_BASE_URL` in `.env` at a bigger box on your network — see below). Default for correction, trim-span detection, and tagging (`qwen3:8b`).
- **openai_compat** — any OpenAI-compatible server you run yourself: LM Studio, llama.cpp server, vLLM, LocalAI, Jan, and the like. Set `OPENAI_COMPAT_BASE_URL` in `.env` (include the `/v1` suffix — e.g. LM Studio on the Docker host is `http://host.docker.internal:1234/v1`), plus `OPENAI_COMPAT_API_KEY` if your server enforces one. Any chat step — and semantic-search embeddings, via the provider dropdown under **Settings → Library intelligence** — can be assigned to it.
- **anthropic** — Claude API. Default for summary, the Claude deep dive, the merge, quick-references, the podcast script, and the mind map (all `claude-sonnet-5`; swap in `claude-opus-4-8` for more depth on any of these, or `claude-haiku-4-5` to cut cost on summary/quick-refs).
- **gemini** — Gemini API. Default for the Gemini deep dive (`gemini-3.5-flash`); can also be assigned to ASR (native audio transcription) or TTS (native multi-speaker speech) if you'd rather not run those locally.

The model fields for the two local providers suggest what's actually
installed on each server (Ollama's model list, or the server's `/models`
endpoint), so you can pick instead of typing. Changing a dropdown takes
effect on the *next run* of that step — nothing needs restarting. There's no
requirement to use both frontier providers; if you only have an Anthropic
key, for example, reassign the Gemini deep-dive step to
`anthropic`/`claude-sonnet-5` (you'll get two Claude passes merged into one
instead of a Claude+Gemini cross-check — still useful, just not the default
two-perspective design) or to a local provider if you'd rather keep it fully
local.

### Local model tuning

**Settings → Advanced → Local models** controls how local providers are
called:

- **Context window** (`num_ctx`, Ollama only) — requested per call, so no
  Modelfile edits are needed. Synapse defaults to 16k tokens because Ollama's
  own default (4k in current releases) silently truncates the correction
  pass's ~24k-character transcript chunks; raise it (up to 256k) for
  local deep dives over long sources, or lower it if a big window doesn't fit
  your RAM/VRAM. For `openai_compat` servers, set the context length in the
  server itself (LM Studio's model settings, llama.cpp's `-c` flag).
- **Keep model loaded** (Ollama only) — Ollama's `keep_alive`: `"5m"`
  default, `"-1"` pins the model in memory between steps, `"0"` frees it
  immediately after each call.
- **Thinking** (Ollama only) — for reasoning models like `qwen3` or
  `deepseek-r1`: `auto` keeps the model's default, `off` answers faster and
  avoids reasoning loops on mechanical steps (tagging, correction), `on`
  forces deliberate reasoning. Leave on `auto` for models without thinking
  support — forcing a value errors on them. Inline `<think>` blocks are
  stripped from outputs regardless.
- **Request timeout** — for both local providers (default 300 s). Raise it if
  a CPU-only box times out generating long outputs.
- **JSON enforcement** — structured steps (trim spans, mind map, quick-ref
  matching, tagging, the podcast-script outline) ask the server for
  guaranteed-valid JSON (Ollama's `format`, OpenAI-compatible
  `response_format`). Servers that reject `response_format` are retried
  without it automatically; disable the toggle only if yours misbehaves with
  it.

The `asr` and `tts` rows aren't LLM chat calls, so they have their own
provider sets: **asr** is `faster-whisper` (local, default) or `gemini`
(native audio transcription); **tts** is `piper` (local, fast, recommended),
`kokoro` (local, the older default), or `gemini` (native multi-speaker
speech).

## Local files, cookies, and remote GPUs

**Browser upload (recommended for an ordinary file):** choose **Upload a
file** on Projects. Synapse streams it into that project's private
`data/media/<slug>/` folder, keeps a playable transcription-audio sidecar, and
removes it with the project. The default limit is 20 GiB (`MAX_UPLOAD_BYTES`);
nginx streams the request instead of buffering it in memory or temporary disk.

**Mounted local-file input (useful for a large existing collection):** set
`HOST_MEDIA_DIR` in `.env` to a folder on your host
machine (default is `./data/media`, i.e. inside the project checkout). It's
mounted read-only into the containers at `/host-media`. When creating a
project with source type "local file", give a path **relative to that
directory** — e.g. if `HOST_MEDIA_DIR=D:\Videos` and your file is
`D:\Videos\talks\recon.mp4`, enter `talks/recon.mp4`.

URL sources reject loopback, link-local, and private IP literals by default.
Set `ALLOW_PRIVATE_URLS=true` only when you intentionally need a source served
inside your trusted network. Credentials embedded in a URL are always rejected;
use the per-project cookies upload for authenticated sites instead.

**Authenticated sites (Udemy, etc.):** on the project detail page, upload a
`cookies.txt` (Netscape format, exportable with any browser cookie-export
extension) before running **Ingest**, **Download & keep media**, or
**Transcript** — yt-dlp uses it for that project's requests.

**Bigger local models on other hardware:** if you have a GPU box elsewhere on
your network already running Ollama, set `OLLAMA_BASE_URL` in `.env` to its
address (e.g. `http://10.0.0.5:11434`) instead of the bundled container. Every
step assigned to the `ollama` provider then runs there — no code changes,
just point Settings at bigger model names (e.g. `qwen3:30b-a3b-instruct`)
once they're pulled on that box. If that box runs LM Studio, llama.cpp,
vLLM, or another OpenAI-compatible server instead of Ollama, set
`OPENAI_COMPAT_BASE_URL` to it and assign steps to the `openai_compat`
provider — same effect.

**Local NVIDIA GPU:** if the machine running Docker has an NVIDIA GPU, start
the stack with the GPU overlay instead:

```bash
docker compose -f docker-compose.yml -f docker-compose.gpu.yml up --build
```

This grants the `ollama`, `worker`, and `api` containers GPU access (requires
the NVIDIA Container Toolkit — bundled with Docker Desktop on Windows when an
NVIDIA driver is present; `api` gets it only so the [System monitor](#using-the-app)'s
`nvidia-smi` call can report utilization/VRAM, not for compute) and builds the
worker with the CUDA libraries faster-whisper needs. Ollama then uses the GPU
automatically for every `ollama`-assigned step; Whisper transcription is
controlled from **Settings → Advanced → Compute** (device `auto`/`cpu`/`cuda`
and compute type — `float16` for best GPU quality, `int8` for CPU). The
default `auto` settings are safe either way: they use the GPU when it's
available and fall back to CPU when it isn't. TTS (both Piper and Kokoro)
runs on CPU regardless of the overlay — see
[Current limitations](#current-limitations). Note that consumer GPUs around
8 GB VRAM speed up the *same-size* local models dramatically but don't unlock
meaningfully bigger ones — 7–8B-class quantized models remain the practical
ceiling.

## Network exposure

Synapse does not currently provide user authentication. The normal stack
therefore publishes only the frontend, and only on `127.0.0.1:8080`; the API
stays on Docker's private network and is reached through the frontend proxy.

To intentionally make the full app available on your LAN, set
`SYNAPSE_BIND_ADDRESS=0.0.0.0` in `.env` and restart the stack. Treat that as
granting everyone who can reach the host full access to the app and its
library. Do not expose port 8080 directly to the internet; put authentication
and TLS in a trusted reverse proxy first if remote access is required.

Port 8000 is published only by `docker-compose.dev.yml`, and that development
overlay also defaults to loopback. It is not needed when using the built-in
frontend container.

## Development

Backend tests (pure Python, no Docker needed):

```bash
cd backend
pip install -r requirements-dev.txt
pytest tests -q
```

Frontend dev server with hot reload (proxies `/api` to `localhost:8000`, so
run the backend separately or start the backend containers with the
development overlay first):

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml up api redis ollama worker beat
```

Then, in another terminal:

```bash
cd frontend
npm ci
npm run dev
```

`npm run typecheck` checks TypeScript without producing a build;
`npm run build` runs that check and creates the production bundle. CI runs the
backend tests, frontend type-check/build, and validates the default,
development, and GPU Compose configurations on every pull request.

Backend container and CI installs are also locked while the requirement files
remain portable for local development. Edit `requirements.txt` or
`requirements-dev.txt`, then regenerate their Linux constraint locks from the
`backend` directory with the same pinned compiler CI expects:

```bash
python -m pip install pip-tools==7.5.3
pip-compile --allow-unsafe --no-emit-index-url --no-emit-trusted-host --strip-extras --output-file=constraints.txt requirements.txt
pip-compile --allow-unsafe --no-emit-index-url --no-emit-trusted-host --strip-extras --output-file=constraints-dev.txt requirements-dev.txt
```

Commit both the input and generated file, then rerun the backend tests.

Rebuilding after backend/frontend code changes: `docker compose up --build`.

Starting fresh (destructive — confirm you want to lose the library first):
`docker compose down -v` removes the named volumes (Ollama/Whisper/Redis
caches) but **not** the library, media, database, and logs — those are host
bind mounts under `./data`. To wipe those too, also delete the directory:
`docker compose down -v && rm -rf ./data`.

## Logging

The API, worker, and scheduler write structured, timestamped logs to two
places at once: container stdout (visible through `docker compose logs`) and
rotating log files on a shared volume — `data/logs/synapse-api.log`,
`data/logs/synapse-worker.log`, and `data/logs/synapse-beat.log` (5 MB × 3
rotations each). Every pipeline step logs its start/completion/failure, LLM
calls log at debug level, and previously-silent background failures (cloud
sync enqueueing, auto-tagging, caption fetch fallback) leave a warning trace
instead of vanishing. Chatty third-party per-request logging (`httpx`, used
by the system monitor's 2-second Ollama poll among other things) is pinned to
WARNING so it doesn't drown out the app's own log lines.

- **In-app viewer**: the **Logs** tab tails either service live in the
  browser — no docker CLI or network access to the host needed. Filter by
  minimum level or free text, choose how many lines to tail, watch it update
  every 2 seconds or freeze it, and download the current tail. Multi-line
  entries (tracebacks) stay grouped and colored under their error's level
  even when you filter to errors-only.
- **Log level**: set `LOG_LEVEL=DEBUG` (or WARNING/ERROR) in `.env` and
  restart; default is INFO.
- **Raw API**: `GET /api/logs` lists services with log files, and
  `GET /api/logs/worker?lines=200` tails one directly if you want to script
  against it.

## Troubleshooting

- **A step fails immediately with "missing prerequisite artifact"** — steps depend on earlier ones (e.g. the merge step needs both deep dives). Run the pipeline board top to bottom.
- **Multiple projects are queued but nothing is running** — whole-project runs are serial by design. Worker startup normally clears interrupted jobs and resumes the oldest queued run automatically; if the broker was unavailable during that hand-off, use **Continue queue** in Jobs after Redis/worker readiness checks turn green in System.
- **A completed card says “update available”** — one of its source artifacts, prompts, models, voices, or tuning values changed. Run the selected profile to refresh every stale consumer, or use **Re-run downstream** on the earliest changed step.
- **Hybrid search only finds exact phrases** — enable semantic search in Settings, make sure `nomic-embed-text` (or your chosen embedding model) is installed in Ollama, and queue **Rebuild search index**. The System readiness card reports a missing embedding model.
- **Transcription is slow** — faster-whisper on CPU is realistic for CPU-only hardware but not fast; for long videos, consider assigning the `asr` function to `gemini` in Settings instead, or run the [GPU overlay](#local-files-cookies-and-remote-gpus) if you have an NVIDIA card.
- **TTS (podcast audio) is slow** — check the **System** tab while it runs: if CPU is pegged and no GPU shows activity, that's expected (TTS is CPU-only today, see [Current limitations](#current-limitations)). Try the `piper` provider if you're on `kokoro` — it's the faster of the two on CPU — and/or raise **Advanced → Audio → TTS parallel workers**.
- **A frontier-model step errors with an auth/key message** — check the corresponding `_API_KEY` in `.env` and that you restarted (`docker compose up`) after editing it.
- **yt-dlp fails on a URL** — the site may need cookies (see above) or may not be supported; check the **Logs** tab (or `docker compose logs worker`) for the underlying yt-dlp error.
- **JSON-producing steps (trim spans, mind map, quick-ref matching) occasionally fail** — local models are more prone to malformed JSON than frontier ones. Synapse asks local servers for guaranteed-valid JSON natively (see [Local model tuning](#local-model-tuning)) and retries automatically; if a local model still consistently fails structured-output steps, assign those specific functions to a frontier provider instead.
- **A local model seems to "forget" the start of long inputs, or a correction pass drops content** — the input exceeded the model's context window. Raise **Advanced → Local models → Context window** (Ollama), or your server's own context setting (`openai_compat`), and check the model itself supports that length.
- **A step assigned to `openai_compat` errors immediately** — set `OPENAI_COMPAT_BASE_URL` in `.env` (include the `/v1` suffix) and restart the stack; the System tab's readiness card shows whether the server is reachable and how many models it offers (their names appear in the Settings model matrix's suggestions).
- **Cloud sync fails** — check the status line under Settings → Advanced → Cloud storage for the specific rclone error. Common causes: an S3 `endpoint`/`bucket` typo, a WebDAV `password` that's your login password instead of an app password, or an expired Drive/Dropbox/OneDrive token (re-run `rclone authorize` and paste the fresh token).
- **Google Drive shows duplicate files for the same artifact** — this can happen if two full syncs ever overlapped (Drive allows same-name duplicates in a folder, unlike the other four backends). Click **Sync everything now**: its final dedupe pass folds duplicates back down to the newest copy automatically.

## Current limitations

- ElevenLabs is listed as a future TTS option in the design but isn't wired
  into the TTS step yet — the three working TTS providers today are Piper
  (local), Kokoro (local), and Gemini (cloud). `ELEVENLABS_API_KEY` in
  `.env.example` is a placeholder for that later addition.
- No authentication — this is designed to run on a trusted local network for a
  single user, not to be exposed to the internet.
- **TTS runs on CPU even under the GPU overlay.** The GPU overlay accelerates
  Ollama and faster-whisper; Piper and Kokoro both run on CPU regardless.
  This isn't an oversight so much as a version conflict worth documenting: an
  earlier attempt swapped in `onnxruntime-gpu` for Kokoro, but its current
  release needs the CUDA 13 runtime while the image (matched to what
  faster-whisper/ctranslate2 needs) ships CUDA 12 — so `onnxruntime` failed
  to import entirely, breaking *both* TTS engines (Piper depends on the same
  `onnxruntime` package). The fix reverted to CPU onnxruntime; Piper is
  fast enough there that this hasn't been revisited. Resolving it for real
  means either finding an `onnxruntime-gpu` build compatible with CUDA 12, or
  moving the whole image to CUDA 13 and re-validating faster-whisper against
  it. The **System** tab's GPU card is the way to confirm what's actually
  running where on your hardware.
- Cloud sync only pushes — there's no pull/bidirectional sync, and no
  scheduled sync (auto-upload-per-artifact or the manual "sync now" button
  are the only triggers). The backend image's rclone package (Debian's
  packaged v1.60) covers all five supported providers but is a few years
  behind upstream; if Google/Microsoft ever change their token format in a
  way it can't parse, switching the Dockerfile to rclone's official install
  script would pull the latest release.
- Job leasing/restart recovery currently assumes the single worker service
  defined by `docker-compose.yml`. Running multiple independent worker
  services would need a distributed lease/leader design for the serialized
  whole-project queue.
- Semantic retrieval stores vectors in SQLite and scores them in-process. It is
  intentionally simple and private for a personal library; a very large
  multi-user collection would warrant a dedicated vector index.
