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

// Module-level source of truth. localStorage is best-effort persistence only —
// if its write throws (quota / private mode), same-tab instances still agree
// via this variable, so the button can never show ON while the thread stays
// untranslated. null = not read yet this page load.
let current: boolean | null = null;

export function readTranslateMode(): boolean {
  if (current !== null) return current;
  if (typeof window === "undefined") return false;
  try {
    current = window.localStorage.getItem(STORAGE_KEY) === "1";
  } catch {
    current = false;
  }
  return current;
}

export function useTranslateMode(): [boolean, (next: boolean) => void] {
  const [val, setVal] = useState<boolean>(() => readTranslateMode());

  useEffect(() => {
    const onLocal = () => setVal(readTranslateMode());
    // Cross-tab flip: another tab wrote localStorage — invalidate the module
    // cache so the re-read picks up the other tab's value.
    const onStorage = () => { current = null; setVal(readTranslateMode()); };
    window.addEventListener(EVENT, onLocal);
    window.addEventListener("storage", onStorage);
    return () => {
      window.removeEventListener(EVENT, onLocal);
      window.removeEventListener("storage", onStorage);
    };
  }, []);

  function update(next: boolean) {
    current = next;
    setVal(next);
    try {
      if (next) window.localStorage.setItem(STORAGE_KEY, "1");
      else window.localStorage.removeItem(STORAGE_KEY);
    } catch { /* quota — silent; module state still flipped */ }
    window.dispatchEvent(new Event(EVENT));
  }

  return [val, update];
}
