"use client";

/**
 * useMediaCache — id → VaultMedia resolver shared across an editor screen.
 *
 * The catalog rows (ScriptsTab) store only numeric vault ids. To render a
 * thumbnail we need the full VaultMedia (thumb url + type). Two writers fill
 * one shared cache:
 *
 *   • prime(media[])  — called on every VaultPicker confirm. Anything picked
 *     from the vault lands in the cache instantly (the picker already carries
 *     files.thumb.url), so freshly-attached media previews with no fetch.
 *
 *   • request(ids[])  — rows register their saved ids. A single tab-level
 *     resolver pages the vault (via the existing useVaultMedia, bounded to a
 *     few pages) and fills the cache for any id it finds. Fresh uploads sit at
 *     the top of the vault, so they resolve from page 1; ids past the page cap
 *     degrade to a bare id chip (re-pick to preview). No backend change.
 *
 * One resolver for the whole provider — never re-pages the vault per row. The
 * resolver shares the useVaultMedia queryKey with the open VaultPicker, so the
 * two never double-fetch.
 */

import {
  createContext, useCallback, useContext, useEffect, useMemo, useRef, useState,
} from "react";

import type { VaultMedia } from "@/lib/relay";
import { useVaultMedia } from "@/hooks/useVaultMedia";

/** ~5 pages of 24. Bounds how far we'll page the vault chasing a saved id
 *  before giving up (it then renders as an id chip until re-picked). */
const MAX_RESOLVE_ITEMS = 120;

interface MediaCacheValue {
  /** Resolved media for an id, or undefined while unresolved. */
  get: (id: number) => VaultMedia | undefined;
  /** Seed the cache with already-resolved media (picker confirm). */
  prime: (media: VaultMedia[]) => void;
  /** Ask the resolver to fetch thumbnails for these saved ids. */
  request: (ids: number[]) => void;
}

const NOOP: MediaCacheValue = {
  get: () => undefined,
  prime: () => {},
  request: () => {},
};

const MediaCacheContext = createContext<MediaCacheValue | null>(null);

export function useMediaCache(): MediaCacheValue {
  return useContext(MediaCacheContext) ?? NOOP;
}

export function MediaCacheProvider({ accountId, children }: {
  accountId: string | null;
  children: React.ReactNode;
}) {
  const [cache, setCache] = useState<Map<number, VaultMedia>>(() => new Map());
  const [requested, setRequested] = useState<Set<number>>(() => new Set());
  // Read inside callbacks without making them depend on the latest cache.
  const cacheRef = useRef(cache);
  cacheRef.current = cache;

  const prime = useCallback((media: VaultMedia[]) => {
    if (!media.length) return;
    setCache((prev) => {
      let changed = false;
      const next = new Map(prev);
      for (const m of media) {
        if (m && typeof m.id === "number" && !next.has(m.id)) {
          next.set(m.id, m);
          changed = true;
        }
      }
      return changed ? next : prev;
    });
  }, []);

  const request = useCallback((ids: number[]) => {
    setRequested((prev) => {
      let changed = false;
      let next = prev;
      for (const id of ids) {
        if (!Number.isFinite(id)) continue;
        if (prev.has(id) || cacheRef.current.has(id)) continue;
        if (!changed) { next = new Set(prev); changed = true; }
        next.add(id);
      }
      return changed ? next : prev;
    });
  }, []);

  // Ids still wanted but not yet resolved — drives whether we page at all.
  const missing = useMemo(() => {
    for (const id of requested) if (!cache.has(id)) return true;
    return false;
  }, [requested, cache]);

  const vault = useVaultMedia({ accountId, type: "all", enabled: missing });

  // Fold every loaded vault page into the cache. prime() is a no-op when a
  // page brings nothing new, so re-running on each render is cheap.
  useEffect(() => {
    if (vault.items.length) prime(vault.items);
  }, [vault.items, prime]);

  // Page deeper while there are still unresolved ids and headroom left. The
  // guards (not-fetching, hasMore, under cap) make this a self-terminating
  // chain: each landed page re-runs the effect until missing clears.
  useEffect(() => {
    if (!missing || !accountId) return;
    if (vault.isFetching || vault.isFetchingNextPage) return;
    if (!vault.hasMore) return;
    if (vault.items.length >= MAX_RESOLVE_ITEMS) return;
    vault.loadMore();
  }, [missing, accountId, vault.isFetching, vault.isFetchingNextPage,
      vault.hasMore, vault.items.length, vault.loadMore]);

  const value = useMemo<MediaCacheValue>(
    () => ({ get: (id) => cache.get(id), prime, request }),
    [cache, prime, request],
  );

  return (
    <MediaCacheContext.Provider value={value}>
      {children}
    </MediaCacheContext.Provider>
  );
}
