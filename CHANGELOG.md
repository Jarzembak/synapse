# Changelog

All notable changes to Synapse are documented here. Releases use Semantic
Versioning (`MAJOR.MINOR.PATCH`).

## [1.2.0] - 2026-07-17

### Added

- GitHub.com repository projects pinned to immutable commits, including
  encrypted read-only private-repository access and manual update checks.
- Static repository inventory, dependency/environment detection, hierarchical
  evidence analysis, file-and-line citations, and repository-grounded search.
- Repository overview, setup and usage, architecture, required-knowledge, and
  environment guides alongside deep dives, quick-references, podcast output,
  and mind maps.

### Security

- All repository source and derived artifacts, public or private, are restricted
  to local Ollama/Piper processing, bypass proxy environment variables, carry a
  sticky repository-origin marker, and are excluded from cloud synchronization.
- Repository archives are extracted with traversal, link, collision, bomb,
  file-count, size, secret-redaction, workload, and model-fan-out defenses;
  repository code is never executed.
- Recovery fails closed for GitHub-derived material, raw snapshot backups always
  require encryption, and backup/deletion/audio publication use coherent
  lifecycle guards.

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
