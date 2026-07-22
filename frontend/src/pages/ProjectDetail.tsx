import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import {
  api,
  Artifact,
  fmtDateTime,
  fmtTime,
  isPaperProject,
  isRepositoryProject,
  Project,
  RepositoryDetail,
  RepositoryUpdateStatus,
  shortSha,
  Step,
  typeLabel,
} from "../api";
import { useEventSource } from "../useEventSource";
import { StreamStatus, useStreamStatus } from "../useStreamStatus";
import PaperProjectPanel from "../components/PaperProjectPanel";

interface DetailStep extends Step {
  missing: string[];
  blocked: boolean;
  done: boolean;
  stale: boolean;
  not_applicable: boolean;
}

interface PipelineProfile {
  label: string;
  description: string;
  steps: string[];
  custom?: boolean;
}

interface Detail {
  project: Project;
  artifacts: Artifact[];
  steps: DetailStep[];
  remaining: number;
  run_all_active: boolean;
  run_all_state: "queued" | "running" | null;
  any_active: boolean;
  profiles: Record<string, PipelineProfile>;
  repository?: RepositoryDetail | null;
}

const REPOSITORY_GUIDES = [
  { step: "summary", type: "summary", label: "Repository overview", description: "Purpose, audience, capabilities, and major parts in clear language." },
  { step: "repo_usage", type: "repo_usage", label: "Setup and usage guide", description: "How to install, configure, run, build, deploy, and troubleshoot the code." },
  { step: "repo_architecture", type: "repo_architecture", label: "Architecture and code map", description: "Entrypoints, components, data flow, storage, and important files." },
  { step: "repo_expertise", type: "repo_expertise", label: "Required knowledge", description: "Languages, frameworks, concepts, tools, and a suggested learning order." },
  { step: "repo_environment", type: "repo_environment", label: "Dependencies and environment", description: "Runtimes, packages, services, variables, ports, OS, and hardware needs." },
] as const;

function repositoryCoveragePercent(repository: RepositoryDetail): number {
  const coverage = repository.coverage;
  const total = coverage.total_files ?? coverage.file_count ?? coverage.preview?.total_files ?? 0;
  const included = coverage.included_files ?? coverage.indexed_file_count
    ?? coverage.preview?.eligible_files ?? 0;
  if (coverage.percent !== undefined) return Math.max(0, Math.min(100, coverage.percent));
  return total
    ? Math.round((included / total) * 100)
    : 0;
}

function repositoryCoverageCounts(repository: RepositoryDetail): { included: number | null; total: number | null } {
  const coverage = repository.coverage;
  return {
    included: coverage.included_files ?? coverage.indexed_file_count
      ?? coverage.preview?.eligible_files ?? null,
    total: coverage.total_files ?? coverage.file_count ?? coverage.preview?.total_files ?? null,
  };
}

function hasRepositoryUpdate(update?: RepositoryUpdateStatus | null): boolean {
  return update?.update_available ?? update?.changed ?? false;
}

function repositoryUpdateTarget(update?: RepositoryUpdateStatus | null): string {
  return update?.latest_sha ?? update?.target_sha ?? "";
}

const STREAM_LABEL: Record<StreamStatus, string> = {
  connecting: "Updates connecting",
  live: "Updates live",
  offline: "Updates offline",
  stale: "Updates stale",
};

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "Unexpected error";
}

export default function ProjectDetail() {
  const { id } = useParams();
  const navigate = useNavigate();
  const routeState = useRef({ id, version: 0 });
  if (routeState.current.id !== id) {
    routeState.current = { id, version: routeState.current.version + 1 };
  }
  const routeVersion = routeState.current.version;
  const [detail, setDetail] = useState<Detail | null>(null);
  const [repository, setRepository] = useState<RepositoryDetail | null>(null);
  const [repositoryError, setRepositoryError] = useState("");
  const [checkingUpdate, setCheckingUpdate] = useState(false);
  const [updatingRepository, setUpdatingRepository] = useState(false);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState("");
  const [actionError, setActionError] = useState("");
  const [notice, setNotice] = useState("");
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [renaming, setRenaming] = useState(false);
  const [renamePending, setRenamePending] = useState(false);
  const [titleDraft, setTitleDraft] = useState("");
  const [cookieFile, setCookieFile] = useState<File | null>(null);
  const [uploadingCookies, setUploadingCookies] = useState(false);
  const [resetting, setResetting] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [selectedProfile, setSelectedProfile] = useState("full");
  const [runAllPending, setRunAllPending] = useState(false);
  const [rerunningStep, setRerunningStep] = useState("");
  const [streamConnected, setStreamConnected] = useState(false);
  const [hasStreamSnapshot, setHasStreamSnapshot] = useState(false);
  const loadAbort = useRef<AbortController | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const isCurrentRoute = useCallback(
    () => routeState.current.version === routeVersion,
    [routeVersion],
  );

  const load = useCallback(async () => {
    if (!isCurrentRoute()) return;
    if (!id) {
      setLoading(false);
      setLoadError("This project address is missing an ID.");
      return;
    }

    loadAbort.current?.abort();
    const controller = new AbortController();
    loadAbort.current = controller;
    setLoading(true);

    try {
      const nextDetail = await api<Detail>(`/projects/${id}`, { signal: controller.signal });
      if (!isCurrentRoute() || loadAbort.current !== controller) return;
      setDetail(nextDetail);
      if (isRepositoryProject(nextDetail.project)) {
        try {
          const nextRepository = nextDetail.repository ?? await api<RepositoryDetail>(
            `/repositories/${id}`,
            { signal: controller.signal },
          );
          if (!isCurrentRoute() || loadAbort.current !== controller) return;
          setRepository(nextRepository);
          setRepositoryError("");
        } catch (caught) {
          if (!controller.signal.aborted) {
            setRepository(nextDetail.repository ?? null);
            setRepositoryError(errorMessage(caught));
          }
        }
      } else {
        setRepository(null);
        setRepositoryError("");
      }
      setSelectedProfile((current) => {
        if (isRepositoryProject(nextDetail.project) && nextDetail.profiles?.repository) {
          return current !== "full" && nextDetail.profiles[current] ? current : "repository";
        }
        return nextDetail.profiles?.[current] ? current : "full";
      });
      setLoadError("");
    } catch (caught) {
      if (
        !isCurrentRoute() ||
        controller.signal.aborted ||
        loadAbort.current !== controller
      ) return;
      setLoadError(errorMessage(caught));
    } finally {
      if (isCurrentRoute() && loadAbort.current === controller) {
        loadAbort.current = null;
        setLoading(false);
      }
    }
  }, [id, isCurrentRoute]);

  useEffect(() => {
    setDetail(null);
    setRepository(null);
    setRepositoryError("");
    setCheckingUpdate(false);
    setUpdatingRepository(false);
    setLoadError("");
    setActionError("");
    setNotice("");
    setExpanded(new Set());
    setRenaming(false);
    setRenamePending(false);
    setTitleDraft("");
    setCookieFile(null);
    setUploadingCookies(false);
    setResetting(false);
    setDeleting(false);
    setSelectedProfile("full");
    setRunAllPending(false);
    setRerunningStep("");
    setStreamConnected(false);
    setHasStreamSnapshot(false);
    void load();

    return () => {
      loadAbort.current?.abort();
      loadAbort.current = null;
    };
  }, [id, load]);

  useEventSource<unknown>(
    `/api/jobs/stream?project_id=${id ?? ""}`,
    "jobs",
    () => {
      setHasStreamSnapshot(true);
      void load();
    },
    setStreamConnected,
  );

  const streamStatus = useStreamStatus(
    streamConnected && hasStreamSnapshot,
    hasStreamSnapshot,
  );

  useEffect(() => {
    if (!detail || detail.profiles?.[selectedProfile]) return;
    const fallback = detail.profiles?.full ? "full" : Object.keys(detail.profiles ?? {})[0];
    if (fallback) setSelectedProfile(fallback);
  }, [detail, selectedProfile]);

  function beginAction() {
    setActionError("");
    setNotice("");
  }

  async function run(step: string) {
    beginAction();
    try {
      await api(`/projects/${id}/run/${step}`, { method: "POST" });
      if (!isCurrentRoute()) return;
      await load();
    } catch (caught) {
      if (isCurrentRoute()) setActionError(errorMessage(caught));
    }
  }

  async function runAll() {
    beginAction();
    setRunAllPending(true);
    const profile = detail?.profiles?.[selectedProfile]
      ? selectedProfile
      : detail?.profiles?.full ? "full" : Object.keys(detail?.profiles ?? {})[0];
    try {
      await api(`/projects/${id}/run_all`, {
        method: "POST",
        body: JSON.stringify({ profile }),
      });
      if (!isCurrentRoute()) return;
      await load();
    } catch (caught) {
      if (isCurrentRoute()) setActionError(errorMessage(caught));
    } finally {
      if (isCurrentRoute()) setRunAllPending(false);
    }
  }

  async function rerunAffected(step: string) {
    beginAction();
    setRerunningStep(step);
    try {
      await api(`/projects/${id}/rerun/${step}`, { method: "POST" });
      if (!isCurrentRoute()) return;
      setNotice("The changed step and everything that depends on it have been queued.");
      await load();
    } catch (caught) {
      if (isCurrentRoute()) setActionError(errorMessage(caught));
    } finally {
      if (isCurrentRoute()) setRerunningStep("");
    }
  }

  function toggle(step: string) {
    setExpanded((current) => {
      const next = new Set(current);
      if (next.has(step)) next.delete(step);
      else next.add(step);
      return next;
    });
  }

  async function uploadCookies() {
    if (!cookieFile) {
      setActionError("Choose a cookies.txt file before uploading.");
      return;
    }

    beginAction();
    setUploadingCookies(true);
    const form = new FormData();
    form.append("file", cookieFile);

    try {
      const response = await fetch(`/api/projects/${id}/cookies`, {
        method: "POST",
        body: form,
      });
      if (!response.ok) {
        let message = response.statusText;
        try {
          message = (await response.json()).detail ?? message;
        } catch {
          // The status text is still useful when an upstream proxy returns HTML.
        }
        throw new Error(message);
      }
      if (!isCurrentRoute()) return;
      setCookieFile(null);
      if (fileRef.current) fileRef.current.value = "";
      setNotice("cookies.txt uploaded. It will be used for authenticated media sources.");
    } catch (caught) {
      if (isCurrentRoute()) {
        setActionError(`Cookies upload failed: ${errorMessage(caught)}`);
      }
    } finally {
      if (isCurrentRoute()) setUploadingCookies(false);
    }
  }

  async function saveRename() {
    const title = titleDraft.trim();
    if (!title) {
      setActionError("Project title cannot be empty.");
      return;
    }

    beginAction();
    setRenamePending(true);
    try {
      await api(`/projects/${id}`, {
        method: "PATCH",
        body: JSON.stringify({ title }),
      });
      if (!isCurrentRoute()) return;
      setRenaming(false);
      await load();
    } catch (caught) {
      if (isCurrentRoute()) setActionError(errorMessage(caught));
    } finally {
      if (isCurrentRoute()) setRenamePending(false);
    }
  }

  async function deleteProject() {
    if (!detail) return;
    const confirmed = confirm(
      `Delete "${detail.project.title}"?\n\n` +
      (isRepositoryProject(detail.project)
        ? "This permanently deletes all project artifacts and retained repository snapshots. "
        : isPaperProject(detail.project)
          ? "This permanently deletes the source PDF, extracted evidence, audience tracks, and all generated artifacts. "
        : "This permanently deletes all project artifacts and downloaded source media. ") +
      (isPaperProject(detail.project) ? "" : "Quick-reference documents it contributed to will remain. ") +
      "\n\nThis cannot be undone.",
    );
    if (!confirmed) return;

    beginAction();
    setDeleting(true);
    try {
      await api(`/projects/${id}`, { method: "DELETE" });
      if (isCurrentRoute()) navigate("/projects");
    } catch (caught) {
      if (isCurrentRoute()) {
        setActionError(errorMessage(caught));
        setDeleting(false);
      }
    }
  }

  async function resetJobs() {
    const confirmed = confirm(
      "Cancel all queued and running jobs for this project?",
    );
    if (!confirmed) return;

    beginAction();
    setResetting(true);
    try {
      const result = await api<{ reset: number }>(`/projects/${id}/reset_jobs`, {
        method: "POST",
      });
      if (!isCurrentRoute()) return;
      await load();
      if (!isCurrentRoute()) return;
      setNotice(
        result.reset === 1
          ? "Canceled 1 active job."
          : `Canceled ${result.reset} active jobs.`,
      );
    } catch (caught) {
      if (isCurrentRoute()) {
        setActionError(`Could not reset stuck jobs: ${errorMessage(caught)}`);
      }
    } finally {
      if (isCurrentRoute()) setResetting(false);
    }
  }

  async function checkRepositoryUpdate() {
    beginAction();
    setCheckingUpdate(true);
    try {
      const update = await api<RepositoryUpdateStatus>(`/repositories/${id}/check-update`, {
        method: "POST",
      });
      if (!isCurrentRoute()) return;
      setRepository((current) => current ? { ...current, update } : current);
      setNotice(hasRepositoryUpdate(update)
        ? `A newer commit is available (${shortSha(repositoryUpdateTarget(update))}). Nothing changes until you choose Update snapshot.`
        : "This repository snapshot is up to date.");
    } catch (caught) {
      if (isCurrentRoute()) setActionError(`Update check failed: ${errorMessage(caught)}`);
    } finally {
      if (isCurrentRoute()) setCheckingUpdate(false);
    }
  }

  async function updateRepositorySnapshot() {
    if (!repository || !hasRepositoryUpdate(repository.update)) return;
    const targetSha = repositoryUpdateTarget(repository.update);
    const confirmed = confirm(
      `Update this project from ${shortSha(repository.snapshot.commit_sha)} to ` +
      `${shortSha(targetSha)}?\n\n` +
      "A new immutable snapshot will be created. Existing artifacts remain available but affected outputs will be marked for refresh.",
    );
    if (!confirmed) return;

    beginAction();
    setUpdatingRepository(true);
    try {
      await api(`/repositories/${id}/update`, {
        method: "POST",
        body: JSON.stringify({ target_sha: targetSha }),
      });
      await api(`/projects/${id}/rerun/repo_snapshot`, { method: "POST" });
      if (!isCurrentRoute()) return;
      setNotice("The new commit was selected. Snapshotting and all affected repository guides are now queued.");
      await load();
    } catch (caught) {
      if (isCurrentRoute()) setActionError(`Repository update failed: ${errorMessage(caught)}`);
    } finally {
      if (isCurrentRoute()) setUpdatingRepository(false);
    }
  }

  if (!detail || detail.project.id !== Number(id)) {
    return (
      <section className="loading-state" aria-live="polite">
        {(loading || !loadError) && <p role="status">Loading project...</p>}
        {loadError && (
          <>
            <p className="error" role="alert">Could not load the project: {loadError}</p>
            <div className="row">
              <button type="button" onClick={() => void load()} disabled={loading}>Try again</button>
              <Link to="/projects">Back to projects</Link>
            </div>
          </>
        )}
      </section>
    );
  }

  const { project, steps } = detail;
  if (isPaperProject(project)) {
    return (
      <PaperProjectPanel
        project={project}
        fallbackArtifacts={detail.artifacts}
        onProjectReload={load}
        onDelete={deleteProject}
        deleting={deleting}
        streamLabel={STREAM_LABEL[streamStatus]}
        streamClass={streamStatus}
      />
    );
  }
  const repositoryProject = isRepositoryProject(project);
  const repositoryGuides = repositoryProject ? REPOSITORY_GUIDES.map((guide) => ({
    ...guide,
    pipelineStep: steps.find((step) =>
      step.name === guide.step || step.artifact?.type === guide.type,
    ),
  })) : [];
  const displayedSteps = repositoryProject
    ? steps.filter((step) => !step.not_applicable)
    : steps;
  const repositoryCounts = repository ? repositoryCoverageCounts(repository) : null;
  const repositoryQuickrefs = repositoryProject
    ? (detail.artifacts ?? []).filter((artifact) => artifact.type.startsWith("quickref_"))
    : [];
  const repositoryQuickrefStep = steps.find((step) => step.name === "quickref");
  const profileEntries = Object.entries(detail.profiles ?? {});
  const activeProfile = detail.profiles?.[selectedProfile] ?? detail.profiles?.full;
  const profileSteps = new Set(activeProfile?.steps ?? steps.map((step) => step.name));
  const profileWork = displayedSteps.filter(
    (step) => profileSteps.has(step.name) && !step.not_applicable && (!step.done || step.stale),
  ).length;
  return (
    <div className="project-detail">
      <div className="project-head">
        {renaming ? (
          <span className="rename">
            <label className="sr-only" htmlFor="project-title">Project title</label>
            <input
              id="project-title"
              autoFocus
              value={titleDraft}
              onChange={(event) => setTitleDraft(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter") void saveRename();
                if (event.key === "Escape") setRenaming(false);
              }}
              disabled={renamePending}
            />
            <button type="button" onClick={() => void saveRename()} disabled={renamePending}>
              {renamePending ? "Saving..." : "Save"}
            </button>
            <button type="button" onClick={() => setRenaming(false)} disabled={renamePending}>
              Cancel
            </button>
          </span>
        ) : (
          <>
            <h2>{project.title}</h2>
            <button
              type="button"
              className="linkish"
              title="Rename project"
              onClick={() => {
                setTitleDraft(project.title);
                setRenaming(true);
              }}
            >
              Rename
            </button>
            <button
              type="button"
              className="linkish danger"
              title="Delete project"
              onClick={() => void deleteProject()}
              disabled={deleting}
            >
              {deleting ? "Deleting..." : "Delete"}
            </button>
          </>
        )}
        <span
          className={`stream-status project-stream ${streamStatus}`}
          role="status"
          title={
            streamStatus === "stale"
              ? "The board is showing its last update while the live stream reconnects"
              : undefined
          }
        >
          {STREAM_LABEL[streamStatus]}
        </span>
      </div>
      {repositoryProject ? (
        <section className="repository-project-head" aria-label="Repository snapshot">
          <div className="repository-project-meta">
            <span className="source-badge repository">GitHub repository</span>
            {repository ? (
              <>
                <a href={repository.source.canonical_url ?? repository.source.url}
                  target="_blank" rel="noreferrer">{repository.source.full_name}</a>
                <span>{repository.source.private || repository.source.privacy === "private" ? "Private" : "Public"}</span>
                <span>Ref <b>{repository.source.requested_ref || repository.source.resolved_ref || repository.source.default_branch}</b></span>
                <span>Commit <code>{shortSha(repository.snapshot.commit_sha || repository.source.commit_sha)}</code></span>
              </>
            ) : (
              <span className="mono project-source">{project.source}</span>
            )}
          </div>
          {repository && (
            <div className="repository-project-coverage">
              <div>
                <b>{repositoryCounts?.included ?? "—"}</b> of {repositoryCounts?.total ?? "unknown"} files analyzed
                <span className="meta"> ({repositoryCoveragePercent(repository)}% coverage)</span>
              </div>
              <div className="coverage-meter compact" role="progressbar"
                aria-label="Repository file coverage" aria-valuemin={0} aria-valuemax={100}
                aria-valuenow={repositoryCoveragePercent(repository)}>
                <i style={{ width: `${repositoryCoveragePercent(repository)}%` }} />
              </div>
            </div>
          )}
          <div className="repository-project-actions">
            <button type="button" onClick={() => void checkRepositoryUpdate()}
              disabled={checkingUpdate || updatingRepository}>
              {checkingUpdate ? "Checking..." : "Check for updates"}
            </button>
            {hasRepositoryUpdate(repository?.update) && (
              <button type="button" className="primary" onClick={() => void updateRepositorySnapshot()}
                disabled={updatingRepository}>
                {updatingRepository
                  ? "Updating snapshot..."
                  : `Update to ${shortSha(repositoryUpdateTarget(repository?.update))}`}
              </button>
            )}
            {repository?.update && !repository.update.pending && !hasRepositoryUpdate(repository.update) && (
              <span className="jobstatus done">Up to date</span>
            )}
            <Link to={`/?project_id=${project.id}&mode=hybrid#ask-library-title`}>
              Ask this repository
            </Link>
          </div>
          {hasRepositoryUpdate(repository?.update) && repository?.update && (
            <p className="notice">
              {repository.update.ahead_by
                ? `${repository.update.ahead_by} newer commit${repository.update.ahead_by === 1 ? " is" : "s are"} available`
                : "A newer commit is available"}
              {repository.update.changed_files !== undefined
                ? ` across ${repository.update.changed_files} changed file${repository.update.changed_files === 1 ? "" : "s"}.`
                : "."}
              {" "}Your current guides remain pinned to {shortSha(repository.update.current_sha)} until you update.
            </p>
          )}
          {repository?.source.cloud_purge_pending && (
            <p className="error" role="alert">
              This repository became private after cloud copies may have been created.
              Synapse is removing those remote copies now; cloud sync and project deletion
              remain guarded until the purge succeeds.
            </p>
          )}
          {repositoryError && (
            <p className="error" role="alert">Repository details could not be refreshed: {repositoryError}</p>
          )}
        </section>
      ) : (
        <p className="mono project-source">{project.source}</p>
      )}

      {repositoryProject && (
        <section className="repository-guides" aria-labelledby="repository-guides-title">
          <div className="section-heading">
            <div>
              <p className="eyebrow">Start here</p>
              <h3 id="repository-guides-title">Repository learning guides</h3>
            </div>
            <Link to={`/?project_id=${project.id}&mode=hybrid#ask-library-title`}>
              Ask this repository with citations
            </Link>
          </div>
          <div className="repository-guide-grid">
            {repositoryGuides.map((guide) => {
              const step = guide.pipelineStep;
              const active = step?.job?.status === "running" || step?.job?.status === "queued";
              return (
                <article className={`card repository-guide-card ${step?.done ? "complete" : ""}`}
                  key={guide.step}>
                  <div className="repository-guide-title">
                    <h4>{guide.label}</h4>
                    <span className={`jobstatus ${step?.done ? "done" : active ? "running" : "new"}`}>
                      {step?.stale ? "Update available" : step?.done ? "Ready" : active ? step.job?.status : "Not run"}
                    </span>
                  </div>
                  <p>{guide.description}</p>
                  {step?.job?.progress && active && <p className="meta" role="status">{step.job.progress}</p>}
                  <div className="repository-guide-actions">
                    {step?.artifact ? (
                      <Link to={`/artifacts/${step.artifact.id}`}>Open guide</Link>
                    ) : (
                      <button type="button" onClick={() => void run(guide.step)}
                        disabled={!step || active || step.blocked}>
                        {active ? "Working..." : "Generate guide"}
                      </button>
                    )}
                  </div>
                </article>
              );
            })}
          </div>
        </section>
      )}

      {repositoryProject && (repositoryQuickrefStep?.done || repositoryQuickrefs.length > 0) && (
        <section className="repository-guides" aria-labelledby="repository-quickrefs-title">
          <div className="section-heading">
            <div>
              <p className="eyebrow">Focused references</p>
              <h3 id="repository-quickrefs-title">Repository quick references</h3>
            </div>
            <Link to={`/?project_id=${project.id}&mode=hybrid`}>Search this project</Link>
          </div>
          {repositoryQuickrefs.length ? (
            <div className="repository-guide-grid">
              {repositoryQuickrefs.map((artifact) => (
                <article className="card repository-guide-card complete" key={artifact.id}>
                  <div className="repository-guide-title">
                    <h4>{artifact.title}</h4>
                    <span className="jobstatus done">Ready</span>
                  </div>
                  <p>{typeLabel(artifact.type)}</p>
                  <div className="repository-guide-actions">
                    <Link to={`/artifacts/${artifact.id}`}>Open quick reference</Link>
                  </div>
                </article>
              ))}
            </div>
          ) : (
            <p className="meta">No focused quick-reference topics were identified for this commit.</p>
          )}
        </section>
      )}

      {repositoryProject && (
        <div className="section-heading pipeline-heading">
          <div>
            <p className="eyebrow">Build and refresh</p>
            <h3>Applicable repository pipeline</h3>
          </div>
        </div>
      )}
      <div className="board-toolbar">
        <label className="profile-picker" htmlFor="pipeline-profile">
          Pipeline
          <select
            id="pipeline-profile"
            value={selectedProfile}
            onChange={(event) => setSelectedProfile(event.target.value)}
            disabled={detail.run_all_active || runAllPending}
          >
            {profileEntries.map(([key, profile]) => (
              <option key={key} value={key}>{profile.label}</option>
            ))}
          </select>
        </label>
        <button
          type="button"
          className="runall"
          onClick={() => void runAll()}
          disabled={detail.run_all_active || runAllPending || profileWork === 0}
          title={activeProfile?.description ?? "Queues missing or stale steps in this profile."}
        >
          {detail.run_all_state === "running"
            ? "Running all..."
            : detail.run_all_state === "queued"
              ? "Queued - waiting for another run"
              : runAllPending
                ? "Queuing profile..."
              : profileWork === 0
                ? "Profile is up to date"
                : `Run ${activeProfile?.label ?? "profile"} (${profileWork})`}
        </button>

        {activeProfile?.description && (
          <span className="meta profile-description">{activeProfile.description}</span>
        )}

        {!repositoryProject && <div className="cookies">
          <label htmlFor="cookies-file">cookies.txt:</label>
          <input
            id="cookies-file"
            type="file"
            accept=".txt,text/plain"
            ref={fileRef}
            onChange={(event) => setCookieFile(event.target.files?.[0] ?? null)}
            aria-describedby="cookies-help"
          />
          <button
            type="button"
            onClick={() => void uploadCookies()}
            disabled={!cookieFile || uploadingCookies}
          >
            {uploadingCookies ? "Uploading..." : "Upload"}
          </button>
          <span id="cookies-help" className="sr-only">
            Used for authenticated media sources such as Udemy.
          </span>
        </div>}

        {detail.any_active && (
          <button
            type="button"
            className="reset-jobs"
            title="Cancel every queued or running job for this project"
            onClick={() => void resetJobs()}
            disabled={resetting}
          >
            {resetting ? "Canceling jobs..." : "Cancel active jobs"}
          </button>
        )}

        <button type="button" className="linkish" onClick={() => void load()} disabled={loading}>
          {loading ? "Refreshing..." : "Refresh"}
        </button>
      </div>

      {loadError && (
        <p className="error" role="alert">
          Refresh failed; the board below is the last loaded version: {loadError}
        </p>
      )}
      {actionError && <p className="error" role="alert">{actionError}</p>}
      {notice && <p className="notice" role="status">{notice}</p>}

      <div className="steplist">
        {displayedSteps.map((step) => {
          const status = step.job?.status ?? (step.done ? "done" : "not-run");
          const statusLabel = step.not_applicable
            ? "n/a"
            : status === "not-run" ? "not run" : status;
          const isOpen = expanded.has(step.name);
          const dimmed = (step.blocked && status === "not-run") || step.not_applicable;
          const detailId = `step-${step.name.replace(/[^a-zA-Z0-9_-]/g, "-")}-detail`;

          return (
            <div
              key={step.name}
              className={`step-row ${status} ${dimmed ? "dim" : ""}`}
            >
              <div className="step-main">
                <button
                  type="button"
                  className="step-toggle"
                  onClick={() => toggle(step.name)}
                  aria-expanded={isOpen}
                  aria-controls={detailId}
                >
                  <span className={`chev ${isOpen ? "open" : ""}`} aria-hidden="true">&#9654;</span>
                  <strong>{step.label}</strong>
                  <span className="step-status">
                    {statusLabel}
                    {step.stale && <em className="stale-badge"> - update available</em>}
                    {step.job?.status === "running" && step.job.progress && (
                      <em> - {step.job.progress}</em>
                    )}
                  </span>
                  {step.blocked && !step.done && !step.not_applicable && (
                    <span className="prereq" title="Run these first">
                      requires: {step.missing.join(", ")}
                    </span>
                  )}
                  {step.not_applicable && <span className="prereq">already local</span>}
                </button>

                <span className="step-actions">
                  {step.artifact && (
                    <Link to={`/artifacts/${step.artifact.id}`}>Open artifact</Link>
                  )}
                  {!step.not_applicable && (
                    <>
                      <button
                        type="button"
                        onClick={() => void run(step.name)}
                        disabled={
                          status === "running" ||
                          status === "queued" ||
                          (step.blocked && !step.done) ||
                          rerunningStep !== ""
                        }
                      >
                        {step.artifact || step.job ? "Re-run only" : "Run"}
                      </button>
                      {step.done && (
                        <button
                          type="button"
                          className={step.stale ? "" : "linkish"}
                          onClick={() => void rerunAffected(step.name)}
                          disabled={detail.any_active || rerunningStep !== ""}
                          title="Rebuild this output and every later output that consumes it"
                        >
                          {rerunningStep === step.name ? "Queuing..." : "Re-run downstream"}
                        </button>
                      )}
                    </>
                  )}
                </span>
              </div>

              {isOpen && (
                <div className="step-detail" id={detailId}>
                  {step.job ? (
                    <>
                      <p className="meta">
                        status: <b>{step.job.status}</b>
                        {step.job.progress && <> - progress: {step.job.progress}</>}
                        {step.job.updated && <> - updated {fmtTime(step.job.updated)}</>}
                      </p>
                      {step.job.error && <pre className="error">{step.job.error}</pre>}
                    </>
                  ) : (
                    <p className="meta">not run yet</p>
                  )}
                  {step.artifact && (
                    <p className="meta">
                      artifact: <Link to={`/artifacts/${step.artifact.id}`}>{step.artifact.title}</Link>
                      {step.artifact.provider && (
                        <> - {step.artifact.provider}/{step.artifact.model}</>
                      )}
                      {" - "}updated {fmtDateTime(step.artifact.updated)}
                    </p>
                  )}
                  {step.stale && (
                    <p className="notice">
                      This output was made with older source content or settings. A profile run
                      will refresh it automatically.
                    </p>
                  )}
                  {step.blocked && !step.done && (
                    <p className="meta">prerequisites: {step.missing.join(", ")}</p>
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
