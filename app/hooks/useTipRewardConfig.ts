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
  /** Context matcher: swap up to 3 reward photos for vault photos matching the
   *  fan's recent asks (last 20 msgs, vault-AI descriptions). Backend default ON. */
  context_pick_enabled?: boolean;
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
  // Flag 3: vision-describe the photo/gif/clip HE sends, so the AI can react to it
  // ("[he sent: …]"). DEFAULT ON server-side — an absent key means enabled, which
  // is why the UI must read it with `?? true`, not `!!`.
  image_describe_enabled?: boolean;
  image_describe_scope?: "all" | "paid"; // who to spend a vision call on
  image_describe_prompt?: string;        // operator emphasis appended to the read
  // Hot-thread proactive teaser — SELECTED here (vault-media home), SENT by
  // ai_chatter when thread_heat says the thread is HOT and no priced offer is going
  // out. Free warm-up for a $0 fan (capped), a priced tease PPV for a proven buyer.
  hot_teaser_enabled?: boolean;
  hot_teaser_count?: number; // vault items per teaser
  hot_teaser_cooldown_hours?: number; // per-fan throttle (both branches)
  hot_teaser_free_folder?: string; // vault folder for $0 fans (sent FREE)
  hot_teaser_free_max?: number; // hard cap on FREE teasers a fan ever gets
  hot_teaser_paid_folder?: string; // vault folder for proven buyers (priced PPV)
  hot_teaser_price_cents?: number; // price of the paid tease PPV
  // Conversational teaser LADDER (read by ai_chatter) — not hot-gated. Every N of his
  // messages, send the next rung; the price climbs free → $10 → $50 as the chat goes.
  teaser_convo_enabled?: boolean;
  teaser_convo_after_fan_msgs?: number; // his messages between rungs
  teaser_convo_count?: number; // vault items per tease
  teaser_convo_rungs?: { folder: string; price_cents: number }[];
  // Adaptive climb (backend default ON): the rung climbs only when the LAST teaser
  // actually sold; on a no-buy the ask softens to 65–73% and the rung holds.
  // false → legacy climb-one-rung-every-send regardless of buying.
  teaser_convo_adaptive?: boolean;
  // Free BAIT leg for a PROVEN buyer (backend default ON). Off, his ask sinks to the
  // rung's set price and repeats it — that price is the bottom AND the bait, so the
  // ladder has no next move. On, it alternates set price ↔ free instead.
  teaser_convo_bait_for_buyers?: boolean;
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
