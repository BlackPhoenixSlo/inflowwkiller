"use client";

import { useEffect } from "react";

/**
 * Open a tabbed page on the tab named in the URL — `/growth?tab=promotion`.
 *
 * Exists so the help assistant's click paths can be links instead of
 * instructions to read and re-follow by hand (components/assistant/answerLinks.ts).
 *
 * READS `window.location` IN AN EFFECT, not `useSearchParams`. The Next hook
 * suspends, which would force every caller to grow a `<Suspense>` boundary and
 * a loading fallback for what is one optional string; the effect costs one
 * extra render on mount instead. It also keeps the server render identical to
 * the first client render, so there is no hydration mismatch to reconcile.
 *
 * Deliberately mount-only: it seeds the tab, it does not own it. Once the page
 * is open the tab strip is the source of truth, so a later `?tab=` in the URL
 * (from a back-navigation) never yanks the reader off the tab they clicked.
 */
export function useTabParam<T extends string>(
  param: string,
  isValid: (v: string) => v is T,
  onFound: (tab: T) => void,
): void {
  useEffect(() => {
    let raw: string | null = null;
    try {
      raw = new URLSearchParams(window.location.search).get(param);
    } catch {
      return; // no URL to read (jsdom without a location, SSR shim)
    }
    if (raw && isValid(raw)) onFound(raw);
    // Mount-only by design — see the note above.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
}
