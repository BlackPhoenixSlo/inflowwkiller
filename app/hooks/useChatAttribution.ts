"use client";

/**
 * useChatAttribution — map of outbound message_id → sender employee.
 *
 * Backs the "· Sent by {name}" label under outbound bubbles. Reads from
 * the local DB via /admin/messages/.../attribution; deleted-employee rows
 * come back with `display_name: null` so the UI's truthy-guard skips them
 * (never renders "Sent by null").
 *
 * Keyed off the oldest visible message id so loading older pages can
 * widen the range; staleTime is long because outbound-employee attribution
 * never mutates after write.
 */

import { useQuery } from "@tanstack/react-query";

import { relay } from "@/lib/relay";

export interface AttributionEntry {
  employee_id: number | null;
  display_name: string | null;
  color: string | null;
}

export interface AttributionResponse {
  by_msg_id: Record<string, AttributionEntry>;
}

export function useChatAttribution(
  accountId: string | null,
  fanId: number | null,
  oldestId: number | null,
) {
  return useQuery<AttributionResponse>({
    queryKey: ["msg-attribution", accountId, fanId, oldestId],
    enabled: !!accountId && fanId != null,
    queryFn: () => {
      const params = new URLSearchParams();
      if (oldestId != null && Number.isFinite(oldestId)) {
        params.set("since_id", String(oldestId));
      }
      const qs = params.toString();
      const path = `/admin/messages/${encodeURIComponent(accountId!)}/${fanId}/attribution${qs ? `?${qs}` : ""}`;
      return relay.get<AttributionResponse>(path);
    },
    staleTime: 5 * 60 * 1000,
    refetchOnWindowFocus: false,
  });
}
