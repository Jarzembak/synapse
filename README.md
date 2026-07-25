# Synapse

Synapse is a self-hosted knowledge-production studio for media, software
repositories, and research papers. Give it a talk, a codebase, or a dense PDF
and it creates source-grounded learning material: transcripts, summaries,
deep dives, study guides, quick references, podcasts, mind maps, and searchable
Q&A.

The application runs in Docker and keeps a browsable Markdown library on your
machine. You can use bundled local models, connect another local model server,
or assign Anthropic, Gemini, and OpenAI models independently to different
pipeline steps.

## What Synapse can process

- **Video and audio** — browser uploads, mounted local files, and URLs supported
  by [yt-dlp](https://github.com/yt-dlp/yt-dlp), including authenticated sources
  when you provide a valid browser session or cookies.
- **GitHub repositories** — public or private repositories pinned to an exact
  commit, analyzed without executing repository code, with file-and-line
  citations.
- **Research papers** — PDF extraction with local OCR, page-grounded evidence,
  quality review, paper Q&A, and independently planned Generalist,
  Practitioner, and Expert multipart series.

## Highlights

- A staged, resumable pipeline with live progress and targeted regeneration.
- Exact SQLite FTS5 search plus optional semantic retrieval.
- Answers grounded in timestamped media, repository lines, or PDF pages.
- Cross-project quick-reference documents that improve as new sources arrive.
- Two-host podcast scripts and local or cloud text-to-speech.
- Per-step provider, model, prompt, temperature, and output-token controls.
- Local Ollama and OpenAI-compatible servers such as LM Studio, llama.cpp,
  vLLM, LocalAI, and Jan.
- Optional Anthropic, Gemini, and OpenAI providers.
- Markdown-first storage that can also be opened as an Obsidian vault.
- Encrypted backups and optional S3, WebDAV, Google Drive, Dropbox, or OneDrive
  synchronization.
- Local-only privacy boundaries for repository projects and eligible paper
  projects.

## Quick start

You only need Docker Desktop on Windows or macOS, or Docker Engine with Compose
on Linux.

```bash
git clone https://github.com/Jarzembak/synapse.git
cd synapse
cp .env.example .env
docker compose up --build
```

In another terminal, install the default local chat model:

```bash
docker compose exec ollama ollama pull qwen3:8b
```

For optional semantic search, also install the default embedding model:

```bash
docker compose exec ollama ollama pull nomic-embed-text
```

Open [http://localhost:8080](http://localhost:8080). You can also install Ollama
models later from **Settings → Model matrix**.

API keys are optional. Add only the providers you intend to use to `.env`, then
assign their models in **Settings → Model matrix**:

```dotenv
ANTHROPIC_API_KEY=
GEMINI_API_KEY=
OPENAI_API_KEY=
```

Without cloud keys, reassign cloud-backed steps to Ollama or another local
provider before running them.

### NVIDIA GPU

Start with the GPU overlay to accelerate Ollama and faster-whisper:

```bash
docker compose -f docker-compose.yml -f docker-compose.gpu.yml up --build
```

To make that overlay the default on Windows, add this to `.env`:

```dotenv
COMPOSE_FILE=docker-compose.yml;docker-compose.gpu.yml
COMPOSE_PATH_SEPARATOR=;
```

On Linux and macOS, use
`COMPOSE_FILE=docker-compose.yml:docker-compose.gpu.yml` instead.

### Create your first project

1. Open **Projects**.
2. Choose a media URL/file, GitHub repository, or research paper.
3. Review the project settings and privacy policy.
4. Run a built-in pipeline profile, or run individual steps from the pipeline
   board.
5. Open the generated artifacts from the project or search them in **Library**.

The default deployment binds only to `127.0.0.1`. Synapse does not provide
multi-user authentication, so do not expose it directly to the internet.

## Documentation

Detailed documentation is maintained as wiki-ready Markdown in
[`docs/wiki`](docs/wiki/Home.md):

- [Getting started](docs/wiki/Getting-Started.md)
- [Using Synapse](docs/wiki/Using-Synapse.md)
- [How Synapse works](docs/wiki/How-Synapse-Works.md)
- [Media ingestion](docs/wiki/Media-Ingestion.md)
- [Authenticated media](docs/wiki/Authenticated-Media.md)
- [GitHub repository analysis](docs/wiki/Repository-Analysis.md)
- [Research paper analysis](docs/wiki/Research-Paper-Analysis.md)
- [Models and providers](docs/wiki/Models-and-Providers.md)
- [Configuration](docs/wiki/Configuration.md)
- [Storage, backups, and cloud sync](docs/wiki/Storage-Backups-and-Cloud-Sync.md)
- [Operations and troubleshooting](docs/wiki/Operations-and-Troubleshooting.md)
- [Development](docs/wiki/Development.md)

The [wiki publishing guide](docs/wiki/Wiki-Publishing.md) explains how these
pages map to GitHub's separate wiki repository.

## Development

```bash
cd backend
pip install -r requirements-dev.txt
pytest tests -q
```

```bash
cd frontend
npm ci
npm run typecheck
npm run build
```

See [Development](docs/wiki/Development.md) for the hot-reload stack,
dependency-lock workflow, Compose validation, and logging.

## Project notes

Synapse was vibe coded by Fable and Sol, inspired by Jeff McJunkin's
methodology.
