"use client";

/**
 * useStyleConfig — read/write account_ai_config.style_config_json, the per-
 * automation opt-in for the "human texting style" package (short/casual girl
 * voice + 3-bubble splitting + casualized lowercase Q/Tease).
 *
 * Each flag gates ONE automation; when false (the default) that automation runs
 * its current prompt + 2-bubble cap unchanged. Surfaced as three checkboxes in
 * the Settings "Auto Convo" tab. Mirrors useAutoreplyConfig.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { relay, type RelayContext } from "@/lib/relay";

const KEY = "style-config";
const BG_CTX: RelayContext = { priority: "background" };

export interface StyleConfig {
  of_ai_chat?: boolean;
  autoreply?: boolean;
  deep_convo?: boolean;
  ai_chatter?: boolean;
  // account-wide (not per-automation): strip every emoji at send time
  strip_emojis?: boolean;
  // Auto Convo (of_ai_chat) rich-profile grounding — default ON
  factground_of_ai_chat?: boolean;
  // account-wide brevity/emotion framing ("painful texting") — default ON
  painful_texting?: boolean;
  // May this creator sell CUSTOMS — a paid voice note recorded to order and
  // delivered here later? DEFAULT **OFF**, unlike every other flag on this type,
  // because it governs what the bot may PROMISE a fan who has already paid.
  // Independent of the tip-ask toggle: on its own it opens customs in Auto Convo,
  // of_ai_chat and the AI Chatter manifest. Fulfilment is manual — nothing in the
  // product records the owed recording.
  sell_customs?: boolean;
  // cat-sticker reaction pack (AI Chatter ends some replies with a cat gif) — default ON
  cat_stickers?: boolean;
  /** Does the creator have a DAY the chat engines can answer with — "what are you
   *  up to?" gets something true and concrete instead of a bounced question.
   *  Account-wide (not per-automation) because the day belongs to the creator, not
   *  to whichever engine happened to answer. DEFAULT ON. */
  day_log_enabled?: boolean;
  // sticker rate knobs (numeric): % of replies that never see the pack (default 0),
  // % nudged to a gif-ONLY reply (default 5), per-fan minutes between gifs (default 0)
  cat_sticker_skip_pct?: number;
  cat_sticker_solo_pct?: number;
  cat_sticker_gap_min?: number;
  // independent thumb-typo injector toggle, per automation
  typos_of_ai_chat?: boolean;
  typos_autoreply?: boolean;
  typos_deep_convo?: boolean;
  typos_ai_chatter?: boolean;
  // independent non-native English layer toggle, per automation
  nonnative_of_ai_chat?: boolean;
  nonnative_autoreply?: boolean;
  nonnative_deep_convo?: boolean;
  nonnative_ai_chatter?: boolean;
  /** Space before "?" — "you like it ?". Part of the non-native register but its
   *  own key, so it can be turned off without losing the rest of the layer.
   *  Same tri-state default as its parent (ai_chatter ON, others OFF). */
  nonnative_spacing_of_ai_chat?: boolean;
  nonnative_spacing_autoreply?: boolean;
  nonnative_spacing_deep_convo?: boolean;
  nonnative_spacing_ai_chatter?: boolean;
  // PHASE 2 pre-send self-consistency check. ONLY these two engines call it, so
  // there is deliberately no consistency_autoreply / consistency_deep_convo — a
  // flag nothing reads is a checkbox that lies. Costs a second LLM call on the
  // replies it fires for, so unlike the layers above it defaults OFF, never
  // tri-state.
  consistency_of_ai_chat?: boolean;
  consistency_ai_chatter?: boolean;
}

interface StyleConfigResponse {
  account_id: string;
  config: StyleConfig;
  defaults: StyleConfig;
}

export function useStyleConfig(accountId: string | null) {
  return useQuery<StyleConfigResponse>({
    queryKey: [KEY, accountId],
    enabled: !!accountId,
    queryFn: () =>
      relay.get<StyleConfigResponse>(
        `/admin/style-config?account_id=${encodeURIComponent(accountId!)}`,
        BG_CTX,
      ),
    // Short stale + always refetch on mount so the two surfaces that edit this
    // (the Auto Convo tab + the rule editor) always show the same fresh state.
    staleTime: 5_000,
    refetchOnMount: "always",
    refetchOnWindowFocus: false,
  });
}

export function useSaveStyleConfig(accountId: string | null) {
  const qc = useQueryClient();
  return useMutation<
    { account_id: string; config: StyleConfig },
    Error,
    StyleConfig
  >({
    mutationFn: (config) =>
      relay.put(`/admin/style-config`, { account_id: accountId, config }),
    onSuccess: () => qc.invalidateQueries({ queryKey: [KEY, accountId] }),
  });
}
