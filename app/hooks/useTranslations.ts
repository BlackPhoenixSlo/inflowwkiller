"use client";

/**
 * useTranslations — batch-fetch English translations for chat bubble texts
 * via the relay's POST /admin/translate (Google's free gtx endpoint,
 * proxied server-side because the browser can't reach it cross-origin).
 *
 * Keyed by the STRIPPED text (not message id) so translations survive
 * optimistic-id churn and repeat texts across fans cost one fetch. The
 * module-level cache lives for the tab's lifetime; the relay keeps its own
 * cross-session cache on top.
 *
 * A `null` cache entry means "tried and failed" — we deliberately don't
 * retry within the session so a rate-limited Google endpoint degrades to
 * originals instead of a request storm.
 */

import { useEffect, useState } from "react";

import { relay } from "@/lib/relay";

export interface BubbleTranslation {
  /** English translation of the bubble text. */
  text: string;
  /** Detected ISO 639-1 source language ("es", "en", …). */
  lang: string;
}

const CHUNK = 40;

const cache = new Map<string, BubbleTranslation | null>();
const inFlight = new Set<string>();

// ── Live status for the header button ───────────────────────────────
// Module-level so ChatSurface's 🌐 button can show "translating…" / "⚠ failed"
// without plumbing state up from MessageList. Same window-event pattern as
// useTranslateMode.
const STATUS_EVENT = "chatterly-translate-status-change";
let _activeBatches = 0;
let _lastBatchFailed = false;

function setStatus(delta: number, failed?: boolean) {
  _activeBatches = Math.max(0, _activeBatches + delta);
  if (failed !== undefined) _lastBatchFailed = failed;
  window.dispatchEvent(new Event(STATUS_EVENT));
}

export interface TranslateStatus {
  /** True while at least one batch is being fetched. */
  busy: boolean;
  /** True when the most recent batch errored (endpoint unreachable etc.). */
  failed: boolean;
}

export function useTranslateStatus(): TranslateStatus {
  const [status, setSt] = useState<TranslateStatus>({ busy: false, failed: false });
  useEffect(() => {
    const onChange = () =>
      setSt({ busy: _activeBatches > 0, failed: _lastBatchFailed });
    window.addEventListener(STATUS_EVENT, onChange);
    return () => window.removeEventListener(STATUS_EVENT, onChange);
  }, []);
  return status;
}

export function useTranslations(
  texts: string[],
  enabled: boolean,
): Map<string, BubbleTranslation | null> {
  const [, bump] = useState(0);

  useEffect(() => {
    if (!enabled) return;
    const missing = Array.from(new Set(texts)).filter(
      (t) => t && !cache.has(t) && !inFlight.has(t),
    );
    if (!missing.length) return;
    missing.forEach((t) => inFlight.add(t));
    let cancelled = false;
    (async () => {
      for (let i = 0; i < missing.length; i += CHUNK) {
        const chunk = missing.slice(i, i + CHUNK);
        setStatus(+1);
        try {
          const resp = await relay.post<{ results: Array<BubbleTranslation | null> }>(
            "/admin/translate",
            { texts: chunk, target: "en" },
          );
          chunk.forEach((t, j) => cache.set(t, resp.results?.[j] ?? null));
          setStatus(-1, false);
        } catch {
          chunk.forEach((t) => cache.set(t, null));
          setStatus(-1, true);
        } finally {
          chunk.forEach((t) => inFlight.delete(t));
        }
        if (cancelled) return;
        bump((v) => v + 1);
      }
    })();
    return () => { cancelled = true; };
  }, [texts, enabled]);

  return cache;
}
