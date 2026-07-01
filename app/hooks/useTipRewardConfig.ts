import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { relay, type RelayContext } from "@/lib/relay";

const KEY = "tip-reward-config";
const BG_CTX: RelayContext = { priority: "background" };

/** One folder tier: which vault folders to draw from once the tier basis
 *  (max of this tip and the rolling-window tip sum) reaches `min_basis_cents`. */
export interface TipRewardTier {
  name: string;
  min_basis_cents: number;
  folders: string[];
}

export interface TipRewardConfig {
  enabled?: boolean;
  // Fire the reward on every tip even when an ai_chatter PPV offer is open for
  // the fan (overrides the standdown; the offer is still credited).
  always_reward?: boolean;
  dollars_per_image?: number;
  min_images?: number;
  max_images?: number;
  caption?: string;
  window_hours?: number;
  tiers?: TipRewardTier[];
  // ASK side of the loop (read by of_ai_chat/autoreply): when a fan asks to see
  // content, ask them to tip. On by default; amount null = ask with no set price.
  ask_enabled?: boolean;
  ask_amount_dollars?: number | null;
  ask_template?: string;
  // Inbound-image buying-signal handler — a fan sends US a photo. Two independent
  // flags, both default OFF (handled by webhook_dispatch.on_inbound_image):
  image_reply_enabled?: boolean; // Flag 1: send ONE free vault item back (basic tier)
  image_closer_enabled?: boolean; // Flag 2: kick the ai_chatter closer for the fan
  image_reply_count?: number; // how many free items to send back (usually 1)
  image_reply_basis_cents?: number; // tier basis for the freebie (default 999 = "under $10")
  image_reply_cooldown_hours?: number; // per-fan throttle (also dedups webhook replays)
  image_reply_caption?: string; // optional caption ('' → media-only)
  // Item 42 — "tip request" follow-up: a fan buys a MASS PPV and goes quiet →
  // send one free teaser image + a "send me a tip?" caption. Its own automation
  // (`tip_request`), nested here because it shares this config column.
  tip_request?: {
    enabled?: boolean;
    media_id?: number | null; // global-default teaser vault media id
    caption?: string;
    min_wait_hours?: number; // wait this long after the buy before nudging
    max_age_hours?: number; // don't chase a purchase older than this
    cooldown_hours?: number; // per-fan: at most one tip-request every N hours
  };
}

interface TipRewardConfigResponse {
  account_id: string;
  config: TipRewardConfig;
  defaults: TipRewardConfig;
}

export function useTipRewardConfig(accountId: string | null) {
  return useQuery<TipRewardConfigResponse>({
    queryKey: [KEY, accountId],
    enabled: !!accountId,
    queryFn: () =>
      relay.get<TipRewardConfigResponse>(
        `/admin/tip-reward-config?account_id=${encodeURIComponent(accountId!)}`,
        BG_CTX,
      ),
    staleTime: 60_000,
    refetchOnWindowFocus: false,
  });
}

export function useSaveTipRewardConfig(accountId: string | null) {
  const qc = useQueryClient();
  return useMutation<
    { account_id: string; config: TipRewardConfig },
    Error,
    TipRewardConfig
  >({
    mutationFn: (config) =>
      relay.put(`/admin/tip-reward-config`, { account_id: accountId, config }),
    onSuccess: () => qc.invalidateQueries({ queryKey: [KEY, accountId] }),
  });
}
