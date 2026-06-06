"use client";

/**
 * useAllModelsInclude — persisted "which accounts participate in the
 * all-models aggregate?" set.
 *
 * Storage: localStorage key `chatterly:allModelsInclude` as JSON-encoded
 * string[]. Missing / empty / unparseable → degenerate guard:
 * `isIncluded(_) === true` for every account. This preserves the
 * zero-config UX (a fresh install sees every model) and means "uncheck
 * everything" silently falls back to "include all" instead of blanking
 * the inbox.
 *
 * Surfaces:
 *   • ScopeSwitcher row checkmarks (TopNav).
 *   • MassMessageComposer all-models broadcast chip strip.
 *   • useChatList "all" branch filter — accounts dropped from the set are
 *     omitted from the fan-out + accountKey.
 */

import { useCallback, useEffect, useMemo, useState } from "react";

const STORAGE_KEY = "chatterly:allModelsInclude";
const EVENT = "chatterly-all-models-include-change";

function read(): string[] {
  if (typeof window === "undefined") return [];
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
  /** Set of account ids the user has explicitly opted into the aggregate.
   *  Empty → treat as "include everything" (degenerate guard). */
  included: Set<string>;
  /** Is `id` part of the aggregate? True when set is empty (zero-config). */
  isIncluded: (id: string) => boolean;
  /** Toggle one id. If the toggle would empty the set after a non-empty
   *  state, persists the explicit empty (still degenerate-included). */
  toggle: (id: string, allAccountIds: string[]) => void;
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

  const included = useMemo(() => new Set(list), [list]);

  const isIncluded = useCallback(
    (id: string) => included.size === 0 || included.has(id),
    [included],
  );

  const toggle = useCallback(
    (id: string, allAccountIds: string[]) => {
      // Materialize the implicit "all" set on first toggle so the user's
      // intent — "uncheck just this one" — survives the round-trip. If we
      // wrote `[id]` while empty, every other account would silently fall
      // out of the aggregate.
      const base = included.size === 0 ? new Set(allAccountIds) : new Set(included);
      if (base.has(id)) base.delete(id);
      else base.add(id);
      const next = Array.from(base);
      setList(next);
      write(next);
    },
    [included],
  );

  return { included, isIncluded, toggle };
}
