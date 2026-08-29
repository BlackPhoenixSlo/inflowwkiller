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
import { useQuery, type QueryClient } from "@tanstack/react-query";

import { MASS_PLACEHOLDER_END, MASS_PLACEHOLDER_MIN, orderThread } from "@/hooks/useChatMessages";
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
  is_paid: boolean | null;
  is_unsent: boolean | null;
  purchased_at: string | null;
  created_at: string | null;
}

interface LocalMessagesResp {
  messages: LocalMessageRow[];
  limit: number;
  next_before_id: number | null;
}

/** Cache key for one chat's seed, minus the page size — pass it to a
 *  partial-match filter, or spread it and append `limit` for the query
 *  itself. Nobody outside this module should spell the prefix out. */
export const seedKey = (accountId: string | null, fanId: number | null) =>
  ["messages-local-seed", accountId, fanId] as const;

/** Drop one message from this chat's seed, every page size at once.
 *  An unsend has to reach in here: the seed is re-hydrated into the thread
 *  on every mount (ChatSurface), so a row left standing repaints the bubble
 *  the operator just deleted — as an empty shell, since seed rows carry no
 *  media. The relay flips `messages.is_unsent` on the same call, which fixes
 *  the next cold fetch; this keeps the CURRENT session honest regardless. */
export function dropSeedMessage(
  qc: QueryClient,
  accountId: string | null,
  fanId: number | null,
  messageId: number,
): void {
  qc.setQueriesData<LocalMessagesResp>(
    { queryKey: seedKey(accountId, fanId) },
    (curr) => (curr
      ? { ...curr, messages: curr.messages.filter((r) => Number(r.message_id) !== messageId) }
      : curr),
  );
}

export function useChatMessagesLocal(opts: {
  enabled?: boolean;
  accountId: string | null;
  fanId: number | null;
  limit?: number;
}): { rows: OFMessage[]; isLoading: boolean } {
  const { enabled = true, accountId, fanId, limit = 30 } = opts;
  const q = useQuery<LocalMessagesResp>({
    queryKey: [...seedKey(accountId, fanId), limit],
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

/** One older page from the local mirror, mapped to the render shape.
 *
 *  Same endpoint and same mapping as the head seed above — only the cursor
 *  differs. `beforeId` MUST be the cursor `useChatMessages.olderCursor()`
 *  returns, i.e. an id OF itself handed us: the mirror filters on
 *  `message_id < beforeId`, and every synthetic id (mass placeholders at
 *  5e15, ledger tips at 6e15) sits above every real OF id — so paging from
 *  a real cursor excludes both bands structurally, with no age rule needed.
 *  Passing a synthetic id here would silently re-admit them.
 *
 *  Returns [] on any failure: a mirror miss (the ~7% of threads whose local
 *  history is shallower than the fan has scrolled) must degrade to the plain
 *  OF wait, never to an error surface. */
export async function fetchOlderSeed(
  accountId: string | null,
  fanId: number | null,
  beforeId: number | null,
  limit = 30,
): Promise<OFMessage[]> {
  if (!accountId || fanId == null || beforeId == null || !(beforeId > 0)) return [];
  try {
    const qs = new URLSearchParams({ limit: String(limit), before_id: String(beforeId) });
    const resp = await relay.get<LocalMessagesResp>(
      `/admin/messages/${encodeURIComponent(accountId)}/${fanId}?${qs.toString()}`,
    );
    return mapRows(resp.messages ?? [], accountId, fanId);
  } catch {
    return [];
  }
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
 *  Returns the SAME `prev` reference when nothing is missing AND the existing
 *  order is already canonical, so a no-op hydration never notifies observers
 *  (never fights the patcher).
 *
 *  Ordering is delegated to orderThread rather than a blind prepend: a seed
 *  carries ledger-tip rows (6e15 band) whose synthetic id says nothing about
 *  time, so prepending them stacked every tip at the TOP of the thread. Routing
 *  through orderThread slots each tip into its true created_at position — and
 *  also heals a persisted cache that already held the tips at the wrong spot
 *  (nothing missing, but the order still gets corrected). */
export function mergeSeedIntoMessages(
  prev: OFMessage[] | undefined,
  seed: OFMessage[],
): OFMessage[] {
  if (!prev || prev.length === 0) return orderThread(seed);
  const have = new Set(prev.map((m) => String(m.id)));
  const missing = seed.filter((m) => !have.has(String(m.id)));
  return orderThread(missing.length > 0 ? [...missing, ...prev] : prev);
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
      // Ledger-confirmed purchase → render the PPV bubble unlocked without
      // waiting on OF's `isOpened` (the payouts/ledger tick stamps is_paid
      // minutes before a fresh OF fetch would echo the unlock).
      isPaid: !!r.is_paid,
      _side: isOut ? "right" : "left",
    };
    out.push(row);
  }
  return out;
}
