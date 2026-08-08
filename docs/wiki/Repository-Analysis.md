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

All repository analysis is local-only by default, including public
repositories. While a project is local-only:

- evidence mapping and final writing use the configured repository Ollama
  model, while hierarchical reduction has an independent local-model setting;
- podcast audio uses local Piper TTS;
- repository excerpts cannot be sent to cloud or OpenAI-compatible providers;
- the Ollama endpoint must be the bundled service or loopback; and
- model names containing a `cloud` token are rejected.

**Cloud analysis is opt-in per project** from the project page. Public
repositories opt out of local-only with one click; a private repository
additionally requires typing its full name to record explicit consent, and
that consent is revoked automatically whenever the repository's visibility
changes (the project falls back to local-only until re-consented). Enabling
cloud analysis lets steps follow the global model matrix — the cloud-default
deep dives, merge, and quick references use frontier providers, while
map/reduce stay on their configured (typically local) models. Repository
artifacts remain excluded from cloud vault sync regardless of this setting,
and disabling it re-restricts the project and purges any formerly-eligible
cloud copies through the same durable outbox used when a public repository
turns private.

Compose sets `OLLAMA_NO_CLOUD=1`. If you provide another loopback Ollama
daemon, launch it with the same setting. Environment HTTP proxies are bypassed
for repository model calls.

Synapse sizes each Ollama context request from the actual prompt, output
budget, and a safety margin, then respects the model's advertised native
window. Short map and reduction calls therefore do not reserve an unnecessary
64K key-value cache. Synapse still uses bounded hierarchical prompts rather
than sending an unbounded repository in one request.

Successful reductions are cached by the pinned snapshot, exact input, prompt,
model tag and installed digest, parameters, resolved context settings, and
reduction-contract version. The cache is shared by the
inventory, overview, usage, architecture, knowledge, environment, and
deep-dive guides. If a reduction times out or produces unusable structured
output, Synapse subdivides that batch instead of repeating the same request.
The Jobs and project pages show the effective model, context decision,
reduction location, cache reuse, and subdivision history.

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

Unchanged evidence summaries and successful hierarchical reductions can be
reused by content and configuration hash.

When upgrading a pre-v5 installation, Synapse preserves the existing mapping-model
selection and compatible leaf maps, while the new reduction role adopts Qwen
3.5 4B unless you explicitly select another reducer. Pre-v5 leaf maps did not
record an immutable Ollama digest, so their reuse is counted and labeled in
coverage diagnostics. Newly generated maps are bound to the installed digest.

## Capacity and backups

The default repository model download is approximately 3.4 GB before runtime
and context-cache overhead. CPU-only analysis of a large repository can still
take hours. Each retained commit may consume up to the configured 512 MiB
compressed and 1 GiB expanded limits.

The System startup checks report whether both configured repository models are
installed.

Repository origin is sticky even after project deletion or a derived
quick-reference merge. This prevents retained repository material from later
being reclassified for cloud sync.

`BACKUP_ENCRYPTION_KEY` is required before Synapse will back up a library that
contains repository analysis. Raw repository snapshots are excluded by
default; opting them into backups also requires encryption because source
trees can contain credentials that static exclusions did not detect.
