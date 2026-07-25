# Configuration

Most user-facing configuration lives in Settings. Environment variables handle
deployment boundaries and provider secrets; advanced model and pipeline
behavior can be changed without editing code.

## Model matrix

Every LLM-driven step has an independent provider/model assignment. ASR, TTS,
and embeddings have their own applicable providers. See
[Models and Providers](https://github.com/Jarzembak/synapse/wiki/Models-and-Providers).

## Pipeline profiles

A pipeline profile selects the steps that should exist for a use case. Built-in
profiles cover full production, research library, quick notes, and audio
edition. Custom profiles can combine the available steps.

Running a profile creates only missing or stale outputs; it does not
unconditionally regenerate completed work.

## Prompt editor

**Settings → Advanced → Prompt editor** exposes the system prompts used by
pipeline functions, including:

- correction;
- both media deep dives and merge;
- entity extraction;
- built-in and custom quick-reference document shapes;
- podcast outline and segments;
- trim-span detection;
- mind-map generation; and
- tagging.

A modified badge marks an override. **Reset to default** removes the override.
Changes apply the next time the step runs and participate in artifact
staleness.

Paper structured-output prompts retain mandatory contracts even when customized
so evidence coverage and lineage cannot be accidentally removed.

## Generation parameters

Set temperature and maximum output tokens independently for model functions.
Lower temperature is useful for correction and structured extraction; a higher
output ceiling may be necessary for unusually long source material.

## Audio

Audio settings include:

- the selected host voices;
- speaking speed;
- pause length between dialogue lines;
- parallel TTS worker count (`0` means automatic);
- trim silence threshold in dB; and
- minimum silence duration.

Piper renders dialogue lines in separate processes, so parallelism can reduce
wall time until CPU or memory becomes saturated. Kokoro and Piper are currently
CPU-based.

## Media pipeline

Media tuning includes:

- correction-pass chunk size;
- concise, standard, or exhaustive deep-dive depth;
- target podcast segment count (`0` lets the model decide);
- maximum tags per artifact;
- whether auto-tagging may create vocabulary entries;
- archived-video resolution; and
- the correction glossary.

Large chunks still need to fit the effective model context. Synapse records
relevant settings in provenance so affected consumers become stale after a
change.

## ASR and compute

ASR settings include:

- faster-whisper model size in the Model matrix;
- voice-activity detection;
- an optional source-language hint; and
- device and compute type under **Advanced → Compute**.

Device `auto` uses CUDA when available and falls back to CPU. `float16` is a
normal GPU choice; `int8` is useful on CPU. Disable voice-activity detection if
quiet speech is being omitted.

## Tags and quick-reference categories

The tag vocabulary is shared and editable. Auto-tagging can be restricted to
existing vocabulary or allowed to propose new tags. Generated proposals are
sanitized; deliberately created vocabulary entries remain trusted.

A custom quick-reference category defines:

- a stable key;
- display label and icon;
- library folder;
- an extraction description; and
- a document-writing prompt.

The key and folder cannot change after creation. A category that owns documents
cannot be deleted until its documents are removed.

## Desktop notifications

Completion notifications can be enabled in Settings, subject to browser
notification permission.

## Relevant environment variables

Consult `.env.example` for the complete, current list. Common deployment values
include:

```dotenv
ANTHROPIC_API_KEY=
GEMINI_API_KEY=
OPENAI_API_KEY=
OPENAI_COMPAT_BASE_URL=
OPENAI_COMPAT_API_KEY=
OLLAMA_BASE_URL=http://ollama:11434

HOST_MEDIA_DIR=./data/media
MAX_UPLOAD_BYTES=
ALLOW_PRIVATE_URLS=false
SYNAPSE_BIND_ADDRESS=127.0.0.1
SYNAPSE_PUBLIC_ORIGIN=http://localhost:8080

AUTH_BROWSER_SESSION_MINUTES=30
AUTH_BROWSER_SESSION_SECONDS=1800
MEDIA_EGRESS_CONNECTION_TIMEOUT_SECONDS=21600

BACKUP_ENCRYPTION_KEY=
SETTINGS_ENCRYPTION_KEY=
LOG_LEVEL=INFO
```

Restart the affected containers after changing environment values. Settings
changes made in the browser ordinarily take effect on the next relevant job
without a restart.

`SYNAPSE_PUBLIC_ORIGIN` is the exact canonical origin users open in their
browser. It protects the authenticated-media controls and tokenized noVNC
relay from Host-header and cross-origin requests. It must contain only
`http(s)://host[:port]`, with no path. For Vite development,
`SYNAPSE_DEV_PUBLIC_ORIGIN` defaults to `http://localhost:5173` and the
development Compose overlay gives the API the matching value.

The Compose stack sets `MEDIA_EGRESS_PROXY_URL` internally so yt-dlp uses the
public-address-only, DNS-pinning proxy. You normally should not add it to
`.env`. A non-Compose installation that ingests URL media must run the guarded
proxy and set this value explicitly. When
`ALLOW_PRIVATE_URLS=false`, all URL media retrieval fails closed if the
boundary is absent.

## Storage and cloud settings

Backup scheduling, retention, source-media inclusion, and cloud synchronization
have additional security and recovery implications. See
[Storage, Backups, and Cloud Sync](https://github.com/Jarzembak/synapse/wiki/Storage-Backups-and-Cloud-Sync).
