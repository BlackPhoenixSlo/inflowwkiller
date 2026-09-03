"use client";

/**
 * useLikeMessage — toggle a heart-react on a chat message.
 *
 * OF only lets you like the OTHER party's messages (POST on your own
 * returns 404). We hide the affordance for outgoing bubbles in the UI,
 * but the hook still tolerates a 404 by reverting the optimistic flip
 * — failure shouldn't leave the user staring at a wrong-state heart.
 *
 * Optimistic update writes the new `isLiked` flag straight into the
 * ["messages", accountId, fanId] cache so the heart appears instantly.
 */

import { useCallback } from "react";
import { useQueryClient } from "@tanstack/react-query";

import { relay, type OFMessage } from "@/lib/relay";
import { type FanId } from "@/lib/fanId";

export function useLikeMessage(accountId: string | null, fanId: FanId | null) {
  const qc = useQueryClient();
  const queryKey = ["messages", accountId, fanId] as const;

  const patchLocal = useCallback(
    (messageId: string, isLiked: boolean) => {
      qc.setQueryData<OFMessage[]>(queryKey, (prev = []) =>
        prev.map((m) => (String(m.id) === messageId ? { ...m, isLiked } : m)),
      );
    },
    [qc, queryKey],
  );

  const toggle = useCallback(
    async (msg: OFMessage) => {
      if (!accountId || fanId == null) return;
      // Exact, not Number(): a Fansly message id is a snowflake past 2^53,
      // so a rounded value here would like a DIFFERENT message (or 404).
      const id = String(msg.id ?? "");
      if (!/^\d+$/.test(id) || /^0+$/.test(id)) return; // optimistic / pseudo bubbles
      const next = !msg.isLiked;
      patchLocal(id, next);
      try {
        if (next) {
          await relay.post(`/api/of/v2/messages/${id}/like`, undefined, { accountId });
        } else {
          await relay.delete(`/api/of/v2/messages/${id}/like`, { accountId });
        }
      } catch (err) {
        console.warn("[like] failed — reverting", err);
        patchLocal(id, !next);
      }
    },
    [accountId, fanId, patchLocal],
  );

  return { toggle };
}
