import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { api, Artifact, fmtDateTime, Project, typeLabel } from "../api";
import MindMap, { Graph } from "../components/MindMap";

interface Detail {
  artifact: Artifact;
  meta: Record<string, any>;
  body: string;
  tags: string[];
  project: Project | null;
}

function parseGraph(body: string): Graph | null {
  const m = body.match(/```json\s*([\s\S]*?)```/) ?? [null, body];
  try {
    const g = JSON.parse(m[1] ?? body);
    return g.nodes ? g : null;
  } catch {
    return null;
  }
}

export default function ArtifactView() {
  const { id } = useParams();
  const [d, setD] = useState<Detail | null>(null);
  const [error, setError] = useState("");
  const [editingTags, setEditingTags] = useState(false);
  const [tagText, setTagText] = useState("");

  useEffect(() => {
    api<Detail>(`/artifacts/${id}`).then(setD).catch((e) => setError(e.message));
  }, [id]);

  async function saveTags() {
    const tags = tagText.split(",").map((t) => t.trim()).filter(Boolean);
    const r = await api<{ tags: string[] }>(`/artifacts/${id}/tags`, {
      method: "PUT",
      body: JSON.stringify({ tags }),
    });
    setD((prev) => (prev ? { ...prev, tags: r.tags } : prev));
    setEditingTags(false);
  }

  if (error) return <p className="error">{error}</p>;
  if (!d) return <p>loading…</p>;

  const graph = d.artifact.type === "mindmap" ? parseGraph(d.body) : null;

  return (
    <div className="artifact">
      <header>
        <h2>{d.artifact.title}</h2>
        <p className="meta">
          {typeLabel(d.artifact.type)}
          {d.project && <> · <Link to={`/projects/${d.project.id}`}>{d.project.title}</Link></>}
          {d.artifact.model && <> · {d.artifact.provider}/{d.artifact.model}</>}
          {" · "}{fmtDateTime(d.artifact.updated)}
        </p>
        <p className="tags">
          {d.tags.map((t) => <span key={t} className="tag">{t}</span>)}
          {editingTags ? (
            <>
              <input value={tagText} onChange={(e) => setTagText(e.target.value)} />
              <button onClick={saveTags}>save</button>
            </>
          ) : (
            <button onClick={() => { setTagText(d.tags.join(", ")); setEditingTags(true); }}>
              edit tags
            </button>
          )}
        </p>
      </header>

      {d.artifact.media_path && (
        <>
          {d.artifact.type === "source_video" ? (
            <video controls src={`/api/media/${d.artifact.id}`}
                   style={{ width: "100%", maxHeight: "70vh", background: "#000" }} />
          ) : (
            <audio controls src={`/api/media/${d.artifact.id}`} style={{ width: "100%" }} />
          )}
          <p>
            <a href={`/api/media/${d.artifact.id}`} download>⬇ download file</a>
          </p>
        </>
      )}

      {graph ? (
        <MindMap graph={graph} />
      ) : (
        <article className="markdown">
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{d.body}</ReactMarkdown>
        </article>
      )}
    </div>
  );
}
