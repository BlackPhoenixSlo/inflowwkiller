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

import { relay, type OFChatItem } from "@/lib/relay";
import { useOptionalUser } from "@/contexts/UserContext";
import { useOptionalChatter } from "@/contexts/ChatterContext";

export interface RosterCount {
  unread: number;
  owe_reply: number;
}

export interface RosterCountsResp {
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
  const qc = useQueryClient();

  const q = useQuery<RosterCountsResp>({
    queryKey: rosterCountsKey(principalId),
    queryFn: async () => {
      // Stamp BEFORE the network read: any optimistic patchLocal that lands
      // while this poll is in flight stamps a LATER time, and mergeRosterCounts
      // then keeps the fresher local value instead of letting this (now stale)
      // server snapshot clobber it. Without this, removing the per-message
      // refreshOne — which used to cancelQueries the in-flight poll — would let
      // the 60s poll revert an instant badge back to a pre-event count.
      const fetchStartedAt = Date.now();
      const resp = await relay.get<RosterCountsResp>("/admin/accounts/roster-counts");
      const prev = qc.getQueryData<RosterCountsResp>(rosterCountsKey(principalId));
      return mergeRosterCounts(
        resp,
        prev,
        fetchStartedAt,
        (aid) => lastLocalPatchAt.get(`${principalId}:${aid}`) ?? 0,
      );
    },
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

/** A conversation's PRIOR state, distilled from its chat-list row, in the two
 *  dimensions the roster badge cares about. */
export interface RosterPrior {
  /** Had unread fan messages (blue) before this event. */
  wasUnread: boolean;
  /** The fan spoke last while the chat was READ (orange "owe reply"). Only
   *  meaningful when !wasUnread — an unread chat is blue, never orange. */
  fanSpokeLast: boolean;
}

export interface RosterDelta {
  dUnread: number;
  dOwe: number;
}

/** Pure: the per-model badge delta for ONE message, given the fan conversation's
 *  PRIOR state and the message direction. CONVERSATION-count semantics — a chat
 *  contributes at most 1 to unread OR owe_reply, so a burst of messages to the
 *  same chat only moves the badge on the FIRST state transition (dedupe). */
export function rosterDelta(isOutbound: boolean, prior: RosterPrior): RosterDelta {
  const { wasUnread, fanSpokeLast } = prior;
  if (isOutbound) {
    // We replied → the conversation is answered.
    //  • was unread (blue)     → clear blue (mirrors the server zeroing unread on
    //    an outbound reply + useSendMessage's optimistic row flip).
    //  • was owe_reply (orange) → clear orange.
    //  • was caught-up          → nothing to move.
    if (wasUnread) return { dUnread: -1, dOwe: 0 };
    if (fanSpokeLast) return { dUnread: 0, dOwe: -1 };
    return { dUnread: 0, dOwe: 0 };
  }
  // Inbound (the fan spoke).
  //  • already unread → still one unread conversation, no change (dedupe).
  //  • was owe_reply (orange) → becomes unread (blue): +blue, -orange.
  //  • was caught-up          → becomes unread (blue).
  if (wasUnread) return { dUnread: 0, dOwe: 0 };
  if (fanSpokeLast) return { dUnread: 1, dOwe: -1 };
  return { dUnread: 1, dOwe: 0 };
}

/** Pure: apply a delta to a count, clamped at zero so a duplicate/stale event
 *  can never drive a badge negative. */
export function applyRosterDelta(cur: RosterCount, d: RosterDelta): RosterCount {
  return {
    unread: Math.max(0, cur.unread + d.dUnread),
    owe_reply: Math.max(0, cur.owe_reply + d.dOwe),
  };
}

/** Pure: distill a chat-list row into the roster-relevant prior state. Returns
 *  null when there's no row (caller then falls back to an authoritative re-read).
 *  `fanSpokeLast` = the last message's author is the fan, not us. */
export function rosterPriorFromRow(row: OFChatItem | null | undefined): RosterPrior | null {
  if (!row) return null;
  const wasUnread = (row.unreadMessagesCount ?? 0) > 0 || row.hasUnread === true;
  const lastFrom = row.lastMessage?.fromUser?.id;
  const fanSpokeLast =
    lastFrom != null && String(lastFrom) === String(row.withUser?.id);
  return { wasUnread, fanSpokeLast };
}

/** Pure: fold a fresh server roster response over the currently-cached one,
 *  PRESERVING any per-account value that an optimistic local patch rewrote WHILE
 *  this poll was in flight. `patchedAt(aid)` returns an account's last local-patch
 *  timestamp; `fetchStartedAt` is when this poll began its network read. Any
 *  account patched AFTER that keeps its local value (the server snapshot predates
 *  the patch); the next poll — started after the patch — reconciles to truth. */
export function mergeRosterCounts(
  server: RosterCountsResp | undefined,
  prev: RosterCountsResp | undefined,
  fetchStartedAt: number,
  patchedAt: (accountId: string) => number,
): RosterCountsResp {
  const serverCounts = server?.counts ?? {};
  const prevCounts = prev?.counts ?? {};
  const out: Record<string, RosterCount> = { ...serverCounts };
  for (const aid of Object.keys(prevCounts)) {
    if (patchedAt(aid) > fetchStartedAt) out[aid] = prevCounts[aid];
  }
  return { counts: out };
}

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
      const existing = qc.getQueryData<RosterCountsResp>(rosterCountsKey(principalId));
      // No baseline yet (the roster query hasn't returned) → do NOT fabricate a
      // from-zero count. mergeRosterCounts would then pin that fake {0,…}+delta
      // over the real initial fetch (the stamp below marks it "newer"), so the
      // badge would show just this one delta instead of the true total for up to
      // 60s. The in-flight / next poll establishes truth and already counts this
      // event (it's in the DB the server aggregates).
      if (!existing) return;
      const counts = existing.counts ?? {};
      const cur = counts[accountId] ?? { unread: 0, owe_reply: 0 };
      qc.setQueryData<RosterCountsResp>(rosterCountsKey(principalId), {
        counts: { ...counts, [accountId]: fn(cur) },
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
      // In-flight handling:
      //  • NOT forced → ride the pending read (coalesce hover/swap/WS bursts to a
      //    single bust).
      //  • forced → the caller needs state that may have changed AFTER that read
      //    began (e.g. a send that just landed), so we must NOT ride it. Wait it
      //    out, THEN do our own fresh read. Draining sequentially (not racing a
      //    second concurrent bust) means our `startedAt` is stamped after the prior
      //    write, so the latest-wins guard below lets our fresher read win.
      let pending = inFlightRefresh.get(cacheKey);
      if (pending && !opts?.force) return pending;
      while (pending) {
        await pending.catch(() => {});
        pending = inFlightRefresh.get(cacheKey);
      }
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
          // Write DIRECTLY, not via patchLocal: this is an authoritative OF read,
          // so it must land even when the roster cache is still empty (patchLocal
          // skips a from-nothing write to avoid fabricating an optimistic baseline
          // — see its guard). Stamp so a racing poll can't clobber this fresh read.
          if (fresh) {
            qc.setQueryData<RosterCountsResp>(rosterCountsKey(principalId), (old) => {
              const counts = old?.counts ?? {};
              return { counts: { ...counts, [accountId]: fresh } };
            });
            lastLocalPatchAt.set(cacheKey, Date.now());
          }
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
