import { isValidElement, ReactNode, useEffect, useMemo, useRef, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  api,
  Artifact,
  fmtDateTime,
  isRepositoryProject,
  Project,
  RepositoryCitation,
  RepositoryDetail,
  shortSha,
  typeLabel,
} from "../api";
import MindMap, { Graph } from "../components/MindMap";

interface Detail {
  artifact: Artifact;
  meta: Record<string, unknown>;
  body: string;
  tags: string[];
  project: Project | null;
  repository?: RepositoryDetail | null;
  citations?: RepositoryCitation[];
  related_artifacts?: Artifact[];
}

interface TocItem {
  level: number;
  text: string;
  id: string;
}

function parseGraph(body: string): Graph | null {
  if (body.length > 2_000_000) return null;
  const match = body.match(/```(?:json|text)\s*([\s\S]*?)```/) ?? [null, body];
  try {
    const graph = JSON.parse(match[1] ?? body) as Partial<Graph>;
    if (!Array.isArray(graph.nodes) || graph.nodes.length > 2_000
      || (graph.edges !== undefined && (!Array.isArray(graph.edges) || graph.edges.length > 5_000))) {
      return null;
    }
    const validNodes = graph.nodes.every((node) => node && typeof node === "object"
      && typeof node.id === "string" && node.id.length <= 200
      && typeof node.label === "string" && node.label.length <= 1_000
      && typeof node.kind === "string" && node.kind.length <= 100);
    const validEdges = (graph.edges ?? []).every((edge) => edge && typeof edge === "object"
      && typeof edge.source === "string" && edge.source.length <= 200
      && typeof edge.target === "string" && edge.target.length <= 200);
    return validNodes && validEdges
      ? { nodes: graph.nodes, edges: graph.edges ?? [] } as Graph
      : null;
  } catch {
    return null;
  }
}

function requestedTime(value: string | null): number {
  const seconds = Number(value);
  return Number.isFinite(seconds) && seconds >= 0 ? seconds : 0;
}

function safeRestrictedImageSource(src: string | undefined): boolean {
  if (!src) return true;
  if (!src.startsWith("/") || src.startsWith("//") || /[\\\u0000-\u001f\u007f]/.test(src)) {
    return false;
  }
  try {
    return new URL(src, window.location.origin).origin === window.location.origin;
  } catch {
    return false;
  }
}

function textFromNode(node: ReactNode): string {
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(textFromNode).join("");
  if (isValidElement(node)) {
    return textFromNode((node.props as { children?: ReactNode }).children);
  }
  return "";
}

function slugPart(value: string): string {
  return value
    .toLocaleLowerCase()
    .replace(/[`*_~[\](){}<>]/g, "")
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "") || "section";
}

function extractToc(markdown: string): TocItem[] {
  const used = new Map<string, number>();
  const items: TocItem[] = [];
  let fenced = false;
  for (const line of markdown.split(/\r?\n/)) {
    if (/^\s*```/.test(line)) {
      fenced = !fenced;
      continue;
    }
    if (fenced) continue;
    const heading = line.match(/^(#{1,4})\s+(.+?)\s*#*$/);
    if (!heading) continue;
    const text = heading[2].replace(/[*_`[\]]/g, "").trim();
    const base = slugPart(text);
    const count = used.get(base) ?? 0;
    used.set(base, count + 1);
    items.push({ level: heading[1].length, text, id: count ? `${base}-${count + 1}` : base });
  }
  return items;
}

function citationUrl(citation: RepositoryCitation, repository: RepositoryDetail | null): string | null {
  if (citation.permalink || citation.url) return citation.permalink ?? citation.url ?? null;
  const root = repository?.source.canonical_url ?? repository?.source.url;
  const sha = citation.commit_sha || repository?.snapshot.commit_sha;
  if (!root || !sha) return null;
  const path = citation.path.split("/").map(encodeURIComponent).join("/");
  const end = citation.end_line && citation.end_line !== citation.start_line
    ? `-L${citation.end_line}`
    : "";
  return `${root.replace(/\.git$/, "")}/blob/${sha}/${path}#L${citation.start_line}${end}`;
}

function citationsFromMarkdown(markdown: string, fallbackSha: string): RepositoryCitation[] {
  const citations: RepositoryCitation[] = [];
  const pattern = /\[`?([^\]`]+):L(\d+)(?:-L(\d+))?`?\]\((https?:\/\/[^)]+)\)(?:<!--E:([^>]+)-->)?/g;
  for (const match of markdown.matchAll(pattern)) {
    const commit = match[4].match(/\/blob\/([^/]+)\//)?.[1] ?? fallbackSha;
    citations.push({
      marker: match[5] || `S${citations.length + 1}`,
      path: match[1],
      start_line: Number(match[2]),
      end_line: Number(match[3] || match[2]),
      commit_sha: commit,
      permalink: match[4],
      classification: "detected",
    });
  }
  return citations;
}

function CommandBlock({ children }: { children?: ReactNode }) {
  const [copied, setCopied] = useState(false);
  const content = textFromNode(children).replace(/\n$/, "");

  async function copy() {
    try {
      await navigator.clipboard.writeText(content);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1600);
    } catch {
      setCopied(false);
    }
  }

  return (
    <div className="code-block">
      <button type="button" className="copy-command" onClick={() => void copy()}
        aria-label="Copy code or command">
        {copied ? "Copied" : "Copy"}
      </button>
      <pre>{children}</pre>
    </div>
  );
}

export default function ArtifactView() {
  const { id } = useParams();
  const [searchParams] = useSearchParams();
  const [detail, setDetail] = useState<Detail | null>(null);
  const [repository, setRepository] = useState<RepositoryDetail | null>(null);
  const [related, setRelated] = useState<Artifact[]>([]);
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
    setDetail(null);
    setRepository(null);
    setRelated([]);
    setError("");
    setErrorFor(requestId);
    setActionError("");
    setEditingTags(false);
    setSavingTags(false);

    void api<Detail>(`/artifacts/${id}`, { signal: controller.signal })
      .then(async (nextDetail) => {
        if (controller.signal.aborted || currentId.current !== requestId) return;
        setDetail(nextDetail);
        setRepository(nextDetail.repository ?? null);
        setRelated(nextDetail.related_artifacts ?? []);

        if (isRepositoryProject(nextDetail.project) && nextDetail.project) {
          const tasks: Promise<void>[] = [];
          if (!nextDetail.repository) {
            tasks.push(api<RepositoryDetail>(`/repositories/${nextDetail.project.id}`, {
              signal: controller.signal,
            }).then((value) => { if (!controller.signal.aborted) setRepository(value); }).catch(() => {}));
          }
          if (!nextDetail.related_artifacts?.length) {
            const params = new URLSearchParams({
              project_id: String(nextDetail.project.id),
              sort: "updated",
              order: "desc",
              offset: "0",
              limit: "25",
            });
            tasks.push(api<{ items: Artifact[] }>(`/library/query?${params.toString()}`, {
              signal: controller.signal,
            }).then((value) => {
              if (!controller.signal.aborted) {
                setRelated(value.items.filter((artifact) => artifact.id !== nextDetail.artifact.id));
              }
            }).catch(() => {}));
          }
          await Promise.all(tasks);
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
  }, [id, startAt, detail?.artifact.id]);

  async function saveTags() {
    const requestId = id;
    const tags = tagText.split(",").map((tag) => tag.trim()).filter(Boolean);
    setSavingTags(true);
    setActionError("");
    try {
      const response = await api<{ tags: string[] }>(`/artifacts/${id}/tags`, {
        method: "PUT",
        body: JSON.stringify({ tags }),
      });
      if (currentId.current !== requestId) return;
      setDetail((previous) => previous ? { ...previous, tags: response.tags } : previous);
      setEditingTags(false);
    } catch (caught) {
      if (currentId.current === requestId) {
        setActionError(caught instanceof Error ? caught.message : "Could not save tags");
      }
    } finally {
      if (currentId.current === requestId) setSavingTags(false);
    }
  }

  const toc = useMemo(() => extractToc(detail?.body ?? ""), [detail?.body]);

  if (error && errorFor === (id ?? "")) return <p className="error" role="alert">{error}</p>;
  if (!detail || detail.artifact.id !== Number(id)) return <p role="status">Loading artifact...</p>;

  const graph = detail.artifact.type === "mindmap" ? parseGraph(detail.body) : null;
  const repositoryProject = isRepositoryProject(detail.project);
  const commitSha = repository?.snapshot.commit_sha
    ?? (typeof detail.meta.commit_sha === "string" ? detail.meta.commit_sha : null);
  const rawCitations = detail.citations ?? detail.meta.citations;
  const citations = Array.isArray(rawCitations)
    ? rawCitations as RepositoryCitation[]
    : citationsFromMarkdown(detail.body, commitSha ?? "");
  const coverage = repository?.coverage;
  const metaCoverage = detail.meta.coverage && typeof detail.meta.coverage === "object"
    ? detail.meta.coverage as Record<string, unknown>
    : null;
  const headingPosition = new Map<string, number>();

  function heading(level: 1 | 2 | 3 | 4, children: ReactNode) {
    const text = textFromNode(children);
    const key = `${level}:${text}`;
    const occurrence = headingPosition.get(key) ?? 0;
    headingPosition.set(key, occurrence + 1);
    const match = toc.filter((item) => item.level === level && item.text === text)[occurrence];
    const headingId = match?.id ?? slugPart(text);
    const Heading = `h${level}` as keyof JSX.IntrinsicElements;
    return <Heading id={headingId} className="anchored-heading">{children}</Heading>;
  }

  return (
    <div className="artifact">
      <header>
        <h2>{detail.artifact.title}</h2>
        <p className="meta">
          {typeLabel(detail.artifact.type)}
          {detail.project && <> · <Link to={`/projects/${detail.project.id}`}>{detail.project.title}</Link></>}
          {detail.artifact.model && <> · {detail.artifact.provider}/{detail.artifact.model}</>}
          {" · "}{fmtDateTime(detail.artifact.updated)}
        </p>
        {repositoryProject && (
          <div className="artifact-provenance" aria-label="Repository provenance">
            <span className="source-badge repository">Repository guide</span>
            {repository?.source.full_name && <span>{repository.source.full_name}</span>}
            {commitSha && <span>Commit <code>{shortSha(commitSha)}</code></span>}
            {coverage && (
              <span>
                {coverage.included_files ?? coverage.indexed_file_count ?? coverage.preview?.eligible_files ?? "—"} of{" "}
                {coverage.total_files ?? coverage.file_count ?? coverage.preview?.total_files ?? "unknown"} files in analysis scope
              </span>
            )}
            {!coverage && metaCoverage && (
              <span>
                {String(metaCoverage.analyzed_evidence_chunks ?? "—")} of{" "}
                {String(metaCoverage.total_evidence_chunks ?? "unknown")} evidence chunks analyzed
              </span>
            )}
            <span>Static analysis · no repository code executed</span>
            {detail.project && (
              <Link to={`/?project_id=${detail.project.id}&mode=hybrid#ask-library-title`}>
                Ask this repository
              </Link>
            )}
          </div>
        )}
        <p className="tags">
          {detail.tags.map((tag) => <span key={tag} className="tag">{tag}</span>)}
          {editingTags ? (
            <>
              <label className="sr-only" htmlFor="artifact-tags">Comma-separated tags</label>
              <input id="artifact-tags" value={tagText}
                onChange={(event) => setTagText(event.target.value)} disabled={savingTags} />
              <button type="button" onClick={() => void saveTags()} disabled={savingTags}>
                {savingTags ? "Saving..." : "Save"}
              </button>
              <button type="button" onClick={() => setEditingTags(false)} disabled={savingTags}>Cancel</button>
            </>
          ) : (
            <button type="button" onClick={() => {
              setTagText(detail.tags.join(", "));
              setActionError("");
              setEditingTags(true);
            }}>Edit tags</button>
          )}
        </p>
        {actionError && <p className="error" role="alert">Could not save tags: {actionError}</p>}
      </header>

      {!repositoryProject && detail.artifact.media_path && (
        <>
          {detail.artifact.type === "source_video" ? (
            <video controls src={`/api/media/${detail.artifact.id}`}
              aria-label={`${detail.artifact.title} video`}
              ref={(node) => { mediaRef.current = node; }}
              onLoadedMetadata={seekToCitation}
              style={{ width: "100%", maxHeight: "70vh", background: "#000" }} />
          ) : (
            <audio controls src={`/api/media/${detail.artifact.id}`}
              aria-label={`${detail.artifact.title} audio`}
              ref={(node) => { mediaRef.current = node; }}
              onLoadedMetadata={seekToCitation}
              style={{ width: "100%" }} />
          )}
          {startAt > 0 && <p className="notice">Opened at {Math.floor(startAt / 60)}:{String(Math.floor(startAt % 60)).padStart(2, "0")}.</p>}
          <p><a href={`/api/media/${detail.artifact.id}`} download>Download file</a></p>
        </>
      )}

      {graph ? (
        <MindMap graph={graph} />
      ) : (
        <div className={`artifact-reading-layout ${toc.length < 2 ? "without-toc" : ""}`}>
          {toc.length >= 2 && (
            <nav className="artifact-toc card" aria-label="On this page">
              <strong>On this page</strong>
              <ol>
                {toc.map((item) => (
                  <li key={item.id} className={`toc-level-${item.level}`}>
                    <a href={`#${item.id}`}>{item.text}</a>
                  </li>
                ))}
              </ol>
            </nav>
          )}
          <article className="markdown">
            <ReactMarkdown
              remarkPlugins={[remarkGfm]}
              components={{
                h1: ({ children }) => heading(1, children),
                h2: ({ children }) => heading(2, children),
                h3: ({ children }) => heading(3, children),
                h4: ({ children }) => heading(4, children),
                pre: ({ children }) => <CommandBlock>{children}</CommandBlock>,
                img: ({ node: _node, src, alt, ...props }) => {
                  const safeSource = safeRestrictedImageSource(src);
                  if (detail.artifact.restricted && !safeSource) {
                    return <span className="notice">External image omitted for local-only safety{alt ? `: ${alt}` : ""}</span>;
                  }
                  return <img {...props} src={src} alt={alt ?? ""} />;
                },
              }}
            >{detail.body}</ReactMarkdown>
          </article>
        </div>
      )}

      {repositoryProject && citations.length > 0 && (
        <section className="artifact-citations" aria-labelledby="artifact-citations-title">
          <h3 id="artifact-citations-title">Repository evidence</h3>
          <p className="meta">Each link is pinned to the exact commit used for this analysis.</p>
          <ol>
            {citations.map((citation, index) => {
              const link = citationUrl(citation, repository);
              const end = citation.end_line && citation.end_line !== citation.start_line
                ? `–${citation.end_line}`
                : "";
              return (
                <li key={`${citation.path}:${citation.start_line}:${index}`}>
                  <div className="citation-head">
                    <span className="citation-marker">[{citation.marker ?? `S${index + 1}`}]</span>
                    {link ? (
                      <a href={link} target="_blank" rel="noreferrer">
                        <code>{citation.path}:{citation.start_line}{end}</code>
                      </a>
                    ) : (
                      <code>{citation.path}:{citation.start_line}{end}</code>
                    )}
                    <span className="source-badge commit">{shortSha(citation.commit_sha || commitSha)}</span>
                    {citation.classification && <span className="kindbadge">{citation.classification}</span>}
                  </div>
                  {citation.excerpt && <blockquote>{citation.excerpt}</blockquote>}
                </li>
              );
            })}
          </ol>
        </section>
      )}

      {repositoryProject && related.length > 0 && (
        <aside className="related-artifacts" aria-labelledby="related-artifacts-title">
          <h3 id="related-artifacts-title">Continue exploring this repository</h3>
          <div className="related-artifact-grid">
            {related.slice(0, 8).map((artifact) => (
              <Link className="card" to={`/artifacts/${artifact.id}`} key={artifact.id}>
                <span className="kindbadge">{typeLabel(artifact.type)}</span>
                <strong>{artifact.title}</strong>
              </Link>
            ))}
          </div>
        </aside>
      )}
    </div>
  );
}
