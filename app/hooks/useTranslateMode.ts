"use client";

/**
 * useTranslateMode — persisted "🌐 translate to English" toggle for the chat
 * thread. Off (default): bubbles render the original text. On: non-English
 * bubbles show the English translation prefixed with a colored "(es)" tag
 * (see useTranslations for the fetch/cache layer).
 *
 * Same custom-event + storage-event pattern as useCompactMedia so the header
 * button (ChatSurface) and every mounted MessageList stay in sync.
 */

import { useEffect, useState } from "react";

const STORAGE_KEY = "chatterly:translate-en";
const EVENT = "chatterly-translate-mode-change";

export function readTranslateMode(): boolean {
  if (typeof window === "undefined") return false;
  return window.localStorage.getItem(STORAGE_KEY) === "1";
}

export function useTranslateMode(): [boolean, (next: boolean) => void] {
  const [val, setVal] = useState<boolean>(() => readTranslateMode());

  useEffect(() => {
    const onChange = () => setVal(readTranslateMode());
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
      if (next) window.localStorage.setItem(STORAGE_KEY, "1");
      else window.localStorage.removeItem(STORAGE_KEY);
      window.dispatchEvent(new Event(EVENT));
    } catch { /* quota — silent */ }
  }

  return [val, update];
}
