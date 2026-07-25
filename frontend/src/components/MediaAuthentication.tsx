import { useCallback, useEffect, useRef, useState } from "react";
import { api, fmtDateTime, MediaAuthStatus } from "../api";

interface Props {
  projectId: number;
  disabled?: boolean;
  onPipelineChanged?: () => Promise<void> | void;
}

let authenticationPopup: Window | null = null;

function message(error: unknown): string {
  return error instanceof Error ? error.message : "Unexpected source-access error";
}

export function openAuthenticationWindow(): Window | null {
  const popup = window.open(
    "about:blank",
    "synapse-source-login",
    "popup,width=1460,height=980,resizable=yes,scrollbars=yes",
  );
  if (popup) {
    authenticationPopup = popup;
    try {
      popup.opener = null;
      popup.document.title = "Synapse source login";
      popup.document.body.textContent = "Starting the isolated sign-in browser…";
    } catch {
      // The placeholder is only cosmetic; navigation still works if the
      // browser refuses access to its initial document.
    }
  }
  return popup;
}

export function closeAuthenticationWindow(): void {
  try {
    authenticationPopup?.close();
  } finally {
    authenticationPopup = null;
  }
}

export function navigateAuthenticationWindow(
  popup: Window,
  browserUrl: string,
): void {
  popup.location.replace(new URL(browserUrl, window.location.origin).toString());
  popup.focus();
}

export default function MediaAuthentication({
  projectId,
  disabled = false,
  onPipelineChanged,
}: Props) {
  const [status, setStatus] = useState<MediaAuthStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [working, setWorking] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [cookieFile, setCookieFile] = useState<File | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  const refresh = useCallback(async () => {
    try {
      const next = await api<MediaAuthStatus>(`/projects/${projectId}/auth`);
      setStatus(next);
      setError("");
    } catch (caught) {
      setError(message(caught));
    } finally {
      setLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  async function start() {
    setError("");
    setNotice("");
    const popup = openAuthenticationWindow();
    if (!popup) {
      setError("Allow pop-ups for Synapse, then try signing in again.");
      return;
    }
    setWorking("start");
    try {
      const next = await api<MediaAuthStatus>(
        `/projects/${projectId}/auth/browser`,
        { method: "POST" },
      );
      if (!next.browser_url) {
        throw new Error("The authentication browser did not provide a view URL.");
      }
      navigateAuthenticationWindow(popup, next.browser_url);
      setStatus(next);
      setNotice("Sign in in the new window, return here, then save the session.");
    } catch (caught) {
      popup.close();
      setError(message(caught));
    } finally {
      setWorking("");
    }
  }

  function reopen() {
    if (!status?.browser_url) return;
    const popup = openAuthenticationWindow();
    if (!popup) {
      setError("Allow pop-ups for Synapse, then reopen the sign-in browser.");
      return;
    }
    navigateAuthenticationWindow(popup, status.browser_url);
  }

  async function complete(startIngest: boolean) {
    setWorking(startIngest ? "complete-ingest" : "complete");
    setError("");
    setNotice("");
    try {
      const next = await api<MediaAuthStatus>(
        `/projects/${projectId}/auth/browser/complete`,
        { method: "POST" },
      );
      setStatus(next);
      closeAuthenticationWindow();
      if (startIngest) {
        await api(`/projects/${projectId}/run/ingest`, { method: "POST" });
        await onPipelineChanged?.();
        setNotice("Source access saved and media ingest queued.");
      } else {
        setNotice("Source access saved. The temporary browser profile was destroyed.");
      }
    } catch (caught) {
      setError(message(caught));
    } finally {
      setWorking("");
    }
  }

  async function cancel() {
    setWorking("cancel");
    setError("");
    setNotice("");
    try {
      const next = await api<MediaAuthStatus>(
        `/projects/${projectId}/auth/browser`,
        { method: "DELETE" },
      );
      setStatus(next);
      closeAuthenticationWindow();
      setNotice("Interactive sign-in canceled and its temporary browser destroyed.");
    } catch (caught) {
      setError(message(caught));
    } finally {
      setWorking("");
    }
  }

  async function clear() {
    if (!confirm("Clear this project's saved source cookies and authorized URL?")) return;
    setWorking("clear");
    setError("");
    setNotice("");
    try {
      const next = await api<MediaAuthStatus>(
        `/projects/${projectId}/cookies`,
        { method: "DELETE" },
      );
      setStatus(next);
      setNotice("Saved source access cleared.");
    } catch (caught) {
      setError(message(caught));
    } finally {
      setWorking("");
    }
  }

  async function uploadCookies() {
    if (!cookieFile) return;
    setWorking("upload");
    setError("");
    setNotice("");
    const form = new FormData();
    form.append("file", cookieFile);
    try {
      await api(`/projects/${projectId}/cookies`, {
        method: "POST",
        body: form,
      });
      setCookieFile(null);
      if (fileRef.current) fileRef.current.value = "";
      await refresh();
      setNotice("cookies.txt uploaded for this project.");
    } catch (caught) {
      setError(message(caught));
    } finally {
      setWorking("");
    }
  }

  const busy = disabled || Boolean(working);
  const saved = Boolean(status?.cookies_present || status?.captured_at);

  return (
    <section className="card source-access">
      <div className="source-access-heading">
        <div>
          <span className="eyebrow">Authenticated media</span>
          <h3>Source access</h3>
        </div>
        <span className={`source-access-state ${status?.active ? "waiting" : saved ? "ready" : ""}`}>
          {loading
            ? "Checking…"
            : status?.cleanup_pending
              ? "Cleanup pending"
              : status?.active
              ? "Waiting for sign-in"
              : saved
                ? "Access saved"
                : "Not signed in"}
        </span>
      </div>

      <p>
        For Zoom, Udemy, X, and other login-protected sources, sign in inside a
        disposable Chromium session. Synapse saves target-site cookies and the
        final authorized URL—not your password or browser profile.
      </p>

      {status?.captured_at && (
        <p className="meta">
          Captured {fmtDateTime(status.captured_at)}
          {status.authenticated_host ? ` for ${status.authenticated_host}` : ""}
          {status.cookie_count !== undefined ? ` · ${status.cookie_count} cookies` : ""}
        </p>
      )}

      {!loading && !status?.available && (
        <p className="warning">
          The interactive browser is unavailable. Start the bundled
          <code> auth-browser </code> service or use cookies.txt below.
        </p>
      )}
      {status?.cleanup_pending && (
        <p className="warning">
          The browser is still starting or a previous cleanup could not be
          confirmed. If it does not become available, cancel this sign-in and
          retry.
        </p>
      )}

      <div className="source-access-actions">
        {status?.active ? (
          <>
            <button type="button" onClick={reopen} disabled={busy || !status.browser_url}>
              Open sign-in browser
            </button>
            <button
              type="button"
              onClick={() => void complete(false)}
              disabled={busy || status.cleanup_pending}
            >
              {working === "complete" ? "Saving…" : "Use this sign-in"}
            </button>
            <button
              type="button"
              className="primary"
              onClick={() => void complete(true)}
              disabled={busy || status.cleanup_pending}
            >
              {working === "complete-ingest" ? "Saving…" : "Save & start ingest"}
            </button>
            <button type="button" className="linkish" onClick={() => void cancel()} disabled={busy}>
              Cancel
            </button>
          </>
        ) : (
          <button
            type="button"
            className="primary"
            onClick={() => void start()}
            disabled={busy || !status?.available}
          >
            {working === "start" ? "Starting browser…" : saved ? "Sign in again" : "Sign in to source"}
          </button>
        )}
        {saved && !status?.active && (
          <button type="button" className="linkish danger" onClick={() => void clear()} disabled={busy}>
            {working === "clear" ? "Clearing…" : "Clear sign-in"}
          </button>
        )}
      </div>

      <details className="source-access-advanced">
        <summary>Advanced fallback: upload cookies.txt</summary>
        <div className="cookies">
          <input
            type="file"
            accept=".txt,text/plain"
            ref={fileRef}
            aria-label="Netscape cookies file"
            onChange={(event) => setCookieFile(event.target.files?.[0] ?? null)}
          />
          <button
            type="button"
            onClick={() => void uploadCookies()}
            disabled={!cookieFile || busy}
          >
            {working === "upload" ? "Uploading…" : "Upload cookies"}
          </button>
        </div>
      </details>

      <p className="meta">
        Session cookies are credentials. They remain project-local and are
        excluded from backups and cloud sync. Authenticated media may still use
        your configured cloud processing or media-sync settings. This workflow
        cannot bypass DRM or a platform owner's download restrictions.
      </p>
      {notice && <p className="notice" role="status">{notice}</p>}
      {error && <p className="error" role="alert">{error}</p>}
    </section>
  );
}
