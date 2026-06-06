"use client";

/**
 * useCompactMedia — persisted "shrink chat media previews" toggle.
 *
 * Off (default): photos/videos/GIFs render at the normal in-bubble size
 * (max-h ~48 / max-w ~260).
 * On: previews are smaller (max-h ~32 / max-w ~160) so long chats with
 * lots of attached media stay scannable without all the scrolling.
 *
 * Same custom-event + storage-event pattern as useBlurMode so multiple
 * mounted bubbles re-render in sync after a flip.
 */

import { useEffect, useState } from "react";

const STORAGE_KEY = "chatterly:compact-media";
const EVENT = "chatterly-compact-media-change";

export function readCompactMedia(): boolean {
  if (typeof window === "undefined") return true;
  return window.localStorage.getItem(STORAGE_KEY) !== "0";
}

export function useCompactMedia(): [boolean, (next: boolean) => void] {
  const [val, setVal] = useState<boolean>(() => readCompactMedia());

  useEffect(() => {
    const onChange = () => setVal(readCompactMedia());
    window.addEventListener(EVENT, onChange);
    window.addEventListener("storage", onChange);
    return () => {
      window.removeEventListener(EVENT, onChange);
      window.removeEventListener("storage", onChange);
    };
  }, []);

  function update(next: boolean) {
    setVal(next);
    try {
      if (next) window.localStorage.removeItem(STORAGE_KEY);
      else window.localStorage.setItem(STORAGE_KEY, "0");
      window.dispatchEvent(new Event(EVENT));
    } catch { /* quota — silent */ }
  }

  return [val, update];
}
