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
        try {
          const resp = await relay.post<{ results: Array<BubbleTranslation | null> }>(
            "/admin/translate",
            { texts: chunk, target: "en" },
          );
          chunk.forEach((t, j) => cache.set(t, resp.results?.[j] ?? null));
        } catch {
          chunk.forEach((t) => cache.set(t, null));
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
