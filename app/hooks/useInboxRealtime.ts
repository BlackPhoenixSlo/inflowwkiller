"use client";

/**
 * useInboxRealtime — listens to the SSE bus and invalidates the right
 * TanStack Query keys so the inbox updates without polling.
 *
 * Events we care about (matching service/event_transcoder.py):
 *   • api2_chat_message       — a new message in some chat
 *   • chat_messages           — a chat preview update (OF push)
 *   • transaction_recorded    — a revenue row landed in our ledger (PPV
 *     unlock / tip / sub), emitted post-commit by transaction_ingest.py
 *     and event_transcoder.py. Drops the fan drawer's revenue caches
 *     (chart, Sales list, spend stats) so a sale shows up without
 *     waiting out their staleTimes.
 *
 * Strategy:
 *   • For chat message events, append the message to that chat's cache
 *     directly (zero round-trip) and bump the chat-list cache so the
 *     row jumps to the top.
 *   • Fallback: invalidate the chat-list query so the next paint refetches.
 *
 * Mount this once at the top of /inbox.
 */

import { useEffect, useMemo, useRef } from "react";
import { useQueryClient, type QueryClient } from "@tanstack/react-query";

import { eventBus, type EventEnvelope } from "@/lib/events";
import { useActiveAccounts } from "@/hooks/useAccounts";
import {
  useRosterCountActions,
  rosterPriorFromRow,
  rosterDelta,
  applyRosterDelta,
  type RosterDelta,
} from "@/hooks/useRosterCounts";
import { scheduledSendsKey } from "@/hooks/useServerScheduledSends";
import type { OFChatItem, OFMessage } from "@/lib/relay";

interface ChatMessagePayload {
  id: number;
  text?: string;
  fromUser?: { id: number; name?: string; username?: string; avatar?: string };
  toUser?: { id: number };
  createdAt?: string;
  mediaCount?: number;
  media?: OFMessage["media"];
  price?: number;
  isFree?: boolean;
  isTip?: boolean;
  isOpened?: boolean;
}

// SSE envelope shape: { api2_chat_message: {...payload}, __account_id, ... }.
// The OF event type is the top-level key holding the actual message body —
// NOT a flat `message` field. We had this wrong, which is why incoming
// messages weren't updating the inbox without a manual refresh.
//
// `__fan_id` rides on WORKER-emitted OUTBOUND events only (welcome / AI reply /
// mass / funnel). OF's native outbound event omits the recipient, so the relay
// stamps it on so the patcher knows which chat to target (see
// events.publish_db_message).
interface ChatMessageEvent {
  api2_chat_message?: ChatMessagePayload;
  __account_id?: string;
  __fan_id?: number;
}

export interface ChatTarget {
  accountId: string;
  fanId: number;
  isOutbound: boolean;
  msg: ChatMessagePayload;
  fromId: number;
}

/**
 * Pure: resolve which chat an `api2_chat_message` envelope targets, and whether
 * it's our own (outbound) send. Exported for unit tests.
 *
 * Direction: `fromUser.id === __account_id` means WE sent it. OF's native
 * outbound event omits the recipient, so worker-emitted outbound events carry
 * the recipient as `__fan_id` (with `toUser.id` as a fallback). Returns null for
 * a malformed event OR a bare OF outbound echo with no recipient to route to —
 * those are left to the optimistic path + the 60s chat-list refetch.
 */
export function resolveChatTarget(e: ChatMessageEvent): ChatTarget | null {
  const accountId = e.__account_id ?? null;
  const msg = e.api2_chat_message ?? null;
  const fromUser = msg?.fromUser ?? null;
  if (!accountId || !msg || !fromUser) return null;
  const fromId = Number(fromUser.id);
  if (!Number.isFinite(fromId)) return null;
  const isOutbound = String(fromId) === accountId;
  const fanId = isOutbound ? Number(e.__fan_id ?? msg.toUser?.id ?? NaN) : fromId;
  if (!Number.isFinite(fanId)) return null;
  return { accountId, fanId, isOutbound, msg, fromId };
}

/** Scan every chats cache for a fan's row and return the FRESHEST match (or null).
 *  The same fan can sit in several chats caches (unfiltered, folder, search,
 *  all-scope) at different freshness — a first-match could be a stale inactive
 *  variant and drive a wrong prior/delta. Pick the row whose `lastMessage` is
 *  newest (ties / undated → first seen). Used to read a conversation's PRIOR
 *  badge state before a live event mutates it. Exported for tests. */
export function findFanChatRow(
  qc: QueryClient,
  accountId: string,
  fanId: number,
): OFChatItem | null {
  type Page = { rows: OFChatItem[]; hasMore: boolean };
  type Infinite = { pages: Page[]; pageParams: unknown[] };
  let best: OFChatItem | null = null;
  let bestAt = -Infinity;
  for (const q of qc.getQueryCache().findAll({ queryKey: ["chats"] })) {
    const data = q.state.data as Infinite | undefined;
    if (!data?.pages?.length) continue;
    for (const p of data.pages) {
      for (const c of p.rows) {
        if ((c.__accountId ?? "") === accountId && c.withUser?.id === fanId) {
          const parsed = Date.parse(c.lastMessage?.createdAt ?? "");
          const ts = Number.isFinite(parsed) ? parsed : -Infinity;
          if (best === null || ts > bestAt) { best = c; bestAt = ts; }
        }
      }
    }
  }
  return best;
}

/** Pure: the roster badge delta for a live message, given the fan's PRIOR chat
 *  row and the event direction/time. Returns null to mean "don't patch":
 *   • existing == null → conversation not loaded; caller decides the fallback.
 *   • out-of-order     → the event predates the cached last message (a replay /
 *     reorder). Applying a delta would double-count; the chat-thread insert has
 *     the same guard (insertByCreatedAt), this mirrors it for the badge.
 *  Exported for tests. */
export function rosterDeltaForEvent(args: {
  isOutbound: boolean;
  existing: OFChatItem | null;
  eventCreatedAt: string;
}): RosterDelta | null {
  const { isOutbound, existing, eventCreatedAt } = args;
  if (!existing) return null;
  const lastAt = existing.lastMessage?.createdAt;
  if (lastAt) {
    const evt = Date.parse(eventCreatedAt);
    const prev = Date.parse(lastAt);
    if (Number.isFinite(evt) && Number.isFinite(prev) && evt < prev) return null;
  }
  const prior = rosterPriorFromRow(existing);
  if (!prior) return null;
  return rosterDelta(isOutbound, prior);
}

/**
 * Drop every cache that renders money for this fan. Prefix-matched, so the
 * trailing key parts (limit / purchased flag) are covered too. Active queries
 * (drawer open — including pinned/popout, which never remount and therefore
 * never hit staleTime on their own) refetch immediately; inactive ones just
 * go stale and refetch on next mount.
 */
function invalidateFanRevenue(qc: QueryClient, accountId: string, fanId: number) {
  // Local ledger — the chart ("PPV purchases by fan") + Sales row spine.
  void qc.invalidateQueries({ queryKey: ["fan-ppv-history", accountId, fanId] });
  // OF gallery enrichment — thumbnails/text for Sales + the Unsold list.
  void qc.invalidateQueries({ queryKey: ["fan-chat-media", accountId, fanId] });
  // Live OF profile — lifetime spend / tips / messages stats grid.
  void qc.invalidateQueries({ queryKey: ["of-user", accountId, fanId] });
  // Our local fan mirror — lifetime_spend_cents fallback.
  void qc.invalidateQueries({ queryKey: ["fan", accountId, fanId] });
  // Per-account payouts walk — "Last purchase" stat (24h staleTime otherwise).
  void qc.invalidateQueries({ queryKey: ["last-purchases", accountId] });
}

/** Normalize a message timestamp to a UTC ISO string (…Z).
 *
 *  The relay emits live (`emit_live`) outbound rows — welcome / AI reply / mass
 *  / funnel — with the DB's tz-NAIVE wall clock (`"2026-06-14 09:36:17"`, already
 *  UTC but zone-less). `new Date()` parses that as the VIEWER's local time, which
 *  both skews the bubble's displayed time and corrupts the sort key. REST
 *  `/messages` rows already carry a zone, so the two paths disagreed: live
 *  funnel/mass bubbles rendered at the wrong time AND (see insertByCreatedAt)
 *  stuck out of order. Tag a zone-less value as UTC; pass through one already
 *  zoned. */
export function toUtcIso(raw: string | undefined): string {
  if (!raw) return new Date().toISOString();
  const s = raw.trim();
  const hasZone = /[zZ]$/.test(s) || /[+-]\d{2}:?\d{2}$/.test(s);
  const candidate = hasZone
    ? s
    : `${s.replace(" ", "T").replace(/(\.\d{3})\d+$/, "$1")}Z`;
  const d = new Date(candidate);
  return Number.isNaN(d.getTime()) ? s : d.toISOString();
}

/** Insert `row` into a thread cache kept in created_at order, instead of
 *  blind-appending. The bug: a live message can enter the cache AFTER newer
 *  rows are already present (a re-run funnel, an out-of-order SSE replay), and a
 *  plain `[...prev, row]` then pins it to the BOTTOM as if it were newest. We
 *  scan from the tail — the overwhelmingly common case is a genuinely new
 *  message that belongs at the end, so that path stays O(1) — and splice the row
 *  in just after the last one that isn't strictly newer (`<=` keeps ties in
 *  arrival order). Timestamps are normalized for the compare so a zoned REST row
 *  and a zone-less live row sort consistently. */
export function insertByCreatedAt(list: OFMessage[], row: OFMessage): OFMessage[] {
  const t = Date.parse(toUtcIso(row.createdAt));
  if (Number.isNaN(t)) return [...list, row];
  let i = list.length;
  while (i > 0) {
    const prevT = Date.parse(toUtcIso(list[i - 1].createdAt));
    if (Number.isNaN(prevT) || prevT <= t) break;
    i--;
  }
  return i >= list.length
    ? [...list, row]
    : [...list.slice(0, i), row, ...list.slice(i)];
}

export function useInboxRealtime() {
  const qc = useQueryClient();

  // Accounts the current principal actually owns/has a session for. The
  // SSE `all` scope can carry events for models outside this principal —
  // those must NOT bubble to the top of the list (they belong to other
  // accounts entirely). We keep this in a ref so the subscription below
  // stays mounted across account-list changes instead of re-subscribing.
  const accounts = useActiveAccounts();
  const allowedAccountIds = useMemo(
    () => new Set(accounts.map((a) => String(a.id))),
    [accounts],
  );
  const allowedRef = useRef(allowedAccountIds);
  allowedRef.current = allowedAccountIds;

  // Roster-badge actions, held in refs so the subscription below stays mounted
  // across principal changes (same pattern as allowedRef). We patch the badge
  // OPTIMISTICALLY from the message we already have (patchLocal) instead of an
  // extra OF round-trip; refreshOne is only the fallback for a conversation we
  // haven't loaded yet (can't dedupe → one authoritative read).
  const { refreshOne, patchLocal } = useRosterCountActions();
  const refreshOneRef = useRef(refreshOne);
  refreshOneRef.current = refreshOne;
  const patchLocalRef = useRef(patchLocal);
  patchLocalRef.current = patchLocal;

  useEffect(() => {
    const offMsg = eventBus.on("api2_chat_message", (env: EventEnvelope) => {
      const target = resolveChatTarget(env as unknown as ChatMessageEvent);
      if (!target) return;
      const { accountId, fanId, isOutbound, msg, fromId } = target;
      const fromUser = msg.fromUser!;

      // Drop events for models not on this principal's account. Without
      // this, webhooks for other owners' models would prepend to the list.
      // Guard against the cold-start window where accounts haven't loaded
      // yet (empty set) by letting events through until we know the roster.
      const allowed = allowedRef.current;
      if (allowed.size > 0 && !allowed.has(accountId)) return;

      // Prior conversation state, read NOW before any patch below mutates it. It
      // drives BOTH the optimistic roster delta and the staleness guard: an event
      // older than the fan's cached last message is a replay/reorder and must not
      // move the badge OR reorder/bump the chat-list row (that would show a stale
      // preview, inflate unread, and poison the very row we read as `prior` next
      // time). Mirrors the message-thread insertByCreatedAt guard.
      const owned = allowedRef.current.has(accountId);
      const priorRow = owned ? findFanChatRow(qc, accountId, fanId) : null;
      const eventAt = toUtcIso(msg.createdAt);
      const evtMs = Date.parse(eventAt);
      const priorMs = priorRow?.lastMessage?.createdAt
        ? Date.parse(priorRow.lastMessage.createdAt)
        : NaN;
      const isStale =
        Number.isFinite(evtMs) && Number.isFinite(priorMs) && evtMs < priorMs;

      // Roster badge counts (per-model unread / owe-reply) move on every inbound
      // (adds unread) and outbound (clears the owe-reply we answered). We already
      // have the message AND the fan's PRIOR row, so derive the delta LOCALLY and
      // patch the badge instantly — no extra OF round-trip.
      //   • Manual outbound sends already decremented at send-time in
      //     useSendMessage; by the time their echo lands here the row is flipped,
      //     so the delta is a no-op. This branch moves AUTOMATION outbound
      //     (welcome / AI / mass / funnel — they never touched useSendMessage).
      //   • Cold (fan not in the loaded list) → can't derive a delta, so fall back
      //     to ONE authoritative re-read (cooldown-guarded) — for outbound too,
      //     since an off-screen owe-reply fan we just answered still needs clearing.
      // Gate strictly on the owned set: during cold start `allowedRef` is empty and
      // the guard above lets events through for LIST patching, but a roster patch
      // for a foreign/unknown account is wasted and could hit the pre-auth cache.
      // (rosterDeltaForEvent re-checks staleness itself, so no `isStale` guard here.)
      if (owned) {
        if (!priorRow) {
          void refreshOneRef.current(accountId);
        } else {
          const delta = rosterDeltaForEvent({ isOutbound, existing: priorRow, eventCreatedAt: eventAt });
          if (delta && (delta.dUnread !== 0 || delta.dOwe !== 0)) {
            patchLocalRef.current(accountId, (cur) => applyRosterDelta(cur, delta));
          }
        }
      }

      // A scheduled send firing arrives as an outbound message. Drop its ghost
      // bubble the instant the real one lands (instead of waiting for the poll).
      if (isOutbound) {
        void qc.invalidateQueries({ queryKey: scheduledSendsKey(accountId, fanId) });
      }

      const candidate: OFMessage = {
        id: msg.id,
        text: msg.text ?? "",
        fromUser: { id: fromId, name: fromUser.name ?? fromUser.username ?? "" },
        createdAt: eventAt,
        media: msg.media ?? [],
        mediaCount: msg.mediaCount ?? 0,
        price: msg.price ?? 0,
      };

      // A tip rides on a normal inbound message (isTip + price>0) — refresh
      // the revenue surfaces right away. The ledger ingest reconciles exact
      // amounts a few minutes later via `transaction_recorded`.
      if (!isOutbound && msg.isTip && (msg.price ?? 0) > 0) {
        invalidateFanRevenue(qc, accountId, fanId);
      }

      // Insert into the messages cache in created_at order (de-dup by id).
      // Blind-appending pinned live funnel/mass bubbles to the bottom when they
      // landed after newer rows were already cached; insertByCreatedAt drops
      // each row where its timestamp belongs.
      qc.setQueryData<OFMessage[]>(["messages", accountId, fanId], (prev = []) => {
        const incoming = String(candidate.id);
        if (prev.some((m) => String(m.id) === incoming)) return prev;
        return insertByCreatedAt(prev, candidate);
      });

      // Move the matching row to the top of page 0 — matches legacy /ui
      // behavior (incoming message bubbles to the top, unread badge bumps).
      // The InfiniteData cache is shaped { pages: [{ rows, hasMore }], pageParams }.
      // Strategy: pluck the row from whatever page it's on, update it,
      // prepend to page 0. useChatList's flatten step dedupes by
      // (accountId, fanId) so any stale copy on a later page is ignored.
      // `findAll({ queryKey: ["chats"] })` prefix-matches EVERY chats cache,
      // but the full key is
      //   ["chats", scope.kind, accountKey, filter, listId, query, limit].
      // We must not inject a brand-new row into a filtered / folder / search
      // view this fan may not belong to (pinned/priority/<folder>/<search>):
      // the flatten step only de-dupes, it never re-applies the filter, so a
      // bogus row would stay until that view's next refetch. So only patch a
      // cache that ALREADY contains this fan's row (bump-to-top, always valid),
      // and only synthesize a NEW row in the unfiltered key
      // (filter==null && listId==null && query==null).
      type Page = { rows: OFChatItem[]; hasMore: boolean };
      type Infinite = { pages: Page[]; pageParams: unknown[] };
      // Out-of-order replay: the message-thread insert above already slotted it by
      // timestamp; do NOT reorder/bump the chat-list row — that would surface a
      // stale preview, inflate unread, and corrupt the prior we read next event.
      if (isStale) return;
      qc.getQueryCache().findAll({ queryKey: ["chats"] }).forEach((q) => {
        const data = q.state.data as Infinite | undefined;
        if (!data?.pages?.length) return;

        const filterKey = q.queryKey[3] ?? null;
        const listIdKey = q.queryKey[4] ?? null;
        const queryKeyText = q.queryKey[5] ?? null;
        const isUnfiltered =
          filterKey === null && listIdKey === null && queryKeyText === null;

        // Does this cache's SCOPE actually include the event's account? The key
        // is ["chats", kind, accountKey, …]; accountKey is the single id in a
        // model scope, or the sorted comma-joined included ids in an all scope.
        // An inbound DM for one of the principal's models (model A) must NOT be
        // synthesized into ANOTHER of its models' single-account list (model B) —
        // that's the cross-model leak where the same fan shows up under a model
        // it doesn't belong to, double-counting unread until the next refetch.
        const scopeKind = q.queryKey[1];
        const accountKey = String(q.queryKey[2] ?? "");
        const accountInScope =
          scopeKind === "model"
            ? accountKey === accountId
            : accountKey.split(",").includes(accountId);

        let existing: OFChatItem | null = null;
        const stripped: Page[] = data.pages.map((p) => ({
          ...p,
          rows: p.rows.filter((c) => {
            if ((c.__accountId ?? "") !== accountId) return true;
            if (c.withUser.id !== fanId) return true;
            existing = c;
            return false;
          }),
        }));

        // Only SYNTHESIZE a brand-new row into a cache that can validly hold it.
        // Skip when the fan isn't already present AND either:
        //   • it's a filtered/folder/search view (we can't know the fan matches
        //     the filter), or
        //   • the event's account isn't in this cache's scope (a DIFFERENT
        //     model's list — the cross-model leak).
        // Bumping an EXISTING row is always fine: it's already correctly scoped,
        // so this guard only gates the new-row path.
        if (!existing && (!isUnfiltered || !accountInScope)) return;

        const base: OFChatItem = existing ?? ({
          __accountId: accountId,
          // Outbound to a fan not yet in the list: fromUser is US, so we don't
          // know the fan's name — leave it blank; the 60s refetch fills it in.
          withUser: {
            id: fanId,
            name: isOutbound ? "" : (fromUser.name ?? fromUser.username ?? ""),
          },
        } as OFChatItem);
        const updatedRow: OFChatItem = {
          ...base,
          // We sent it → don't raise the unread badge; only inbound bumps it.
          hasUnread: isOutbound ? (base.hasUnread ?? false) : true,
          unreadMessagesCount: isOutbound
            ? (base.unreadMessagesCount ?? 0)
            : (base.unreadMessagesCount ?? 0) + 1,
          lastMessage: {
            id: candidate.id as number,
            text: candidate.text,
            createdAt: candidate.createdAt,
            mediaCount: candidate.mediaCount ?? 0,
            fromUser: candidate.fromUser,
          },
        };

        const [first, ...rest] = stripped;
        const newPages: Page[] = [
          { ...first, rows: [updatedRow, ...first.rows] },
          ...rest,
        ];
        qc.setQueryData(q.queryKey, { ...data, pages: newPages });
      });
    });

    // A revenue row landed in the local transactions ledger (PPV unlock seen
    // by the payouts ingest, tip promote, status flip). Emitted post-commit
    // server-side, so refetching here always reads the new row.
    const offTx = eventBus.on("transaction_recorded", (env: EventEnvelope) => {
      const accountId = env.__account_id ?? null;
      const tx = env.transaction_recorded as { fan_id?: number } | undefined;
      const fanId = Number(tx?.fan_id ?? NaN);
      if (!accountId || !Number.isFinite(fanId)) return;
      const allowed = allowedRef.current;
      if (allowed.size > 0 && !allowed.has(accountId)) return;
      invalidateFanRevenue(qc, accountId, fanId);
      // A ppv_message ledger row means a PPV just unlocked. Force the thread's
      // head page to refetch so OF's now-true `isOpened` lands on the bubble
      // (our is_paid stamp already flips it via the DB seed, but an already-
      // open thread renders off the OF `["messages"]` cache, not the seed).
      void qc.invalidateQueries({ queryKey: ["messages", accountId, fanId] });
    });

    // Note: we used to invalidate ["chats"] on every "chat_messages" event,
    // but with useInfiniteQuery that refetches *every* loaded page — after a
    // few "load more" clicks that becomes 40+ pages per OF preview event.
    // The api2_chat_message handler above already patches rows in place
    // (preview, unread flag), and the 60s refetchInterval on useChatList
    // covers anything that slipped through.

    return () => { offMsg(); offTx(); };
  }, [qc]);
}
