export async function api<T = any>(path: string, opts?: RequestInit): Promise<T> {
  const isFormData = typeof FormData !== "undefined" && opts?.body instanceof FormData;
  const res = await fetch(`/api${path}`, {
    ...opts,
    headers: isFormData
      ? opts?.headers
      : { "Content-Type": "application/json", ...opts?.headers },
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
  paper?: PaperSource | null;
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
  paper_series_id?: number | null;
  paper_part_id?: number | null;
  audience?: PaperAudience | null;
  cloud_sync_excluded?: boolean;
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
  paper_series_id?: number | null;
  paper_part_id?: number | null;
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
  paper_source: "Source paper",
  source_paper: "Source paper",
  paper_coverage: "Extraction and coverage report",
  paper_extraction_report: "Source extraction report",
  paper_argument_map: "Claim and argument map",
  paper_mindmap: "Whole-paper mind map",
  paper_quickrefs: "Paper quick references",
  paper_quick_references: "Paper quick references",
  paper_overview: "Paper overview",
  paper_methods: "Methods and reproducibility guide",
  paper_evidence: "Evidence and results guide",
  paper_prerequisites: "Prerequisites and terminology",
  paper_critique: "Limitations and critique",
  paper_explanatory_deepdive: "Explanatory deep dive",
  paper_methodology_deepdive: "Critical-methodology deep dive",
  paper_deepdive_explanatory: "Explanatory deep dive",
  paper_deepdive_methodology: "Critical-methodology deep dive",
  paper_study_guide: "Definitive study guide",
  paper_part_guide: "Part study guide and show notes",
  paper_part_script: "Two-host episode script",
  paper_part_audio: "Podcast audio",
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

export const isPaperProject = (project: Project | null | undefined): boolean =>
  project?.source_type === "paper";

export const shortSha = (sha?: string | null): string => sha ? sha.slice(0, 8) : "unknown";

export type PaperAudience = "generalist" | "practitioner" | "expert";
export type PaperQualityGrade = "EXCELLENT" | "GOOD" | "FAIR" | "POOR" | string;
export type PaperEvidenceKind =
  | "prose" | "heading" | "definition" | "equation" | "table"
  | "caption" | "footnote" | "reference" | "visual" | string;

export interface PaperPageIssue {
  page: number;
  grade?: PaperQualityGrade;
  reason?: string;
  acknowledged?: boolean;
  acknowledgement_reason?: string | null;
  visual_review_needed?: boolean;
}

export interface PaperSource {
  id?: number;
  project_id: number;
  filename?: string;
  original_filename?: string;
  path?: string;
  source_hash?: string;
  sha256?: string;
  page_count?: number;
  character_count?: number;
  extracted_characters?: number;
  status?: string;
  extraction_status?: string;
  quality_grade?: PaperQualityGrade;
  quality?: PaperQualityGrade;
  quality_report?: Record<string, unknown>;
  extraction_method?: string;
  parser_version?: string;
  ocr_languages?: string[];
  local_only?: boolean;
  privacy_locked?: boolean;
  cloud_sync_excluded?: boolean;
  poor_pages?: Array<number | PaperPageIssue>;
  unacknowledged_poor_pages?: number[];
  analysis_blocked?: boolean;
  page_issues?: PaperPageIssue[];
  acknowledged_pages?: Array<number | PaperPageIssue>;
  pdf_url?: string;
  created?: string;
  updated?: string;
}

export interface PaperCoverageTopic {
  id: string;
  title: string;
  kind?: string;
  importance?: "critical" | "major" | "supporting" | string;
  evidence_ids?: string[];
  assigned_part_id?: number | null;
  assigned_part?: number | null;
  omitted?: boolean;
  omission_reason?: string | null;
}

export interface PaperCoverage {
  evidence_blocks?: number;
  mapped_blocks?: number;
  pages_total?: number;
  pages_admitted?: number;
  pages_acknowledged?: number;
  critical_total?: number;
  critical_assigned?: number;
  critical_omitted?: number;
  major_total?: number;
  major_assigned?: number;
  percent?: number;
  complete?: boolean;
  analysis_blocked?: boolean;
  warnings?: string[];
  topics?: PaperCoverageTopic[];
}

export interface PaperPartEvidence {
  evidence_id?: string;
  chunk_id?: number;
  role?: "primary" | "bridge" | string;
  importance?: string;
  title?: string;
  page?: number;
  section?: string;
  reason?: string;
}

export interface PaperSeriesPart {
  id: number;
  paper_series_id?: number;
  position: number;
  title: string;
  focus?: string;
  target_minutes?: number;
  status?: string;
  stale?: boolean;
  structure_locked?: boolean;
  locked?: boolean;
  assignments?: Array<string | PaperPartEvidence>;
  evidence?: PaperPartEvidence[];
  evidence_ids?: string[];
  topics?: string[];
  duration_minutes?: number;
  learning_objectives?: string[];
  primary_evidence_ids?: string[];
  bridge_evidence_ids?: string[];
  artifacts?: Artifact[];
  jobs?: Job[];
  guide_artifact?: Artifact | null;
  script_artifact?: Artifact | null;
  audio_artifact?: Artifact | null;
  memory_revision_id?: number | null;
  guide_status?: string;
  script_status?: string;
  audio_status?: string;
  created?: string;
  updated?: string;
}

export interface PaperPlanOmission {
  topic_id?: string | null;
  reason: string;
  importance?: string;
  evidence_id?: string | null;
  demoted_from?: "critical" | "major" | null;
}

export interface PaperSeriesPlan {
  version?: number;
  title?: string;
  rationale?: string;
  parts?: PaperSeriesPart[];
  omissions?: PaperPlanOmission[];
  topics?: PaperCoverageTopic[];
  critical_topics?: PaperCoverageTopic[];
  coverage?: PaperCoverage;
}

export interface PaperMemoryState {
  terminology?: Array<string | { term: string; pronunciation?: string; meaning?: string }>;
  introduced_topics?: string[];
  completed_topics?: string[];
  deferred_topics?: string[];
  covered_claims?: string[];
  examples?: string[];
  stories_and_analogies?: string[];
  open_questions?: string[];
  promised_callbacks?: string[];
  handoff_notes?: string[];
  evidence_ids?: string[];
  [key: string]: unknown;
}

export interface PaperMemoryRevision {
  id: number;
  paper_series_id?: number;
  paper_part_id?: number | null;
  parent_id?: number | null;
  revision: number;
  state?: PaperMemoryState;
  state_json?: PaperMemoryState | string;
  content_hash?: string;
  created?: string;
}

export interface PaperSeries {
  id: number;
  project_id: number;
  audience: PaperAudience;
  title?: string;
  status: string;
  target_minutes?: number;
  max_parts?: number;
  plan_version?: number;
  plan_hash?: string;
  plan?: PaperSeriesPlan;
  parts?: PaperSeriesPart[];
  omissions?: PaperPlanOmission[];
  coverage?: PaperCoverage;
  memory_revision?: PaperMemoryRevision | null;
  memory_revisions?: PaperMemoryRevision[];
  user_guidance?: string;
  artifacts?: Artifact[];
  jobs?: Job[];
  created?: string;
  updated?: string;
}

export interface PaperQuality {
  grade?: PaperQualityGrade;
  status?: string;
  blocked?: boolean;
  poor_pages?: Array<number | PaperPageIssue>;
  page_issues?: PaperPageIssue[];
  acknowledged_pages?: Array<number | PaperPageIssue>;
  warnings?: string[];
  report?: Record<string, unknown>;
}

export interface PaperDetail {
  project: Project;
  source: PaperSource;
  quality?: PaperQuality;
  coverage?: PaperCoverage;
  artifacts?: Artifact[];
  shared_artifacts?: Artifact[];
  series?: PaperSeries[];
  tracks?: PaperSeries[];
  jobs?: Job[];
}

export interface PaperCitation {
  kind?: "paper";
  source_hash?: string;
  evidence_id: string;
  page: number;
  section?: string | string[];
  bounding_box?: number[] | Record<string, number> | null;
  excerpt?: string;
  internal_url?: string;
  pdf_url?: string;
  url?: string;
  extraction_method?: string;
}

export type SourceCitation = RepositoryCitation | PaperCitation;

export function isPaperCitation(citation: SourceCitation): citation is PaperCitation {
  return Boolean(citation && typeof citation === "object"
    && "evidence_id" in citation && "page" in citation);
}

export const PAPER_AUDIENCES: Array<{ key: PaperAudience; label: string; description: string }> = [
  { key: "generalist", label: "Generalist", description: "Build intuition first and explain specialist terms in plain language." },
  { key: "practitioner", label: "Practitioner", description: "Emphasize application, implementation choices, and reproducibility." },
  { key: "expert", label: "Expert", description: "Preserve technical detail and foreground methodology and uncertainty." },
];

export function paperAudienceLabel(audience?: string | null): string {
  return PAPER_AUDIENCES.find((item) => item.key === audience)?.label
    ?? (audience ? audience.charAt(0).toUpperCase() + audience.slice(1) : "Audience");
}
