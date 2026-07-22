import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import {
  api,
  Artifact,
  fmtDateTime,
  Job,
  PAPER_AUDIENCES,
  PaperAudience,
  PaperDetail,
  PaperPageIssue,
  PaperSeries,
  PaperSource,
  Project,
  typeLabel,
} from "../api";
import { useEventSource } from "../useEventSource";

interface PaperProjectPanelProps {
  project: Project;
  fallbackArtifacts?: Artifact[];
  onProjectReload: () => Promise<void> | void;
  onDelete: () => Promise<void> | void;
  deleting?: boolean;
  streamLabel?: string;
  streamClass?: string;
}

function errorMessage(value: unknown): string {
  return value instanceof Error ? value.message : "Unexpected error";
}

function sourceGrade(source: PaperSource, detail: PaperDetail): string {
  return detail.quality?.grade ?? source.quality_grade ?? source.quality ?? "PENDING";
}

function issueFrom(value: number | PaperPageIssue): PaperPageIssue {
  return typeof value === "number" ? { page: value } : value;
}

function pageIssues(detail: PaperDetail): PaperPageIssue[] {
  const reportPages = [detail.quality?.report?.pages, detail.source.quality_report?.pages]
    .flatMap((value) => Array.isArray(value) ? value : [])
    .filter((value): value is PaperPageIssue => Boolean(value && typeof value === "object" && "page" in value))
    .filter((value) => String(value.grade ?? "").toLocaleUpperCase() === "POOR" || value.visual_review_needed);
  const candidates = [
    ...reportPages,
    ...(detail.quality?.page_issues ?? []),
    ...(detail.source.page_issues ?? []),
    ...(detail.quality?.poor_pages ?? []).map(issueFrom),
    ...(detail.source.poor_pages ?? []).map(issueFrom),
  ];
  const acknowledged = new Map<number, PaperPageIssue>();
  for (const value of [
    ...(detail.quality?.acknowledged_pages ?? []),
    ...(detail.source.acknowledged_pages ?? []),
  ]) {
    const issue = issueFrom(value);
    acknowledged.set(issue.page, {
      ...issue,
      acknowledged: true,
      acknowledgement_reason: issue.acknowledgement_reason ?? issue.reason,
    });
  }
  const merged = new Map<number, PaperPageIssue>();
  for (const issue of candidates) {
    merged.set(issue.page, { ...merged.get(issue.page), ...issue });
  }
  for (const [page, issue] of acknowledged) {
    merged.set(page, { ...merged.get(page), ...issue, acknowledged: true });
  }
  return [...merged.values()].filter((issue) => issue.page > 0).sort((a, b) => a.page - b.page);
}

function percent(value: number | undefined, numerator: number, denominator: number): number {
  if (typeof value === "number") return Math.max(0, Math.min(100, Math.round(value)));
  return denominator > 0 ? Math.round((numerator / denominator) * 100) : 0;
}

function trackFromResponse(value: PaperSeries | { series: PaperSeries }): PaperSeries {
  return "series" in value ? value.series : value;
}

export default function PaperProjectPanel({
  project,
  fallbackArtifacts = [],
  onProjectReload,
  onDelete,
  deleting = false,
  streamLabel,
  streamClass,
}: PaperProjectPanelProps) {
  const navigate = useNavigate();
  const [detail, setDetail] = useState<PaperDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState("");
  const [actionError, setActionError] = useState("");
  const [notice, setNotice] = useState("");
  const [page, setPage] = useState(1);
  const [ackPage, setAckPage] = useState<number | null>(null);
  const [ackReason, setAckReason] = useState("");
  const [acknowledging, setAcknowledging] = useState(false);
  const [rerunning, setRerunning] = useState(false);
  const [creatingAudience, setCreatingAudience] = useState<PaperAudience | null>(null);
  const [deletingSeries, setDeletingSeries] = useState<number | null>(null);
  const [renaming, setRenaming] = useState(false);
  const [title, setTitle] = useState(project.title);
  const [savingTitle, setSavingTitle] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const value = await api<PaperDetail>(`/papers/${project.id}`);
      setDetail(value);
      setLoadError("");
    } catch (caught) {
      setLoadError(errorMessage(caught));
    } finally {
      setLoading(false);
    }
  }, [project.id]);

  useEffect(() => { void load(); }, [load]);
  useEffect(() => { setTitle(project.title); }, [project.title]);
  useEventSource(`/api/jobs/stream?project_id=${project.id}`, "jobs", () => void load());

  const source = detail?.source ?? project.paper ?? {
    project_id: project.id,
    filename: project.source.split(/[\\/]/).pop(),
  };
  const artifacts = detail?.artifacts ?? fallbackArtifacts;
  const tracks = detail?.series ?? detail?.tracks ?? [];
  const issues = detail ? pageIssues(detail) : [];
  const unacknowledged = issues.filter((issue) => !issue.acknowledged);
  const acknowledged = issues.filter((issue) => issue.acknowledged);
  const coverage = detail?.coverage;
  const evidenceTotal = coverage?.evidence_blocks ?? 0;
  const evidenceMapped = coverage?.mapped_blocks ?? 0;
  const coveragePercent = percent(coverage?.percent, evidenceMapped, evidenceTotal);
  const blocked = detail?.quality?.blocked ?? source.analysis_blocked
    ?? coverage?.analysis_blocked ?? unacknowledged.length > 0;
  const pdfUrl = source.pdf_url || `/api/papers/${project.id}/source`;
  const sharedArtifacts = (detail?.shared_artifacts ?? artifacts).filter((artifact) =>
    !artifact.paper_series_id && !artifact.paper_part_id
    && !["paper_source", "source_paper"].includes(artifact.type));
  const sourceArtifact = artifacts.find((artifact) => ["paper_source", "source_paper"].includes(artifact.type));
  const jobs = detail?.jobs ?? [];

  async function saveTitle() {
    const next = title.trim();
    if (!next) {
      setActionError("Project title cannot be empty.");
      return;
    }
    setSavingTitle(true);
    setActionError("");
    try {
      await api(`/projects/${project.id}`, { method: "PATCH", body: JSON.stringify({ title: next }) });
      setRenaming(false);
      await onProjectReload();
    } catch (caught) {
      setActionError(errorMessage(caught));
    } finally {
      setSavingTitle(false);
    }
  }

  async function acknowledgePage() {
    if (!ackPage || !ackReason.trim()) {
      setActionError("Choose a page and record why its extraction gap is acceptable.");
      return;
    }
    setAcknowledging(true);
    setActionError("");
    setNotice("");
    try {
      await api(`/papers/${project.id}/acknowledgements`, {
        method: "POST",
        body: JSON.stringify({ pages: [{ page: ackPage, reason: ackReason.trim() }] }),
      });
      setNotice(`Page ${ackPage} acknowledged. Its gap remains visible in coverage reports.`);
      setAckPage(null);
      setAckReason("");
      await load();
    } catch (caught) {
      setActionError(errorMessage(caught));
    } finally {
      setAcknowledging(false);
    }
  }

  async function rerunExtraction() {
    setRerunning(true);
    setActionError("");
    setNotice("");
    try {
      await api(`/papers/${project.id}/rerun-extraction`, { method: "POST" });
      setNotice("Extraction was queued. Existing evidence remains available until the replacement completes.");
      await load();
    } catch (caught) {
      setActionError(errorMessage(caught));
    } finally {
      setRerunning(false);
    }
  }

  async function createTrack(audience: PaperAudience) {
    setCreatingAudience(audience);
    setActionError("");
    setNotice("");
    try {
      const result = trackFromResponse(await api<PaperSeries | { series: PaperSeries }>(
        `/papers/${project.id}/series`,
        { method: "POST", body: JSON.stringify({ audience, target_minutes: 50 }) },
      ));
      await load();
      navigate(`/paper-series/${result.id}`);
    } catch (caught) {
      setActionError(errorMessage(caught));
    } finally {
      setCreatingAudience(null);
    }
  }

  async function deleteTrack(track: PaperSeries) {
    if (!confirm(
      `Delete the ${track.audience} audience track?\n\n` +
      "Its plans, memory, scripts, guides, and audio are removed. The source paper and other audience tracks remain.",
    )) return;
    setDeletingSeries(track.id);
    setActionError("");
    try {
      await api(`/paper-series/${track.id}`, { method: "DELETE" });
      setNotice("Audience track deleted; the shared paper analysis was retained.");
      await load();
    } catch (caught) {
      setActionError(errorMessage(caught));
    } finally {
      setDeletingSeries(null);
    }
  }

  const trackByAudience = useMemo(() => new Map(tracks.map((track) => [track.audience, track])), [tracks]);

  return (
    <div className="project-detail paper-project">
      <div className="project-head">
        {renaming ? (
          <span className="rename">
            <label className="sr-only" htmlFor="paper-project-title">Project title</label>
            <input id="paper-project-title" autoFocus value={title}
              onChange={(event) => setTitle(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter") void saveTitle();
                if (event.key === "Escape") setRenaming(false);
              }} disabled={savingTitle} />
            <button type="button" onClick={() => void saveTitle()} disabled={savingTitle}>
              {savingTitle ? "Saving…" : "Save"}
            </button>
            <button type="button" onClick={() => setRenaming(false)} disabled={savingTitle}>Cancel</button>
          </span>
        ) : (
          <>
            <h2>{project.title}</h2>
            <button type="button" className="linkish" onClick={() => setRenaming(true)}>Rename</button>
            <button type="button" className="linkish danger" onClick={() => void onDelete()} disabled={deleting}>
              {deleting ? "Deleting…" : "Delete"}
            </button>
          </>
        )}
        {streamLabel && <span className={`stream-status project-stream ${streamClass ?? ""}`}>{streamLabel}</span>}
      </div>

      <section className="paper-source-summary" aria-labelledby="paper-source-summary-title">
        <div className="repository-project-meta">
          <span className="source-badge paper">Research paper</span>
          <strong id="paper-source-summary-title">{source.original_filename ?? source.filename ?? project.source}</strong>
          {source.page_count !== undefined && <span>{source.page_count} pages</span>}
          <span className={`quality-grade grade-${sourceGrade(source, detail ?? { project, source }).toLocaleLowerCase()}`}>
            Extraction {sourceGrade(source, detail ?? { project, source })}
          </span>
          {source.local_only !== false && <span className="privacy-chip">Local-only</span>}
          {source.privacy_locked && <span className="privacy-chip locked">Policy locked</span>}
        </div>
        <div className="paper-source-actions">
          <button type="button" onClick={() => setPage(1)}>Open PDF</button>
          {sourceArtifact && <Link to={`/artifacts/${sourceArtifact.id}`}>Source artifact</Link>}
          <Link to={`/?project_id=${project.id}&source_type=paper&mode=hybrid#ask-library-title`}>
            Ask this paper with citations
          </Link>
          <button type="button" className="linkish" onClick={() => void load()} disabled={loading}>
            {loading ? "Refreshing…" : "Refresh"}
          </button>
        </div>
        <dl className="paper-source-facts">
          <div><dt>Source hash</dt><dd><code>{(source.source_hash ?? source.sha256 ?? "pending").slice(0, 16)}</code></dd></div>
          <div><dt>OCR</dt><dd>{source.ocr_languages?.join(", ") || "automatic"}</dd></div>
          <div><dt>Parser</dt><dd>{source.parser_version ?? source.extraction_method ?? "pending"}</dd></div>
          <div><dt>Extracted text</dt><dd>{(source.extracted_characters ?? source.character_count)?.toLocaleString() ?? "pending"} characters</dd></div>
        </dl>
      </section>

      {loadError && <p className="error" role="alert">Paper details could not be loaded: {loadError}</p>}
      {actionError && <p className="error" role="alert">{actionError}</p>}
      {notice && <p className="notice" role="status">{notice}</p>}

      <div className="paper-review-layout">
        <section className="card paper-pdf-card" aria-labelledby="paper-viewer-title">
          <div className="section-heading">
            <div>
              <p className="eyebrow">Immutable source</p>
              <h3 id="paper-viewer-title">PDF viewer</h3>
            </div>
            <label className="paper-page-jump">
              Page
              <input type="number" min={1} max={source.page_count || 500} value={page}
                onChange={(event) => setPage(Math.max(1, Number(event.target.value) || 1))} />
            </label>
          </div>
          <iframe key={page} className="paper-pdf-viewer" title={`${project.title}, page ${page}`}
            src={`${pdfUrl}#page=${page}&view=FitH`} />
          <p className="meta">
            <a href={`${pdfUrl}#page=${page}`} target="_blank" rel="noreferrer">Open page {page} in a new tab</a>
            {" · "}The source PDF is always excluded from cloud sync and remains in normal backups.
          </p>
        </section>

        <section className={`card paper-extraction-review ${blocked ? "blocked" : "ready"}`}
          aria-labelledby="paper-extraction-review-title">
          <div className="section-heading">
            <div>
              <p className="eyebrow">Quality gate</p>
              <h3 id="paper-extraction-review-title">Extraction review</h3>
            </div>
            <span className={`jobstatus ${blocked ? "error" : "done"}`}>{blocked ? "Review required" : "Admitted"}</span>
          </div>
          <p>
            {blocked
              ? "Analysis is blocked until each poor nontrivial page is replaced or explicitly acknowledged."
              : "Every page passed the quality gate or has a recorded acknowledgement."}
          </p>
          {detail?.quality?.warnings?.map((warning) => <p className="notice" key={warning}>{warning}</p>)}
          {issues.length > 0 ? (
            <ol className="paper-page-issues">
              {issues.map((issue) => (
                <li key={issue.page} className={issue.acknowledged ? "acknowledged" : "poor"}>
                  <button type="button" className="linkish" onClick={() => setPage(issue.page)}>Page {issue.page}</button>
                  <span>{issue.reason ?? (issue.visual_review_needed ? "Visual review needed" : "Poor extraction")}</span>
                  {issue.acknowledged ? (
                    <span className="jobstatus partial" title={issue.acknowledgement_reason ?? undefined}>Acknowledged gap</span>
                  ) : (
                    <button type="button" onClick={() => { setPage(issue.page); setAckPage(issue.page); }}>Review and acknowledge</button>
                  )}
                </li>
              ))}
            </ol>
          ) : (
            <p className="meta">No poor pages reported.</p>
          )}
          {ackPage !== null && (
            <div className="paper-ack-form">
              <h4>Acknowledge page {ackPage}</h4>
              <p className="meta">This gap remains visible and cannot be the sole support for a critical claim.</p>
              <label className="stacked">
                Reason
                <textarea value={ackReason} onChange={(event) => setAckReason(event.target.value)}
                  rows={3} placeholder="Why it is acceptable to continue despite this extraction gap" />
              </label>
              <div className="row">
                <button type="button" onClick={() => void acknowledgePage()} disabled={acknowledging || !ackReason.trim()}>
                  {acknowledging ? "Recording…" : "Record acknowledgement"}
                </button>
                <button type="button" className="linkish" onClick={() => { setAckPage(null); setAckReason(""); }}>Cancel</button>
              </div>
            </div>
          )}
          <button type="button" className="linkish" onClick={() => void rerunExtraction()} disabled={rerunning}>
            {rerunning ? "Queuing extraction…" : "Re-run extraction"}
          </button>
          {acknowledged.length > 0 && <p className="meta">{acknowledged.length} acknowledged gap{acknowledged.length === 1 ? "" : "s"} retained in coverage.</p>}
        </section>
      </div>

      <section className="paper-coverage" aria-labelledby="paper-coverage-title">
        <div className="section-heading">
          <div>
            <p className="eyebrow">No hidden truncation</p>
            <h3 id="paper-coverage-title">Evidence coverage</h3>
          </div>
          <strong>{coveragePercent}% mapped</strong>
        </div>
        <div className="coverage-meter" role="progressbar" aria-valuemin={0} aria-valuemax={100}
          aria-valuenow={coveragePercent} aria-label="Paper evidence mapping coverage">
          <i style={{ width: `${coveragePercent}%` }} />
        </div>
        <div className="paper-coverage-stats">
          <span><b>{evidenceMapped.toLocaleString()}</b> / {evidenceTotal.toLocaleString() || "—"} evidence blocks mapped</span>
          <span><b>{coverage?.pages_admitted ?? "—"}</b> / {coverage?.pages_total ?? source.page_count ?? "—"} pages admitted</span>
          <span><b>{coverage?.critical_assigned ?? 0}</b> / {coverage?.critical_total ?? 0} critical topics assigned</span>
          <span><b>{coverage?.critical_omitted ?? 0}</b> critical omissions</span>
        </div>
        {coverage?.warnings?.map((warning) => <p className="notice" key={warning}>{warning}</p>)}
      </section>

      <section className="paper-shared-artifacts" aria-labelledby="paper-shared-title">
        <div className="section-heading">
          <div>
            <p className="eyebrow">Shared across audiences</p>
            <h3 id="paper-shared-title">Whole-paper analysis</h3>
          </div>
          <Link to={`/?project_id=${project.id}&source_type=paper&mode=hybrid`}>Search this paper</Link>
        </div>
        {sharedArtifacts.length > 0 ? (
          <div className="repository-guide-grid">
            {sharedArtifacts.map((artifact) => (
              <article className="card repository-guide-card complete" key={artifact.id}>
                <div className="repository-guide-title">
                  <h4>{artifact.title}</h4>
                  <span className="jobstatus done">Ready</span>
                </div>
                <p>{typeLabel(artifact.type)}</p>
                <Link to={`/artifacts/${artifact.id}`}>Open artifact</Link>
              </article>
            ))}
          </div>
        ) : (
          <p className="meta">Shared artifacts appear after extraction and evidence mapping complete.</p>
        )}
      </section>

      <section className="paper-audiences" aria-labelledby="paper-audiences-title">
        <div className="section-heading">
          <div>
            <p className="eyebrow">Independent teaching tracks</p>
            <h3 id="paper-audiences-title">Audience series</h3>
          </div>
          <span className="meta">1–5 sequential parts · 40–60 minutes each</span>
        </div>
        <div className="paper-audience-grid">
          {PAPER_AUDIENCES.map((audience) => {
            const track = trackByAudience.get(audience.key);
            const parts = track?.parts ?? track?.plan?.parts ?? [];
            return (
              <article className={`card paper-audience-card ${track ? "exists" : ""}`} key={audience.key}>
                <span className={`source-badge audience ${audience.key}`}>{audience.label}</span>
                <h4>{track?.title || `${audience.label} series`}</h4>
                <p>{audience.description}</p>
                {track ? (
                  <>
                    <div className="paper-track-meta">
                      <span className={`jobstatus ${track.status === "complete" ? "done" : track.status === "draft" ? "new" : "running"}`}>
                        {track.status}
                      </span>
                      <span>{parts.length || "Drafting"} part{parts.length === 1 ? "" : "s"}</span>
                      <span>{track.target_minutes ?? 50} min target</span>
                    </div>
                    <div className="paper-track-actions">
                      <Link className="button-link" to={`/paper-series/${track.id}`}>
                        {track.status === "draft" ? "Review plan" : "Open series"}
                      </Link>
                      <button type="button" className="linkish danger" onClick={() => void deleteTrack(track)}
                        disabled={deletingSeries === track.id}>
                        {deletingSeries === track.id ? "Deleting…" : "Delete track"}
                      </button>
                    </div>
                  </>
                ) : (
                  <button type="button" onClick={() => void createTrack(audience.key)}
                    disabled={blocked || creatingAudience !== null}>
                    {creatingAudience === audience.key ? "Drafting plan…" : `Draft ${audience.label} plan`}
                  </button>
                )}
              </article>
            );
          })}
        </div>
        {blocked && <p className="notice">Audience planning is unavailable until extraction review is complete.</p>}
      </section>

      {jobs.length > 0 && (
        <details className="advanced paper-jobs">
          <summary>Paper jobs <small>({jobs.length})</small></summary>
          <ul className="paper-job-list">
            {jobs.map((job: Job) => (
              <li key={job.id}>
                <span>{job.task_label ?? job.task}</span>
                <span className={`jobstatus ${job.status}`}>{job.status}</span>
                <span className="meta">{job.progress}</span>
                {job.updated && <time>{fmtDateTime(job.updated)}</time>}
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}
