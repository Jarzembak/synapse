# Changelog

All notable changes to Synapse are documented here. Releases use Semantic
Versioning (`MAJOR.MINOR.PATCH`).

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
