import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, fmtTime, Job } from "../api";

interface Snapshot {
  active: Job[];   // queued + running, oldest first
  recent: Job[];   // done/error/canceled, newest first
}

const STATUS_LABEL: Record<string, string> = {
  running: "running",
  queued: "queued",
  done: "done",
  error: "error",
  canceled: "canceled",
};

function JobRow({ job, onCancel }: { job: Job; onCancel?: (j: Job) => void }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <tr className={`job ${job.status}`}>
        <td><span className={`jobstatus ${job.status}`}>{STATUS_LABEL[job.status] ?? job.status}</span></td>
        <td>
          {job.project_id
            ? <Link to={`/projects/${job.project_id}`}>{job.project_title || `project ${job.project_id}`}</Link>
            : <span className="muted">—</span>}
        </td>
        <td>{job.task_label ?? job.task}</td>
        <td className="progress">{job.progress}</td>
        <td className="mono">{job.updated ? fmtTime(job.updated) : ""}</td>
        <td className="jobactions">
          {job.error && (
            <button className="linkish" onClick={() => setOpen((o) => !o)}>
              {open ? "hide" : "error"}
            </button>
          )}
          {onCancel && (job.status === "queued" || job.status === "running") && (
            <button className="linkish danger" onClick={() => onCancel(job)}>cancel</button>
          )}
        </td>
      </tr>
      {open && job.error && (
        <tr className="job-error-row"><td colSpan={6}><pre className="error">{job.error}</pre></td></tr>
      )}
    </>
  );
}

export default function Jobs() {
  const [snap, setSnap] = useState<Snapshot>({ active: [], recent: [] });

  useEffect(() => {
    const es = new EventSource("/api/jobs/stream");
    es.addEventListener("jobs", (e) => setSnap(JSON.parse((e as MessageEvent).data)));
    return () => es.close();
  }, []);

  async function cancel(job: Job) {
    if (!confirm(`Cancel "${job.task_label ?? job.task}"${job.project_title ? ` for ${job.project_title}` : ""}?`)) return;
    try {
      await api(`/jobs/${job.id}/cancel`, { method: "POST" });
    } catch (e: any) {
      alert(e.message);
    }
  }

  const running = snap.active.filter((j) => j.status === "running");
  const queued = snap.active.filter((j) => j.status === "queued");

  return (
    <div className="jobs">
      <h2>Job queue</h2>
      <p className="meta">
        Live view of everything running, waiting, and recently finished across all
        projects. Whole-project "run all" jobs execute one at a time and auto-chain;
        individual steps run concurrently as worker capacity frees up.
      </p>

      <Section title={`Running (${running.length})`} jobs={running} onCancel={cancel} empty="Nothing running." />
      <Section title={`Queued (${queued.length})`} jobs={queued} onCancel={cancel} empty="Queue is empty." />
      <Section title="Recently finished" jobs={snap.recent} empty="No history yet." />
    </div>
  );
}

function Section({ title, jobs, onCancel, empty }:
  { title: string; jobs: Job[]; onCancel?: (j: Job) => void; empty: string }) {
  return (
    <section className="jobsection">
      <h3>{title}</h3>
      {jobs.length === 0 ? (
        <p className="empty">{empty}</p>
      ) : (
        <table className="list">
          <thead>
            <tr><th>Status</th><th>Project</th><th>Task</th><th>Progress</th><th>Updated</th><th></th></tr>
          </thead>
          <tbody>
            {jobs.map((j) => <JobRow key={j.id} job={j} onCancel={onCancel} />)}
          </tbody>
        </table>
      )}
    </section>
  );
}
