"use client";

/**
 * useRosterCounts — per-model inbox badge counts for the left roster strip.
 *
 *   • unread    → number of CONVERSATIONS with unread messages (blue badge:
 *                 fans waiting to be read).
 *   • owe_reply → conversations READ but the fan spoke last, i.e. we owe a
 *                 reply (orange badge: seen, not yet answered).
 *
 * Backed by GET /admin/accounts/roster-counts, scoped to the signed-in
 * principal. Both principals get badges: an owner sees its own models, a
 * chatter sees the union across every linked owner (the same set its roster
 * strip already renders). Polls every 60s to match the chat-list cadence.
 */

import { useCallback } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";

import { relay } from "@/lib/relay";
import { useOptionalUser } from "@/contexts/UserContext";
import { useOptionalChatter } from "@/contexts/ChatterContext";

export interface RosterCount {
  unread: number;
  owe_reply: number;
}

interface RosterCountsResp {
  counts: Record<string, RosterCount>;
}

const EMPTY: Record<string, RosterCount> = {};

/** Per-principal cache key. An owner's counts must never bleed into a chatter's
 *  view (or vice-versa) on an identity switch in the same browser. */
export function rosterCountsKey(principalId: string): readonly [string, string] {
  return ["roster-counts", principalId] as const;
}

function usePrincipalId(): string {
  const user = useOptionalUser()?.user;
  const chatter = useOptionalChatter()?.chatter;
  return user?.user_id ?? chatter?.chatter_id ?? "anon";
}

export function useRosterCounts(): Record<string, RosterCount> {
  const user = useOptionalUser()?.user;
  const chatter = useOptionalChatter()?.chatter;
  const principalId = usePrincipalId();

  const q = useQuery<RosterCountsResp>({
    queryKey: rosterCountsKey(principalId),
    queryFn: () => relay.get<RosterCountsResp>("/admin/accounts/roster-counts"),
    enabled: !!user || !!chatter,
    refetchInterval: 60_000,
    staleTime: 30_000,
    refetchOnWindowFocus: false,
  });

  return q.data?.counts ?? EMPTY;
}

/** Per-account cooldown (ms) so re-hovering / mass-send bursts don't fire a live
 *  OF re-read every time. The 60s poll is the slow reconcile; this is the fast
 *  one, and it only needs to run once per interaction cluster. */
const REFRESH_COOLDOWN_MS = 8_000;

// MODULE scope, not per-hook: the cooldown + in-flight coalescing must be SHARED
// across every useRosterCountActions() instance (roster strip, chat surface,
// realtime hook). Per-instance state would let a hover AND a read AND a WS event
// on the same model each fire their own OF re-read inside the window, and would
// let an older targeted refresh resolve after a newer one and overwrite the
// fresher badge. Keyed by `${principalId}:${accountId}`. Bounded by account count.
const lastRefreshAt = new Map<string, number>();
const inFlightRefresh = new Map<string, Promise<void>>();
// Last time an OPTIMISTIC local patch (e.g. a read dropping unread) rewrote a
// model's badge. A targeted refresh started BEFORE that patch read OF's pre-patch
// state, so if it resolves afterward it must NOT blindly overwrite the newer
// optimistic value — it would revert the badge. Latest-wins: refreshOne skips its
// write when a local patch landed after the refresh began. Keyed principalId:accountId.
const lastLocalPatchAt = new Map<string, number>();

export interface RosterCountActions {
  /** Force this ONE model's badge to authoritative NOW: bust its 5-min server
   *  cache, re-read from OF, merge just that account into the local map. Cooldown-
   *  guarded (skippable with `force`) so swap/hover/WS bursts stay cheap. */
  refreshOne: (accountId: string, opts?: { force?: boolean }) => Promise<void>;
  /** Optimistically rewrite one model's cached count in the same tick (no
   *  network) so the badge moves instantly; `refreshOne`/the poll then reconcile
   *  the exact value. */
  patchLocal: (accountId: string, fn: (cur: RosterCount) => RosterCount) => void;
}

export function useRosterCountActions(): RosterCountActions {
  const qc = useQueryClient();
  const principalId = usePrincipalId();

  const patchLocal = useCallback<RosterCountActions["patchLocal"]>(
    (accountId, fn) => {
      qc.setQueryData<RosterCountsResp>(rosterCountsKey(principalId), (old) => {
        const counts = old?.counts ?? {};
        const cur = counts[accountId] ?? { unread: 0, owe_reply: 0 };
        return { counts: { ...counts, [accountId]: fn(cur) } };
      });
      // Stamp so an older targeted refresh (which read OF before this patch) can't
      // resolve later and revert it. See lastLocalPatchAt.
      lastLocalPatchAt.set(`${principalId}:${accountId}`, Date.now());
    },
    [qc, principalId],
  );

  const refreshOne = useCallback<RosterCountActions["refreshOne"]>(
    async (accountId, opts) => {
      if (!accountId) return;
      const cacheKey = `${principalId}:${accountId}`;
      // Coalesce: if a refresh for this model is already busting + re-reading it,
      // ride that one request instead of racing a second bust (which could land
      // out of order and overwrite the fresher badge).
      const pending = inFlightRefresh.get(cacheKey);
      if (pending) return pending;
      const now = Date.now();
      if (!opts?.force && now - (lastRefreshAt.get(cacheKey) ?? 0) < REFRESH_COOLDOWN_MS) {
        return;
      }
      lastRefreshAt.set(cacheKey, now);
      const startedAt = now;
      const run = (async () => {
        // Cancel any in-flight full-map poll first: it may have read this account's
        // count from the server BEFORE we bust it, so letting it resolve after our
        // merge would clobber the fresh value back to stale.
        await qc.cancelQueries({ queryKey: rosterCountsKey(principalId) });
        try {
          const resp = await relay.get<RosterCountsResp>(
            `/admin/accounts/roster-counts?bust=${encodeURIComponent(accountId)}`,
          );
          // Latest-wins: if an optimistic local patch (e.g. a read dropping unread)
          // landed AFTER we kicked this read, our OF snapshot predates it — skip the
          // write so we don't revert the newer value. mark-read busted the server
          // cache, so the next poll/refresh reconciles to OF-truth cleanly.
          if ((lastLocalPatchAt.get(cacheKey) ?? 0) > startedAt) return;
          const fresh = resp?.counts?.[accountId];
          // Merge ONLY the target so we don't stomp fresher optimistic values on
          // the other models (the bust response carries only the target anyway).
          if (fresh) patchLocal(accountId, () => fresh);
        } catch {
          // Best-effort: the 60s poll reconciles if the targeted read fails.
        }
      })();
      inFlightRefresh.set(cacheKey, run);
      try {
        await run;
      } finally {
        inFlightRefresh.delete(cacheKey);
      }
    },
    [qc, principalId, patchLocal],
  );

  return { refreshOne, patchLocal };
}
