# Media ingestion

Synapse accepts media through a URL, browser upload, or a read-only host-media
mount. Each route produces the same downstream transcript and learning
artifacts, but source ownership and storage differ.

## URL sources

Create a Media project and paste a URL from YouTube, Vimeo, or another
[yt-dlp-supported site](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md).

The normal **Ingest** step downloads the best audio needed for transcription.
The optional **Download & keep media** step also archives:

- the best video up to the configured resolution cap, merged to MP4; and
- an audio-only copy.

Archived media is registered in the Library, searchable, playable, seekable,
and downloadable. It remains under `data/media/<project>/`, while Markdown
sidecars in the library hold metadata and provenance.

For a source that requires an account, see
[Authenticated Media](https://github.com/Jarzembak/synapse/wiki/Authenticated-Media).

## Browser uploads

Choose **Upload a file** when the file is on the computer running the browser.
Synapse streams the upload into the project's private directory without nginx
buffering the entire request in memory or temporary disk.

The default maximum is 20 GiB and can be changed with `MAX_UPLOAD_BYTES`.
Uploaded media is removed when its project is deleted. Synapse retains a
playable transcription-audio sidecar.

## Mounted local files

A mount avoids copying a large existing collection into the upload request.
Set `HOST_MEDIA_DIR` in `.env` to the host directory:

```dotenv
HOST_MEDIA_DIR=D:\Videos
```

The directory appears read-only as `/host-media` in the containers. Enter a
path relative to the configured directory when creating the project. For
example, for `D:\Videos\talks\recon.mp4`, enter:

```text
talks/recon.mp4
```

The default `HOST_MEDIA_DIR` is `./data/media` inside the checkout.

## URL safety

URL sources reject loopback, link-local, and private IP literals by default.
Enable trusted-network sources only when intentional:

```dotenv
ALLOW_PRIVATE_URLS=true
```

Credentials embedded in URLs are always rejected. Use the authenticated-media
workflow instead. Synapse does not bypass DRM; only ingest material you are
authorized to access and retain.

## Captions and transcription

Transcript generation prefers the site's own captions:

1. manual subtitles;
2. automatic captions; then
3. ASR when captions are unavailable.

WebVTT rolling captions are deduplicated. ASR can use local faster-whisper or
Gemini native audio transcription, selected in **Settings → Model matrix**.

The local faster-whisper model size is configured on the `asr` row. Under
**Settings → Advanced → ASR options**, you can:

- enable or disable voice-activity detection;
- provide a language hint, or leave it blank for automatic detection; and
- select compute settings under **Advanced → Compute**.

Disable voice-activity detection if quiet words are being dropped.

## Correction glossary

The correction pass repairs transcription errors without summarizing or
rewriting the source. Add names, acronyms, shell commands, and specialist terms
to the glossary in Settings. After editing it, rerun correction; downstream
artifacts will show that an update is available.

## Download and compute settings

Settings includes a maximum archived-video resolution. Ollama and
faster-whisper can use the NVIDIA GPU overlay. Piper and Kokoro currently use
the CPU.

For a remote Ollama server used by ordinary media projects:

```dotenv
OLLAMA_BASE_URL=http://10.0.0.5:11434
```

For LM Studio, llama.cpp, vLLM, or another OpenAI-compatible server:

```dotenv
OPENAI_COMPAT_BASE_URL=http://10.0.0.5:1234/v1
```

Repository analysis has a stricter local-only boundary and does not accept a
LAN Ollama endpoint. See
[Repository Analysis](https://github.com/Jarzembak/synapse/wiki/Repository-Analysis).
