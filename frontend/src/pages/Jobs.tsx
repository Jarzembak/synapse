import { useState } from "react";
import { Link } from "react-router-dom";
import { api, fmtTime, Job } from "../api";
import { useEventSource } from "../useEventSource";
import { StreamStatus, useStreamStatus } from "../useStreamStatus";

interface Snapshot {
  active: Job[];
  recent: Job[];
}

const STATUS_LABEL: Record<string, string> = {
  running: "running",
  queued: "queued",
  done: "done",
  error: "error",
  canceled: "canceled",
};

const STREAM_MESSAGE: Record<StreamStatus, string> = {
  connecting: "Connecting to live job updates...",
  live: "Live job updates connected.",
  offline: "Live job updates are offline. No snapshot is available yet.",
  stale: "Reconnecting. This is the last received snapshot; queue actions are paused.",
};

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "Unexpected error";
}

function JobRow({
  job,
  onCancel,
  canceling = false,
  actionsDisabled = false,
}: {
  job: Job;
  onCancel?: (job: Job) => void;
  canceling?: boolean;
  actionsDisabled?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const errorId = `job-error-${job.id}`;

  return (
    <>
      <tr className={`job ${job.status}`}>
        <td>
          <span className={`jobstatus ${job.status}`}>
            {STATUS_LABEL[job.status] ?? job.status}
          </span>
        </td>
        <td>
          {job.project_id ? (
            <Link to={`/projects/${job.project_id}`}>
              {job.project_title || `project ${job.project_id}`}
            </Link>
          ) : (
            <span className="muted">-</span>
          )}
        </td>
        <td>{job.task_label ?? job.task}</td>
        <td className="progress">{job.progress}</td>
        <td className="mono">
          {job.updated ? <time dateTime={job.updated}>{fmtTime(job.updated)}</time> : ""}
        </td>
        <td className="jobactions">
          {job.error && (
            <button
              type="button"
              className="linkish"
              onClick={() => setOpen((current) => !current)}
              aria-expanded={open}
              aria-controls={errorId}
              aria-label={`${open ? "Hide" : "Show"} error for ${job.task_label ?? job.task}`}
            >
              {open ? "hide" : "error"}
            </button>
          )}
          {onCancel && (job.status === "queued" || job.status === "running") && (
            <button
              type="button"
              className="linkish danger"
              onClick={() => onCancel(job)}
              disabled={actionsDisabled || canceling}
              title={actionsDisabled ? "Reconnect to live updates before acting on this job" : undefined}
            >
              {canceling ? "canceling..." : "cancel"}
            </button>
          )}
        </td>
      </tr>
      {open && job.error && (
        <tr className="job-error-row" id={errorId}>
          <td colSpan={6}><pre className="error">{job.error}</pre></td>
        </tr>
      )}
    </>
  );
}

export default function Jobs() {
  const [snapshot, setSnapshot] = useState<Snapshot>({ active: [], recent: [] });
  const [connected, setConnected] = useState(false);
  const [hasSnapshot, setHasSnapshot] = useState(false);
  const [error, setError] = useState("");
  const [canceling, setCanceling] = useState<Set<number>>(new Set());
  const [continuing, setContinuing] = useState(false);

  useEventSource<Snapshot>(
    "/api/jobs/stream",
    "jobs",
    (nextSnapshot) => {
      setSnapshot(nextSnapshot);
      setHasSnapshot(true);
      const activeIds = new Set(nextSnapshot.active.map((job) => job.id));
      setCanceling((current) => {
        const next = new Set([...current].filter((id) => activeIds.has(id)));
        return next.size === current.size ? current : next;
      });
    },
    setConnected,
  );

  const streamStatus = useStreamStatus(connected && hasSnapshot, hasSnapshot);
  const actionsDisabled = streamStatus !== "live";

  async function cancel(job: Job) {
    const project = job.project_title ? ` for ${job.project_title}` : "";
    if (!confirm(`Cancel "${job.task_label ?? job.task}"${project}?`)) return;

    setError("");
    setCanceling((current) => new Set(current).add(job.id));
    try {
      await api(`/jobs/${job.id}/cancel`, { method: "POST" });
    } catch (caught) {
      setCanceling((current) => {
        const next = new Set(current);
        next.delete(job.id);
        return next;
      });
      setError(`Could not cancel the job: ${errorMessage(caught)}`);
    }
  }

  async function continueQueue() {
    setError("");
    setContinuing(true);
    try {
      await api("/jobs/continue", { method: "POST" });
    } catch (caught) {
      setError(`Could not continue the queue: ${errorMessage(caught)}`);
    } finally {
      setContinuing(false);
    }
  }

  const running = snapshot.active.filter((job) => job.status === "running");
  const queued = snapshot.active.filter((job) => job.status === "queued");
  const runAllStalled =
    queued.some((job) => job.task === "run_all") &&
    !running.some((job) => job.task === "run_all");
  const waitingMessage = hasSnapshot ? undefined : "Waiting for the first job snapshot...";

  return (
    <div className="jobs">
      <div className="jobs-head">
        <h2>Job queue</h2>
        <span
          className={`stream-status ${streamStatus}`}
          role="status"
          aria-live="polite"
        >
          {STREAM_MESSAGE[streamStatus]}
        </span>
        {runAllStalled && (
          <button
            type="button"
            className="continue-btn"
            onClick={continueQueue}
            disabled={actionsDisabled || continuing}
            title={
              actionsDisabled
                ? "Reconnect to live updates before continuing the queue"
                : "Resume the run-all queue after a worker restart interrupted the hand-off"
            }
          >
            {continuing ? "Continuing..." : "Continue queue"}
          </button>
        )}
      </div>
      <p className="meta">
        Live view of everything running, waiting, and recently finished across all
        projects. Whole-project runs execute one at a time; individual steps run as
        worker capacity becomes available.
      </p>
      {error && <p className="error" role="alert">{error}</p>}

      <Section
        title={`Running (${running.length})`}
        jobs={running}
        onCancel={cancel}
        canceling={canceling}
        actionsDisabled={actionsDisabled}
        empty={waitingMessage ?? "Nothing running."}
      />
      <Section
        title={`Queued (${queued.length})`}
        jobs={queued}
        onCancel={cancel}
        canceling={canceling}
        actionsDisabled={actionsDisabled}
        empty={waitingMessage ?? "Queue is empty."}
      />
      <Section
        title="Recently finished"
        jobs={snapshot.recent}
        empty={waitingMessage ?? "No history yet."}
      />
    </div>
  );
}

function Section({
  title,
  jobs,
  onCancel,
  canceling = new Set<number>(),
  actionsDisabled = false,
  empty,
}: {
  title: string;
  jobs: Job[];
  onCancel?: (job: Job) => void;
  canceling?: Set<number>;
  actionsDisabled?: boolean;
  empty: string;
}) {
  return (
    <section className="jobsection">
      <h3>{title}</h3>
      {jobs.length === 0 ? (
        <p className="empty">{empty}</p>
      ) : (
        <div className="table-scroll" tabIndex={0} aria-label={`${title} table; scroll horizontally if needed`}>
          <table className="list">
            <caption className="sr-only">{title}</caption>
            <thead>
              <tr>
                <th scope="col">Status</th>
                <th scope="col">Project</th>
                <th scope="col">Task</th>
                <th scope="col">Progress</th>
                <th scope="col">Updated</th>
                <th scope="col"><span className="sr-only">Actions</span></th>
              </tr>
            </thead>
            <tbody>
              {jobs.map((job) => (
                <JobRow
                  key={job.id}
                  job={job}
                  onCancel={onCancel}
                  canceling={canceling.has(job.id)}
                  actionsDisabled={actionsDisabled}
                />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
