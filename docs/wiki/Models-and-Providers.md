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
work through a local Ollama endpoint and podcast audio through Piper. Evidence
mapping/final writing and hierarchical reduction have separate local-model
settings so a reduction-model change does not invalidate compatible leaf maps.
On upgrade, an existing mapper selection is retained while the newly separate
reducer starts with the Qwen 3.5 4B default.

## Local versus remote

`ollama` can point to:

- the bundled container;
- an Ollama daemon on the Docker host; or
- for ordinary media workflows, another machine on a trusted network.

Resource-fit admission (the blocked/recommended tiers in the model catalog)
applies only when Synapse can measure the Ollama host's memory — the bundled
container or a same-machine daemon. For `host.docker.internal` or a remote
address the Synapse containers cannot see that machine's RAM or GPU, so the
catalog reports resource status as unavailable and requests proceed;
capability and installation checks still run.

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

## RTX 5060 Laptop GPU (8 GB) profile

This capacity-balanced profile is tailored to an NVIDIA GeForce RTX 5060
Laptop GPU with 8 GB VRAM and approximately 32 GB of system memory. It is a
starting point rather than a universal model ranking. Runtime memory includes
model weights, context cache, working buffers, Docker, and other GPU users, so
an Ollama download size is not the complete VRAM requirement.

Both recommended Qwen 3.5 models passed Synapse's production-path paper
evidence and multipart-plan contracts after contract normalization. The
retained benchmark does not establish a reliable quality or speed difference
between them, so the assignments below combine those compatibility results
with the actual memory and context requirements of each pipeline.

| Workload | Model Matrix rows or setting | Recommended assignment |
|---|---|---|
| Media cleanup and classification | `correct`, `trim_spans`, `tag`, `mindmap` | `qwen3.5:4b-q4_K_M` |
| Media generation and Q&A | `summarize`, both deep dives, `merge`, `quickref`, `podcast_script`, `library_qa` | Start with `qwen3.5:4b-q4_K_M`; use `qwen3.5:9b-q4_K_M` when prose quality matters more than speed |
| GitHub repository analysis | Both model fields under **Settings → Local repository analysis** | `qwen3.5:4b-q4_K_M` |
| Local-only research papers | The same local repository model setting | `qwen3.5:4b-q4_K_M` |
| Cloud-enabled papers using local chat models | `paper_map`, `paper_reduce`, `paper_plan`, `paper_memory` | `qwen3.5:4b-q4_K_M` |
| Cloud-enabled paper prose | `paper_synthesis`, `paper_script` | `qwen3.5:9b-q4_K_M` when slower hybrid GPU/system-memory execution is acceptable; otherwise use the 4B model |
| Semantic search | **Settings → Search → Embedding model** | `nomic-embed-text`; consider `qwen3-embedding:0.6b` for multilingual and code-heavy libraries |
| English transcription | `asr` | faster-whisper `distil-large-v3` with the GPU overlay |
| Multilingual transcription | `asr` | faster-whisper `turbo` |
| Local podcast speech | `tts` | Piper with a medium voice; this path uses the CPU |

The repository map/writing model setting also controls every language-model
call for a local-only paper. Privacy enforcement overrides the individual
paper rows in the Model Matrix, forces Ollama, and disables thinking. Synapse
now sizes native Ollama context per call from the input, output budget, and
safety margin instead of forcing every restricted call to 65,536 tokens. The
4B Q4 model is approximately 3.4 GB, leaving much more room for dense context
than the approximately 6.6 GB 9B Q4 model. The 9B model can still run through
partial CPU offload when sufficient system memory is available, but it will be
slower.

The older `qwen3:8b` general-purpose default remains useful for ordinary 16K
work, but its current Ollama artifact declares a 40,960-token context. For this
hardware, Qwen 3.5 4B is the repository default.

Install the recommended chat models:

```bash
docker compose exec ollama ollama pull qwen3.5:4b-q4_K_M
docker compose exec ollama ollama pull qwen3.5:9b-q4_K_M
```

Optionally install the newer embedding alternative:

```bash
docker compose exec ollama ollama pull qwen3-embedding:0.6b
```

Recommended advanced settings:

- Keep **Context window** at 16,384 as the minimum for ordinary media,
  repository, and paper calls. Synapse raises it only when a particular prompt
  and output budget require more room, subject to the model's native limit.
- Set **Thinking** to **off** for predictable latency and structured output.
  Restricted runs enforce this setting automatically.
- Keep **JSON enforcement** enabled.
- Do not pin the 9B model with `keep_alive=-1` when faster-whisper also uses
  the GPU. Keep the normal five-minute setting, or use `0` to unload after each
  call when VRAM contention is visible.
- Check **System** while a job runs. Ollama's `PROCESSOR` value should be
  `100% GPU` for maximum throughput; a CPU/GPU split indicates offload.

The hardware and model details above are documented by
[NVIDIA's RTX 50-series laptop specifications](https://www.nvidia.com/en-us/geforce/laptops/50-series/),
the Ollama pages for
[Qwen 3.5 4B](https://ollama.com/library/qwen3.5:4b),
[Qwen 3.5 9B](https://ollama.com/library/qwen3.5:9b), and
[Qwen3 Embedding](https://ollama.com/library/qwen3-embedding:0.6b), plus
[Ollama's context and offload guidance](https://docs.ollama.com/context-length).
The ASR choices follow the
[faster-whisper model guidance](https://github.com/SYSTRAN/faster-whisper) and
[Distil-Whisper model card](https://huggingface.co/distil-whisper/distil-large-v3).

## Embeddings

Hybrid search can use local Ollama embeddings or a configured
OpenAI-compatible embedding server. The default Ollama embedding model is
`nomic-embed-text`:

```bash
docker compose exec ollama ollama pull nomic-embed-text
```

For a multilingual or code-heavy library, `qwen3-embedding:0.6b` is a current
639 MB alternative with a substantially larger context window. It is not the
shipping default and has not yet been compared with `nomic-embed-text` on a
retained Synapse retrieval benchmark.

After enabling semantic search or changing its model, rebuild the search index.
Embeddings from different models are not interchangeable. System readiness
reports a missing embedding model.

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
quantized 4B model. A quantized 9B model can also run, but large context
allocations are likely to spill into system memory and become much slower.
Model weight size is not the only requirement: reserve memory for the context
cache, Docker, extraction, and the operating system.

Use the System page to inspect loaded models, CPU/GPU utilization, and VRAM
while a job runs.
