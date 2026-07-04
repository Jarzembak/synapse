import { useEffect, useState } from "react";
import { Job } from "../api";

/** Live badge in the nav showing running jobs, fed by the SSE stream. */
export default function JobTicker() {
  const [active, setActive] = useState<Job[]>([]);

  useEffect(() => {
    const es = new EventSource("/api/jobs/stream");
    es.addEventListener("jobs", (e) => {
      const data = JSON.parse((e as MessageEvent).data);
      setActive(data.active);
    });
    return () => es.close();
  }, []);

  if (active.length === 0) return <span className="ticker idle">idle</span>;
  const j = active[0];
  return (
    <span className="ticker busy" title={active.map((a) => `${a.task}: ${a.progress}`).join("\n")}>
      ⏳ {active.length} running — {j.task} {j.progress && `· ${j.progress}`}
    </span>
  );
}
