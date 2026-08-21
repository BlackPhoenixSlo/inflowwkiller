"use client";

/**
 * useSmartListAudience — the mass composer's Smart-List include/exclude cluster.
 *
 * A Smart List is a saved fan-id segment (spend / recency / status / tag). This
 * hook owns the two picked sets, keeps them mutually exclusive (a list is
 * include OR exclude, never both), resets them when the account / all-models
 * scope changes, and resolves a picked set to explicit fan ids on send. Lifting
 * it out of MassMessageComposer keeps the composer about message + audience-chip
 * assembly and gives the segment mechanics one testable home next to
 * useSmartLists.
 *
 * The query is disabled (null account) in all-models and compose-into-rule
 * modes: segments are per-account, and useInRule() freezes a rule payload
 * without resolving them, so offering the picker there would silently drop the
 * pick.
 */

import { useCallback, useEffect, useState } from "react";

import { useSmartLists, resolveSmartList, type SmartList } from "@/hooks/useSmartLists";

/** Toggle `id` in a copy of `s` — add if absent, remove if present. */
function toggled(s: Set<number>, id: number): Set<number> {
  const next = new Set(s);
  if (next.has(id)) next.delete(id); else next.add(id);
  return next;
}

/** Drop `id` from `s`, preserving the reference when `id` isn't there — the
 *  set that didn't change shouldn't force a re-render. */
function without(s: Set<number>, id: number): Set<number> {
  if (!s.has(id)) return s;
  const next = new Set(s); next.delete(id); return next;
}

export interface SmartListAudience {
  /** Saved segments for the picker ([] until loaded, or while disabled). */
  lists: SmartList[];
  include: Set<number>;
  exclude: Set<number>;
  /** Toggle a list in `include`; a list can't be in both, so this also drops
   *  it from `exclude`. */
  toggleInclude: (id: number) => void;
  /** Toggle a list in `exclude`; symmetric — also drops it from `include`. */
  toggleExclude: (id: number) => void;
  /** Resolve a picked set to a de-duped fan-id array (empty for an empty set). */
  resolve: (ids: Set<number>) => Promise<number[]>;
}

export function useSmartListAudience(
  accountId: string | null,
  { allModels, composeMode }: { allModels: boolean; composeMode: boolean },
): SmartListAudience {
  const listsQ = useSmartLists(allModels || composeMode ? null : accountId);
  const [include, setInclude] = useState<Set<number>>(new Set());
  const [exclude, setExclude] = useState<Set<number>>(new Set());

  // Segments are per-account — a scope change invalidates the current picks.
  useEffect(() => {
    setInclude(new Set());
    setExclude(new Set());
  }, [accountId, allModels]);

  const toggleInclude = useCallback((id: number) => {
    setInclude((prev) => toggled(prev, id));
    setExclude((prev) => without(prev, id));
  }, []);
  const toggleExclude = useCallback((id: number) => {
    setExclude((prev) => toggled(prev, id));
    setInclude((prev) => without(prev, id));
  }, []);

  const resolve = useCallback(async (ids: Set<number>): Promise<number[]> => {
    if (ids.size === 0) return [];
    const arrays = await Promise.all([...ids].map((id) => resolveSmartList(id)));
    return Array.from(new Set(arrays.flat()));
  }, []);

  return {
    lists: listsQ.data ?? [],
    include,
    exclude,
    toggleInclude,
    toggleExclude,
    resolve,
  };
}
