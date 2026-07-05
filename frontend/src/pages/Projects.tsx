import { FormEvent, useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api, Project } from "../api";

export default function Projects() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [source, setSource] = useState("");
  const [sourceType, setSourceType] = useState<"url" | "local">("url");
  const [title, setTitle] = useState("");
  const [error, setError] = useState("");
  const nav = useNavigate();

  useEffect(() => {
    api<Project[]>("/projects").then(setProjects).catch((e) => setError(e.message));
  }, []);

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
        <thead><tr><th>Title</th><th>Source</th><th>Status</th><th>Created</th><th></th></tr></thead>
        <tbody>
          {projects.map((p) => (
            <tr key={p.id}>
              <td><Link to={`/projects/${p.id}`}>{p.title}</Link></td>
              <td className="mono">{p.source.slice(0, 60)}</td>
              <td>{p.status}</td>
              <td>{new Date(p.created).toLocaleDateString()}</td>
              <td>
                <button className="linkish danger" title="delete project"
                        onClick={() => remove(p)}>🗑</button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
