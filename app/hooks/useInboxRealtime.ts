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

      // A scheduled send firing arrives as an outbound message. Drop its ghost
      // bubble the instant the real one lands (instead of waiting for the poll).
      if (isOutbound) {
        void qc.invalidateQueries({ queryKey: scheduledSendsKey(accountId, fanId) });
      }

      const candidate: OFMessage = {
        id: msg.id,
        text: msg.text ?? "",
        fromUser: { id: fromId, name: fromUser.name ?? fromUser.username ?? "" },
        createdAt: msg.createdAt ?? new Date().toISOString(),
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

      // Append to messages cache (de-dup by id).
      qc.setQueryData<OFMessage[]>(["messages", accountId, fanId], (prev = []) => {
        const incoming = String(candidate.id);
        if (prev.some((m) => String(m.id) === incoming)) return prev;
        return [...prev, candidate];
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
      qc.getQueryCache().findAll({ queryKey: ["chats"] }).forEach((q) => {
        const data = q.state.data as Infinite | undefined;
        if (!data?.pages?.length) return;

        const filterKey = q.queryKey[3] ?? null;
        const listIdKey = q.queryKey[4] ?? null;
        const queryKeyText = q.queryKey[5] ?? null;
        const isUnfiltered =
          filterKey === null && listIdKey === null && queryKeyText === null;

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

        // Filtered/folder/search cache that doesn't already hold this fan:
        // skip it. We can't know whether the fan satisfies the filter, so
        // leave it to that view's next refetch rather than inject a row it
        // may not match.
        if (!existing && !isUnfiltered) return;

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
