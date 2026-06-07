"use client";

/**
 * useWebhookConfig — read/write account_ai_config.webhook_config_json, the
 * per-account gate for W7 webhook-priority dispatch (Settings → "Instant reply").
 *
 * When enabled, an inbound fan DM makes the bot react in real time (seconds)
 * instead of waiting for the 30s poll tick. `delay_seconds` (+ random
 * `jitter_seconds`) is how long the AI waits before its reply lands — feels
 * human AND gives a live human chatter a head start. Default OFF.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { relay, type RelayContext } from "@/lib/relay";

const KEY = "webhook-config";
const BG_CTX: RelayContext = { priority: "background" };

export interface WebhookConfig {
  enabled?: boolean;
  delay_seconds?: number;
  jitter_seconds?: number;
  /** Minutes the bot stands down after a HUMAN sends in the chat (manual chatter
   *  wins). 0 disables the yield. Default 1. */
  manual_yield_minutes?: number;
  /** Typing speed (words/min). Each reply bubble is held for the time it'd take
   *  to type it, so replies don't pop instantly. 0 disables. Default 60. */
  typing_wpm?: number;
  /** Show the fan OF's live "...is typing" bubble during the typing_wpm hold.
   *  ON by default for every account; set false to opt this account out. */
  typing_indicator?: boolean;
}

interface WebhookConfigResponse {
  account_id: string;
  config: WebhookConfig;
  defaults: WebhookConfig;
}

export function useWebhookConfig(accountId: string | null) {
  return useQuery<WebhookConfigResponse>({
    queryKey: [KEY, accountId],
    enabled: !!accountId,
    queryFn: () =>
      relay.get<WebhookConfigResponse>(
        `/admin/webhook-config?account_id=${encodeURIComponent(accountId!)}`,
        BG_CTX,
      ),
    staleTime: 60_000,
    refetchOnWindowFocus: false,
  });
}

export function useSaveWebhookConfig(accountId: string | null) {
  const qc = useQueryClient();
  return useMutation<
    { account_id: string; config: WebhookConfig },
    Error,
    WebhookConfig
  >({
    mutationFn: (config) =>
      relay.put(`/admin/webhook-config`, { account_id: accountId, config }),
    onSuccess: () => qc.invalidateQueries({ queryKey: [KEY, accountId] }),
  });
}
