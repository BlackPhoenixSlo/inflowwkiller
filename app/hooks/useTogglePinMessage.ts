"use client";

/**
 * useTogglePinMessage — pin/unpin a message inside a chat.
 *
 * OF wire path is /messages/{msg_id}/pin/user/{chat_partner_id}. The
 * partner id is the fan's user id for this chat. Like the like hook,
 * we flip `isPinned` optimistically in the messages cache and revert
 * on failure.
 *
 * Outgoing AND incoming messages can be pinned (unlike `like` which
 * only works on the other party's messages), so we don't gate by
 * sender direction here.
 */

import { useCallback } from "react";
import { useQueryClient } from "@tanstack/react-query";

import { relay, type OFMessage } from "@/lib/relay";

export function useTogglePinMessage(accountId: string | null, fanId: number | null) {
  const qc = useQueryClient();
  const queryKey = ["messages", accountId, fanId] as const;

  const patchLocal = useCallback(
    (messageId: number, isPinned: boolean) => {
      qc.setQueryData<OFMessage[]>(queryKey, (prev = []) =>
        prev.map((m) => (Number(m.id) === messageId ? { ...m, isPinned } : m)),
      );
    },
    [qc, queryKey],
  );

  const toggle = useCallback(
    async (msg: OFMessage) => {
      if (!accountId || fanId == null) return;
      const id = Number(msg.id);
      if (!Number.isFinite(id) || id <= 0) return;
      const next = !msg.isPinned;
      patchLocal(id, next);
      try {
        if (next) {
          await relay.post(
            `/api/of/v2/messages/${id}/pin/user/${fanId}`,
            undefined,
            { accountId },
          );
        } else {
          await relay.delete(
            `/api/of/v2/messages/${id}/pin/user/${fanId}`,
            { accountId },
          );
        }
      } catch (err) {
        console.warn("[pin] failed — reverting", err);
        patchLocal(id, !next);
      }
    },
    [accountId, fanId, patchLocal],
  );

  return { toggle };
}
