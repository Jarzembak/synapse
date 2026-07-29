# Getting started

Synapse is a containerized application. Docker is the only host dependency for
ordinary use:

- Docker Desktop on Windows or macOS; or
- Docker Engine with the Compose plugin on Linux.

Model downloads, speech models, media tools, and application services live in
containers or persistent Docker volumes.

## Install and start

```bash
git clone https://github.com/Jarzembak/synapse.git
cd synapse
cp .env.example .env
docker compose up --build
```

The first build takes longer because Docker must build the application images.
Day-to-day startup is:

```bash
docker compose up -d
```

Use `--build` after pulling application code changes.

## Install local models

In another terminal, once the containers are running:

```bash
docker compose exec ollama ollama pull qwen3:8b
docker compose exec ollama ollama pull qwen3.5:4b-q4_K_M
```

The 8B model remains the general local-chat default. The smaller Qwen 3.5 model
is the default for repository mapping and hierarchical reduction because it
leaves more GPU memory for dense context. If you enable Hybrid semantic search,
install the default embedding model too:

```bash
docker compose exec ollama ollama pull nomic-embed-text
```

You can perform both installations in the browser from **Settings → Model
matrix → Install an Ollama model**. Downloads run as background jobs with live
progress.

Open [http://localhost:8080](http://localhost:8080).

## Optional API providers

Edit `.env` and fill in only the providers you use:

```dotenv
ANTHROPIC_API_KEY=
GEMINI_API_KEY=
OPENAI_API_KEY=
```

The keys are independently optional. Before running a pipeline, use **Settings
→ Model matrix** to assign every step to a provider for which you have a model
or key.

For a self-hosted OpenAI-compatible server, configure:

```dotenv
OPENAI_COMPAT_BASE_URL=http://host.docker.internal:1234/v1
OPENAI_COMPAT_API_KEY=
```

The API key may remain blank when your server does not require one. See
[Models and Providers](https://github.com/Jarzembak/synapse/wiki/Models-and-Providers) for the provider matrix and
remote-server details.

## NVIDIA GPU mode

The default Compose stack is CPU-compatible. To give Ollama and
faster-whisper access to an NVIDIA GPU:

```bash
docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d --build
```

The GPU overlay also lets the System page read `nvidia-smi`. Piper and Kokoro
TTS remain CPU-based.

To make GPU mode the default for plain `docker compose` commands, add one of
the following to `.env`.

Windows:

```dotenv
COMPOSE_FILE=docker-compose.yml;docker-compose.gpu.yml
COMPOSE_PATH_SEPARATOR=;
```

Linux or macOS:

```dotenv
COMPOSE_FILE=docker-compose.yml:docker-compose.gpu.yml
```

Docker Desktop's start button restarts containers in the mode in which they
were last created. Run the desired `docker compose ... up` command once when
switching modes. Check the active mode with:

```bash
docker compose exec ollama nvidia-smi
```

GPU details indicate GPU mode; an error indicates CPU mode.

## Create a first project

1. Open **Projects**.
2. Choose **Media**, **GitHub repository**, or **Research paper**.
3. Enter or upload the source and review its settings.
4. Open the new project.
5. Choose a built-in pipeline profile or run steps individually from the
   pipeline board.
6. Follow live progress from the board or **Jobs**.
7. Open completed artifacts from the project or find them in **Library**.

For media URLs, see
[Media Ingestion](https://github.com/Jarzembak/synapse/wiki/Media-Ingestion).
Sources that require a sign-in are covered by
[Authenticated Media](https://github.com/Jarzembak/synapse/wiki/Authenticated-Media).

## First-run downloads

The selected faster-whisper ASR model and Piper or Kokoro voice models download
on first use and remain cached. The first transcription or TTS run therefore
takes longer than subsequent runs.

## Network boundary

The standard stack exposes the frontend only on `127.0.0.1:8080`; the API
remains on Docker's private network and is reached through the frontend proxy.
Synapse does not currently provide application authentication.

To intentionally expose the application on a trusted LAN, set:

```dotenv
SYNAPSE_BIND_ADDRESS=0.0.0.0
```

Restart the stack after changing `.env`. Anyone who can reach the host can then
access the application and library. Do not expose port 8080 directly to the
internet. Use a trusted reverse proxy with authentication and TLS if remote
access is necessary.

## Next steps

- Learn the interface in
  [Using Synapse](https://github.com/Jarzembak/synapse/wiki/Using-Synapse).
- Understand the pipeline in
  [How Synapse Works](https://github.com/Jarzembak/synapse/wiki/How-Synapse-Works).
- Choose models in
  [Models and Providers](https://github.com/Jarzembak/synapse/wiki/Models-and-Providers).
- Configure backups before building a large library in
  [Storage, Backups, and Cloud Sync](https://github.com/Jarzembak/synapse/wiki/Storage-Backups-and-Cloud-Sync).
