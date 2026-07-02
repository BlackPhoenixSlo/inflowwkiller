"use client";

/**
 * useChatMessagesLocal — instant-load seed for the per-chat message pane.
 *
 * Mirrors `useRecentChatsLocal` one level down: reads the LOCAL `messages`
 * table via /admin/messages/{accountId}/{fanId}, populated by the WS
 * transcoder, and maps each row to the `OFMessage` shape the message-list
 * renderer already consumes. ChatSurface copies these rows ONE-TIME into
 * the real `["messages", accountId, fanId]` cache (a `setQueryData`
 * hydration) so DB history renders instantly through the SAME query the
 * realtime patcher and the send-flow write to — the OF fetch then
 * reconciles into that key in place (seed + OF rows share the real
 * `message_id`, so the swap is a prop update, not a remount: no
 * mid-session flash). See ChatSurface's seed-hydration effect.
 *
 * Still lives in its OWN cache key `["messages-local-seed", ...]` —
 * explicitly NOT `["messages", ...]`. We HYDRATE the latter by copying
 * rows across, never by sharing the key: two `useQuery`s on one key would
 * race their queryFns and the real OF fetch could be skipped, breaking
 * pagination, attribution, and media. The one-time copy keeps the WS
 * realtime patcher in `useInboxRealtime` the SOLE ongoing writer.
 *
 * Media is intentionally `[]` in the mapped rows: OF's signed CDN URLs
 * need per-request proxying we don't want to do for a bridge that
 * resolves in <500ms. Text bubbles in the cold-start window are the win;
 * media populates correctly once the OF response lands.
 */

import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";

import { MASS_PLACEHOLDER_END, MASS_PLACEHOLDER_MIN } from "@/hooks/useChatMessages";
import { toUtcIso } from "@/hooks/useInboxRealtime";
import { relay, type OFMessage } from "@/lib/relay";

// Mass-send optimistic placeholders (service/attribution.py 5e15 band) are a
// just-sent bridge, not history. A placeholder that outlived this window is a
// dead reconcile or an already-unsent broadcast — and once seeded, the OF
// fetch can't retire it by id (the synthetic id sits above every real OF id).
// The relay's /admin/messages seed applies the same cutoff server-side; this
// mirror covers a relay that hasn't been restarted onto that filter yet.
const SEED_PLACEHOLDER_MAX_AGE_MS = 60 * 60_000;

interface LocalMessageRow {
  account_id: string;
  fan_id: number;
  message_id: number;
  direction: "in" | "out";
  sender_name: string | null;
  body: string | null;
  media_count: number | null;
  price_cents: number | null;
  is_tip: boolean | null;
  is_unsent: boolean | null;
  purchased_at: string | null;
  created_at: string | null;
}

interface LocalMessagesResp {
  messages: LocalMessageRow[];
  limit: number;
  next_before_id: number | null;
}

export function useChatMessagesLocal(opts: {
  enabled?: boolean;
  accountId: string | null;
  fanId: number | null;
  limit?: number;
}): { rows: OFMessage[]; isLoading: boolean } {
  const { enabled = true, accountId, fanId, limit = 30 } = opts;
  const q = useQuery<LocalMessagesResp>({
    queryKey: ["messages-local-seed", accountId, fanId, limit],
    enabled: enabled && !!accountId && fanId != null,
    queryFn: () => {
      const qs = new URLSearchParams({ limit: String(limit) });
      return relay.get<LocalMessagesResp>(
        `/admin/messages/${encodeURIComponent(accountId as string)}/${fanId}?${qs.toString()}`,
      );
    },
    staleTime: 5 * 60 * 1000,
    gcTime: 30 * 60 * 1000,
    refetchOnWindowFocus: false,
    refetchOnMount: false,
  });

  // Memoize so `rows` keeps a stable identity across renders. ChatSurface
  // depends on it in a one-time hydration effect; a fresh array on every
  // render would re-fire that effect needlessly.
  const rows: OFMessage[] = useMemo(
    () => mapRows(q.data?.messages ?? [], accountId, fanId),
    [q.data, accountId, fanId],
  );

  return { rows, isLoading: q.isLoading };
}

/** Fold DB-seed rows into whatever the real `["messages", ...]` cache holds,
 *  for ChatSurface's one-time hydration. Two cases:
 *   • Empty/undefined cache (the common cold-open): the seed becomes the
 *     content verbatim.
 *   • Already-populated cache (OF won the open race, or the realtime patcher
 *     landed a live message in the cold-open window): the existing rows are
 *     richer/authoritative, so we keep them and only PREPEND seed rows the
 *     cache is missing, deduped by id. The seed is older-or-equal history,
 *     so prepending preserves oldest→newest order.
 *  Returns the SAME `prev` reference when nothing is missing, so a no-op
 *  hydration never notifies observers (never fights the patcher). */
export function mergeSeedIntoMessages(
  prev: OFMessage[] | undefined,
  seed: OFMessage[],
): OFMessage[] {
  if (!prev || prev.length === 0) return seed;
  const have = new Set(prev.map((m) => String(m.id)));
  const missing = seed.filter((m) => !have.has(String(m.id)));
  return missing.length > 0 ? [...missing, ...prev] : prev;
}

export function mapRows(
  raw: LocalMessageRow[],
  accountId: string | null,
  fanId: number | null,
  now: number = Date.now(),
): OFMessage[] {
  if (raw.length === 0 || !accountId || fanId == null) return [];
  // Backend orders DESC by message_id; consumer reads oldest → newest
  // top-to-bottom (see useChatMessages.ts:89). Reverse here so the seed
  // matches the real query's ordering.
  const ascending = raw.slice().reverse();
  const ownerId = Number(accountId);
  const out: OFMessage[] = [];
  for (const r of ascending) {
    // Optimistic placeholders never live in the local DB, but guard
    // anyway — if one ever slipped through it would collide with the
    // send-flow's reconciliation by tempId.
    if (!Number.isFinite(r.message_id) || r.message_id <= 0) continue;
    if (r.is_unsent) continue;
    // Band-scoped: ids at/above MASS_PLACEHOLDER_END are ledger-synthesized
    // TIP rows (permanent history whose ONLY render path is this seed) — the
    // bridge cutoff must never age those out.
    if (r.message_id >= MASS_PLACEHOLDER_MIN && r.message_id < MASS_PLACEHOLDER_END) {
      const at = Date.parse(toUtcIso(r.created_at ?? undefined));
      if (!Number.isFinite(at) || now - at > SEED_PLACEHOLDER_MAX_AGE_MS) continue;
    }
    const isOut = r.direction === "out";
    // Carry the bubble side explicitly off the authoritative `direction`
    // column. The renderer reads `_side` first so DB-seed rows never have
    // to compare fromUser.id against the async-fetched owner id — that
    // comparison is what caused the "all-left-then-spread" flash before
    // meQ resolved. (relay.ts owns OFMessage; we attach the optional
    // _side here via an intersection rather than widening that type.)
    const row: OFMessage & { _side: "left" | "right" } = {
      id: r.message_id,
      text: r.body ?? "",
      fromUser: {
        id: isOut ? ownerId : fanId,
        name: r.sender_name ?? "",
      },
      createdAt: r.created_at ?? new Date().toISOString(),
      media: [],
      mediaCount: r.media_count ?? 0,
      price: (r.price_cents ?? 0) / 100,
      isTip: !!r.is_tip,
      _side: isOut ? "right" : "left",
    };
    out.push(row);
  }
  return out;
}
