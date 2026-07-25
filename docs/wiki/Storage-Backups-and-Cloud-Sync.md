# Storage, backups, and cloud sync

Synapse stores generated content as Markdown plus an SQLite index. Archived
media and working files live separately so the library remains portable.

## Library layout

Markdown is the source of truth for generated text; SQLite indexes search,
sorting, relationships, jobs, and metadata. You can grep, version, synchronize,
or back up `data/library/` directly. It can also be opened as a second Obsidian
vault because documents use standard Markdown, YAML frontmatter, and
`[[wikilinks]]`.

Typical layout:

```text
data/library/
├── projects/<project-slug>/
│   ├── transcript.md
│   ├── corrected.md
│   ├── summary.md
│   ├── deepdive_claude.md
│   ├── deepdive_gemini.md
│   ├── deepdive_merged.md
│   ├── podcast_script.md
│   ├── podcast_audio.md
│   ├── podcast_audio.mp3
│   ├── trimmed_audio.md
│   ├── trimmed_audio.mp3
│   ├── source_video.md
│   ├── source_audio.md
│   └── mindmap.md
├── tools/
├── techniques/
├── concepts/
├── technologies/
├── <custom-category-folder>/
└── .history/
```

Paper and repository projects add their own evidence, coverage, track, and
source metadata under the project directory.

Frontmatter includes artifact type, title, project, timestamps, provider,
model, tags, effective input/configuration provenance, and applicable source
signatures. Quick references also record aliases and contributing projects.

## Media storage

Working and archived media live under `data/media/<project>/`:

- transcription working audio;
- yt-dlp temporary files and project authentication state;
- browser uploads;
- archived `source_video` and `source_audio` files.

Markdown sidecars keep permanent downloads searchable and playable without
putting large binaries in the vault. Back up `data/media/` or opt source media
into Synapse backups if the archived copies matter.

## Backup contents and consistency

Synapse creates ZIP snapshots containing:

- an SQLite snapshot;
- the Markdown library; and
- optionally archived or uploaded source media.

Backups are written to `data/backups/`, shared by the API and worker. The
backup process waits for processing jobs, copies files to a stable staging
area, verifies they did not change, and then captures the database.

Verification checks the ZIP CRC and runs SQLite's integrity check against the
contained database. Create, verify, download, and list snapshots from
**System → Backups**.

The `beat` service checks hourly whether a scheduled backup is due. Scheduling
is disabled by default, and retention defaults to the five newest archives.
Configure interval, retention, and media inclusion under **Settings →
Backups**.

## Encryption and key recovery

Set a long, random backup key in `.env`:

```dotenv
BACKUP_ENCRYPTION_KEY=<store-this-in-a-password-manager>
```

There is no recovery route if this key is lost. Unencrypted ZIP backups are
allowed only when the project privacy policies permit them. A library
containing repository analysis or applicable local-only papers requires
encrypted backups.

Saved cloud and GitHub credentials are protected by:

```dotenv
SETTINGS_ENCRYPTION_KEY=<portable-stable-key>
```

When this is blank, Synapse generates `data/db/.settings.key`. That file is not
inside the database backup. For portable disaster recovery, either configure
and retain the environment key or secure a separate copy of `.settings.key`.

Backups stored on the same host are not sufficient disaster recovery. Copy
verified archives to another device or storage provider.

## Vault recovery

**System → Library integrity → Rebuild index from vault** reconstructs projects,
artifacts, quick-reference relationships, tags, full-text search rows, and
retrieval chunks from Markdown without overwriting it.

Project deletion stages folders before changing the database. Startup resolves
staging left by a power loss, either restoring or completing the operation.

## Cloud sync overview

Synapse uses [rclone](https://rclone.org/) to support:

- S3-compatible services such as AWS S3, MinIO, Backblaze B2, and Wasabi;
- WebDAV services such as Nextcloud and ownCloud;
- Google Drive;
- Dropbox; and
- OneDrive.

The library can upload automatically whenever an artifact is produced or
through **Sync everything now**. Archived source media is included after
**Download & keep media**. Temporary media, cookies, and transcription-only
working copies never sync.

The raw source PDF for a paper is always cloud-sync excluded. Repository
artifacts and local-only paper artifacts follow their enforced privacy policy.

There is no scheduled cloud sync. Per-artifact auto-upload and the manual full
sync are the available triggers.

## One-way and two-way behavior

The default is one-way local-to-cloud synchronization. It never changes local
content.

Optional two-way mode applies only to the full library pass triggered by
**Sync everything now**. After pulling changes, Synapse rebuilds the local
index and, when enabled, embeddings.

Understand these two-way rules:

- deletions propagate in both directions;
- a run that would delete more than half of either side aborts;
- after the baseline, conflicting edits keep the newer copy and preserve the
  older one with a `.conflict` suffix;
- the first run establishes a baseline, merges files unique to either side,
  and lets the newer same-path copy win;
- during that first baseline only, the older same-path copy is overwritten
  rather than retained as `.conflict`;
- if the storage cannot provide modification times during baseline, local wins;
- provider, credential, or remote-folder changes establish a new baseline;
- archived media and per-artifact uploads always remain one-way.

Google Drive permits duplicate same-name files. A full sync ends with a dedupe
pass that retains the newest copy.

## Configure S3-compatible storage

Under **Settings → Advanced → Cloud storage**, select S3-compatible and enter:

- `endpoint` — for example AWS's regional S3 URL or the MinIO server URL;
- `bucket` — create it at the provider first;
- `access_key_id`;
- `secret_access_key`; and
- optional `region`.

MinIO and many non-AWS providers do not require a region.

## Configure WebDAV

Enter:

- `url` — for example
  `https://nextcloud.example/remote.php/dav/files/<username>`;
- `vendor` — `nextcloud` or `owncloud`;
- `user`; and
- `password`.

Use an app password rather than the account's normal login password. In
Nextcloud, create one under Settings → Security.

## Configure Drive, Dropbox, or OneDrive

These providers use an rclone OAuth token:

1. Install rclone on a trusted computer with a browser.
2. Run `rclone authorize "drive"`, `rclone authorize "dropbox"`, or
   `rclone authorize "onedrive"`.
3. Sign in and approve access.
4. Copy the complete JSON token printed by rclone.
5. Paste it into Synapse's `token` field.

Google Drive's `root_folder_id` is optional. OneDrive's `drive_type` is
`personal` by default or `business`. If synchronization begins failing after a
long period, authorize again and save the refreshed token.

## Complete cloud setup

1. Select and configure a provider.
2. Choose the remote base folder, `synapse` by default.
3. Choose one-way or two-way full synchronization.
4. Optionally enable per-artifact auto-upload.
5. Save cloud settings.
6. Select **Sync everything now** for the initial backfill.

The remote layout uses `<remote-base>/library/` and
`<remote-base>/media/`. Progress appears in Jobs and the navigation job ticker.
The cloud settings show the last result, timestamp, and error.

Secrets become masked after save. Leaving a masked field or blank value
unchanged preserves the existing secret; enter a new value to rotate it.
