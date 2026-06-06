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
 * Outside OF's edit window (~24h) the upstream returns 4xx — we revert.
 */

import { useCallback } from "react";
import { useQueryClient } from "@tanstack/react-query";

import { relay, RelayError, type OFMessage } from "@/lib/relay";

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

export function useUnsendMessage(accountId: string | null, fanId: number | null) {
  const qc = useQueryClient();
  const queryKey = ["messages", accountId, fanId] as const;

  const unsendOne = useCallback(
    async (msg: OFMessage): Promise<UnsendResp | null> => {
      if (!accountId || fanId == null) return null;
      const id = Number(msg.id);
      if (!Number.isFinite(id) || id <= 0) return null;

      // Optimistic: snapshot prior state so we can restore on failure,
      // then drop the bubble from the cache.
      const prev = qc.getQueryData<OFMessage[]>(queryKey);
      qc.setQueryData<OFMessage[]>(queryKey, (curr = []) =>
        curr.filter((m) => Number(m.id) !== id),
      );

      try {
        const resp = await relay.delete<UnsendResp>(
          `/api/of/v2/messages/${id}`,
          { accountId },
          { withUserId: fanId },
        );
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
