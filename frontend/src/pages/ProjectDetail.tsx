import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api, fmtDateTime, fmtTime, Project, Step } from "../api";

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
  run_all_state: "queued" | "running" | null;
  any_active: boolean;
}

export default function ProjectDetail() {
  const { id } = useParams();
  const nav = useNavigate();
  const [detail, setDetail] = useState<Detail | null>(null);
  const [error, setError] = useState("");
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [renaming, setRenaming] = useState(false);
  const [titleDraft, setTitleDraft] = useState("");
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
    // raw fetch (multipart body, not JSON) — but still surface a failed upload
    // instead of alerting success unconditionally
    try {
      const res = await fetch(`/api/projects/${id}/cookies`, { method: "POST", body: form });
      if (!res.ok) {
        let detail = res.statusText;
        try { detail = (await res.json()).detail ?? detail; } catch {}
        throw new Error(detail);
      }
      alert("cookies.txt uploaded — used by ingest/download/transcript for auth sites");
    } catch (e: any) {
      setError(`cookies upload failed: ${e.message}`);
    }
  }

  async function saveRename() {
    const title = titleDraft.trim();
    if (!title) return;
    try {
      await api(`/projects/${id}`, { method: "PATCH", body: JSON.stringify({ title }) });
      setRenaming(false);
      load();
    } catch (e: any) {
      setError(e.message);
    }
  }

  async function deleteProject() {
    if (!detail) return;
    const ok = confirm(
      `Delete "${detail.project.title}"?\n\n` +
      "This permanently deletes ALL of this project's artifacts — transcript, " +
      "summary, deep dives, podcast script and audio, trimmed audio, mind map — " +
      "and any downloaded source video/audio.\n\n" +
      "Quick-reference docs it contributed to will remain.\n\nThis cannot be undone."
    );
    if (!ok) return;
    try {
      await api(`/projects/${id}`, { method: "DELETE" });
      nav("/projects");
    } catch (e: any) {
      setError(e.message);
    }
  }

  if (!detail) return <p>{error || "loading…"}</p>;
  const { project, steps } = detail;

  return (
    <div className="project-detail">
      <div className="project-head">
        {renaming ? (
          <span className="rename">
            <input
              autoFocus
              value={titleDraft}
              onChange={(e) => setTitleDraft(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter") saveRename(); }}
            />
            <button onClick={saveRename}>save</button>
            <button onClick={() => setRenaming(false)}>cancel</button>
          </span>
        ) : (
          <>
            <h2>{project.title}</h2>
            <button
              className="linkish"
              title="rename project"
              onClick={() => { setTitleDraft(project.title); setRenaming(true); }}
            >✎ rename</button>
            <button className="linkish danger" title="delete project" onClick={deleteProject}>
              🗑 delete
            </button>
          </>
        )}
      </div>
      <p className="mono">{project.source}</p>
      <div className="board-toolbar">
        <button
          className="runall"
          onClick={runAll}
          disabled={detail.run_all_active || detail.remaining === 0}
          title="Queues every remaining step. Runs now if no other project's run-all is active, otherwise waits its turn."
        >
          {detail.run_all_state === "running"
            ? "⏳ running all…"
            : detail.run_all_state === "queued"
              ? "⏳ queued — waiting for another run"
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
                          <> · updated {fmtTime(s.job.updated)}</>
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
                      {" · "}updated {fmtDateTime(s.artifact.updated)}
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
