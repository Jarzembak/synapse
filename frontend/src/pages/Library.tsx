import { FormEvent, ReactNode, useEffect, useMemo, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import {
  api,
  Artifact,
  fmtDate,
  Project,
  TYPE_LABELS,
  isPaperProject,
  isRepositoryProject,
  paperAudienceLabel,
  shortSha,
  typeLabel,
} from "../api";

interface TagInfo {
  id: number;
  name: string;
  kind: string;
  count: number;
}

interface Facets {
  types: Record<string, number>;
  projects: Record<string, number>;
  tags: Record<string, number>;
}

interface QueryResponse {
  items: Artifact[];
  total: number;
  offset: number;
  limit: number;
  facets: Facets;
}

interface HybridResult {
  chunk_id: number | string;
  artifact_id: number | null;
  artifact_title: string;
  artifact_type: string;
  project_id: number | null;
  project_title: string | null;
  project_slug: string | null;
  media_artifact_id: number | null;
  start_time: string | null;
  excerpt: string;
  tags: string[];
  score: number;
  source_kind?: "artifact" | "repository" | "repository_file" | "paper" | "paper_evidence";
  path?: string | null;
  repository_path?: string | null;
  file_path?: string | null;
  start_line?: number | null;
  end_line?: number | null;
  commit_sha?: string | null;
  immutable_url?: string | null;
  permalink?: string | null;
  source_url?: string | null;
  restricted?: boolean;
  paper_series_id?: number | null;
  paper_part_id?: number | null;
  audience?: string | null;
  evidence_id?: string | null;
  page?: number | null;
  section?: string | string[] | null;
  internal_pdf_url?: string | null;
}

interface HybridResponse {
  results: HybridResult[];
  semantic_enabled: boolean;
  embedding_model?: string;
}

interface AskSource extends HybridResult {
  marker: string;
}

interface AskResponse {
  answer: string;
  sources: AskSource[];
  grounded: boolean;
}

interface AskResult extends AskResponse {
  question: string;
  contextKey: string;
}

interface Loaded<T> {
  key: string;
  data: T;
}

type SearchMode = "exact" | "hybrid";
type SortKey = "relevance" | "title" | "type" | "created" | "updated";
type SortOrder = "asc" | "desc";

const SORT_KEYS = new Set<SortKey>([
  "relevance",
  "title",
  "type",
  "created",
  "updated",
]);
const PAGE_SIZES = [25, 50, 100] as const;

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "Unexpected error";
}

function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

function csvValues(value: string): string[] {
  return [...new Set(value.split(",").map((item) => item.trim()).filter(Boolean))].sort();
}

function positiveInteger(value: string | null): number | null {
  if (!value) return null;
  const parsed = Number(value);
  return Number.isInteger(parsed) && parsed > 0 ? parsed : null;
}

function timestampSeconds(timestamp: string): number {
  const [hours, minutes, seconds] = timestamp.split(":").map(Number);
  if (![hours, minutes, seconds].every(Number.isFinite)) return 0;
  return Math.max(0, hours * 3600 + minutes * 60 + seconds);
}

function GroundedAnswer({ answer, sources }: { answer: string; sources: AskSource[] }) {
  const markers = new Set(sources.map((source) => source.marker));
  const parts = answer.split(/(\[S\d+\])/g);

  return (
    <div className="ask-answer">
      {parts.map((part, index) => {
        const marker = part.slice(1, -1);
        return markers.has(marker) ? (
          <a key={`${part}-${index}`} href={`#ask-source-${marker}`} className="inline-citation">
            {part}
          </a>
        ) : (
          <span key={`${part}-${index}`}>{part}</span>
        );
      })}
    </div>
  );
}

function SourceMeta({ source }: { source: HybridResult }) {
  const repositoryPath = source.repository_path ?? source.file_path ?? source.path;
  const repositoryUrl = source.immutable_url ?? source.permalink ?? source.source_url;
  const lineLabel = source.start_line
    ? `:${source.start_line}${source.end_line && source.end_line !== source.start_line ? `–${source.end_line}` : ""}`
    : "";
  return (
    <span className="source-meta">
      {source.project_id ? (
        <Link to={`/projects/${source.project_id}`}>
          {source.project_title ?? source.project_slug ?? `Project ${source.project_id}`}
        </Link>
      ) : (
        <span>Shared library</span>
      )}
      {source.artifact_id && (
        <>
          <span aria-hidden="true"> / </span>
          <Link to={`/artifacts/${source.artifact_id}`}>{source.artifact_title}</Link>
        </>
      )}
      {repositoryPath && (
        <>
          <span aria-hidden="true"> / </span>
          {repositoryUrl ? (
            <a className="repository-source-link" href={repositoryUrl} target="_blank" rel="noreferrer"
              title="Open the file at the analyzed commit">
              <code>{repositoryPath}{lineLabel}</code>
            </a>
          ) : (
            <code>{repositoryPath}{lineLabel}</code>
          )}
          {source.commit_sha && <span className="source-badge commit">{shortSha(source.commit_sha)}</span>}
          {source.restricted && <span className="source-badge private">Local only</span>}
        </>
      )}
      {source.start_time && source.media_artifact_id ? (
        <Link
          className="citation-time play-citation"
          title={`Play source media at ${source.start_time}`}
          to={`/artifacts/${source.media_artifact_id}?t=${timestampSeconds(source.start_time)}`}
        >
          play @ {source.start_time}
        </Link>
      ) : source.start_time ? (
        <span className="citation-time" title="Transcript timestamp">
          @ {source.start_time}
        </span>
      ) : null}
      {source.page && (
        <a className="citation-time paper-page-citation"
          href={source.internal_pdf_url ?? source.source_url ?? `/api/papers/${source.project_id}/source#page=${source.page}`}
          target="_blank" rel="noreferrer" title={Array.isArray(source.section) ? source.section.join(" › ") : source.section ?? "Open cited paper page"}>
          page {source.page}{source.section ? ` · ${Array.isArray(source.section) ? source.section.join(" › ") : source.section}` : ""}
        </a>
      )}
      {source.evidence_id && <code className="paper-evidence-id">{source.evidence_id}</code>}
    </span>
  );
}

function FilterOption({
  checked,
  label,
  count,
  onChange,
}: {
  checked: boolean;
  label: string;
  count?: number;
  onChange: () => void;
}) {
  return (
    <label className="filter-option">
      <input type="checkbox" checked={checked} onChange={onChange} />
      <span>{label}</span>
      {count !== undefined && <small>{count}</small>}
    </label>
  );
}

export default function Library() {
  const [urlParams, setUrlParams] = useSearchParams();
  const [allTags, setAllTags] = useState<TagInfo[]>([]);
  const [projects, setProjects] = useState<Project[]>([]);
  const [metadataLoading, setMetadataLoading] = useState(true);
  const [metadataError, setMetadataError] = useState("");
  const [exactLoaded, setExactLoaded] = useState<Loaded<QueryResponse> | null>(null);
  const [hybridLoaded, setHybridLoaded] = useState<Loaded<HybridResponse> | null>(null);
  const [searchLoading, setSearchLoading] = useState(true);
  const [searchError, setSearchError] = useState("");
  const [refreshToken, setRefreshToken] = useState(0);
  const [askQuestion, setAskQuestion] = useState("");
  const [askResult, setAskResult] = useState<AskResult | null>(null);
  const [askLoading, setAskLoading] = useState(false);
  const [askError, setAskError] = useState("");
  const searchSequence = useRef(0);
  const askSequence = useRef(0);
  const askAbort = useRef<AbortController | null>(null);

  const mode: SearchMode = urlParams.get("mode") === "hybrid" ? "hybrid" : "exact";
  const q = urlParams.get("q") ?? "";
  const title = urlParams.get("title") ?? "";
  const typeParam = urlParams.get("type") ?? "";
  const tagParam = urlParams.get("tag") ?? "";
  const selectedTypes = useMemo(() => csvValues(typeParam), [typeParam]);
  const selectedTags = useMemo(() => csvValues(tagParam), [tagParam]);
  const projectId = positiveInteger(urlParams.get("project_id"));
  const paperSeriesId = positiveInteger(urlParams.get("paper_series_id"));
  const paperPartId = positiveInteger(urlParams.get("paper_part_id"));
  const audience = urlParams.get("audience") ?? "";
  const projectSlug = urlParams.get("project") ?? "";
  const rawSort = urlParams.get("sort") as SortKey | null;
  const sort: SortKey = rawSort && SORT_KEYS.has(rawSort)
    ? rawSort
    : q.trim() ? "relevance" : "updated";
  const order: SortOrder = urlParams.get("order") === "asc" ? "asc" : "desc";
  const rawOffset = Number(urlParams.get("offset") ?? 0);
  const offset = Number.isInteger(rawOffset) && rawOffset >= 0 ? rawOffset : 0;
  const requestedLimit = positiveInteger(urlParams.get("limit")) ?? 25;
  const limit = PAGE_SIZES.includes(requestedLimit as (typeof PAGE_SIZES)[number])
    ? requestedLimit
    : 25;

  const resolvedProjectId = projectId ??
    projects.find((project) => project.slug === projectSlug)?.id ?? null;
  const unresolvedProject = Boolean(projectSlug && !resolvedProjectId && !metadataLoading);
  const selectedProject = projects.find((project) => project.id === resolvedProjectId) ?? null;
  const askingRepository = isRepositoryProject(selectedProject);
  const askingPaper = isPaperProject(selectedProject);

  function updateUrl(
    updates: Record<string, string | number | null>,
    { resetPage = true, replace = true }: { resetPage?: boolean; replace?: boolean } = {},
  ) {
    const next = new URLSearchParams(urlParams);
    for (const [key, value] of Object.entries(updates)) {
      if (value === null || value === "") next.delete(key);
      else next.set(key, String(value));
    }
    if (resetPage) next.delete("offset");
    setUrlParams(next, { replace });
  }

  function toggleCsvFilter(key: "type" | "tag", selected: string[], value: string) {
    const next = new Set(selected);
    if (next.has(value)) next.delete(value);
    else next.add(value);
    updateUrl({ [key]: [...next].sort().join(",") || null });
  }

  function selectProject(value: string) {
    if (value.startsWith("id:")) {
      updateUrl({ project_id: value.slice(3), project: null });
    } else if (value.startsWith("slug:")) {
      updateUrl({ project: value.slice(5), project_id: null });
    } else {
      updateUrl({ project: null, project_id: null });
    }
  }

  function clearFilters() {
    updateUrl({
      title: null, type: null, tag: null, project: null, project_id: null,
      paper_series_id: null, paper_part_id: null, audience: null,
    });
  }

  function changeMode(nextMode: SearchMode) {
    updateUrl({ mode: nextMode === "hybrid" ? "hybrid" : null });
  }

  function changeQuery(value: string) {
    const updates: Record<string, string | null> = { q: value || null };
    if (!value.trim() && sort === "relevance") {
      updates.sort = null;
      updates.order = null;
    }
    updateUrl(updates);
  }

  useEffect(() => {
    const controller = new AbortController();
    let stopped = false;
    setMetadataLoading(true);
    setMetadataError("");

    void Promise.allSettled([
      api<TagInfo[]>("/tags", { signal: controller.signal }),
      api<Project[]>("/projects", { signal: controller.signal }),
    ]).then(([tagResult, projectResult]) => {
      if (stopped) return;
      const errors: string[] = [];
      if (tagResult.status === "fulfilled") setAllTags(tagResult.value);
      else if (!isAbortError(tagResult.reason)) errors.push("tags");
      if (projectResult.status === "fulfilled") setProjects(projectResult.value);
      else if (!isAbortError(projectResult.reason)) errors.push("projects");
      setMetadataError(errors.length ? `Could not load ${errors.join(" and ")} filters.` : "");
      setMetadataLoading(false);
    });

    return () => {
      stopped = true;
      controller.abort();
    };
  }, []);

  const exactRequest = useMemo(() => {
    const params = new URLSearchParams();
    if (q) params.set("q", q);
    if (title) params.set("title", title);
    if (selectedTypes.length) params.set("type", selectedTypes.join(","));
    if (selectedTags.length) params.set("tag", selectedTags.join(","));
    if (projectSlug) params.set("project", projectSlug);
    if (projectId) params.set("project_id", String(projectId));
    if (paperSeriesId) params.set("paper_series_id", String(paperSeriesId));
    if (paperPartId) params.set("paper_part_id", String(paperPartId));
    if (audience) params.set("audience", audience);
    params.set("sort", sort);
    params.set("order", order);
    params.set("offset", String(offset));
    params.set("limit", String(limit));
    return `/library/query?${params.toString()}`;
  }, [q, title, typeParam, tagParam, projectSlug, projectId, paperSeriesId, paperPartId, audience, sort, order, offset, limit]);

  const hybridRequest = useMemo(() => {
    const params = new URLSearchParams({ q, limit: "12" });
    if (selectedTypes.length) params.set("type", selectedTypes.join(","));
    if (selectedTags.length) params.set("tag", selectedTags.join(","));
    if (resolvedProjectId) params.set("project_id", String(resolvedProjectId));
    if (paperSeriesId) params.set("paper_series_id", String(paperSeriesId));
    if (paperPartId) params.set("paper_part_id", String(paperPartId));
    if (audience) params.set("audience", audience);
    return `/library/hybrid?${params.toString()}`;
  }, [q, typeParam, tagParam, resolvedProjectId, paperSeriesId, paperPartId, audience]);

  const activeRequest = mode === "exact" ? exactRequest : hybridRequest;

  useEffect(() => {
    const sequence = ++searchSequence.current;
    if (mode === "hybrid" && projectSlug && !resolvedProjectId) {
      setSearchLoading(metadataLoading);
      setSearchError(
        metadataLoading ? "" : "The project in this URL could not be resolved for hybrid search.",
      );
      setHybridLoaded(null);
      return;
    }

    const controller = new AbortController();
    setSearchLoading(true);
    setSearchError("");

    const timer = window.setTimeout(() => {
      const request: Promise<QueryResponse | HybridResponse> = mode === "exact"
        ? api<QueryResponse>(activeRequest, { signal: controller.signal })
        : api<HybridResponse>(activeRequest, { signal: controller.signal });

      void request.then((response) => {
        if (sequence !== searchSequence.current) return;
        if (mode === "exact") {
          setExactLoaded({ key: activeRequest, data: response as QueryResponse });
        } else {
          setHybridLoaded({ key: activeRequest, data: response as HybridResponse });
        }
        setSearchError("");
      }).catch((caught) => {
        if (sequence === searchSequence.current && !isAbortError(caught)) {
          setSearchError(errorMessage(caught));
        }
      }).finally(() => {
        if (sequence === searchSequence.current) setSearchLoading(false);
      });
    }, 220);

    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [mode, activeRequest, refreshToken, metadataLoading, projectSlug, resolvedProjectId]);

  useEffect(() => () => askAbort.current?.abort(), []);

  const latestFacets = exactLoaded?.data.facets ?? {
    types: {},
    projects: {},
    tags: {},
  };

  const typeOptions = useMemo(() => {
    const values = new Set([
      ...Object.keys(TYPE_LABELS),
      ...Object.keys(latestFacets.types),
      ...selectedTypes,
    ]);
    return [...values].sort((left, right) => typeLabel(left).localeCompare(typeLabel(right)));
  }, [latestFacets.types, typeParam]);

  const tagOptions = useMemo(() => {
    const counts = new Map(allTags.map((tag) => [tag.name, tag.count]));
    for (const [tag, count] of Object.entries(latestFacets.tags)) {
      if (!counts.has(tag)) counts.set(tag, count);
    }
    for (const tag of selectedTags) if (!counts.has(tag)) counts.set(tag, 0);
    return [...counts.entries()]
      .filter(([tag, count]) => count > 0 || selectedTags.includes(tag))
      .sort(([left], [right]) => left.localeCompare(right));
  }, [allTags, latestFacets.tags, tagParam]);

  const sortedProjects = useMemo(
    () => [...projects].sort((left, right) => left.title.localeCompare(right.title)),
    [projects],
  );
  const knownProjectSlugs = new Set(projects.map((project) => project.slug));
  const fallbackProjectSlugs = Object.keys(latestFacets.projects)
    .filter((slug) => !knownProjectSlugs.has(slug))
    .sort();
  if (projectSlug && !knownProjectSlugs.has(projectSlug) && !fallbackProjectSlugs.includes(projectSlug)) {
    fallbackProjectSlugs.push(projectSlug);
  }

  const projectValue = projectId
    ? `id:${projectId}`
    : projectSlug && resolvedProjectId
      ? `id:${resolvedProjectId}`
      : projectSlug ? `slug:${projectSlug}` : "";
  const hasFilters = Boolean(
    title || selectedTypes.length || selectedTags.length || projectId || projectSlug
      || paperSeriesId || paperPartId || audience,
  );
  const hasSearchCriteria = Boolean(q.trim() || hasFilters);
  const exact = exactLoaded?.key === exactRequest ? exactLoaded.data : null;
  const hybrid = hybridLoaded?.key === hybridRequest ? hybridLoaded.data : null;

  const askContextKey = JSON.stringify({
    type: selectedTypes,
    tags: selectedTags,
    project_id: resolvedProjectId,
    paper_series_id: paperSeriesId,
    paper_part_id: paperPartId,
    audience,
    unresolved_project: unresolvedProject ? projectSlug : null,
  });
  const askIsStale = Boolean(askResult && askResult.contextKey !== askContextKey);

  async function submitQuestion(event: FormEvent) {
    event.preventDefault();
    const question = askQuestion.trim();
    if (!question) {
      setAskError("Enter a question first.");
      return;
    }
    if (unresolvedProject || (metadataLoading && projectSlug && !resolvedProjectId)) {
      setAskError("Wait for the selected project filter to resolve before asking.");
      return;
    }

    askAbort.current?.abort();
    const controller = new AbortController();
    askAbort.current = controller;
    const sequence = ++askSequence.current;
    const contextKey = askContextKey;
    setAskLoading(true);
    setAskError("");

    try {
      const response = await api<AskResponse>("/library/ask", {
        method: "POST",
        signal: controller.signal,
        body: JSON.stringify({
          question,
          type: selectedTypes,
          tags: selectedTags,
          project_id: resolvedProjectId,
          paper_series_id: paperSeriesId,
          paper_part_id: paperPartId,
          audience: audience || null,
          limit: 8,
        }),
      });
      if (sequence !== askSequence.current) return;
      setAskResult({ ...response, question, contextKey });
      setAskError("");
    } catch (caught) {
      if (sequence === askSequence.current && !isAbortError(caught)) {
        setAskError(errorMessage(caught));
      }
    } finally {
      if (sequence === askSequence.current) {
        setAskLoading(false);
        if (askAbort.current === controller) askAbort.current = null;
      }
    }
  }

  function renderTags(tags: string[], interactive = true): ReactNode {
    return tags.map((tag) => interactive ? (
      <button
        type="button"
        key={tag}
        className={`tag ${selectedTags.includes(tag) ? "on" : ""}`}
        aria-pressed={selectedTags.includes(tag)}
        title={`Filter by ${tag}`}
        onClick={() => toggleCsvFilter("tag", selectedTags, tag)}
      >
        {tag}
      </button>
    ) : (
      <span className="tag" key={tag}>{tag}</span>
    ));
  }

  function renderExactResults() {
    if (!exact) return null;
    const start = exact.total ? exact.offset + 1 : 0;
    const end = Math.min(exact.offset + exact.items.length, exact.total);
    const page = Math.floor(exact.offset / exact.limit) + 1;
    const pageCount = Math.max(1, Math.ceil(exact.total / exact.limit));
    const pastLastPage = exact.total > 0 && exact.items.length === 0;

    return (
      <>
        <div className="result-summary">
          <span>
            {exact.total === 0
              ? "No artifacts"
              : `Showing ${start}-${end} of ${exact.total} artifacts`}
          </span>
          <div className="sort-controls">
            <label>
              Sort
              <select
                value={sort}
                onChange={(event) => {
                  const nextSort = event.target.value as SortKey;
                  updateUrl({
                    sort: nextSort,
                    order: nextSort === "title" || nextSort === "type" ? "asc" : "desc",
                  });
                }}
              >
                <option value="relevance" disabled={!q.trim()}>Relevance</option>
                <option value="updated">Updated</option>
                <option value="created">Created</option>
                <option value="title">Title</option>
                <option value="type">Type</option>
              </select>
            </label>
            <button
              type="button"
              onClick={() => updateUrl({ order: order === "asc" ? "desc" : "asc" })}
              aria-label={`Sort ${order === "asc" ? "descending" : "ascending"}`}
            >
              {order === "asc" ? "Ascending" : "Descending"}
            </button>
            <label>
              Per page
              <select
                value={limit}
                onChange={(event) => updateUrl({ limit: event.target.value })}
              >
                {PAGE_SIZES.map((size) => <option key={size} value={size}>{size}</option>)}
              </select>
            </label>
          </div>
        </div>

        {pastLastPage ? (
          <div className="search-state">
            <p>This page is past the end of the current result set.</p>
            <button type="button" onClick={() => updateUrl({ offset: null })}>
              Return to the first page
            </button>
          </div>
        ) : exact.items.length === 0 ? (
          <p className="empty search-state">
            {hasSearchCriteria
              ? "No artifacts match the current search and filters."
              : "Nothing here yet. Add a source under Projects to build your library."}
          </p>
        ) : (
          <div className="table-scroll" tabIndex={0} aria-label="Library artifacts; scroll horizontally if needed">
            <table className="list library-table">
              <caption className="sr-only">Exact library search results</caption>
              <thead>
                <tr>
                  <th scope="col">Title</th>
                  <th scope="col">Type</th>
                  <th scope="col">Project</th>
                  <th scope="col">Tags</th>
                  <th scope="col">Updated</th>
                </tr>
              </thead>
              <tbody>
                {exact.items.map((artifact) => (
                  <tr key={artifact.id}>
                    <td><Link to={`/artifacts/${artifact.id}`}>{artifact.title}</Link></td>
                    <td>{typeLabel(artifact.type)}</td>
                    <td>
                      {artifact.project_id ? (
                        <Link to={`/projects/${artifact.project_id}`}>
                          {artifact.project_slug ?? `Project ${artifact.project_id}`}
                        </Link>
                      ) : (
                        <span className="muted">Shared</span>
                      )}
                    </td>
                    <td className="result-tags">{renderTags(artifact.tags ?? [])}</td>
                    <td><time dateTime={artifact.updated}>{fmtDate(artifact.updated)}</time></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {exact.total > exact.limit && (
          <nav className="pagination" aria-label="Library result pages">
            <button
              type="button"
              disabled={exact.offset === 0}
              onClick={() => updateUrl(
                { offset: Math.max(0, exact.offset - exact.limit) || null },
                { resetPage: false, replace: false },
              )}
            >
              Previous
            </button>
            <span>Page {Math.min(page, pageCount)} of {pageCount}</span>
            <button
              type="button"
              disabled={exact.offset + exact.limit >= exact.total}
              onClick={() => updateUrl(
                { offset: exact.offset + exact.limit },
                { resetPage: false, replace: false },
              )}
            >
              Next
            </button>
          </nav>
        )}
      </>
    );
  }

  function renderHybridResults() {
    if (!hybrid) return null;
    return (
      <>
        {!hybrid.semantic_enabled && (
          <div className="banner semantic-note" role="status">
            <p>
              Semantic matching is not enabled, so Hybrid is currently using exact
              chunk matches. Enable semantic search and rebuild the index in{" "}
              <Link to="/settings">Settings</Link> to add meaning-based matches.
            </p>
          </div>
        )}
        {hybrid.semantic_enabled && hybrid.embedding_model && (
          <p className="meta">Semantic model: {hybrid.embedding_model}</p>
        )}
        <p className="result-count">
          {hybrid.results.length} relevant excerpt{hybrid.results.length === 1 ? "" : "s"}
        </p>
        {!q.trim() ? (
          <p className="empty search-state">Enter a search above to find related passages.</p>
        ) : hybrid.results.length === 0 ? (
          <p className="empty search-state">No relevant library passages were found.</p>
        ) : (
          <ol className="hybrid-results">
            {hybrid.results.map((result) => (
              <li key={result.chunk_id} className="hybrid-result">
                <div className="hybrid-result-head">
                  <strong>{typeLabel(result.artifact_type)}</strong>
                  <SourceMeta source={result} />
                </div>
                <blockquote>{result.excerpt}</blockquote>
                {result.tags.length > 0 && (
                  <div className="result-tags" aria-label="Tags">{renderTags(result.tags)}</div>
                )}
              </li>
            ))}
          </ol>
        )}
      </>
    );
  }

  return (
    <div className="library-page">
      <header className="library-header">
        <div>
          <h2>Library</h2>
          <p className="meta">Search every artifact, then ask questions grounded in the source material.</p>
        </div>
        <div className="library-search-row">
          <label className="sr-only" htmlFor="library-search">Search the library</label>
          <input
            id="library-search"
            className="search"
            type="search"
            placeholder={mode === "exact" ? "Search exact words and phrases..." : "Search by meaning or phrase..."}
            value={q}
            onChange={(event) => changeQuery(event.target.value)}
          />
          <div className="mode-switch" role="group" aria-label="Search mode">
            <button
              type="button"
              className={mode === "exact" ? "on" : ""}
              aria-pressed={mode === "exact"}
              onClick={() => changeMode("exact")}
            >
              Exact
            </button>
            <button
              type="button"
              className={mode === "hybrid" ? "on" : ""}
              aria-pressed={mode === "hybrid"}
              onClick={() => changeMode("hybrid")}
            >
              Hybrid
            </button>
          </div>
        </div>
      </header>

      <div className="library">
        <aside className="filters library-filters" aria-label="Library filters">
          <div className="filter-heading">
            <h3>Filters</h3>
            {hasFilters && <button type="button" className="linkish" onClick={clearFilters}>Clear</button>}
          </div>

          <div className="filter-section">
            <label htmlFor="title-filter">Title contains</label>
            <input
              id="title-filter"
              type="search"
              value={title}
              disabled={mode === "hybrid"}
              onChange={(event) => updateUrl({ title: event.target.value || null })}
            />
            {mode === "hybrid" && (
              <small>{title ? "Paused in Hybrid mode." : "Available in Exact mode."}</small>
            )}
          </div>

          {(askingPaper || audience || paperSeriesId || paperPartId) && (
            <div className="filter-section paper-library-filters">
              <label htmlFor="paper-audience-filter">Paper audience</label>
              <select id="paper-audience-filter" value={audience}
                onChange={(event) => updateUrl({ audience: event.target.value || null })}>
                <option value="">All audiences and shared analysis</option>
                <option value="generalist">Generalist</option>
                <option value="practitioner">Practitioner</option>
                <option value="expert">Expert</option>
              </select>
              <label htmlFor="paper-series-filter">Series ID <small>(optional)</small></label>
              <input id="paper-series-filter" type="number" min={1} value={paperSeriesId ?? ""}
                onChange={(event) => updateUrl({ paper_series_id: event.target.value || null })} />
              <label htmlFor="paper-part-filter">Part ID <small>(optional)</small></label>
              <input id="paper-part-filter" type="number" min={1} value={paperPartId ?? ""}
                onChange={(event) => updateUrl({ paper_part_id: event.target.value || null })} />
            </div>
          )}

          <div className="filter-section">
            <label htmlFor="project-filter">Project</label>
            <select
              id="project-filter"
              value={projectValue}
              onChange={(event) => selectProject(event.target.value)}
            >
              <option value="">All projects</option>
              {projectId && !projects.some((project) => project.id === projectId) && (
                <option value={`id:${projectId}`}>Project {projectId}</option>
              )}
              {sortedProjects.map((project) => (
                <option key={project.id} value={`id:${project.id}`}>
                  {project.title}
                  {latestFacets.projects[project.slug] !== undefined
                    ? ` (${latestFacets.projects[project.slug]})`
                    : ""}
                </option>
              ))}
              {fallbackProjectSlugs.map((slug) => (
                <option key={slug} value={`slug:${slug}`}>
                  {slug}{latestFacets.projects[slug] !== undefined ? ` (${latestFacets.projects[slug]})` : ""}
                </option>
              ))}
            </select>
          </div>

          <fieldset className="filter-section">
            <legend>Artifact types</legend>
            <div className="filter-options">
              {typeOptions.map((type) => (
                <FilterOption
                  key={type}
                  checked={selectedTypes.includes(type)}
                  label={typeLabel(type)}
                  count={latestFacets.types[type]}
                  onChange={() => toggleCsvFilter("type", selectedTypes, type)}
                />
              ))}
            </div>
          </fieldset>

          <fieldset className="filter-section">
            <legend>Tags <small>(match any)</small></legend>
            {metadataLoading && tagOptions.length === 0 ? (
              <p className="meta" role="status">Loading tags...</p>
            ) : tagOptions.length === 0 ? (
              <p className="empty">No tags yet.</p>
            ) : (
              <div className="tagcloud">
                {tagOptions.map(([tag, count]) => (
                  <button
                    type="button"
                    key={tag}
                    className={`tag ${selectedTags.includes(tag) ? "on" : ""}`}
                    aria-pressed={selectedTags.includes(tag)}
                    onClick={() => toggleCsvFilter("tag", selectedTags, tag)}
                  >
                    {tag} <small>{count}</small>
                  </button>
                ))}
              </div>
            )}
          </fieldset>

          {metadataError && <p className="error filter-error" role="alert">{metadataError}</p>}
        </aside>

        <section
          className="library-main results-region"
          aria-labelledby="library-results-title"
          aria-busy={searchLoading}
        >
          <div className="results-heading">
            <div>
              <h3 id="library-results-title">
                {mode === "exact" ? "Artifacts" : "Relevant passages"}
              </h3>
              <p className="hint">
                {mode === "exact"
                  ? "Exact uses full-text matching and server-side pagination."
                  : "Hybrid combines exact passages with semantic matches when enabled."}
              </p>
            </div>
            {searchLoading && (
              <span className="search-progress" role="status">
                {mode === "exact" && exact ? "Updating..." : mode === "hybrid" && hybrid ? "Updating..." : "Searching..."}
              </span>
            )}
          </div>

          {searchError && (
            <div className="search-error" role="alert">
              <p className="error">Could not load search results: {searchError}</p>
              <button type="button" onClick={() => setRefreshToken((value) => value + 1)}>Try again</button>
            </div>
          )}

          {!searchError && searchLoading && !(mode === "exact" ? exact : hybrid) && (
            <p className="search-state" role="status">Loading library results...</p>
          )}

          <div className={searchLoading ? "results-content updating" : "results-content"}>
            {mode === "exact" ? renderExactResults() : renderHybridResults()}
          </div>
        </section>
      </div>

      <section className="ask-panel" id="ask" aria-labelledby="ask-library-title">
        <div className="ask-intro">
          <p className="eyebrow">Grounded Q&amp;A</p>
          <h3 id="ask-library-title">
            {askingRepository ? "Ask this repository" : askingPaper ? "Ask this paper" : "Ask your library"}
          </h3>
          <p className="meta">
            {askingRepository
              ? `Answers are limited to ${selectedProject?.title} and cite its generated guides or immutable source files.`
              : askingPaper
                ? `Answers are limited to ${selectedProject?.title} and cite stable evidence IDs with clickable PDF pages${audience ? ` for the ${paperAudienceLabel(audience)} track` : ""}.`
              : "Answers use the selected project, artifact types, and tags. Every answer keeps its supporting excerpts visible below it."}
          </p>
          {title && <p className="hint">The title-only filter applies to Exact results, not Q&amp;A.</p>}
        </div>

        <form className="ask-form" onSubmit={(event) => void submitQuestion(event)}>
          <label htmlFor="ask-question">Question</label>
          <textarea
            id="ask-question"
            rows={3}
            value={askQuestion}
            placeholder={askingRepository
              ? "How does this repository work?"
              : askingPaper ? "What evidence supports the paper’s central claim?"
              : "What does my library say about...?"}
            onChange={(event) => setAskQuestion(event.target.value)}
          />
          <div className="ask-actions">
            <button
              type="submit"
              disabled={
                askLoading ||
                !askQuestion.trim() ||
                unresolvedProject ||
                Boolean(metadataLoading && projectSlug && !resolvedProjectId)
              }
            >
              {askLoading ? "Finding and answering..." : "Ask with citations"}
            </button>
            <span className="meta">Up to 8 supporting excerpts</span>
          </div>
        </form>

        {askError && <p className="error ask-error" role="alert">Could not answer: {askError}</p>}
        {askLoading && <p className="ask-status" role="status">Retrieving sources and drafting an answer...</p>}

        {askResult && (
          <div className="ask-result">
            <p className="sr-only" role="status">
              {`Answer ready with ${askResult.sources.length} supporting source${
                askResult.sources.length === 1 ? "" : "s"
              }.`}
            </p>
            <div className="ask-result-head">
              <div>
                <span className="grounded-badge">Grounded</span>
                <strong>{askResult.question}</strong>
              </div>
              <span className="meta">
                {askResult.sources.length} source{askResult.sources.length === 1 ? "" : "s"}
              </span>
            </div>
            {askIsStale && (
              <p className="banner stale-answer">
                Filters changed after this answer was generated. Ask again to use the current filters.
              </p>
            )}
            <GroundedAnswer answer={askResult.answer} sources={askResult.sources} />

            {askResult.sources.length > 0 ? (
              <ol className="ask-sources" aria-label="Answer sources">
                {askResult.sources.map((source) => (
                  <li key={`${source.marker}-${source.chunk_id}`} id={`ask-source-${source.marker}`}>
                    <div className="citation-head">
                      <span className="citation-marker">[{source.marker}]</span>
                      <SourceMeta source={source} />
                      <span className="kindbadge">{typeLabel(source.artifact_type)}</span>
                    </div>
                    <blockquote>{source.excerpt}</blockquote>
                    {source.tags.length > 0 && (
                      <div className="result-tags">{renderTags(source.tags, false)}</div>
                    )}
                  </li>
                ))}
              </ol>
            ) : (
              <p className="empty">No supporting excerpts were found for this answer.</p>
            )}
          </div>
        )}
      </section>
    </div>
  );
}
