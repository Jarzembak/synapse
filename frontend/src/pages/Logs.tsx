import { useEffect, useMemo, useRef, useState, UIEvent } from "react";
import { api } from "../api";

interface Services { file_logging: boolean; services: string[] }
interface Tail { service: string; lines: string[] }

const LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"];
const LINE_COUNTS = [200, 500, 1000, 2000];
// header line: "2026-07-08 04:28:52,817 INFO    synapse.cloud: message"
const HEADER = /^\d{4}-\d\d-\d\d[ T]\d\d:\d\d:\d\d[,.]\d+\s+([A-Z]+)\b/;

interface Line { level: string; text: string }

/** Tag each line with the level of the most recent header line, so multi-line
 * entries (tracebacks) inherit their header's level for filtering/coloring.
 * Lines before the first header (the tail window can begin mid-traceback, its
 * header already scrolled off) get level "" = unknown, and are never filtered
 * out — better to show orphaned context than hide part of an error. */
function parseLines(raw: string[]): Line[] {
  let cur = "";
  return raw.map((text) => {
    const m = text.match(HEADER);
    if (m && LEVELS.includes(m[1])) cur = m[1];
    return { level: cur, text };
  });
}

export default function Logs() {
  const [services, setServices] = useState<string[]>([]);
  const [fileLogging, setFileLogging] = useState(true);
  const [service, setService] = useState("");
  const [count, setCount] = useState(500);
  const [minLevel, setMinLevel] = useState("DEBUG");
  const [search, setSearch] = useState("");
  const [live, setLive] = useState(true);
  const [raw, setRaw] = useState<string[]>([]);
  const [error, setError] = useState("");
  const viewRef = useRef<HTMLDivElement>(null);
  const atBottom = useRef(true);

  useEffect(() => {
    api<Services>("/logs").then((r) => {
      setFileLogging(r.file_logging);
      setServices(r.services);
      setService((s) => s || r.services[0] || "");
    }).catch((e) => setError(e.message));
  }, []);

  // poll the selected service's tail; only the newest response wins
  useEffect(() => {
    if (!service) return;
    let alive = true;
    const seq = { n: 0 };
    async function pull() {
      const mine = ++seq.n;
      try {
        const r = await api<Tail>(`/logs/${service}?lines=${count}`);
        if (alive && mine === seq.n) { setRaw(r.lines); setError(""); }
      } catch (e: any) {
        if (alive && mine === seq.n) setError(e.message);
      }
    }
    pull();
    if (!live) return () => { alive = false; };
    const t = setInterval(pull, 2000);
    return () => { alive = false; clearInterval(t); };
  }, [service, count, live]);

  const lines = useMemo(() => {
    const min = LEVELS.indexOf(minLevel);
    const needle = search.trim().toLowerCase();
    return parseLines(raw).filter((l) => {
      // unknown-level lines (before the first header in the window) always pass
      if (l.level && LEVELS.indexOf(l.level) < min) return false;
      if (needle && !l.text.toLowerCase().includes(needle)) return false;
      return true;
    });
  }, [raw, minLevel, search]);

  // keep pinned to the bottom on new output only if the user hasn't scrolled up.
  // Scroll the container directly (not scrollIntoView, which also scrolls the
  // whole document and would yank the toolbar off-screen on a short window).
  useEffect(() => {
    if (atBottom.current && viewRef.current) {
      viewRef.current.scrollTop = viewRef.current.scrollHeight;
    }
  }, [lines]);

  function onScroll(e: UIEvent<HTMLDivElement>) {
    const el = e.currentTarget;
    atBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
  }

  function download() {
    const blob = new Blob([raw.join("\n")], { type: "text/plain" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `synapse-${service}.log`;
    a.click();
    URL.revokeObjectURL(a.href);
  }

  if (!fileLogging) {
    return (
      <div className="logs">
        <h2>Logs</h2>
        <p className="empty">
          File logging is off (LOG_DIR is unset). Set LOG_DIR in the stack's environment
          to capture per-service log files here. Until then, use <code>docker compose logs</code>.
        </p>
      </div>
    );
  }

  return (
    <div className="logs">
      <div className="logs-toolbar">
        <div className="segmented">
          {services.map((s) => (
            <button key={s} className={service === s ? "on" : ""} onClick={() => setService(s)}>
              {s}
            </button>
          ))}
        </div>
        <input className="search" placeholder="filter text…" value={search}
               onChange={(e) => setSearch(e.target.value)} />
        <select value={minLevel} onChange={(e) => setMinLevel(e.target.value)} title="minimum level">
          <option value="DEBUG">all levels</option>
          <option value="INFO">info +</option>
          <option value="WARNING">warning +</option>
          <option value="ERROR">error only</option>
        </select>
        <select value={count} onChange={(e) => setCount(Number(e.target.value))} title="lines to tail">
          {LINE_COUNTS.map((n) => <option key={n} value={n}>{n} lines</option>)}
        </select>
        <label className="live-toggle" title="auto-refresh every 2s">
          <input type="checkbox" checked={live} onChange={(e) => setLive(e.target.checked)} />
          live
        </label>
        <button onClick={download} title="download the full tail">⬇</button>
      </div>

      {error && <p className="error">{error}</p>}
      <div className="logview" ref={viewRef} onScroll={onScroll}>
        {lines.map((l, i) => (
          <div key={i} className={`logline ${l.level.toLowerCase()}`}>{l.text || " "}</div>
        ))}
        {lines.length === 0 && !error && (
          <p className="empty">No log lines match — widen the level or clear the filter.</p>
        )}
      </div>
      <p className="meta">
        Showing {lines.length} of {raw.length} tailed lines from <code>{service}</code>.
        For more verbose output, set <code>LOG_LEVEL=DEBUG</code> in the stack environment and restart.
      </p>
    </div>
  );
}
