# How Synapse works

Synapse separates interactive web requests from long-running extraction,
generation, transcription, and audio jobs. A source becomes a project; each
pipeline step produces a durable artifact with provenance and citations.

## Architecture

The standard stack is orchestrated by `docker-compose.yml`.

| Service | Role |
|---|---|
| `frontend` | nginx serving the React application and proxying `/api` |
| `api` | FastAPI REST endpoints, server-sent job events, library access |
| `worker` | Celery media, model, TTS, indexing, and general pipeline work |
| `paper-worker` | Concurrency-one, CPU-only Docling/Tesseract PDF extraction |
| `beat` | Periodic backup-schedule checks |
| `redis` | Celery broker and result backend |
| `ollama` | Bundled local chat and embedding model server |
| `auth-browser` | Disposable, one-session Chromium for login-protected media |
| `auth-egress` | DNS-pinned public-web-only HTTP/HTTPS proxy for Chromium and yt-dlp |
| `auth-bridge` | Fixed WebDriver/noVNC bridge back to the API |

`api`, `worker`, and `beat` use the same backend image but run different
processes. Slow model or transcription calls therefore do not block the web
interface. `paper-worker` consumes only the paper extraction queue and uses a
dedicated image with its parser models baked in.

The authentication browser is used only when a source requires sign-in and
holds no application volumes. Its Docker-internal network has no direct
Internet or application route: Chromium reaches public sites only through
`auth-egress`, while the API
reaches WebDriver and the token-gated noVNC relay only through `auth-bridge`.
The browser container restarts after each session.

## Projects, steps, and artifacts

A project represents one source. Its pipeline is a dependency graph of
independent steps rather than one opaque job. Each completed step writes an
artifact that can be opened immediately.

You can:

- run a predefined or custom pipeline profile;
- run one step when its prerequisites exist;
- regenerate only a selected result;
- rebuild a step and every downstream consumer; or
- resume interrupted durable work after a worker restart.

The project board derives New, Partial, Running, Complete, Failed, or Canceled
status from the actual graph. It streams step progress and error details over
server-sent events.

## Media pipeline

The complete media pipeline has thirteen steps:

1. **Ingest** — download the best audio with yt-dlp or extract/copy it from a
   local source.
2. **Download & keep media** — optionally archive a URL source as video and
   audio library artifacts.
3. **Transcript** — prefer site captions, then fall back to faster-whisper or
   Gemini ASR.
4. **Correction pass** — repair transcription mistakes using the correction
   glossary without summarizing or restyling the content.
5. **Summary** — produce a short source overview.
6. **Deep dive A** — independently analyze concepts, procedures, tools, and
   technologies.
7. **Deep dive B** — perform a second independent analysis.
8. **Merge** — deduplicate the two analyses while preserving their union,
   especially complete procedures and commands.
9. **Quick-references** — create or update cross-project tool, technique,
   concept, technology, and custom-category documents.
10. **Podcast script** — outline and expand a structured two-host script.
11. **Podcast audio** — render the script with Piper, Kokoro, or Gemini TTS.
12. **Trim audio** — identify conservative off-topic spans and remove them
    along with silence.
13. **Mind map** — produce a clickable topic graph linked to quick references.

Caption parsing understands WebVTT rolling captions and prefers manual
subtitles over auto-captions. Source-aware prompts retain `[HH:MM:SS]`
timestamps, and generated background material must be labeled as such.

The correction pass changes transcription errors only: misheard terms, damaged
commands, acronyms, and product names. Editing the correction glossary and
rerunning correction marks downstream consumers stale.

Deep-dive prompts require procedural material to remain complete. A walkthrough
must preserve its steps, commands, reasoning, and expected result rather than
becoming a one-sentence summary.

## Quick-reference accumulation

Quick references are cross-project documents. When an entity reappears,
Synapse can match its canonical name or alias to an existing document, merge
the new material, and record the source. The old document is snapshotted before
each merge so it can be viewed or restored.

Built-in document shapes are deliberately different:

- **Tool** — an instruction manual for something a user runs.
- **Technique** — prerequisites and exact steps for a specific goal.
- **Concept** — an explanation of an idea or principle.
- **Technology** — a primer on a platform, protocol, or standard.

Custom categories define their own extraction description and document prompt.

## Provenance and staleness

Generated artifacts record their effective source signature, upstream content,
model, prompt, and relevant settings. A changed source, glossary, prompt,
model, voice, or tuning value makes affected artifacts show **update
available** through the dependency graph.

A profile run rebuilds only missing or stale work. **Re-run downstream**
deliberately regenerates the selected step and every result that consumes it.

Tagging is project-level for ordinary artifacts. The richest available
document is tagged, and that tag set is propagated to the project's media and
text artifacts. Quick references are tagged individually because they combine
material from several projects. Proposed tags are sanitized before entering
the vocabulary.

## Grounded retrieval

The Library provides:

- exact full-text search using SQLite FTS5;
- optional local embeddings and Hybrid ranking;
- filters by source, artifact type, tag, and project;
- line-aware supporting excerpts; and
- grounded Q&A that cites retrieved excerpts and refuses to invent unsupported
  answers.

Citations are source-specific:

- media citations link to source timestamps;
- repository citations identify the immutable commit, file, and line range;
- paper citations identify the source hash, evidence block, page, section, and
  bounding box.

See
[Repository Analysis](https://github.com/Jarzembak/synapse/wiki/Repository-Analysis)
and
[Research Paper Analysis](https://github.com/Jarzembak/synapse/wiki/Research-Paper-Analysis)
for their specialized
pipelines.
