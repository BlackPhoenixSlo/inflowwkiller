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
