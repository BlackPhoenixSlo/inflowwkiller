"use client";

/**
 * useChatPinned — a chat's pinned messages, asked of OnlyFans directly.
 *
 * The rail used to derive its pins by filtering the LOADED BACKLOG for
 * `isPinned`, which silently undercounts: a pin older than the first page is
 * invisible, so a thread holding two renders "Pinned · 1". Verified live
 * 2026-07-27 — a real thread's only pin sat outside the head page and the rail
 * showed none at all.
 *
 * That matters more now than it did: the AI pins a fan's own long-form message
 * so it can re-read it later (service/automations/_pins.py), and the same rail
 * is how a human chatter sees what it kept. A rail that hides pins hides
 * exactly the context this was built to surface.
 *
 * ONE request, no pagination — OF's `hasMore` lies on the pinned filter (see
 * of_client.get_pinned_messages). Pinned sets are a handful of messages, so the
 * whole thing arrives in one page or not at all.
 */

import { useQuery } from "@tanstack/react-query";

import { relay, type OFMessage } from "@/lib/relay";
import { type FanId } from "@/lib/fanId";

interface PinnedResp {
  list?: OFMessage[];
}

export function useChatPinned(
  accountId: string | null,
  fanId: FanId | null,
  enabled = true,
) {
  return useQuery<OFMessage[]>({
    queryKey: ["chat-pinned", accountId ?? "", String(fanId ?? "")],
    enabled: !!accountId && !!fanId && enabled,
    // Pins change when a human clicks pin/unpin or the AI writes one — neither
    // is frequent, and neither is urgent enough to justify refetching per
    // render. A minute keeps an operator's own pin from looking like it failed.
    staleTime: 60_000,
    queryFn: async (): Promise<OFMessage[]> => {
      if (!accountId || !fanId) return [];
      const r = await relay.get<PinnedResp>(
        `/api/of/v2/chats/${fanId}/pinned`,
        { accountId },
      );
      return r.list ?? [];
    },
  });
}
