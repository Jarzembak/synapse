# Operations and troubleshooting

## Health and resource monitoring

The System page reports:

- database, Redis, worker, parser-worker, and media-tool readiness;
- Ollama and embedding-model availability;
- optional provider credentials;
- free disk space;
- CPU and memory use;
- GPU, VRAM, and loaded Ollama models;
- active jobs; and
- library/index integrity.

Use these checks before interpreting a failed pipeline step as a source or
model problem.

## Logs

API, worker, and scheduler logs are written to container stdout and rotating
files:

```text
data/logs/synapse-api.log
data/logs/synapse-worker.log
data/logs/synapse-llm-worker.log
data/logs/synapse-beat.log
```

Each file rotates at 5 MB and retains three rotations. Pipeline steps log
start, completion, and failure; background enqueue failures and caption
fallbacks emit warnings.

The in-app **Logs** page can select a service, severity, text filter, and tail
length, refresh every two seconds, freeze the display, and download the visible
tail.

Set the level in `.env` and restart:

```dotenv
LOG_LEVEL=DEBUG
```

Raw endpoints:

```text
GET /api/logs
GET /api/logs/worker?lines=200
```

You can also use:

```bash
docker compose logs --tail 200 worker
```

## Network exposure

The production stack binds the frontend to `127.0.0.1:8080` and leaves the API
on the private Compose network. The development overlay optionally publishes
port 8000, also on loopback by default.

Synapse has no application-level user accounts. If you set
`SYNAPSE_BIND_ADDRESS=0.0.0.0`, everyone who can reach the host receives full
access. Do not publish port 8080 directly to the internet; put authentication
and TLS in a trusted reverse proxy first.

## Common problems

### Missing prerequisite artifact

Pipeline steps have dependencies. Run earlier required steps or select a
profile that includes them.

### Projects are queued but nothing runs

Whole-project jobs are serialized. Worker startup normally marks interrupted
work and resumes the oldest durable run. Check Redis and worker readiness, then
use **Continue queue** if the handoff could not occur.

### A completed step says update available

Its source, upstream artifact, prompt, model, voice, glossary, or tuning changed.
Run the profile to rebuild stale outputs, or use **Re-run downstream** on the
earliest result you intentionally changed.

### Hybrid search only finds exact phrases

Enable semantic search, confirm the selected embedding model is installed, and
run **Rebuild search index**. The System page reports missing models.

### Transcription is slow

Local faster-whisper on CPU is compute-intensive. Use the NVIDIA GPU overlay or
assign ASR to Gemini. Confirm the device and compute type under **Settings →
Advanced → Compute**.

### Transcription drops quiet speech

Disable voice-activity detection and rerun transcription.

### Podcast audio is slow

Piper and Kokoro currently use CPU. Piper is usually faster. Increase **TTS
parallel workers** carefully and inspect CPU/memory on System.

### Correcting a TTS error

TTS renders the podcast script. Correct the affected host line in the script,
or correct the upstream terminology/glossary and regenerate the script, then
rerun podcast audio. Voice/model/speed changes also make the audio stale.
Series-based papers should regenerate from the relevant part so continuity
staleness is applied correctly.

### A frontier provider reports authentication failure

Verify the matching API key in `.env`, restart after environment changes, and
check the provider readiness card.

### yt-dlp rejects a URL

Inspect the worker error. The site may require authentication, may no longer be
supported by the bundled yt-dlp version, may use DRM, or may be blocking the
server's network. See
[Authenticated Media](https://github.com/Jarzembak/synapse/wiki/Authenticated-Media).

### A structured-output step fails

Local models vary in JSON reliability. Synapse asks compatible servers for
native JSON and retries certain unsupported response formats. If one model
consistently fails, assign that function to a stronger structured-output model.

### A model forgets the beginning of a long input

The effective context window is too small. Increase Ollama's context window in
Advanced settings or the server-side window for an OpenAI-compatible provider.
Confirm the model supports the requested length and the host has enough memory.

### An OpenAI-compatible step fails immediately

Verify `OPENAI_COMPAT_BASE_URL`, including `/v1`, restart, and inspect the
System readiness card. Refresh the Model matrix after the server is reachable.

### Cloud sync fails

Read the rclone error under cloud settings. Common causes include:

- incorrect S3 endpoint, bucket, or region;
- a normal WebDAV password instead of an app password;
- an expired OAuth token; or
- a changed remote folder requiring a new baseline.

See
[Storage, Backups, and Cloud Sync](https://github.com/Jarzembak/synapse/wiki/Storage-Backups-and-Cloud-Sync).

### Google Drive contains duplicate artifacts

Run **Sync everything now**. The final Drive dedupe pass keeps the newest
same-name copy.

## Current limitations

- ElevenLabs is not wired into TTS. Working providers are Piper, Kokoro, and
  Gemini.
- Synapse is designed for a trusted single-user deployment and has no
  application authentication.
- Piper and Kokoro are CPU-only under the current image. Ollama and
  faster-whisper can use the GPU overlay.
- Cloud sync has no scheduler. Auto-upload and manual full sync are the
  available triggers.
- Two-way sync applies only to the Markdown library; archived media remains
  push-only.
- Job recovery partitions running-job ownership by dispatch queue: the
  ordinary worker, the `llm-worker` (serial `local_llm` queue), and the paper
  worker each reset only their own jobs on restart. Scaling a queue to
  independent replicas needs distributed leasing.
- If local-model steps (media correction, repository analysis, tagging,
  semantic indexing) sit queued while cloud steps run, check that
  `llm-worker` is up — it is the only consumer of the `local_llm` queue.
- Embeddings are stored in SQLite and scored in process. A very large
  multi-user library would need a dedicated vector index.
- Paper v1 does not interpret charts or diagrams, search external scholarly
  literature, ingest books above configured limits, or create multipart GitHub
  series.

## Why local TTS is CPU-only

Piper and Kokoro share `onnxruntime`. A prior `onnxruntime-gpu` attempt required
a CUDA runtime incompatible with the faster-whisper/ctranslate2 image and broke
both TTS engines. The image therefore uses CPU ONNX Runtime. A future GPU
change requires a compatible build or an image-wide CUDA upgrade followed by
ASR validation.
