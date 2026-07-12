# Changelog

All notable changes to Synapse are documented here. Releases use Semantic
Versioning (`MAJOR.MINOR.PATCH`).

## [1.1.0] - 2026-07-12

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
