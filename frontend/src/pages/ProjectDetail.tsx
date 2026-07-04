import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, Project, Step } from "../api";

interface Detail {
  project: Project;
  steps: Step[];
}

export default function ProjectDetail() {
  const { id } = useParams();
  const [detail, setDetail] = useState<Detail | null>(null);
  const [error, setError] = useState("");
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

  async function uploadCookies() {
    const file = fileRef.current?.files?.[0];
    if (!file) return;
    const form = new FormData();
    form.append("file", file);
    await fetch(`/api/projects/${id}/cookies`, { method: "POST", body: form });
    alert("cookies.txt uploaded — used by ingest/transcript for auth sites");
  }

  if (!detail) return <p>{error || "loading…"}</p>;
  const { project, steps } = detail;

  return (
    <div className="project-detail">
      <h2>{project.title}</h2>
      <p className="mono">{project.source}</p>
      <p>
        <label className="cookies">
          cookies.txt (for Udemy etc.): <input type="file" ref={fileRef} />
          <button onClick={uploadCookies}>upload</button>
        </label>
      </p>
      {error && <p className="error">{error}</p>}

      <div className="board">
        {steps.map((s) => {
          const status = s.job?.status ?? (s.artifact ? "done" : "—");
          return (
            <div key={s.name} className={`step ${status}`}>
              <div className="step-head">
                <strong>{s.label}</strong>
                <button
                  onClick={() => run(s.name)}
                  disabled={status === "running" || status === "queued"}
                >
                  {s.artifact || s.job ? "re-run" : "run"}
                </button>
              </div>
              <div className="step-status">
                {status}
                {s.job?.status === "running" && s.job.progress && <em> — {s.job.progress}</em>}
              </div>
              {s.job?.status === "error" && (
                <details className="error"><summary>error</summary><pre>{s.job.error}</pre></details>
              )}
              {s.artifact && (
                <Link to={`/artifacts/${s.artifact.id}`}>open artifact →</Link>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
