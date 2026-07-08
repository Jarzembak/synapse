import { FormEvent, useEffect, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api, fmtDate, fmtDateTime, PipelineStatus, Project } from "../api";
import { useEventSource } from "../useEventSource";

// derived pipeline status → chip label + the CSS class it borrows from .jobstatus
const STATUS_META: Record<PipelineStatus, { label: string; cls: string }> = {
  running: { label: "Running", cls: "running" },
  failed: { label: "Failed", cls: "error" },
  complete: { label: "Complete", cls: "done" },
  partial: { label: "Partial", cls: "partial" },
  canceled: { label: "Canceled", cls: "canceled" },
  new: { label: "New", cls: "new" },
};

function StatusCell({ p }: { p: Project }) {
  const pr = p.progress;
  if (!pr) return <>{p.status}</>; // pre-progress API fallback
  const meta = STATUS_META[pr.status];
  const pct = pr.total ? Math.round((pr.done / pr.total) * 100) : 0;
  return (
    <div className="projstatus">
      <div className="projstatus-line">
        <span className={`jobstatus ${meta.cls}`}>{meta.label}</span>
        {pr.detail && <span className="substep">{pr.detail}</span>}
        <span className="frac">{pr.done}/{pr.total}</span>
      </div>
      <div className="pbar" title={`${pr.done} of ${pr.total} steps done`}>
        <i className={meta.cls} style={{ width: `${pct}%` }} />
      </div>
    </div>
  );
}

export default function Projects() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [source, setSource] = useState("");
  const [sourceType, setSourceType] = useState<"url" | "local">("url");
  const [title, setTitle] = useState("");
  const [error, setError] = useState("");
  const nav = useNavigate();
  const reloadSeq = useRef(0);

  function reload() {
    // sequence guard: SSE can fire reloads faster than they resolve — only the
    // newest response is allowed to win, so a slow one can't clobber fresh data
    const seq = ++reloadSeq.current;
    api<Project[]>("/projects")
      .then((r) => {
        if (seq !== reloadSeq.current) return;
        setProjects(r);
        setLoaded(true);
        setError("");           // recovered — drop any stale error banner
      })
      .catch((e) => {
        if (seq === reloadSeq.current) setError(e.message);
      });
  }
  useEffect(reload, []);

  // live-refresh the derived status while pipelines run: the job SSE stream
  // fires whenever the active/recent job set changes (and heartbeats), so
  // re-pull the list then. The reconnecting hook keeps this alive across a
  // worker/api restart instead of freezing after one.
  useEventSource("/api/jobs/stream", "jobs", () => reload());

  const [creating, setCreating] = useState(false);

  async function create(e: FormEvent) {
    e.preventDefault();
    setCreating(true);
    setError("");
    try {
      const p = await api<Project>("/projects", {
        method: "POST",
        body: JSON.stringify({ source, source_type: sourceType, title: title || null }),
      });
      nav(`/projects/${p.id}`);
    } catch (err: any) {
      setError(err.message);
    } finally {
      setCreating(false);
    }
  }

  async function remove(p: Project) {
    const ok = confirm(
      `Delete "${p.title}"?\n\n` +
      "This permanently deletes all of its artifacts and any downloaded media. " +
      "Quick-reference docs it contributed to will remain.\n\nThis cannot be undone."
    );
    if (!ok) return;
    try {
      await api(`/projects/${p.id}`, { method: "DELETE" });
      setProjects((prev) => prev.filter((x) => x.id !== p.id));
    } catch (err: any) {
      setError(err.message);
    }
  }

  return (
    <div className="projects">
      <h2>New project</h2>
      <form onSubmit={create} className="newproject">
        <select value={sourceType} onChange={(e) => setSourceType(e.target.value as any)}>
          <option value="url">URL</option>
          <option value="local">Local file</option>
        </select>
        <input
          placeholder={sourceType === "url"
            ? "https://www.youtube.com/watch?v=…"
            : "path relative to your HOST_MEDIA_DIR, e.g. talks/recon.mp4"}
          value={source}
          onChange={(e) => setSource(e.target.value)}
          required
        />
        <input placeholder="Title (optional — auto-named from the URL)"
               value={title} onChange={(e) => setTitle(e.target.value)} />
        <button type="submit" disabled={creating}>
          {creating ? "creating…" : "Create"}
        </button>
      </form>
      {sourceType === "url" && (
        <p className="hint">Leave the title blank to auto-name it "author/podcast - title" from the URL.</p>
      )}
      {error && <p className="error">{error}</p>}

      <h2>Projects</h2>
      <table className="list">
        <thead><tr>
          <th>Title</th><th>Source</th><th>Status</th>
          <th>Last activity</th><th>Created</th><th></th>
        </tr></thead>
        <tbody>
          {projects.map((p) => (
            <tr key={p.id}>
              <td><Link to={`/projects/${p.id}`}>{p.title}</Link></td>
              <td className="mono">{p.source.slice(0, 60)}</td>
              <td><StatusCell p={p} /></td>
              <td className="muted" title={p.progress?.last_activity
                ? fmtDateTime(p.progress.last_activity) : ""}>
                {p.progress?.last_activity ? fmtDate(p.progress.last_activity) : "—"}
              </td>
              <td>{fmtDate(p.created)}</td>
              <td>
                <button className="linkish danger" title="delete project"
                        onClick={() => remove(p)}>🗑</button>
              </td>
            </tr>
          ))}
          {loaded && projects.length === 0 && (
            <tr><td colSpan={6} className="empty">No projects yet — add one above.</td></tr>
          )}
        </tbody>
      </table>
    </div>
  );
}
