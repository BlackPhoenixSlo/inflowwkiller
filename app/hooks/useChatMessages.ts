"use client";

/**
 * useChatMessages — message history for one chat, with cursor pagination
 * and a smart-refresh action.
 *
 * `chatAccountId` is REQUIRED — chats live under one account, so even in
 * Unified Inbox mode the UI must pass the account that owns the chat
 * (we tag this on the chat-list row as `__accountId`). The relay scopes
 * by X-Account-Id and would otherwise fall back to the active account.
 *
 * Pagination: OF uses `before_id` (older). We treat the lowest positive
 * server id in messagesRef as the cursor. Optimistic messages (id <= 0)
 * are intentionally filtered out before computing the cursor.
 *
 * Smart refresh: §10 from MESSAGING-FULL-RECAP. Page-1 (offset 0) only
 * unless the user clicks the refresh button.
 */

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useRef, useState } from "react";

import { relay, type OFMessage, type OFMessagesResp } from "@/lib/relay";
import { eventBus } from "@/lib/events";
import { perfDelivered, perfError, perfLog, perfOpId } from "@/lib/perfLog";

// 30 matches OF's own chat-list page size — enough that most chats need
// at most one or two scroll-up fetches before reaching the start.
const PAGE_SIZE = 30;
const STALE_MS = 15_000;
// When SSE is down, poll aggressively — that's the only path new
// messages can reach the UI. When SSE is up, the api2_chat_message
// event pushes new rows in real time, so polling is just a safety net
// for missed events and can run much slower.
const POLL_MS_OFFLINE = 30_000;
const POLL_MS_REALTIME = 2 * 60_000;

export interface UseChatMessagesOpts {
  /** The model account that owns this chat. Required. */
  accountId: string | null;
  /** The fan's user id (== OF chat id). */
  fanId: number | null;
  /** Disable polling — used while the chat is closed. */
  enabled?: boolean;
}

export function useChatMessages({ accountId, fanId, enabled = true }: UseChatMessagesOpts) {
  const qc = useQueryClient();

  // Mirror eventBus connection status so the poll interval can stretch
  // out when SSE is delivering pushes (the common case) and snap back
  // to the 30s aggressive cadence the moment the connection drops.
  const [sseConnected, setSseConnected] = useState(() => eventBus.isConnected());
  useEffect(() => eventBus.onStateChange(setSseConnected), []);

  // Tracks the oldest positive id we've loaded — drives the load-older
  // cursor. Optimistic ids (≤ 0) never become the cursor.
  const oldestCursorRef = useRef<number | null>(null);
  // Reflects OF's `hasMore` flag from the most recent page. Default to
  // true so the auto-loader fires at least once; the first fetch will
  // settle it to the real value.
  const [hasMore, setHasMore] = useState(true);
  const [isLoadingOlder, setIsLoadingOlder] = useState(false);
  // Re-entrancy guard for the scroll-to-top trigger.
  const inflightRef = useRef(false);

  const queryKey = ["messages", accountId, fanId] as const;

  const query = useQuery<OFMessage[]>({
    queryKey,
    enabled: enabled && !!accountId && fanId != null,
    queryFn: async ({ signal }) => {
      if (!accountId || fanId == null) return [];
      const opId = perfOpId("chat.messages");
      perfLog(opId, "chat.messages", "requested", {
        accountId, fanId, phase: "initial", limit: PAGE_SIZE,
      });
      try {
        // Forward the abort signal so switching chats fast cancels the
        // previous fetch in flight (frees the relay slot, drops the
        // payload before it returns).
        const resp = await relay.get<OFMessagesResp>(
          `/api/of/v2/chats/${fanId}/messages?limit=${PAGE_SIZE}&order=desc`,
          { accountId },
          signal,
        );
        // OF returns newest-first. Reverse so the UI can scroll oldest→newest
        // top-to-bottom (the way humans read a thread).
        const list = (resp.list || []).slice().reverse();
        oldestCursorRef.current = list.find((m) => Number(m.id) > 0)?.id as number ?? null;
        setHasMore(!!resp.hasMore);
        perfDelivered(opId, "chat.messages", {
          count: list.length, hasMore: !!resp.hasMore, phase: "initial",
        });
        return list;
      } catch (err) {
        perfError(opId, "chat.messages", {
          message: (err as Error)?.message, aborted: signal?.aborted, phase: "initial",
        });
        throw err;
      }
    },
    // Final safety net for React key uniqueness. The cache is written by
    // several paths (initial fetch, load-older merge, optimistic send,
    // reconcile, SSE append) AND OF itself sometimes returns the same
    // message twice in one page (pinned rows, pagination-boundary overlap).
    // Any of those can leave two rows with the same id, which makes
    // MessageList emit duplicate `key={String(m.id)}` children. Dedup here,
    // first-occurrence-wins, so the rendered list is always unique without
    // every writer having to defend independently. structuralSharing keeps
    // the reference stable when nothing actually changed.
    select: dedupById,
    staleTime: STALE_MS,
    refetchInterval: enabled ? (sseConnected ? POLL_MS_REALTIME : POLL_MS_OFFLINE) : false,
    // Override the global `false`: with two windows showing the same
    // chat (inbox + popout), background-tab throttling slows the
    // popout's 30s poll to ~60s+. Refetching on focus means switching
    // to the popout catches it up instantly instead of staring at stale
    // data for up to a minute.
    refetchOnWindowFocus: true,
  });

  /** Load one older page using cursor pagination. Idempotent — overlapping
   *  calls are dropped via inflightRef so the scroll observer can fire
   *  freely without producing duplicate requests. */
  const loadOlder = useCallback(async (): Promise<{ added: number; hasMore: boolean }> => {
    if (!accountId || fanId == null) return { added: 0, hasMore: false };
    if (inflightRef.current) return { added: 0, hasMore };
    const current = qc.getQueryData<OFMessage[]>(queryKey) || [];
    const oldest = current.find((m) => Number(m.id) > 0)?.id ?? oldestCursorRef.current;
    if (oldest == null) return { added: 0, hasMore: false };

    inflightRef.current = true;
    setIsLoadingOlder(true);
    const opId = perfOpId("chat.messages");
    perfLog(opId, "chat.messages", "requested", {
      accountId, fanId, phase: "older", before_id: oldest, limit: PAGE_SIZE,
    });
    try {
      const resp = await relay.get<OFMessagesResp>(
        `/api/of/v2/chats/${fanId}/messages?limit=${PAGE_SIZE}&order=desc&before_id=${oldest}`,
        { accountId },
      );
      const older = (resp.list || []).slice().reverse();
      setHasMore(!!resp.hasMore);
      perfDelivered(opId, "chat.messages", {
        count: older.length, hasMore: !!resp.hasMore, phase: "older",
      });
      if (older.length === 0) return { added: 0, hasMore: !!resp.hasMore };

      qc.setQueryData<OFMessage[]>(queryKey, (prev = []) => {
        const have = new Set(prev.map((m) => String(m.id)));
        const dedup = older.filter((m) => !have.has(String(m.id)));
        return [...dedup, ...prev];
      });
      return { added: older.length, hasMore: !!resp.hasMore };
    } catch (err) {
      perfError(opId, "chat.messages", { message: (err as Error)?.message, phase: "older" });
      throw err;
    } finally {
      inflightRef.current = false;
      setIsLoadingOlder(false);
    }
  }, [accountId, fanId, qc, queryKey, hasMore]);

  /** Force-refresh the top page. Mutates in place — Query handles the
   *  setQueryData internally via refetch. */
  const refresh = useCallback(async () => {
    await qc.invalidateQueries({ queryKey });
  }, [qc, queryKey]);

  /** Append a message locally (used by optimistic send + SSE handlers). */
  const appendLocal = useCallback(
    (msg: OFMessage) => {
      qc.setQueryData<OFMessage[]>(queryKey, (prev = []) => {
        const incoming = String(msg.id);
        if (prev.some((m) => String(m.id) === incoming)) return prev;
        return [...prev, msg];
      });
    },
    [qc, queryKey],
  );

  /** Replace a message by tempId (reconcile optimistic → server). */
  const reconcileLocal = useCallback(
    (tempId: number, serverMsg: OFMessage) => {
      qc.setQueryData<OFMessage[]>(queryKey, (prev = []) => {
        const opt = prev.find((m) => m._tempId === tempId);
        const serverId = String(serverMsg.id);
        // SSE can echo the outbound message (real id) before this HTTP
        // reconcile resolves. If that row already exists, stamping the
        // same id onto the optimistic row would create two rows with the
        // same React key. Instead, fold the optimistic media fallback
        // onto the existing real row and drop the placeholder.
        const existingReal = prev.find(
          (m) => m._tempId !== tempId && String(m.id) === serverId,
        );
        if (existingReal) {
          return prev
            .filter((m) => m._tempId !== tempId)
            .map((m) =>
              m === existingReal && opt ? mergeMedia(opt, m) : m,
            );
        }
        return prev.map((m) => (m._tempId === tempId ? mergeMedia(m, serverMsg) : m));
      });
    },
    [qc, queryKey],
  );

  /** Mark an optimistic message as failed (Retry button shows). */
  const failLocal = useCallback(
    (tempId: number, reason?: string) => {
      qc.setQueryData<OFMessage[]>(queryKey, (prev = []) => {
        return prev.map((m) =>
          m._tempId === tempId
            ? { ...m, _failed: true, _pending: false, _failedReason: reason }
            : m,
        );
      });
    },
    [qc, queryKey],
  );

  /** Remove an optimistic message (used before retry). */
  const dropLocal = useCallback(
    (tempId: number) => {
      qc.setQueryData<OFMessage[]>(queryKey, (prev = []) => {
        return prev.filter((m) => m._tempId !== tempId);
      });
    },
    [qc, queryKey],
  );

  return {
    ...query,
    loadOlder,
    hasOlder: hasMore,
    isLoadingOlder,
    refresh,
    appendLocal,
    reconcileLocal,
    failLocal,
    dropLocal,
  };
}

/** Drop rows that repeat an id, keeping the first occurrence so render
 *  identity (and thus scroll position / mounted Bubble state) stays stable.
 *  Returns the same array reference when there were no dups, so React Query's
 *  structural sharing doesn't see a spurious change. Optimistic rows (id ≤ 0)
 *  carry distinct negative ids per send, so they're never collapsed here. */
function dedupById(list: OFMessage[]): OFMessage[] {
  const seen = new Set<string>();
  let hasDup = false;
  const out: OFMessage[] = [];
  for (const m of list) {
    const k = String(m.id);
    if (seen.has(k)) { hasDup = true; continue; }
    seen.add(k);
    out.push(m);
  }
  return hasDup ? out : list;
}

/** Merge optimistic media's `files` into a server response — OF's CDN
 *  takes ~2-3s to populate `files.{thumb,source}.url`, so the server
 *  response often comes back with media but no playable URLs. We swap
 *  the optimistic file urls in until the next refresh catches them. */
function mergeMedia(opt: OFMessage, server: OFMessage): OFMessage {
  if (!opt?.media?.length || !server?.media?.length) return { ...server, _pending: false, _tempId: opt._tempId };
  const byId = new Map<number, OFMessage["media"][number]>();
  for (const om of opt.media) if (typeof om.id === "number") byId.set(om.id, om);
  const mergedMedia = server.media.map((sm, i) => {
    if (sm.files?.thumb?.url || sm.files?.source?.url) return sm;
    const fallback =
      (typeof sm.id === "number" ? byId.get(sm.id) : undefined) || opt.media[i];
    return fallback?.files ? { ...sm, files: fallback.files } : sm;
  });
  return { ...server, media: mergedMedia, _pending: false, _tempId: opt._tempId };
}
