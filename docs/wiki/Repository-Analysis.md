# GitHub repository analysis

Choose **Projects → GitHub repository** to turn a public or private GitHub.com
codebase into a source-grounded learning project.

Synapse resolves the selected branch, tag, or commit to an immutable commit SHA
and downloads that exact archive. Analysis is static: Synapse never runs
repository hooks, installs packages, initializes submodules, downloads Git LFS
objects, builds, tests, or executes repository code.

## Generated material

The repository pipeline creates:

- a deterministic inventory and coverage report;
- a plain-language overview;
- setup and usage instructions supported by commands found in the repository;
- an architecture and code map;
- required knowledge and a suggested learning order;
- dependency and environment guidance;
- two independent deep dives;
- a merged study guide;
- quick references;
- a two-host podcast script and local audio; and
- a repository-aware mind map.

Claims cite file and line ranges at the analyzed commit. The retained snapshot
feeds project-filtered Library search and **Ask this repository**, whose
citations link directly to supporting code.

## Private repositories

Open **Settings → GitHub access** and provide a fine-grained personal access
token limited to the selected repositories, with read-only **Contents**
permission.

The token is encrypted in the settings store, masked after save, and never
written to a project, artifact, command, URL, or log.

## Local-only policy

All repository analysis is local-only in this release, including public
repositories:

- chat steps use the configured repository Ollama model;
- podcast audio uses local Piper TTS;
- repository artifacts are excluded from cloud sync;
- repository excerpts cannot be sent to cloud or OpenAI-compatible providers;
- the Ollama endpoint must be the bundled service or loopback; and
- model names containing a `cloud` token are rejected.

Compose sets `OLLAMA_NO_CLOUD=1`. If you provide another loopback Ollama
daemon, launch it with the same setting. Environment HTTP proxies are bypassed
for repository model calls.

Compose requests a 65,536-token context allocation for repository work, though
models may cap it at their native window. Synapse uses bounded hierarchical
prompts rather than sending an unbounded repository in one request.

## Scope and coverage

An import can cover:

- the whole repository;
- one folder; or
- explicit include and exclude paths.

Generated, vendored, cached, binary, secret-prone, minified, and oversized
files are cataloged or excluded under configurable limits. Manifests provide
dependency evidence; lockfiles are cataloged and admitted as bounded support.
The coverage report records exclusions rather than silently truncating them.

## Updates

The project remembers its selected branch or ref but never changes the analyzed
commit automatically.

1. Use **Check for updates** to compare the pinned commit with GitHub.
2. Use **Update analysis** to capture a new snapshot and rebuild affected
   artifacts.

Unchanged evidence summaries can be reused by content and configuration hash.

## Capacity and backups

The default model download is approximately 5 GB before runtime and context
cache overhead. CPU-only analysis of a large repository can take hours. Each
retained commit may consume up to the configured 512 MiB compressed and 1 GiB
expanded limits.

The System startup checks report whether the configured repository model is
installed.

Repository origin is sticky even after project deletion or a derived
quick-reference merge. This prevents retained repository material from later
being reclassified for cloud sync.

`BACKUP_ENCRYPTION_KEY` is required before Synapse will back up a library that
contains repository analysis. Raw repository snapshots are excluded by
default; opting them into backups also requires encryption because source
trees can contain credentials that static exclusions did not detect.
