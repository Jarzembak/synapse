import { useEffect, useRef, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
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

function requestedTime(value: string | null): number {
  const seconds = Number(value);
  return Number.isFinite(seconds) && seconds >= 0 ? seconds : 0;
}

export default function ArtifactView() {
  const { id } = useParams();
  const [searchParams] = useSearchParams();
  const [d, setD] = useState<Detail | null>(null);
  const [error, setError] = useState("");
  const [errorFor, setErrorFor] = useState("");
  const [actionError, setActionError] = useState("");
  const [editingTags, setEditingTags] = useState(false);
  const [savingTags, setSavingTags] = useState(false);
  const [tagText, setTagText] = useState("");
  const mediaRef = useRef<HTMLMediaElement | null>(null);
  const currentId = useRef(id);
  currentId.current = id;
  const startAt = requestedTime(searchParams.get("t"));

  function seekToCitation() {
    const player = mediaRef.current;
    if (!player || player.readyState < 1 || startAt <= 0) return;
    player.currentTime = startAt;
  }

  useEffect(() => {
    const controller = new AbortController();
    const requestId = id ?? "";
    setD(null);
    setError("");
    setErrorFor(requestId);
    setActionError("");
    setEditingTags(false);
    setSavingTags(false);
    api<Detail>(`/artifacts/${id}`, { signal: controller.signal })
      .then((nextDetail) => {
        if (!controller.signal.aborted && currentId.current === requestId) {
          setD(nextDetail);
          setError("");
        }
      })
      .catch((caught) => {
        if (!controller.signal.aborted && currentId.current === requestId) {
          setError(caught instanceof Error ? caught.message : "Could not load artifact");
        }
      });
    return () => controller.abort();
  }, [id]);

  useEffect(() => {
    seekToCitation();
  }, [id, startAt, d?.artifact.id]);

  async function saveTags() {
    const requestId = id;
    const tags = tagText.split(",").map((t) => t.trim()).filter(Boolean);
    setSavingTags(true);
    setActionError("");
    try {
      const response = await api<{ tags: string[] }>(`/artifacts/${id}/tags`, {
        method: "PUT",
        body: JSON.stringify({ tags }),
      });
      if (currentId.current !== requestId) return;
      setD((previous) => previous ? { ...previous, tags: response.tags } : previous);
      setEditingTags(false);
    } catch (caught) {
      if (currentId.current === requestId) {
        setActionError(caught instanceof Error ? caught.message : "Could not save tags");
      }
    } finally {
      if (currentId.current === requestId) setSavingTags(false);
    }
  }

  if (error && errorFor === (id ?? "")) return <p className="error" role="alert">{error}</p>;
  if (!d || d.artifact.id !== Number(id)) return <p role="status">Loading artifact...</p>;

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
              <label className="sr-only" htmlFor="artifact-tags">Comma-separated tags</label>
              <input id="artifact-tags" value={tagText}
                onChange={(e) => setTagText(e.target.value)} disabled={savingTags} />
              <button type="button" onClick={() => void saveTags()} disabled={savingTags}>
                {savingTags ? "Saving..." : "Save"}
              </button>
              <button type="button" onClick={() => setEditingTags(false)} disabled={savingTags}>
                Cancel
              </button>
            </>
          ) : (
            <button type="button" onClick={() => {
              setTagText(d.tags.join(", "));
              setActionError("");
              setEditingTags(true);
            }}>
              Edit tags
            </button>
          )}
        </p>
        {actionError && <p className="error" role="alert">Could not save tags: {actionError}</p>}
      </header>

      {d.artifact.media_path && (
        <>
          {d.artifact.type === "source_video" ? (
            <video controls src={`/api/media/${d.artifact.id}`}
                   aria-label={`${d.artifact.title} video`}
                   ref={(node) => { mediaRef.current = node; }}
                   onLoadedMetadata={seekToCitation}
                   style={{ width: "100%", maxHeight: "70vh", background: "#000" }} />
          ) : (
            <audio controls src={`/api/media/${d.artifact.id}`}
                   aria-label={`${d.artifact.title} audio`}
                   ref={(node) => { mediaRef.current = node; }}
                   onLoadedMetadata={seekToCitation}
                   style={{ width: "100%" }} />
          )}
          {startAt > 0 && <p className="notice">Opened at {Math.floor(startAt / 60)}:{String(Math.floor(startAt % 60)).padStart(2, "0")}.</p>}
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
