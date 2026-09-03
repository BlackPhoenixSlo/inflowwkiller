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
import { type FanId } from "@/lib/fanId";

export interface AttributionEntry {
  employee_id: number | null;
  display_name: string | null;
  // Which automation sent it ("welcome_chatter_for_info", "autoreply", "welcome", …). When
  // present, the bubble label shows the specific automation name instead of the
  // flat "Automation" sentinel that every bot send's display_name resolves to.
  automation_kind: string | null;
  // Denormalized mass sender tag ("mass-kingsley1") for human broadcast rows;
  // "" (or null) for 1:1 sends and automation-fired mass. The bubble prefers it
  // over the automation label / employee name when present.
  sender_name: string | null;
  color: string | null;
}

export interface AttributionResponse {
  by_msg_id: Record<string, AttributionEntry>;
}

export function useChatAttribution(
  accountId: string | null,
  fanId: FanId | null,
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
