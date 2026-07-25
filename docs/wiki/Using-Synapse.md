# Using Synapse

## Projects

Projects is the starting point for:

- a URL supported by yt-dlp;
- a browser-uploaded audio or video file;
- a mounted local media file;
- a public or private GitHub repository; or
- a browser-uploaded or mounted research-paper PDF.

The list shows each project's derived status, completed/total step count,
active or failed step, progress, and last activity. Opening a project displays
its pipeline board. Steps are disabled while queued or running; completed steps
link to their artifacts; failed steps expose their error message.

Choose a built-in or custom pipeline profile at the top of a project. A profile
runs only work that is missing or stale. Individual step controls allow
targeted regeneration or a deliberate downstream rebuild.

## Library

Library is the combined artifact catalog. It is server-paginated and has no
silent result ceiling.

Search modes:

- **Exact** uses SQLite FTS5 and is useful for commands, names, and quoted
  transcript phrases.
- **Hybrid** optionally combines exact results with local embeddings, which can
  retrieve passages that express the same concept with different wording.

Search query, mode, filters, sort order, and page are encoded in the URL, so a
filtered view can be bookmarked.

**Ask your library** retrieves evidence before calling the configured answer
model. The answer cites sources as `[S1]`, `[S2]`, and so on; every citation
keeps its supporting excerpt visible. Media results can open playback at the
cited time, repository results link to exact source lines, and paper results
jump to the cited PDF page.

## Quick-refs

Quick-refs is the accumulated cross-project reference library. Search by name,
alias, or tag; filter by category; and sort by name, update time, or source
count. Categories appear in parallel columns.

Opening a document shows:

- its contributing projects;
- canonical name and aliases;
- tags;
- version history;
- previous versions and one-click restore; and
- deletion controls.

The built-in categories are Tools, Techniques, Concepts, and Technologies.
Under **Settings → Quick-ref categories**, you can add a custom key, label,
icon, library folder, extraction description, and document prompt.

The key and folder are immutable once created so existing documents cannot be
orphaned. A category containing documents cannot be deleted until its documents
are removed. When adding a category, review the deep-dive, entity-extraction,
and mind-map prompts so they surface the new material.

## Jobs

Jobs shows running, queued, and recent work across projects, updated over
server-sent events.

Whole-project runs are durable and execute one at a time. Individual eligible
steps can run concurrently as worker capacity becomes available. Queued and
running jobs can be canceled. Cancellation is recorded before worker
revocation, preventing a late provider response from publishing canceled work.

On restart, orphaned jobs are marked interrupted and the oldest durable
whole-project run normally resumes. **Continue queue** is available for manual
recovery when Redis or the worker was unavailable during handoff.

## System

System combines resource monitoring and operational checks:

- CPU, memory, library disk, active jobs, GPU, VRAM, and loaded Ollama models;
- database, Redis, worker, ffmpeg, yt-dlp, and Ollama readiness;
- embedding-model and optional provider-key checks;
- free-space and vault/index integrity checks;
- per-function model calls, failures, token usage, and duration;
- search-index reconstruction from the Markdown vault; and
- backup creation, validation, listing, and download.

## Logs

Logs tails API, worker, and scheduler files in the browser. Choose a service,
minimum severity, text filter, and line count. Live mode refreshes every two
seconds; freeze mode preserves the current view. Multi-line tracebacks stay
grouped with their error. The visible tail can be downloaded.

See
[Operations and Troubleshooting](https://github.com/Jarzembak/synapse/wiki/Operations-and-Troubleshooting)
for file paths, environment settings, and common failures.

## Settings

Settings controls:

- per-function providers and models;
- Ollama model installation;
- Piper, Kokoro, and Gemini voices;
- built-in and custom pipeline profiles;
- semantic retrieval and embedding providers;
- backup scheduling, retention, and media policy;
- desktop completion notifications;
- the correction glossary;
- media download resolution;
- tag vocabulary;
- quick-reference categories;
- GitHub credentials;
- paper privacy defaults; and
- advanced prompts and tuning.

See
[Models and Providers](https://github.com/Jarzembak/synapse/wiki/Models-and-Providers)
and [Configuration](https://github.com/Jarzembak/synapse/wiki/Configuration).

## Themes

The navigation theme selector changes the entire interface immediately. Themes
include Light, Dark, Cyberpunk, Synthwave, Terminal, and Amber CRT. The
selection is stored in browser local storage and also applies to Markdown, code
blocks, and mind maps.
