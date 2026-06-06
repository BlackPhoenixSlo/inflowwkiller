"use client";

import { useEffect } from "react";

/** Set document.title and keep it set. Next's layout reapplies its static
 *  metadata title on re-renders/HMR, so a plain `document.title = X` loses.
 *  A MutationObserver on <head> re-asserts our value whenever it changes. */
export function useDocumentTitle(title: string, enabled = true): void {
  useEffect(() => {
    if (!enabled) return;
    document.title = title;
    const obs = new MutationObserver(() => {
      if (document.title !== title) document.title = title;
    });
    obs.observe(document.head, { childList: true, subtree: true, characterData: true });
    return () => { obs.disconnect(); document.title = "Fastt"; };
  }, [title, enabled]);
}
