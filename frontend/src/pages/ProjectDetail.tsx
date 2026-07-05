import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, Project, Step } from "../api";

interface DetailStep extends Step {
  missing: string[];
  blocked: boolean;
  done: boolean;
  not_applicable: boolean;
}

interface Detail {
  project: Project;
  steps: DetailStep[];
  remaining: number;
  run_all_active: boolean;
  any_active: boolean;
}

export default function ProjectDetail() {
  const { id } = useParams();
  const [detail, setDetail] = useState<Detail | null>(null);
  const [error, setError] = useState("");
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const fileRef = useRef<HTMLInputElement>(null);

  const load = useCallback(() => {
    api<Detail>(`/projects/${id}`).then(setDetail).catch((e) => setError(e.message));
  }, [id]);

  useEffect(load, [load]);

  // refresh the board whenever the project's job set changes
  useEffect(() => {
    const es = new EventSource(`/api/jobs/stream?project_id=${id}`);
    es.addEventListener("jobs", load);
    return () => es.close();
  }, [id, load]);

  async function run(step: string) {
    setError("");
    try {
      await api(`/projects/${id}/run/${step}`, { method: "POST" });
      load();
    } catch (e: any) {
      setError(e.message);
    }
  }

  async function runAll() {
    setError("");
    try {
      await api(`/projects/${id}/run_all`, { method: "POST" });
      load();
    } catch (e: any) {
      setError(e.message);
    }
  }

  function toggle(step: string) {
    setExpanded((prev) => {
      const next = new Set(prev);
      next.has(step) ? next.delete(step) : next.add(step);
      return next;
    });
  }

  async function uploadCookies() {
    const file = fileRef.current?.files?.[0];
    if (!file) return;
    const form = new FormData();
    form.append("file", file);
    await fetch(`/api/projects/${id}/cookies`, { method: "POST", body: form });
    alert("cookies.txt uploaded — used by ingest/download/transcript for auth sites");
  }

  if (!detail) return <p>{error || "loading…"}</p>;
  const { project, steps } = detail;

  return (
    <div className="project-detail">
      <h2>{project.title}</h2>
      <p className="mono">{project.source}</p>
      <div className="board-toolbar">
        <button
          className="runall"
          onClick={runAll}
          disabled={detail.any_active || detail.remaining === 0}
          title="Runs every remaining step — concurrently where dependencies allow"
        >
          {detail.run_all_active
            ? "⏳ running all…"
            : detail.remaining === 0
              ? "✓ all steps complete"
              : `▶ run all (${detail.remaining} remaining)`}
        </button>
        <label className="cookies">
          cookies.txt (for Udemy etc.): <input type="file" ref={fileRef} />
          <button onClick={uploadCookies}>upload</button>
        </label>
        {detail.any_active && (
          <button
            className="reset-jobs"
            title="If a worker crash left jobs stuck queued/running, this clears them so steps can be re-run"
            onClick={async () => {
              if (!confirm("Mark all queued/running jobs as failed? Only use this if a run looks stuck.")) return;
              await api(`/projects/${id}/reset_jobs`, { method: "POST" });
              load();
            }}
          >
            reset stuck jobs
          </button>
        )}
      </div>
      {error && <p className="error">{error}</p>}

      <div className="steplist">
        {steps.map((s) => {
          const status = s.job?.status ?? (s.done ? "done" : "—");
          const isOpen = expanded.has(s.name);
          const dimmed = (s.blocked && status === "—") || s.not_applicable;
          return (
            <div key={s.name} className={`step-row ${status} ${dimmed ? "dim" : ""}`}>
              <div className="step-main" onClick={() => toggle(s.name)}>
                <span className={`chev ${isOpen ? "open" : ""}`}>▶</span>
                <strong>{s.label}</strong>
                <span className="step-status">
                  {s.not_applicable ? "n/a" : status}
                  {s.job?.status === "running" && s.job.progress && (
                    <em> — {s.job.progress}</em>
                  )}
                </span>
                {s.blocked && !s.done && !s.not_applicable && (
                  <span className="prereq" title="run these first">
                    requires: {s.missing.join(", ")}
                  </span>
                )}
                {s.not_applicable && <span className="prereq">already local</span>}
                <span className="step-actions" onClick={(e) => e.stopPropagation()}>
                  {s.artifact && (
                    <Link to={`/artifacts/${s.artifact.id}`}>open artifact →</Link>
                  )}
                  {!s.not_applicable && (
                    <button
                      onClick={() => run(s.name)}
                      disabled={status === "running" || status === "queued" ||
                                (s.blocked && !s.done)}
                    >
                      {s.artifact || s.job ? "re-run" : "run"}
                    </button>
                  )}
                </span>
              </div>

              {isOpen && (
                <div className="step-detail">
                  {s.job ? (
                    <>
                      <p className="meta">
                        status: <b>{s.job.status}</b>
                        {s.job.progress && <> · progress: {s.job.progress}</>}
                        {s.job.updated && (
                          <> · updated {new Date(s.job.updated).toLocaleTimeString()}</>
                        )}
                      </p>
                      {s.job.error && (
                        <pre className="error">{s.job.error}</pre>
                      )}
                    </>
                  ) : (
                    <p className="meta">not run yet</p>
                  )}
                  {s.artifact && (
                    <p className="meta">
                      artifact: <Link to={`/artifacts/${s.artifact.id}`}>{s.artifact.title}</Link>
                      {s.artifact.provider && (
                        <> · {s.artifact.provider}/{s.artifact.model}</>
                      )}
                      {" · "}updated {new Date(s.artifact.updated).toLocaleString()}
                    </p>
                  )}
                  {s.blocked && !s.done && (
                    <p className="meta">prerequisites: {s.missing.join(", ")}</p>
                  )}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
