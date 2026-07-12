import { useEffect, useRef, useState } from "react";
import { api, Job } from "../api";
import { useEventSource } from "../useEventSource";
import { StreamStatus, useStreamStatus } from "../useStreamStatus";

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
  disk: { used_mb: number; total_mb: number; percent: number } | null;
  active_jobs: number;
}
interface PreflightCheck { name: string; ok: boolean; detail: string; required: boolean }
interface Preflight { ready: boolean; checks: PreflightCheck[] }
interface UsageRow {
  function: string; provider: string; model: string; calls: number; errors: number;
  input_tokens: number; output_tokens: number; duration_seconds: number;
}
interface BackupItem { name: string; size: number; updated: number; encrypted: boolean }
interface BackupList { backups: BackupItem[]; encryption_configured: boolean }
interface LibraryHealth {
  healthy: boolean; schema_version: number; files: number; artifacts: number;
  fts_rows: number; fts_consistent: boolean; search_chunks: number; missing_files: string[];
  unindexed_files: string[]; duplicate_paths: string[]; orphan_artifacts: number[];
}

const gb = (mb: number) => (mb / 1024).toFixed(1);
const heat = (pct: number) => (pct >= 85 ? "hot" : pct >= 50 ? "warm" : "");
const STREAM_LABEL: Record<StreamStatus, string> = {
  connecting: "connecting...",
  live: "live",
  offline: "offline",
  stale: "stale",
};

function Meter({ label, pct, sub }: { label: string; pct: number; sub?: string }) {
  return (
    <div className="meter">
      <div className="meter-head">
        <span>{label}</span>
        <span className="mono">{sub ?? `${pct.toFixed(0)}%`}</span>
      </div>
      <div
        className="meter-bar"
        role="progressbar"
        aria-label={label}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={Math.round(Math.min(100, Math.max(0, pct)))}
      >
        <i className={heat(pct)} style={{ width: `${Math.min(100, Math.max(0, pct))}%` }} />
      </div>
    </div>
  );
}

export default function System() {
  const [stats, setStats] = useState<Stats | null>(null);
  const [statsError, setStatsError] = useState("");
  const [live, setLive] = useState(false);
  const [hasStreamSnapshot, setHasStreamSnapshot] = useState(false);
  const [preflight, setPreflight] = useState<Preflight | null>(null);
  const [usage, setUsage] = useState<UsageRow[]>([]);
  const [backups, setBackups] = useState<BackupList | null>(null);
  const [health, setHealth] = useState<LibraryHealth | null>(null);
  const [panelError, setPanelError] = useState("");
  const [actionError, setActionError] = useState("");
  const [action, setAction] = useState("");
  const [verification, setVerification] = useState<Record<string, string>>({});
  const seen = useRef(false);
  const operationsSequence = useRef(0);
  const refreshTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const maintenanceSignature = useRef("");

  async function loadOperations() {
    const sequence = ++operationsSequence.current;
    const [nextPreflight, nextUsage, nextBackups, nextHealth] = await Promise.allSettled([
      api<Preflight>("/system/preflight"),
      api<{ summary: UsageRow[] }>("/system/usage?limit=500"),
      api<BackupList>("/backups"),
      api<LibraryHealth>("/library/health"),
    ]);
    if (sequence !== operationsSequence.current) return;

    const failures: string[] = [];
    if (nextPreflight.status === "fulfilled") setPreflight(nextPreflight.value);
    else failures.push("startup checks");
    if (nextUsage.status === "fulfilled") setUsage(nextUsage.value.summary);
    else failures.push("model usage");
    if (nextBackups.status === "fulfilled") setBackups(nextBackups.value);
    else failures.push("backups");
    if (nextHealth.status === "fulfilled") setHealth(nextHealth.value);
    else failures.push("library health");
    setPanelError(failures.length ? `Could not refresh ${failures.join(", ")}.` : "");
  }

  function loadStats() {
    setStatsError("");
    api<Stats>("/system/stats")
      .then((nextStats) => {
        if (seen.current) return;
        setStats(nextStats);
        setStatsError("");
      })
      .catch((error) => {
        if (!seen.current) {
          setStatsError(error instanceof Error ? error.message : "Could not load system stats");
        }
      });
  }

  useEffect(() => {
    // one immediate snapshot so the page isn't blank for the first ~2s
    loadStats();
    void loadOperations();
    return () => {
      operationsSequence.current += 1;
      if (refreshTimer.current) clearTimeout(refreshTimer.current);
    };
  }, []);

  async function createBackup() {
    setAction("backup");
    setActionError("");
    try {
      await api("/backups", { method: "POST", body: JSON.stringify({}) });
      if (refreshTimer.current) clearTimeout(refreshTimer.current);
      refreshTimer.current = setTimeout(() => void loadOperations(), 1500);
    } catch (error) {
      setActionError(error instanceof Error ? error.message : "Backup could not be queued");
    } finally { setAction(""); }
  }

  async function verifyBackup(name: string) {
    setAction(`verify:${name}`);
    try {
      const result = await api<{ valid: boolean; files: number }>(
        `/backups/${encodeURIComponent(name)}/verify`,
      );
      setVerification((current) => ({
        ...current, [name]: result.valid ? `Verified (${result.files} files)` : "Verification failed",
      }));
    } catch (error) {
      setVerification((current) => ({
        ...current,
        [name]: error instanceof Error ? error.message : "Verification failed",
      }));
    } finally { setAction(""); }
  }

  async function repairLibrary() {
    if (!confirm("Rebuild the searchable library index from the Markdown vault? Existing files are preserved.")) return;
    setAction("repair");
    setActionError("");
    try {
      await api("/library/repair", {
        method: "POST", body: JSON.stringify({ prune_missing: false }),
      });
    } catch (error) {
      setActionError(error instanceof Error ? error.message : "Repair could not be queued");
    } finally { setAction(""); }
  }

  // the /system/stream sends a fresh sample every ~2s, so it doubles as its own
  // heartbeat; the reconnecting hook keeps it alive across an api restart
  useEventSource<Stats>("/api/system/stream", "system",
    (nextStats) => {
      seen.current = true;
      setHasStreamSnapshot(true);
      setStats(nextStats);
      setStatsError("");
    }, setLive);

  useEventSource<{ active: Job[]; recent: Job[] }>(
    "/api/jobs/stream",
    "jobs",
    (snapshot) => {
      const maintenance = [...snapshot.active, ...snapshot.recent]
        .filter((job) => ["create_backup", "rebuild_library", "rebuild_search"].includes(job.task))
        .map((job) => `${job.id}:${job.status}:${job.updated ?? ""}`)
        .sort()
        .join("|");
      if (maintenanceSignature.current && maintenance !== maintenanceSignature.current) {
        void loadOperations();
      }
      maintenanceSignature.current = maintenance;
    },
  );

  const streamStatus = useStreamStatus(live && hasStreamSnapshot, hasStreamSnapshot);

  if (!stats) return (
    <section className="loading-state" aria-live="polite">
      {statsError ? (
        <>
          <p className="error" role="alert">Could not load system stats: {statsError}</p>
          <button type="button" onClick={loadStats}>Try again</button>
        </>
      ) : <p role="status">Loading system stats...</p>}
    </section>
  );

  return (
    <div className="system">
      <div className="sys-head">
        <h2>System monitor</h2>
        <span className={`live-dot ${streamStatus === "live" ? "on" : streamStatus}`} role="status">
          {STREAM_LABEL[streamStatus]}
        </span>
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
          <h3>Storage <small>{stats.active_jobs} active job{stats.active_jobs === 1 ? "" : "s"}</small></h3>
          {stats.disk ? (
            <Meter
              label="Library disk"
              pct={stats.disk.percent}
              sub={`${gb(stats.disk.used_mb)} / ${gb(stats.disk.total_mb)} GB`}
            />
          ) : <p className="empty">Disk usage is unavailable.</p>}
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

      <div className="sys-head operations-head">
        <h2>Readiness and recovery</h2>
        <button type="button" className="linkish" onClick={() => void loadOperations()}>
          Refresh checks
        </button>
      </div>
      {panelError && <p className="error" role="alert">{panelError}</p>}
      {actionError && <p className="error" role="alert">{actionError}</p>}
      <div className="sys-grid operations-grid">
        <section className="card">
          <h3>Startup checks</h3>
          {!preflight ? <p>Checking services…</p> : (
            <>
              <p className={preflight.ready ? "notice" : "error"}>
                {preflight.ready ? "Ready to process projects" : "A required service needs attention"}
              </p>
              <ul className="check-list">
                {preflight.checks.map((check) => (
                  <li key={check.name} className={check.ok ? "ok" : check.required ? "bad" : "warn"}>
                    <span aria-hidden="true">{check.ok ? "✓" : check.required ? "✕" : "!"}</span>
                    <span>
                      <span className="sr-only">
                        {check.ok ? "Passed: " : check.required ? "Failed: " : "Optional warning: "}
                      </span>
                      <b>{check.name}</b><small>{check.detail}</small>
                    </span>
                  </li>
                ))}
              </ul>
            </>
          )}
        </section>

        <section className="card">
          <h3>Library integrity</h3>
          {!health ? <p>Checking the vault…</p> : (
            <>
              <p className={health.healthy ? "notice" : "error"}>
                {health.healthy ? "Vault and index agree" : "The searchable index needs repair"}
              </p>
              <dl className="stat-list">
                <div><dt>Markdown files</dt><dd>{health.files}</dd></div>
                <div><dt>Indexed artifacts</dt><dd>{health.artifacts}</dd></div>
                <div><dt>Search chunks</dt><dd>{health.search_chunks}</dd></div>
                <div><dt>Schema</dt><dd>v{health.schema_version}</dd></div>
              </dl>
              {!health.healthy && (
                <p className="meta">
                  {health.missing_files.length} missing, {health.unindexed_files.length} unindexed,
                  {" "}{health.duplicate_paths.length} duplicate paths
                  {!health.fts_consistent ? ", full-text index count differs" : ""}.
                </p>
              )}
              <button type="button" onClick={() => void repairLibrary()} disabled={action !== ""}>
                {action === "repair" ? "Queuing…" : "Rebuild index from vault"}
              </button>
            </>
          )}
        </section>

        <section className="card backup-card">
          <h3>Backups</h3>
          {backups && !backups.encryption_configured && (
            <p className="warning">New snapshots are not encrypted. Configure the backup key for off-device copies.</p>
          )}
          <button type="button" onClick={() => void createBackup()} disabled={action !== ""}>
            {action === "backup" ? "Queuing…" : "Create backup now"}
          </button>
          {!backups ? <p>Loading backups…</p> : backups.backups.length === 0 ? (
            <p className="empty">No snapshots yet.</p>
          ) : (
            <ul className="backup-list">
              {backups.backups.map((backup) => (
                <li key={backup.name}>
                  <span>
                    <b>{new Date(backup.updated * 1000).toLocaleString()}</b>
                    <small>{(backup.size / 1_048_576).toFixed(1)} MB · {backup.encrypted ? "encrypted" : "plain"}</small>
                    {verification[backup.name] && <small role="status">{verification[backup.name]}</small>}
                  </span>
                  <span className="row">
                    <button type="button" className="linkish"
                      disabled={action !== ""}
                      onClick={() => void verifyBackup(backup.name)}>
                      {action === `verify:${backup.name}` ? "Checking…" : "Verify"}
                    </button>
                    <a href={`/api/backups/${encodeURIComponent(backup.name)}`}>Download</a>
                  </span>
                </li>
              ))}
            </ul>
          )}
        </section>
      </div>

      <section className="card usage-card">
        <h2>Model usage</h2>
        <p className="meta">Local accounting from recent calls; token counts depend on provider reporting.</p>
        {usage.length === 0 ? <p className="empty">No model calls recorded yet.</p> : (
          <div className="table-scroll">
            <table className="list compact">
              <thead><tr><th>Function</th><th>Provider / model</th><th>Calls</th><th>Errors</th><th>Tokens in / out</th><th>Time</th></tr></thead>
              <tbody>{usage.map((row) => (
                <tr key={`${row.function}:${row.provider}:${row.model}`}>
                  <td>{row.function}</td>
                  <td className="mono">{row.provider} / {row.model}</td>
                  <td>{row.calls}</td><td>{row.errors}</td>
                  <td>{row.input_tokens.toLocaleString()} / {row.output_tokens.toLocaleString()}</td>
                  <td>{row.duration_seconds.toFixed(1)}s</td>
                </tr>
              ))}</tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}
