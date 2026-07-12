export async function api<T = any>(path: string, opts?: RequestInit): Promise<T> {
  const res = await fetch(`/api${path}`, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      detail = (await res.json()).detail ?? detail;
    } catch {}
    throw new Error(detail);
  }
  return res.json();
}

/** Parse a backend timestamp as UTC.
 *
 * Datetimes are stored UTC but SQLite drops the offset, so the API emits
 * naive ISO strings ("2026-07-07T18:30:00"). JS parses an offset-less string
 * as LOCAL time, shifting every displayed time by the viewer's UTC offset —
 * so we re-attach 'Z' when the string carries no zone. */
function parseUTC(s: string): Date {
  const hasZone = /[zZ]|[+-]\d\d:?\d\d$/.test(s);
  return new Date(hasZone ? s : s + "Z");
}

export const fmtDateTime = (s: string) => parseUTC(s).toLocaleString();
export const fmtDate = (s: string) => parseUTC(s).toLocaleDateString();
export const fmtTime = (s: string) => parseUTC(s).toLocaleTimeString();
export const parseTime = (s: string) => parseUTC(s).getTime();

export type PipelineStatus =
  "new" | "running" | "partial" | "complete" | "failed" | "canceled";

export interface ProjectProgress {
  done: number;
  total: number;
  status: PipelineStatus;
  detail: string | null;      // active or failed step label
  last_activity: string | null;
}

export interface Project {
  id: number;
  slug: string;
  title: string;
  source: string;
  source_type: string;
  status: string;             // raw ingest/transcribe substatus (legacy)
  created: string;
  progress?: ProjectProgress; // derived pipeline status (list endpoint)
  repository?: GitHubSource | null;
}

export interface Artifact {
  id: number;
  project_id: number | null;
  type: string;
  title: string;
  path: string;
  media_path: string | null;
  provider: string | null;
  model: string | null;
  restricted?: boolean;
  repository_derived?: boolean;
  created: string;
  updated: string;
  tags?: string[];
  project_slug?: string;
}

export type RepositoryPrivacy = "public" | "private";
export type RepositoryRefKind = "branch" | "tag" | "branch_or_tag" | "commit";

/** A durable GitHub source. `commit_sha` always identifies the immutable
 * revision used by the current project analysis; `requested_ref` is the
 * human-facing branch/tag/commit the user selected. */
export interface GitHubSource {
  id?: number;
  project_id?: number;
  url: string;
  canonical_url?: string;
  owner: string;
  name: string;
  repository?: string;
  full_name: string;
  privacy: RepositoryPrivacy;
  private?: boolean;
  is_private?: boolean;
  local_only?: boolean;
  default_branch: string;
  requested_ref?: string | null;
  resolved_ref?: string;
  ref_kind?: RepositoryRefKind;
  commit_sha: string;
  description?: string | null;
  tracked_branch?: string | null;
  pending_sha?: string | null;
  cloud_purge_pending?: boolean;
  created?: string;
  updated?: string;
}

export interface RepositoryLimits {
  max_download_bytes: number;
  max_unpacked_bytes: number;
  max_files: number;
  max_file_bytes: number;
  max_text_file_bytes?: number;
  max_indexed_bytes?: number;
  chunk_lines?: number;
  chunk_chars?: number;
  max_compression_ratio?: number;
  max_map_chunks?: number;
  max_map_input_chars?: number;
}

export interface RepositoryCoverage {
  total_files?: number | null;
  included_files?: number | null;
  excluded_files?: number | null;
  analyzable_files?: number;
  binary_files?: number;
  generated_files?: number;
  vendored_files?: number;
  oversized_files?: number;
  included_bytes?: number;
  total_bytes?: number | null;
  percent?: number;
  include_paths?: string[];
  exclude_paths?: string[];
  languages?: Record<string, number> | string[];
  warnings?: string[];
  available?: boolean;
  tree_truncated?: boolean;
  eligible_files?: number | null;
  eligible_bytes?: number | null;
  submodule_count?: number | null;
  excluded?: Record<string, number>;
  ready?: boolean;
  preview?: RepositoryCoverage;
  file_count?: number;
  indexed_file_count?: number;
  indexed_bytes?: number;
  excluded_file_count?: number;
  files_with_evidence?: number;
  evidence_chunk_count?: number;
  exclusion_reason_counts?: Record<string, number>;
  omitted_link_count?: number;
  secret_finding_count?: number;
  frameworks?: string[];
}

export interface RepositoryPreflight {
  source: GitHubSource;
  coverage?: RepositoryCoverage;
  coverage_preview?: RepositoryCoverage;
  limits?: RepositoryLimits;
  size_bytes?: number;
  file_count?: number;
  branches?: string[];
  tags?: string[];
  submodules?: string[];
  lfs_detected?: boolean;
  credential_required?: boolean;
  local_only: boolean;
  provider?: "ollama" | "local";
  local_model?: string;
  warnings?: string[];
}

export interface RepositoryCreateConfig {
  url: string;
  ref?: string;
  title?: string;
  include_paths?: string[];
  exclude_paths?: string[];
  analyze?: boolean;
  expected_sha?: string;
}

export interface RepositorySnapshot {
  id?: number;
  project_id: number;
  commit_sha: string;
  status?: "pending" | "running" | "ready" | "failed" | string;
  requested_ref?: string | null;
  resolved_ref?: string;
  archive_bytes?: number;
  expanded_bytes?: number;
  file_count?: number;
  indexed_file_count?: number;
  indexed_bytes?: number;
  excluded_file_count?: number;
  omitted_links?: string[];
  manifest_hash?: string;
  path?: string;
  created?: string;
  completed?: string | null;
}

export interface RepositoryUpdateStatus {
  update_available?: boolean;
  changed?: boolean;
  current_sha?: string | null;
  latest_sha?: string;
  target_sha?: string;
  checked_at?: string;
  ahead_by?: number;
  behind_by?: number;
  changed_files?: number;
  additions?: number;
  deletions?: number;
  compare_url?: string | null;
  message?: string;
  pending?: boolean;
}

export interface RepositoryDetail {
  source: GitHubSource;
  snapshot: RepositorySnapshot;
  coverage: RepositoryCoverage;
  update?: RepositoryUpdateStatus | null;
}

export interface RepositoryCreateResponse {
  project: Project;
  source: GitHubSource;
  snapshot: RepositorySnapshot;
  coverage?: RepositoryCoverage;
}

export interface RepositoryCitation {
  marker?: string;
  file_id?: number;
  path: string;
  start_line: number;
  end_line?: number;
  commit_sha: string;
  url?: string;
  permalink?: string;
  excerpt?: string;
  classification?: "detected" | "inferred" | "verified";
}

export interface GitHubCredentialStatus {
  configured: boolean;
  token?: string;
  masked_token?: string | null;
  login?: string | null;
  scopes?: string[];
  selected_repositories?: string[];
  updated?: string | null;
  valid?: boolean;
  message?: string;
  limits?: RepositoryLimits;
}

export interface RepositorySettings {
  local_model: string;
  limits: RepositoryLimits;
  default_exclusions: string[];
  host: "github.com";
  static_only: true;
}

export interface Job {
  id: number;
  project_id: number | null;
  parent_job_id?: number | null;
  task: string;
  task_label?: string;
  project_title?: string;
  status: string;
  progress: string;
  error: string;
  created?: string;
  updated?: string;
  started?: string | null;
  finished?: string | null;
}

export interface Step {
  name: string;
  label: string;
  job: Job | null;
  artifact: Artifact | null;
}

export const TYPE_LABELS: Record<string, string> = {
  transcript: "Transcript",
  corrected: "Corrected transcript",
  summary: "Summary",
  deepdive_claude: "Deep dive (Claude)",
  deepdive_gemini: "Deep dive (Gemini)",
  deepdive_merged: "Deep dive (merged)",
  podcast_script: "Podcast script",
  podcast_audio: "Podcast audio",
  trimmed_audio: "Trimmed audio",
  mindmap: "Mind map",
  quickref_tool: "Quick-ref: tool",
  quickref_technique: "Quick-ref: technique",
  quickref_concept: "Quick-ref: concept",
  quickref_technology: "Quick-ref: technology",
  source_video: "Source video",
  source_audio: "Source audio",
  repo_inventory: "Repository inventory",
  repo_usage: "Setup and usage guide",
  repo_architecture: "Architecture and code map",
  repo_expertise: "Required knowledge",
  repo_environment: "Dependencies and environment",
  repository_source: "Repository source",
};

/** Human label for an artifact type; custom quick-ref categories fall back
 * to "Quick-ref: <kind>" instead of the raw quickref_<kind> type. */
export function typeLabel(type: string): string {
  return TYPE_LABELS[type] ??
    (type.startsWith("quickref_") ? `Quick-ref: ${type.slice(9).replace(/-/g, " ")}` : type);
}

export interface QuickRefCategory {
  key: string;
  label: string;
  plural: string;
  icon: string;
  dir: string;
  builtin: boolean;
  description?: string;
  prompt?: string;
  count: number;
}

export const REPOSITORY_ARTIFACT_TYPES = [
  "summary",
  "repo_usage",
  "repo_architecture",
  "repo_expertise",
  "repo_environment",
] as const;

export const isRepositoryProject = (project: Project | null | undefined): boolean =>
  Boolean(project && ["github", "repository", "github_repository"].includes(project.source_type));

export const shortSha = (sha?: string | null): string => sha ? sha.slice(0, 8) : "unknown";
