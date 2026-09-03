"use client";

/**
 * useUnsendMessage — delete a chat message via OF's unsend endpoint.
 *
 * Two surfaces:
 *   • `unsendOne(msg)` — DELETE /messages/{id} with `{withUserId: fanId}`.
 *     Removes the bubble from THIS chat only. If OF replies with a
 *     `queue` block (the message was part of a mass send), it's returned
 *     so the caller can offer a follow-up "also unsend from everyone".
 *   • `unsendQueue(queueId)` — DELETE /messages/queue/{id}. Cancels the
 *     entire mass-message queue, which unsends the bubble from EVERY fan
 *     who hasn't already read past OF's 24h edit window.
 *
 * Optimistic: `unsendOne` drops the message from the ["messages", account,
 * fan] cache before the request. On failure we re-insert at the original
 * index so the bubble doesn't silently disappear.
 *
 * On success it also calls `dropSeedMessage` — the DB seed is re-hydrated
 * into the thread on every mount, so a row left standing there paints the
 * unsent bubble straight back (see useChatMessagesLocal).
 *
 * Outside OF's edit window (~24h) the upstream returns 4xx — we revert.
 */

import { useCallback } from "react";
import { useQueryClient } from "@tanstack/react-query";

import { dropSeedMessage } from "@/hooks/useChatMessagesLocal";
import { relay, RelayError, type OFMessage } from "@/lib/relay";
import { type FanId } from "@/lib/fanId";

export interface UnsendQueueInfo {
  id: number;
  sentCount?: number;
  viewedCount?: number;
  canUnsend?: boolean;
  unsendSeconds?: number;
  isCanceled?: boolean;
}

interface UnsendResp {
  success?: boolean;
  queue?: UnsendQueueInfo | null;
}

export function useUnsendMessage(accountId: string | null, fanId: FanId | null) {
  const qc = useQueryClient();
  const queryKey = ["messages", accountId, fanId] as const;

  const unsendOne = useCallback(
    async (msg: OFMessage): Promise<UnsendResp | null> => {
      if (!accountId || fanId == null) return null;
      // The id stays EXACTLY as the wire sent it. `Number(msg.id)` rounded a
      // Fansly snowflake (951515634200489987 -> 951515634200490000) and that
      // rounded value was then the target of a DELETE — at best a silent 404,
      // at worst naming a different real message. A truncated id on a read
      // path is a stale badge; on a destructive call it is data loss.
      const id = String(msg.id ?? "");
      if (!/^\d+$/.test(id) || /^0+$/.test(id)) return null;

      // Optimistic: snapshot prior state so we can restore on failure,
      // then drop the bubble from the cache.
      const prev = qc.getQueryData<OFMessage[]>(queryKey);
      qc.setQueryData<OFMessage[]>(queryKey, (curr = []) =>
        curr.filter((m) => String(m.id) !== id),
      );

      try {
        const resp = await relay.delete<UnsendResp>(
          `/api/of/v2/messages/${id}`,
          { accountId },
          { withUserId: fanId },
        );
        dropSeedMessage(qc, accountId, fanId, id);
        return resp ?? null;
      } catch (err) {
        if (prev) qc.setQueryData(queryKey, prev);
        const reason = err instanceof RelayError ? err.message : String(err);
        console.warn("[unsend] failed — restoring bubble:", reason);
        throw err;
      }
    },
    [accountId, fanId, qc, queryKey],
  );

  const unsendQueue = useCallback(
    async (queueId: number): Promise<void> => {
      if (!accountId) return;
      if (!Number.isFinite(queueId) || queueId <= 0) return;
      await relay.delete(`/api/of/v2/messages/queue/${queueId}`, { accountId });
    },
    [accountId],
  );

  return { unsendOne, unsendQueue };
}
