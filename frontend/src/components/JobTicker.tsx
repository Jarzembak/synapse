import { useState } from "react";
import { Link } from "react-router-dom";
import { Job } from "../api";
import { useEventSource } from "../useEventSource";

/** Live badge in the nav (links to the Jobs tab), fed by the SSE stream. */
export default function JobTicker() {
  const [active, setActive] = useState<Job[]>([]);
  const [connected, setConnected] = useState(true);

  useEventSource<{ active: Job[] }>(
    "/api/jobs/stream", "jobs", (d) => setActive(d.active), setConnected);

  // While the stream is down we have no live data, so don't assert "busy" — a
  // dropped connection reconnects within a couple seconds and refreshes this.
  if (!connected || active.length === 0)
    return <Link to="/jobs" className="ticker idle">idle</Link>;

  const running = active.filter((a) => a.status === "running");
  const queued = active.filter((a) => a.status === "queued");
  const lead = running[0] ?? active[0];
  return (
    <Link
      to="/jobs"
      className="ticker busy"
      title={active.map((a) => `${a.status} · ${a.task_label ?? a.task}: ${a.progress}`).join("\n")}
    >
      ⏳ {running.length} running{queued.length ? `, ${queued.length} queued` : ""}
      {lead && ` — ${lead.task_label ?? lead.task}${lead.progress ? ` · ${lead.progress}` : ""}`}
    </Link>
  );
}
