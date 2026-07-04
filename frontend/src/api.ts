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

export interface Project {
  id: number;
  slug: string;
  title: string;
  source: string;
  source_type: string;
  status: string;
  created: string;
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
  created: string;
  updated: string;
  tags?: string[];
  project_slug?: string;
}

export interface Job {
  id: number;
  project_id: number | null;
  task: string;
  status: string;
  progress: string;
  error: string;
  updated?: string;
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
  source_video: "Source video",
  source_audio: "Source audio",
};
