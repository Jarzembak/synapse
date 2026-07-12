import { useEffect, useState } from "react";

export type StreamStatus = "connecting" | "live" | "offline" | "stale";

/**
 * Turn the EventSource hook's connection flag into a user-facing state.
 *
 * A short grace period avoids flashing "offline" during a normal initial
 * connection. Once a snapshot has arrived, a disconnect is "stale" instead:
 * callers can keep showing the last snapshot as long as they label it clearly.
 */
export function useStreamStatus(
  connected: boolean,
  hasSnapshot: boolean,
  offlineDelayMs = 4000,
): StreamStatus {
  const [offline, setOffline] = useState(false);

  useEffect(() => {
    if (connected || hasSnapshot) {
      setOffline(false);
      return;
    }

    setOffline(false);
    const timer = window.setTimeout(() => setOffline(true), offlineDelayMs);
    return () => window.clearTimeout(timer);
  }, [connected, hasSnapshot, offlineDelayMs]);

  if (connected && hasSnapshot) return "live";
  if (hasSnapshot) return "stale";
  return offline ? "offline" : "connecting";
}
