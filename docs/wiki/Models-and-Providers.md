# Models and providers

Every model-driven function has an independent provider and model setting under
**Settings → Model matrix**. A provider describes the API protocol, not
necessarily where the server runs.

## Chat providers

| Provider | Server | Typical use |
|---|---|---|
| `ollama` | Bundled Ollama or another Ollama URL | Local chat and repository analysis |
| `openai_compat` | LM Studio, llama.cpp, vLLM, LocalAI, Jan, or another compatible server | Self-hosted chat and embeddings |
| `anthropic` | Anthropic API | Claude models |
| `gemini` | Google Gemini API | Chat, native audio transcription, or multi-speaker TTS |
| `openai` | OpenAI API | OpenAI chat and reasoning models |

The default assignments use Ollama for correction, trim-span detection, and
tagging; Anthropic for several synthesis steps; and Gemini for the second deep
dive. Defaults can evolve, and Settings shows the effective matrix.

Changing an assignment takes effect on the next run without restarting
Synapse. You do not need accounts with every frontier provider. Reassign a
step to a provider you have configured.

Repository analysis is an exception: its privacy policy forces repository chat
work through a local Ollama endpoint and podcast audio through Piper.

## Local versus remote

`ollama` can point to:

- the bundled container;
- an Ollama daemon on the Docker host; or
- for ordinary media workflows, another machine on a trusted network.

`openai_compat` can similarly point to a compatible server on the host or
network. It is for OpenAI-compatible engines that are not OpenAI itself.
Select `openai` for OpenAI's cloud API because its current request formats,
including reasoning models, are not always accepted by compatibility servers.

Example LM Studio configuration from Docker:

```dotenv
OPENAI_COMPAT_BASE_URL=http://host.docker.internal:1234/v1
OPENAI_COMPAT_API_KEY=
```

Example remote Ollama configuration:

```dotenv
OLLAMA_BASE_URL=http://10.0.0.5:11434
```

The `/v1` suffix is normally required for OpenAI-compatible servers. Their
model lifecycle is managed in the server itself; Synapse lists the models the
server reports.

## Ollama model installation

Ollama models must be pulled before use. Browse
[ollama.com/library](https://ollama.com/library) for model names, sizes, and
quantizations. A model reference uses `name:tag`; omitting the tag means
`latest`.

Install a model in one of three ways:

1. **Settings → Model matrix → Install an Ollama model**;
2. `docker compose exec ollama ollama pull qwen3:8b`; or
3. `ollama pull qwen3:8b` directly on a remote Ollama host.

The first option runs a tracked background job and requires no terminal. Models
are installed on whichever server `OLLAMA_BASE_URL` addresses.

## Model dropdowns

Model fields list what the selected provider currently offers:

- installed Ollama models;
- models exposed by the OpenAI-compatible server; and
- provider model catalogs fetched using configured API credentials.

The OpenAI list is filtered to suitable chat models. Choose **custom…** to
enter an unlisted or not-yet-installed name. Use the refresh control after
installing or publishing a model.

## Local-model tuning

**Settings → Advanced → Local models** controls:

- **Context window** — `num_ctx` requested from Ollama per call. Synapse
  defaults to 16K for ordinary work; long deep dives may need more, subject to
  model and RAM/VRAM limits. Configure context directly in LM Studio,
  llama.cpp, or another OpenAI-compatible server.
- **Keep model loaded** — Ollama `keep_alive`. `5m` is the normal default,
  `-1` pins the model, and `0` unloads it after every call.
- **Thinking** — leave `auto`, disable reasoning for faster mechanical work, or
  force it for compatible reasoning models. Inline `<think>` blocks are
  stripped from saved results.
- **Request timeout** — defaults to 300 seconds. CPU-only generation or large
  context windows may require more.
- **JSON enforcement** — asks local servers for structured JSON on steps such
  as trimming, mind maps, matching, tagging, and podcast outlining. An
  OpenAI-compatible server that rejects `response_format` is retried without
  it automatically.

Ollama itself may cap the requested context at a model's native limit. A larger
window also consumes substantially more RAM or VRAM.

## Embeddings

Hybrid search can use local Ollama embeddings or a configured
OpenAI-compatible embedding server. The default Ollama embedding model is
`nomic-embed-text`:

```bash
docker compose exec ollama ollama pull nomic-embed-text
```

After enabling semantic search or changing its model, rebuild the search index.
System readiness reports a missing embedding model.

## ASR and TTS

Audio functions use their own provider choices:

| Function | Providers |
|---|---|
| ASR | faster-whisper locally, or Gemini native audio transcription |
| TTS | Piper locally, Kokoro locally, or Gemini multi-speaker speech |

Piper is the recommended fast local TTS default. Both Piper and Kokoro
currently run on CPU even when the GPU overlay is active.

## Capacity guidance

A consumer GPU with approximately 8 GB VRAM significantly accelerates a
quantized 7–9B model, but large context allocations can spill to system RAM and
become much slower. Model weight size is not the only requirement: reserve
memory for the context cache, Docker, extraction, and the operating system.

Use the System page to inspect loaded models, CPU/GPU utilization, and VRAM
while a job runs.
