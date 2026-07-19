# Changelog

All notable changes to Synapse are documented here. Releases use Semantic
Versioning (`MAJOR.MINOR.PATCH`).

## [1.2.0] - 2026-07-19

Model-selection and cloud-sync usability.

### Added

- **`openai` provider:** OpenAI's own API joins `anthropic`/`gemini` as a
  frontier provider (`OPENAI_API_KEY` in `.env`), with its live model catalog
  in the matrix dropdowns and a readiness check in System. `openai_compat`
  is now explicitly for OpenAI-compatible servers that *aren't* OpenAI (it
  flags a base URL pointing at api.openai.com and steers you to the new
  provider).
- **Two-way cloud sync (opt-in):** Settings → Advanced → Cloud storage gains a
  sync-direction toggle. Two-way mode runs "Sync everything now" through
  `rclone bisync` for the library — edits, additions, and deletions propagate
  in both directions (newer side wins conflicts; the loser is kept with a
  `.conflict` suffix; mass-deletion runs abort as a safety stop). The first
  run establishes a newer-wins baseline (`--resync-mode newer`) that never
  deletes; a fresh baseline is forced automatically when the provider, its
  credentials, or the remote base changes. Every two-way pass finishes by
  rebuilding the index from the vault — chained into an embedding rebuild
  when semantic search is enabled — so pulled changes appear in the app.
  Archived media and per-artifact auto-upload stay one-way push.
- **Model dropdowns per provider:** every model field in the matrix (and the
  embedding-model field) now lists what the selected provider actually offers
  — installed models for `ollama`/`openai_compat`, the vendor's live model
  catalog for `anthropic`/`gemini` — with a "custom…" escape hatch and a
  refresh control (`GET /api/settings/provider-models` replaces
  `/api/settings/local-models`).
- **Install Ollama models from the UI:** Settings → Model matrix gains an
  "Install an Ollama model" field (`POST /api/settings/ollama/pull`) that runs
  `ollama pull` as a background job with live download progress in Jobs.
- README: a top-of-page quick reference for starting the stack in CPU or GPU
  mode (including making GPU the default via `COMPOSE_FILE` and why Docker
  Desktop's ▶ button doesn't switch modes), and a "Configuring models"
  rewrite clarifying that providers are server types, not locations, and
  where Ollama models come from.

### Changed

- The backend image pins a current rclone release with checksum verification
  (replacing Debian's packaged 1.60), required for reliable bisync and
  current provider token formats.
- Non-pipeline jobs (cloud sync, backups, index rebuilds, model installs) now
  show human-readable labels in Jobs.

## [1.1.0] - 2026-07-16

Local-LLM support overhaul.

### Added

- New `openai_compat` provider: assign any pipeline function — and semantic
  search embeddings — to any OpenAI-compatible local server (LM Studio,
  llama.cpp server, vLLM, LocalAI, Jan) via `OPENAI_COMPAT_BASE_URL` /
  `OPENAI_COMPAT_API_KEY` in `.env`.
- **Settings → Advanced → Local models**: per-call Ollama context window
  (`num_ctx`), model keep-alive, thinking on/off/auto for reasoning models,
  a request timeout for local providers, and a toggle for native JSON
  enforcement on structured steps.
- The model matrix and embedding-model field now suggest the models actually
  installed on each local server (new `GET /api/settings/local-models`), and
  System readiness reports the OpenAI-compatible server alongside Ollama.

### Changed

- The `ollama` provider now calls Ollama's native `/api/chat` instead of its
  OpenAI-compatibility shim. If you had pointed `OLLAMA_BASE_URL` at a
  non-Ollama server, move that URL to `OPENAI_COMPAT_BASE_URL` and assign
  those steps to `openai_compat`.
- Structured-output steps (trim spans, mind map, quick-ref matching, tagging,
  the podcast-script outline) ask local servers for native JSON (Ollama
  `format`, OpenAI-compatible `response_format`) instead of relying on
  prompting alone, and JSON parsing now tolerates `<think>` blocks and prose
  around the payload.
- Empty model responses are retried as transient instead of failing the step;
  local request timeouts are configurable (default raised from 180 s to 300 s).
- Ollama-assigned steps record the effective context-window and thinking
  settings in artifact provenance, so completed steps correctly show
  **update available** after those change (existing ollama-produced artifacts
  will show it once after this upgrade).

### Fixed

- Long transcript chunks sent to Ollama were silently truncated at the
  server's default context window (4k tokens in current releases) — far below
  the correction pass's ~24k-character chunks. Synapse now requests a 16k
  window per call by default, configurable up to 256k.

## [1.0.0] - 2026-07-12

This is the first stable release and a major production-readiness overhaul.

### Added

- Stale-aware pipeline profiles, provenance, and downstream reruns.
- Hybrid exact/semantic library search and source-grounded Q&A with citations
  and timestamp playback.
- Direct browser media uploads, backup scheduling/encryption/verification, and
  Markdown-vault recovery.
- System readiness checks, model-use accounting, browser notifications, and
  expanded Settings controls.
- CI, dependency locking/audits, Docker health checks, and loopback-only
  default exposure.

### Changed

- Hardened job cancellation/restart behavior, SQLite integrity, file writes,
  project deletion, provider timeouts, and accessibility/responsive UI behavior.
- Piper is the default text-to-speech provider for new installations.
