"use client";

/**
 * useAccountConfig — the data layer for the per-account "Brain" editor
 * (account_ai_config) on /automations. Reads/writes the owner-gated
 * /admin/account-config routes (covered by the /admin/:path* rewrite).
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { relay, type RelayContext } from "@/lib/relay";

const BG_CTX: RelayContext = { priority: "background" };

export interface BrainConfig {
  persona: string | null;
  welcome_rules: string | null;
  location: string | null;
  utc_offset: number;
  daily_cost_cap_cents: number;
  model: string | null;
  model_by_purpose: Record<string, string>;
  time_activities: Record<string, string>;
  time_images: Record<string, number>;
}

export interface AccountConfigResp {
  account_id: string;
  config: BrainConfig;
  defaults: BrainConfig;    // Lexi-derived starter brain (no images) — seeds blank
                            // accounts and backs the "Reset to defaults" button
  slots: string[];          // the 6 time-of-day slot keys, ordered
  model_options: string[];  // LLM model ids the account may pick
  purposes: string[];       // per-purpose override targets
}

export function useAccountConfig(accountId: string | null) {
  return useQuery<AccountConfigResp>({
    queryKey: ["account-config", accountId],
    enabled: !!accountId,
    queryFn: () =>
      relay.get<AccountConfigResp>(
        `/admin/account-config?account_id=${encodeURIComponent(accountId!)}`,
        BG_CTX,
      ),
    staleTime: 30_000,
    refetchOnWindowFocus: false,
  });
}

export function useSaveAccountConfig(accountId: string | null) {
  const qc = useQueryClient();
  return useMutation<{ account_id: string; config: BrainConfig }, Error, BrainConfig>({
    mutationFn: (config) =>
      relay.put("/admin/account-config", { account_id: accountId, config }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["account-config", accountId] }),
  });
}
