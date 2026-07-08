import { useEffect, useRef } from "react";

/** Subscribe to a Server-Sent-Events endpoint with automatic reconnection.
 *
 * The browser's built-in EventSource permanently gives up after a *fatal*
 * error — most commonly a non-2xx response, e.g. the nginx 502 returned while
 * the api container restarts — leaving the stream dead until a full page
 * reload (this is what made the job ticker freeze on a stale "busy" state after
 * a redeploy). We take over reconnection ourselves so a dropped stream always
 * comes back. An idle watchdog also forces a reconnect if no message arrives
 * for a while, which catches silently half-open connections; it relies on the
 * server emitting the event periodically as a heartbeat even when nothing
 * changed.
 *
 * `onStatus(connected)` lets callers avoid rendering stale data while the
 * stream is down. Callbacks are held in refs so a caller passing fresh
 * closures each render doesn't tear down and rebuild the connection. */
export function useEventSource<T>(
  url: string,
  event: string,
  onMessage: (data: T) => void,
  onStatus?: (connected: boolean) => void,
) {
  const msgRef = useRef(onMessage);
  const statusRef = useRef(onStatus);
  msgRef.current = onMessage;
  statusRef.current = onStatus;

  useEffect(() => {
    let es: EventSource | null = null;
    let retry: ReturnType<typeof setTimeout> | undefined;
    let watchdog: ReturnType<typeof setTimeout> | undefined;
    let stopped = false;

    const setConnected = (c: boolean) => statusRef.current?.(c);

    function bumpWatchdog() {
      clearTimeout(watchdog);
      // the server heartbeats at least every ~15s; 45s of total silence means
      // the connection is wedged (half-open) — drop it and reconnect
      watchdog = setTimeout(reconnect, 45000);
    }

    function reconnect() {
      es?.close();
      if (stopped) return;
      setConnected(false);
      clearTimeout(retry);
      retry = setTimeout(connect, 2000);
    }

    function connect() {
      if (stopped) return;
      // not "connected" until fresh data actually arrives — reporting connected
      // on 'open' (before the first message) would let a caller briefly re-show
      // its stale pre-disconnect state on every reconnect.
      setConnected(false);
      es = new EventSource(url);
      bumpWatchdog();  // arm now so a socket that opens but never delivers is caught
      es.addEventListener("open", bumpWatchdog);
      es.addEventListener(event, (e) => {
        setConnected(true);
        bumpWatchdog();
        try {
          msgRef.current(JSON.parse((e as MessageEvent).data));
        } catch {
          /* ignore a malformed frame; the next message replaces it */
        }
      });
      // EventSource won't auto-retry a fatal error (e.g. a 502 during restart),
      // so drive the reconnect ourselves with a short backoff.
      es.onerror = () => reconnect();
    }

    connect();
    return () => {
      stopped = true;
      es?.close();
      clearTimeout(retry);
      clearTimeout(watchdog);
    };
  }, [url, event]);
}
