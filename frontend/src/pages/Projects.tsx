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
  const [sourceType, setSourceType] = useState<"url" | "local" | "upload">("url");
  const [uploadedFile, setUploadedFile] = useState<File | null>(null);
  const [title, setTitle] = useState("");
  const [error, setError] = useState("");
  const [creating, setCreating] = useState(false);
  const [uploadProgress, setUploadProgress] = useState<number | null>(null);
  const nav = useNavigate();
  const reloadSeq = useRef(0);
  const uploadRequest = useRef<XMLHttpRequest | null>(null);

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
  useEffect(() => {
    reload();
    return () => {
      reloadSeq.current += 1;
      uploadRequest.current?.abort();
    };
  }, []);

  // live-refresh the derived status while pipelines run: the job SSE stream
  // fires whenever the active/recent job set changes (and heartbeats), so
  // re-pull the list then. The reconnecting hook keeps this alive across a
  // worker/api restart instead of freezing after one.
  useEventSource("/api/jobs/stream", "jobs", () => reload());

  function uploadProject(file: File): Promise<Project> {
    const params = new URLSearchParams({ filename: file.name });
    if (title.trim()) params.set("title", title.trim());
    return new Promise((resolve, reject) => {
      const request = new XMLHttpRequest();
      uploadRequest.current = request;
      request.open("POST", `/api/projects/upload?${params.toString()}`);
      request.setRequestHeader("Content-Type", "application/octet-stream");
      request.responseType = "json";
      request.upload.onprogress = (event) => {
        if (event.lengthComputable && event.total > 0) {
          setUploadProgress(Math.round((event.loaded / event.total) * 100));
        }
      };
      request.onload = () => {
        uploadRequest.current = null;
        const payload = request.response;
        if (request.status >= 200 && request.status < 300) resolve(payload as Project);
        else reject(new Error(payload?.detail ?? request.statusText ?? "Upload failed"));
      };
      request.onerror = () => {
        uploadRequest.current = null;
        reject(new Error("The upload connection failed."));
      };
      request.onabort = () => {
        uploadRequest.current = null;
        reject(new Error("Upload canceled."));
      };
      request.send(file);
    });
  }

  async function create(e: FormEvent) {
    e.preventDefault();
    setCreating(true);
    setError("");
    try {
      let p: Project;
      if (sourceType === "upload") {
        if (!uploadedFile) throw new Error("Choose an audio or video file first.");
        setUploadProgress(0);
        p = await uploadProject(uploadedFile);
      } else {
        p = await api<Project>("/projects", {
          method: "POST",
          body: JSON.stringify({ source, source_type: sourceType, title: title || null }),
        });
      }
      nav(`/projects/${p.id}`);
    } catch (err: any) {
      setError(err.message);
    } finally {
      setCreating(false);
      setUploadProgress(null);
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
        <label className="sr-only" htmlFor="source-kind">Source type</label>
        <select id="source-kind" value={sourceType}
          onChange={(e) => setSourceType(e.target.value as typeof sourceType)}>
          <option value="url">URL</option>
          <option value="upload">Upload a file</option>
          <option value="local">Local file</option>
        </select>
        {sourceType === "upload" ? (
          <>
          <label className="sr-only" htmlFor="source-upload">Audio or video file</label>
          <input
            id="source-upload"
            type="file"
            accept="audio/*,video/*,.mkv,.m4a,.flac,.opus"
            onChange={(e) => setUploadedFile(e.target.files?.[0] ?? null)}
            required
          />
          </>
        ) : (
          <>
          <label className="sr-only" htmlFor="project-source">
            {sourceType === "url" ? "Source URL" : "Local media path"}
          </label>
          <input
            id="project-source"
            placeholder={sourceType === "url"
              ? "https://www.youtube.com/watch?v=…"
              : "path relative to your HOST_MEDIA_DIR, e.g. talks/recon.mp4"}
            value={source}
            onChange={(e) => setSource(e.target.value)}
            required
          />
          </>
        )}
        <label className="sr-only" htmlFor="project-title-draft">Project title</label>
        <input id="project-title-draft" placeholder="Title (optional — auto-named from the URL)"
               value={title} onChange={(e) => setTitle(e.target.value)} />
        <button type="submit" disabled={creating}>
          {creating && sourceType === "upload"
            ? uploadProgress !== null && uploadProgress > 0
              ? `Uploading ${uploadProgress}%…`
              : "Uploading…"
            : creating ? "Creating…" : "Create"}
        </button>
        {creating && sourceType === "upload" && (
          <button type="button" onClick={() => uploadRequest.current?.abort()}>Cancel upload</button>
        )}
      </form>
      {sourceType === "url" && (
        <p className="hint">Leave the title blank to auto-name it "author/podcast - title" from the URL.</p>
      )}
      {sourceType === "upload" && (
        <p className="hint">The file is stored privately with the project and removed when the project is deleted.</p>
      )}
      {error && <p className="error" role="alert">{error}</p>}

      <h2>Projects</h2>
      <div className="table-scroll" tabIndex={0} aria-label="Projects table; scroll horizontally if needed">
      <table className="list projects-table">
        <caption className="sr-only">Projects</caption>
        <thead><tr>
          <th scope="col">Title</th><th scope="col">Source</th><th scope="col">Status</th>
          <th scope="col">Last activity</th><th scope="col">Created</th>
          <th scope="col"><span className="sr-only">Actions</span></th>
        </tr></thead>
        <tbody>
          {projects.map((p) => (
            <tr key={p.id}>
              <td><Link to={`/projects/${p.id}`}>{p.title}</Link></td>
              <td className="mono" title={p.source}>{p.source.slice(0, 60)}</td>
              <td><StatusCell p={p} /></td>
              <td className="muted" title={p.progress?.last_activity
                ? fmtDateTime(p.progress.last_activity) : ""}>
                {p.progress?.last_activity ? fmtDate(p.progress.last_activity) : "—"}
              </td>
              <td>{fmtDate(p.created)}</td>
              <td>
                <button type="button" className="linkish danger"
                        aria-label={`Delete ${p.title}`}
                        onClick={() => void remove(p)}>Delete</button>
              </td>
            </tr>
          ))}
          {loaded && projects.length === 0 && (
            <tr><td colSpan={6} className="empty">No projects yet — add one above.</td></tr>
          )}
          {!loaded && (
            <tr><td colSpan={6} className="empty">Loading projects...</td></tr>
          )}
        </tbody>
      </table>
      </div>
    </div>
  );
}
