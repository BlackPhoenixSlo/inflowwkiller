"use client";

/**
 * useAllModelsInclude — persisted "which accounts participate in the
 * all-models aggregate?" set.
 *
 * Storage: localStorage key `chatterly:allModelsExclude` as JSON-encoded
 * string[] of account ids the user has EXCLUDED. Everything else is
 * included by default — critically, that covers accounts that don't
 * exist yet. The previous encoding (an include-list materialized from
 * the account roster on first toggle) silently dropped any model
 * captured after that moment, or whose session was down right then:
 * "not in the list" read as excluded, and nothing in the UI said so.
 * With an exclude-list, absence means included, so new models always
 * surface and only explicit unchecks are remembered.
 *
 * Missing / empty / unparseable → nothing excluded (zero-config: a
 * fresh install sees every model). The legacy include-list key is
 * deleted on first read rather than migrated — translating it would
 * need the full account universe, and its failure mode (models the
 * user never unchecked staying hidden) is exactly the bug this
 * inversion removes. Cost: a previously-excluded model reappears once
 * per browser until it's re-unchecked.
 *
 * Surfaces:
 *   • ScopeSwitcher row checkmarks (TopNav).
 *   • MassMessageComposer all-models broadcast chip strip.
 *   • useChatList "all" branch filter — accounts dropped from the set are
 *     omitted from the fan-out + accountKey.
 */

import { useCallback, useEffect, useMemo, useState } from "react";

const STORAGE_KEY = "chatterly:allModelsExclude";
const LEGACY_INCLUDE_KEY = "chatterly:allModelsInclude";
const EVENT = "chatterly-all-models-include-change";

let legacyKeyCleared = false;

function read(): string[] {
  if (typeof window === "undefined") return [];
  if (!legacyKeyCleared) {
    legacyKeyCleared = true;
    try { window.localStorage.removeItem(LEGACY_INCLUDE_KEY); } catch { /* noop */ }
  }
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.filter((x): x is string => typeof x === "string");
  } catch {
    return [];
  }
}

function write(next: string[]) {
  try {
    if (next.length === 0) window.localStorage.removeItem(STORAGE_KEY);
    else window.localStorage.setItem(STORAGE_KEY, JSON.stringify(next));
    window.dispatchEvent(new Event(EVENT));
  } catch { /* quota — silent */ }
}

export interface AllModelsInclude {
  /** Account ids the user has explicitly opted OUT of the aggregate. */
  excluded: Set<string>;
  /** Is `id` part of the aggregate? True unless explicitly excluded. */
  isIncluded: (id: string) => boolean;
  /** Toggle one id in/out of the exclude set. */
  toggle: (id: string) => void;
  /** Re-include everything: clears the whole exclude set, including ids
   *  for accounts not currently listed (e.g. session-dropped). */
  includeAll: () => void;
  /** Exclude every id in `ids` (adds to the exclude set). */
  excludeAll: (ids: string[]) => void;
}

export function useAllModelsInclude(): AllModelsInclude {
  const [list, setList] = useState<string[]>(() => read());

  useEffect(() => {
    const onChange = () => setList(read());
    window.addEventListener(EVENT, onChange);
    window.addEventListener("storage", onChange);
    return () => {
      window.removeEventListener(EVENT, onChange);
      window.removeEventListener("storage", onChange);
    };
  }, []);

  const excluded = useMemo(() => new Set(list), [list]);

  const isIncluded = useCallback(
    (id: string) => !excluded.has(id),
    [excluded],
  );

  // Compute from a fresh read() rather than the captured state: storage is
  // the source of truth shared by every hook instance, so two quick toggles
  // (possible while a heavy re-render delays the commit) each apply on top
  // of the other instead of the second clobbering the first.
  const toggle = useCallback((id: string) => {
    const next = new Set(read());
    if (next.has(id)) next.delete(id);
    else next.add(id);
    const arr = Array.from(next);
    setList(arr);
    write(arr);
  }, []);

  const includeAll = useCallback(() => {
    setList([]);
    write([]);
  }, []);

  const excludeAll = useCallback((ids: string[]) => {
    const next = new Set(read());
    for (const id of ids) next.add(id);
    const arr = Array.from(next);
    setList(arr);
    write(arr);
  }, []);

  return { excluded, isIncluded, toggle, includeAll, excludeAll };
}
