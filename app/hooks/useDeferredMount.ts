"use client";

/**
 * useDeferredMount — returns false on first render, true after a small
 * idle delay. Used to gate non-critical "decoration" queries (spend
 * chips, last-purchase, chat folders, badge counters) so they don't
 * compete with the chat list fetch for upstream OF bandwidth and the
 * relay's thread pool on cold load.
 *
 * Pattern at call site:
 *
 *     const ready = useDeferredMount();
 *     useQuery({ ..., enabled: ready && hasInputs });
 *
 * Default 600ms is long enough for the primary /chats fetch to land
 * and the first paint to settle, short enough that the chip "pop-in"
 * still feels live to the user. After the timer fires once, `ready`
 * stays true for the life of the component — so subsequent re-renders
 * (pagination, scope switch) fire decoration queries immediately.
 */

import { useEffect, useState } from "react";

export function useDeferredMount(ms = 600): boolean {
  const [ready, setReady] = useState(false);
  useEffect(() => {
    if (ready) return;
    // Use requestIdleCallback when available so we yield to any
    // already-in-flight work; fall back to setTimeout on Safari.
    const win = window as Window & {
      requestIdleCallback?: (cb: () => void, opts?: { timeout: number }) => number;
      cancelIdleCallback?: (id: number) => void;
    };
    if (win.requestIdleCallback) {
      const id = win.requestIdleCallback(() => setReady(true), { timeout: ms });
      return () => win.cancelIdleCallback?.(id);
    }
    const t = window.setTimeout(() => setReady(true), ms);
    return () => window.clearTimeout(t);
  }, [ms, ready]);
  return ready;
}
