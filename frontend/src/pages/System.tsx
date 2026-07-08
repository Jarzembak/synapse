import { useEffect, useRef, useState } from "react";
import { api } from "../api";

interface Gpu {
  index: number;
  name: string;
  util_percent: number | null;
  mem_used_mb: number | null;
  mem_total_mb: number | null;
  temp_c: number | null;
}
interface OllamaModel {
  name: string;
  size_mb: number;
  vram_mb: number;
  processor: "gpu" | "cpu" | "hybrid";
}
interface Stats {
  cpu_percent: number;
  cpu_per_core: number[];
  cpu_count: number;
  mem_used_mb: number;
  mem_total_mb: number;
  mem_percent: number;
  gpus: Gpu[];
  ollama_models: OllamaModel[];
}

const gb = (mb: number) => (mb / 1024).toFixed(1);
const heat = (pct: number) => (pct >= 85 ? "hot" : pct >= 50 ? "warm" : "");

function Meter({ label, pct, sub }: { label: string; pct: number; sub?: string }) {
  return (
    <div className="meter">
      <div className="meter-head">
        <span>{label}</span>
        <span className="mono">{sub ?? `${pct.toFixed(0)}%`}</span>
      </div>
      <div className="meter-bar">
        <i className={heat(pct)} style={{ width: `${Math.min(100, Math.max(0, pct))}%` }} />
      </div>
    </div>
  );
}

export default function System() {
  const [stats, setStats] = useState<Stats | null>(null);
  const [live, setLive] = useState(false);
  const seen = useRef(false);

  useEffect(() => {
    // one immediate snapshot so the page isn't blank for the first ~2s
    api<Stats>("/system/stats").then((s) => { if (!seen.current) setStats(s); }).catch(() => {});
    const es = new EventSource("/api/system/stream");
    es.addEventListener("system", (e) => {
      seen.current = true;
      setStats(JSON.parse((e as MessageEvent).data));
      setLive(true);
    });
    es.onerror = () => setLive(false);
    return () => es.close();
  }, []);

  if (!stats) return <p>loading system stats…</p>;

  return (
    <div className="system">
      <div className="sys-head">
        <h2>System monitor</h2>
        <span className={`live-dot ${live ? "on" : ""}`}>{live ? "live" : "connecting…"}</span>
      </div>
      <p className="meta">
        Host-wide CPU and memory (covers every container — including the worker running a
        pipeline step). GPU rows appear when an NVIDIA GPU is visible to the app.
      </p>

      <div className="sys-grid">
        <section className="card">
          <h3>CPU <small>{stats.cpu_count} cores</small></h3>
          <Meter label="Total" pct={stats.cpu_percent} />
          <div className="cores">
            {stats.cpu_per_core.map((c, i) => (
              <div key={i} className="core" title={`core ${i}: ${c.toFixed(0)}%`}>
                <i className={heat(c)} style={{ height: `${Math.min(100, c)}%` }} />
              </div>
            ))}
          </div>
        </section>

        <section className="card">
          <h3>Memory</h3>
          <Meter
            label="RAM"
            pct={stats.mem_percent}
            sub={`${gb(stats.mem_used_mb)} / ${gb(stats.mem_total_mb)} GB`}
          />
        </section>

        <section className="card">
          <h3>GPU</h3>
          {stats.gpus.length === 0 ? (
            <p className="empty">
              No GPU visible — local models are running on CPU. Start the stack with the
              GPU overlay to enable acceleration.
            </p>
          ) : (
            stats.gpus.map((g) => (
              <div key={g.index} className="gpu">
                <div className="gpu-name">
                  {g.name}
                  {g.temp_c != null && <span className="mono"> · {g.temp_c.toFixed(0)}°C</span>}
                </div>
                <Meter label="Utilization" pct={g.util_percent ?? 0} />
                {g.mem_total_mb ? (
                  <Meter
                    label="VRAM"
                    pct={((g.mem_used_mb ?? 0) / g.mem_total_mb) * 100}
                    sub={`${gb(g.mem_used_mb ?? 0)} / ${gb(g.mem_total_mb)} GB`}
                  />
                ) : null}
              </div>
            ))
          )}
        </section>

        <section className="card">
          <h3>Ollama <small>resident models</small></h3>
          {stats.ollama_models.length === 0 ? (
            <p className="empty">No models loaded right now.</p>
          ) : (
            <table className="list compact">
              <thead><tr><th>Model</th><th>On</th><th>VRAM</th><th>Size</th></tr></thead>
              <tbody>
                {stats.ollama_models.map((m) => (
                  <tr key={m.name}>
                    <td className="mono">{m.name}</td>
                    <td><span className={`proc ${m.processor}`}>{m.processor}</span></td>
                    <td>{m.vram_mb ? `${gb(m.vram_mb)} GB` : "—"}</td>
                    <td>{gb(m.size_mb)} GB</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>
      </div>
    </div>
  );
}
