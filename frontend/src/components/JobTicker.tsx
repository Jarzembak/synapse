import { useRef, useState } from "react";
import { Link } from "react-router-dom";
import { Job } from "../api";
import { useEventSource } from "../useEventSource";
import { useStreamStatus } from "../useStreamStatus";

/** Live badge in the nav (links to the Jobs tab), fed by the SSE stream. */
export default function JobTicker() {
  const [active, setActive] = useState<Job[]>([]);
  const [connected, setConnected] = useState(false);
  const [hasSnapshot, setHasSnapshot] = useState(false);
  const previous = useRef<Job[] | null>(null);

  useEventSource<{ active: Job[]; recent: Job[] }>(
    "/api/jobs/stream",
    "jobs",
    (data) => {
      if (previous.current && localStorage.getItem("synapse.jobNotifications") === "on"
          && document.hidden && "Notification" in window
          && Notification.permission === "granted") {
        const activeIds = new Set(data.active.map((job) => job.id));
        for (const completed of previous.current.filter(
          (job) => !activeIds.has(job.id) && !job.parent_job_id,
        )) {
          const outcome = data.recent.find((job) => job.id === completed.id);
          if (!outcome) continue;
          const label = outcome.task_label ?? outcome.task;
          try {
            new Notification(`Synapse: ${outcome.status}`, {
              body: `${outcome.project_title ? `${outcome.project_title} — ` : ""}${label}`,
              tag: `synapse-job-${outcome.id}`,
            });
          } catch {
            // A platform notification failure must not interrupt live job state.
          }
        }
      }
      previous.current = data.active;
      setActive(data.active);
      setHasSnapshot(true);
    },
    setConnected,
  );

  const streamStatus = useStreamStatus(connected && hasSnapshot, hasSnapshot);

  if (streamStatus === "connecting") {
    return (
      <Link
        to="/jobs"
        className="ticker connecting"
        aria-label="Jobs: connecting to live updates"
      >
        jobs connecting...
      </Link>
    );
  }

  if (streamStatus === "offline") {
    return (
      <Link
        to="/jobs"
        className="ticker offline"
        aria-label="Jobs: live updates offline"
      >
        jobs offline
      </Link>
    );
  }

  if (streamStatus === "stale") {
    return (
      <Link
        to="/jobs"
        className="ticker stale"
        title="Live job updates disconnected; counts are from the last received snapshot"
        aria-label={`Jobs: updates stale; ${active.length} active when last seen`}
      >
        jobs stale{active.length ? ` - ${active.length} last seen` : ""}
      </Link>
    );
  }

  if (active.length === 0) {
    return (
      <Link
        to="/jobs"
        className="ticker idle"
        aria-label="Jobs: live, no active jobs"
      >
        idle
      </Link>
    );
  }

  const running = active.filter((job) => job.status === "running");
  const queued = active.filter((job) => job.status === "queued");
  const lead = running[0] ?? active[0];
  return (
    <Link
      to="/jobs"
      className="ticker busy"
      title={active
        .map((job) => `${job.status} - ${job.task_label ?? job.task}: ${job.progress}`)
        .join("\n")}
      aria-label={`${running.length} jobs running${queued.length ? `, ${queued.length} queued` : ""}`}
    >
      {running.length} running{queued.length ? `, ${queued.length} queued` : ""}
      {lead && ` - ${lead.task_label ?? lead.task}${lead.progress ? `: ${lead.progress}` : ""}`}
    </Link>
  );
}
