import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api, fmtDateTime, fmtTime, Project, Step } from "../api";
import { useEventSource } from "../useEventSource";
import { StreamStatus, useStreamStatus } from "../useStreamStatus";

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
  steps: DetailStep[];
  remaining: number;
  run_all_active: boolean;
  run_all_state: "queued" | "running" | null;
  any_active: boolean;
  profiles: Record<string, PipelineProfile>;
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
      setSelectedProfile((current) => nextDetail.profiles?.[current] ? current : "full");
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
      "This permanently deletes all project artifacts and downloaded source media. " +
      "Quick-reference documents it contributed to will remain.\n\nThis cannot be undone.",
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
  const profileEntries = Object.entries(detail.profiles ?? {});
  const activeProfile = detail.profiles?.[selectedProfile] ?? detail.profiles?.full;
  const profileSteps = new Set(activeProfile?.steps ?? steps.map((step) => step.name));
  const profileWork = steps.filter(
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
      <p className="mono project-source">{project.source}</p>

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

        <div className="cookies">
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
        </div>

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
        {steps.map((step) => {
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
