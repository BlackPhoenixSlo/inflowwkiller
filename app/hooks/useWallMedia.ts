"use client";

/**
 * useWallMedia — set of vault media ids that appear in the model's wall
 * posts. Drives the blue "posted on wall" ring in the VaultPicker.
 *
 * Backed by SQLite on the server (`wall_media` + `wall_scan_state`):
 * each call to /admin/vault/wall-media walks OF's /posts feed
 * incrementally (forward to refresh, backward to backfill older
 * history) and persists the union. Subsequent calls are cheap because
 * the canonical set lives server-side — we just re-read it.
 *
 * Auto-backfill: when `has_more=true`, we schedule one more refetch
 * after a short delay so a creator with thousands of posts gradually
 * gets fully covered across picker opens, instead of being silently
 * capped at the first 250 posts (the old in-memory behavior).
 *
 * Persisted via the PersistQueryClient layer (`wall-media` is on the
 * persist allowlist) so popout windows + repeat tab opens hydrate
 * from localStorage without waiting on the network.
 */

import { useEffect, useMemo, useRef } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";

import { relay } from "@/lib/relay";
import { perfDelivered, perfError, perfLog, perfOpId } from "@/lib/perfLog";

interface WallMediaResp {
  media_ids: number[];
  /** { "<media_id>": price_cents } for media posted on the wall as a paid /
   *  PPV post. Only priced ids are present — free wall posts are absent
   *  (treated as 0). Drives the picker's "WALL $X.XX" badge. */
  media_prices?: Record<string, number>;
  scanned_posts: number;
  new_media_count?: number;
  has_more: boolean;
  fully_backfilled?: boolean;
  mode?: "first" | "backfill" | "refresh";
  last_scan_at?: string | null;
  newest_post_published_at?: string | null;
  oldest_post_published_at?: string | null;
}

export function useWallMedia(accountId: string | null, enabled = true) {
  const qc = useQueryClient();
  const queryKey = useMemo(() => ["wall-media", accountId] as const, [accountId]);

  const q = useQuery<WallMediaResp>({
    queryKey,
    enabled: enabled && !!accountId,
    queryFn: async ({ signal }) => {
      const qs = new URLSearchParams({ account_id: accountId! });
      // A manual ↻ refresh sends force=true so the walker wipes its
      // watermarks and re-walks from the top — the only path that
      // re-prices media already recorded at price 0 (e.g. rows created
      // before wall-post pricing existed). One-shot: cleared after use so
      // background/auto refetches stay cheap incremental walks.
      if (forceRef.current) {
        qs.set("force", "true");
        forceRef.current = false;
      }
      const opId = perfOpId("wall.media");
      perfLog(opId, "wall.media", "requested", { accountId });
      try {
        const r = await relay.get<WallMediaResp>(
          `/admin/vault/wall-media?${qs.toString()}`,
          { priority: "background" },
          signal,
        );
        perfDelivered(opId, "wall.media", {
          count: r.media_ids.length,
          new: r.new_media_count ?? 0,
          mode: r.mode,
          hasMore: r.has_more,
          backfilled: r.fully_backfilled,
        });
        return r;
      } catch (err) {
        perfError(opId, "wall.media", { message: (err as Error)?.message });
        throw err;
      }
    },
    // Server-side SQLite is now the source of truth; warm reads just
    // re-read the union. 7d staleTime — wall-post coverage on the picker
    // ring is purely cosmetic, and the canonical set lives in SQLite
    // anyway. The explicit ↻ refresh button + the auto-backfill effect
    // below still trigger fresh walks when needed.
    staleTime: 7 * 24 * 60 * 60 * 1000,
    refetchOnWindowFocus: false,
  });

  // Auto-backfill: schedule one more fetch shortly after the first lands
  // if `has_more=true`. Walks the next batch of older history. Bounded
  // by `MAX_AUTO_BACKFILLS` so a brand-new account with thousands of
  // posts doesn't drain rate-budget in one picker open — we'll resume
  // on the next session.
  const backfillsRef = useRef(0);
  // Set by refresh() so the next queryFn run appends force=true exactly once.
  const forceRef = useRef(false);
  const MAX_AUTO_BACKFILLS = 4;  // up to ~1000 extra posts per session
  useEffect(() => {
    if (!enabled || !accountId) return;
    if (!q.data?.has_more) return;
    if (q.isFetching) return;
    if (backfillsRef.current >= MAX_AUTO_BACKFILLS) return;
    backfillsRef.current += 1;
    // Slight delay so the user's vault grid finishes its first paint
    // before we re-tax the relay's per-account proxy slot.
    const t = setTimeout(() => {
      qc.invalidateQueries({ queryKey });
    }, 1500);
    return () => clearTimeout(t);
  }, [enabled, accountId, q.data?.has_more, q.isFetching, qc, queryKey]);

  // Reset the per-session counter when the account changes so a new
  // picker open for a different model gets its own backfill budget.
  useEffect(() => {
    backfillsRef.current = 0;
  }, [accountId]);

  const set = useMemo(
    () => new Set<number>(q.data?.media_ids ?? []),
    [q.data?.media_ids],
  );

  // media_id -> price_cents for paid wall posts (free posts absent). Keyed
  // by number so the picker can look up per-tile with `priceById.get(m.id)`.
  const priceById = useMemo(() => {
    const m = new Map<number, number>();
    const raw = q.data?.media_prices;
    if (raw) {
      for (const [k, v] of Object.entries(raw)) {
        const id = Number(k);
        const cents = Number(v);
        if (Number.isFinite(id) && Number.isFinite(cents) && cents > 0) {
          m.set(id, cents);
        }
      }
    }
    return m;
  }, [q.data?.media_prices]);

  const refresh = () => {
    backfillsRef.current = 0;
    forceRef.current = true;
    return q.refetch();
  };

  return {
    set,
    priceById,
    scanned: q.data?.scanned_posts ?? 0,
    hasMore: !!q.data?.has_more,
    fullyBackfilled: !!q.data?.fully_backfilled,
    lastScanAt: q.data?.last_scan_at ?? null,
    isLoading: q.isLoading,
    isFetching: q.isFetching,
    refresh,
  };
}
