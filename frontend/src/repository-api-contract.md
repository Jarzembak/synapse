# Repository frontend API contract

All routes below are relative to `/api`. The frontend treats repository code as
static, untrusted input and never asks the API to execute it.

## Credentials and repository policy

- `GET /repositories/credentials` returns `{ configured, token }`; `token` is
  either empty or a server-generated mask and is never the stored secret.
- `PUT /repositories/credentials` accepts `{ token }`, validates its fine-grained
  token format, encrypts it, and returns the masked status. Repository access
  and read-only Contents permission are verified during preflight. `DELETE`
  removes it.
- `GET /repositories/settings` returns `{ local_model, limits,
  default_exclusions, host: "github.com", static_only: true }`.
- `PUT /repositories/settings` accepts any of `{ local_model, limits,
  default_exclusions }`. `limits` uses byte counts plus file/chunk counts:
  `max_download_bytes`, `max_unpacked_bytes`, `max_files`, `max_file_bytes`,
  `max_text_file_bytes`, `max_indexed_bytes`, `chunk_lines`, `chunk_chars`, and
  `max_compression_ratio`, `max_map_chunks`, and `max_map_input_chars`.

## Import and immutable snapshots

- `POST /repositories/preflight` accepts `{ url, ref?, include_paths?,
  exclude_paths? }`. It returns the normalized source, resolved commit SHA,
  privacy, optional `coverage_preview`, active limits, warnings, and whether
  model processing is local-only. No archive is downloaded in this call.
- `POST /repositories` accepts `{ url, ref?, title?, include_paths?,
  exclude_paths?, expected_sha? }` and returns `{ project, source, snapshot,
  coverage? }`. `expected_sha` prevents a moving branch from changing between
  inspection and creation without asking the user to inspect it again.
  Creation records metadata; snapshot acquisition begins only when the pipeline
  is queued.
- `GET /repositories/{project_id}` returns `{ source, snapshot, coverage,
  update? }`. The snapshot SHA is the immutable revision supporting artifacts.
- `POST /repositories/{project_id}/check-update` returns
  `{ changed, current_sha, target_sha, ahead_by, changed_files }` without
  changing the project. The UI also accepts the earlier
  `update_available`/`latest_sha` aliases while clients transition.
- `POST /repositories/{project_id}/update` accepts the confirmed `target_sha`
  and selects that immutable target; a ref that moved since the check returns a
  conflict instead of silently selecting a different commit.
  The frontend then calls `POST /projects/{project_id}/rerun/repo_snapshot` to
  acquire it and rebuild affected outputs.
- `GET /repositories/{project_id}/files/{file_id}?start_line=&end_line=` is the
  authenticated internal evidence viewer. Generated citations may also expose
  a GitHub permalink pinned to the same SHA.

## Pipeline and evidence

After create-and-analyze, the frontend calls
`POST /projects/{project_id}/run_all` with `{ profile: "repository" }`.
Repository-only projects omit transcript, correction, download, and media
steps. Search/Q&A evidence can add `repository_path` or `file_path`,
`start_line`, `end_line`, `commit_sha`, and `immutable_url`/`permalink` to the
existing hybrid result shape.

Every repository must report `local_only: true`; the backend is responsible for
enforcing a local/loopback Ollama endpoint for every LLM boundary regardless of
GitHub visibility or the global model matrix. Repository-derived artifacts are
also excluded from cloud sync. The UI presents this policy but does not serve as
the security boundary.
