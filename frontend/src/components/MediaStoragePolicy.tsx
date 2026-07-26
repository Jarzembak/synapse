import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router";
import { api } from "../api";
import { useEventSource } from "../useEventSource";

type MediaStorageMode = "keep_local" | "cloud_primary";

interface MediaStorageSummary {
  eligible_objects: number;
  total_bytes: number;
  local_objects: number;
  local_bytes: number;
  verified_objects: number;
  cloud_only_objects: number;
  remote_objects: number;
  restorable_objects: number;
  pending_objects: number;
  error_objects: number;
  excluded_objects: number;
}

interface MediaStorageObject {
  id: number;
  artifact_id: number | null;
  role: string;
  state: string;
  size_bytes: number;
  local_present: boolean;
  verified_at: string | null;
  last_error: string;
  eligible: boolean;
}

interface MediaStorageStatus {
  policy: {
    mode: MediaStorageMode;
    storage_target_id: number | null;
  };
  target: {
    id: number;
    provider: string;
    remote_base: string;
  } | null;
  summary: MediaStorageSummary;
  objects: MediaStorageObject[];
}

function errorMessage(value: unknown): string {
  return value instanceof Error ? value.message : "Unexpected error";
}

function formatBytes(value: number): string {
  if (!value) return "0 B";
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let amount = value;
  let index = 0;
  while (amount >= 1024 && index < units.length - 1) {
    amount /= 1024;
    index += 1;
  }
  return `${amount >= 10 || index === 0 ? amount.toFixed(0) : amount.toFixed(1)} ${units[index]}`;
}

export default function MediaStoragePolicy({
  projectId,
  disabled = false,
}: {
  projectId: number;
  disabled?: boolean;
}) {
  const [status, setStatus] = useState<MediaStorageStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [pending, setPending] = useState<
    "" | "policy" | "sync" | "evict" | "restore" | "purge"
  >("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const load = useCallback(async () => {
    try {
      const value = await api<MediaStorageStatus>(`/projects/${projectId}/media-storage`);
      if (!value?.policy?.mode || !value.summary) {
        throw new Error("Media storage status returned an invalid response.");
      }
      setStatus(value);
      setError("");
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setLoading(false);
    }
  }, [projectId]);

  useEffect(() => { void load(); }, [load]);
  useEventSource(`/api/jobs/stream?project_id=${projectId}`, "jobs", () => void load());

  async function setMode(mode: MediaStorageMode) {
    if (!status || status.policy.mode === mode) return;
    if (
      mode === "keep_local" &&
      status.summary.restorable_objects > 0 &&
      !confirm(
        "Switch to keep-local storage and restore every missing cloud-backed media file? " +
        "The change may remain in progress until all files are local.",
      )
    ) return;
    setPending("policy");
    setError("");
    setNotice("");
    try {
      const value = await api<MediaStorageStatus>(`/projects/${projectId}/media-storage`, {
        method: "PUT",
        body: JSON.stringify({ mode }),
      });
      setStatus(value);
      if (mode === "keep_local" && status.summary.restorable_objects > 0) {
        await api(`/projects/${projectId}/media-storage/restore`, { method: "POST" });
      }
      setNotice(
        mode === "cloud_primary"
          ? "Cloud-primary policy enabled. Local copies are retained until upload verification and explicit eviction."
          : status.summary.restorable_objects > 0
            ? "Keep-local policy selected. Missing cloud-backed media restoration has been queued."
            : "Keep-local policy selected.",
      );
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setPending("");
    }
  }

  async function runAction(action: "sync" | "evict" | "restore" | "purge") {
    if (
      action === "purge" &&
      !confirm(
        "Remove this project's verified remote media copies? " +
        "Synapse will first require intact local copies. Shared content-addressed " +
        "objects used by another project will be retained.",
      )
    ) return;
    setPending(action);
    setError("");
    setNotice("");
    try {
      await api(`/projects/${projectId}/media-storage/${action}`, { method: "POST" });
      setNotice(
        action === "sync"
          ? "Eligible media upload and verification has been queued."
          : action === "evict"
            ? "Safe eviction of verified local copies has been queued."
            : action === "restore"
              ? "Restoration of cloud-only media has been queued."
              : "Verified remote media removal has been queued.",
      );
      await load();
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setPending("");
    }
  }

  if (loading && !status) {
    return <section className="card media-storage-policy"><p role="status">Loading media storage policy…</p></section>;
  }

  return (
    <section className="card media-storage-policy" aria-labelledby={`media-storage-title-${projectId}`}>
      <div className="section-heading">
        <div>
          <p className="eyebrow">Per-project override</p>
          <h3 id={`media-storage-title-${projectId}`}>Media storage</h3>
        </div>
        {status?.target && (
          <span className="storage-target">{status.target.provider} · {status.target.remote_base}</span>
        )}
      </div>

      {error && (
        <p className="error" role="alert">
          {error}{" "}
          {error.toLocaleLowerCase().includes("cloud") && <Link to="/settings">Open cloud settings</Link>}
        </p>
      )}
      {notice && <p className="notice" role="status">{notice}</p>}

      {status && (
        <>
          <fieldset className="storage-policy-options" disabled={disabled || Boolean(pending)}>
            <legend>Storage policy</legend>
            <label className={status.policy.mode === "keep_local" ? "selected" : ""}>
              <input type="radio" name={`media-storage-${projectId}`} value="keep_local"
                checked={status.policy.mode === "keep_local"}
                onChange={() => void setMode("keep_local")} />
              <span>
                <strong>Keep locally</strong>
                <small>Default. Cloud sync may mirror eligible files, but local media remains authoritative.</small>
              </span>
            </label>
            <label className={status.policy.mode === "cloud_primary" ? "selected" : ""}>
              <input type="radio" name={`media-storage-${projectId}`} value="cloud_primary"
                checked={status.policy.mode === "cloud_primary"}
                onChange={() => void setMode("cloud_primary")} />
              <span>
                <strong>Cloud-primary</strong>
                <small>Upload and verify eligible media, then allow verified local copies to be freed.</small>
              </span>
            </label>
          </fieldset>

          <dl className="media-storage-summary">
            <div><dt>Eligible</dt><dd>{status.summary.eligible_objects} · {formatBytes(status.summary.total_bytes)}</dd></div>
            <div><dt>Local</dt><dd>{status.summary.local_objects} · {formatBytes(status.summary.local_bytes)}</dd></div>
            <div><dt>Verified</dt><dd>{status.summary.verified_objects}</dd></div>
            <div><dt>Cloud-only</dt><dd>{status.summary.cloud_only_objects}</dd></div>
            <div><dt>Pending</dt><dd>{status.summary.pending_objects}</dd></div>
            <div><dt>Errors</dt><dd>{status.summary.error_objects}</dd></div>
          </dl>

          {status.objects.some((object) => object.last_error) && (
            <details className="media-storage-errors">
              <summary>Media storage errors</summary>
              <ul>
                {status.objects.filter((object) => object.last_error).map((object) => (
                  <li key={object.id}>
                    <strong>{object.role.replace(/_/g, " ")}</strong>
                    {" — "}{object.last_error}
                  </li>
                ))}
              </ul>
            </details>
          )}

          {status.summary.excluded_objects > 0 && (
            <p className="meta">
              {status.summary.excluded_objects} local-only or non-media object
              {status.summary.excluded_objects === 1 ? " is" : "s are"} excluded.
              Authentication data and the original paper PDF are never eligible.
            </p>
          )}

          {status.policy.mode === "cloud_primary" && (
            <div className="row">
              <button type="button" onClick={() => void runAction("sync")}
                disabled={disabled || Boolean(pending) || !status.target}>
                {pending === "sync" ? "Queuing…" : "Sync and verify media"}
              </button>
              <button type="button" onClick={() => void runAction("evict")}
                disabled={
                  disabled ||
                  Boolean(pending) ||
                  !status.target ||
                  status.summary.verified_objects === 0
                }>
                {pending === "evict" ? "Queuing…" : "Free verified local copies"}
              </button>
            </div>
          )}
          {status.policy.mode === "keep_local" && (
            <div className="row">
              {status.summary.restorable_objects > 0 && (
                <button type="button" onClick={() => void runAction("restore")}
                  disabled={disabled || Boolean(pending) || !status.target}>
                  {pending === "restore" ? "Queuing…" : "Restore missing cloud media"}
                </button>
              )}
              {status.target && (
                  <button type="button" className="danger"
                    onClick={() => void runAction("purge")}
                    disabled={disabled || Boolean(pending)}>
                    {pending === "purge" ? "Queuing…" : "Remove remote media copies"}
                  </button>
                )}
            </div>
          )}
          <p className="meta">
            Pipeline work restores cloud-only media locally before Whisper, FFmpeg,
            or playback uses it. A remote copy is never sufficient for
            eviction until its identity, size, and checksum have been recorded and verified.
            Restored files remain local until you free verified copies again.
          </p>
        </>
      )}
    </section>
  );
}
