"use client";

/**
 * useAutoreplyConfig — read/write account_ai_config.autoreply_config_json, the
 * per-account gate + knobs for the `autoreply` automation (Automations →
 * "Auto-reply").
 *
 * autoreply keeps a conversation warm: when WE spoke last and a quiet, low-spend
 * KNOWN fan hasn't replied for a few minutes, it sends ONE casual, never-PPV
 * line (tone-matched to the recent messages). Default OFF.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { relay, type RelayContext } from "@/lib/relay";

const KEY = "autoreply-config";
const BG_CTX: RelayContext = { priority: "background" };

export interface AutoreplyConfig {
  enabled?: boolean;
  silence_min_minutes?: number;
  silence_max_minutes?: number;
  max_nudges?: number;
  min_gap_minutes?: number;
  max_lifetime_spend_cents?: number;
  recent_spend_days?: number;
  max_recent_spend_cents?: number;
  min_days_since_purchase?: number;
  min_days_since_first_chat?: number;
  last_n_messages?: number;
  /** Skip the "complete profile" gate — reply from the last few messages +
   *  whatever's already known, even for fans we don't fully know yet. */
  info_not_required?: boolean;
  /** [startHour, endHour] creator-local; null = 24/7 (never quiet). */
  quiet_hours_json?: [number, number] | null;
}

interface AutoreplyConfigResponse {
  account_id: string;
  config: AutoreplyConfig;
  defaults: AutoreplyConfig;
}

export function useAutoreplyConfig(accountId: string | null) {
  return useQuery<AutoreplyConfigResponse>({
    queryKey: [KEY, accountId],
    enabled: !!accountId,
    queryFn: () =>
      relay.get<AutoreplyConfigResponse>(
        `/admin/autoreply-config?account_id=${encodeURIComponent(accountId!)}`,
        BG_CTX,
      ),
    staleTime: 60_000,
    refetchOnWindowFocus: false,
  });
}

export function useSaveAutoreplyConfig(accountId: string | null) {
  const qc = useQueryClient();
  return useMutation<
    { account_id: string; config: AutoreplyConfig },
    Error,
    AutoreplyConfig
  >({
    mutationFn: (config) =>
      relay.put(`/admin/autoreply-config`, { account_id: accountId, config }),
    onSuccess: () => qc.invalidateQueries({ queryKey: [KEY, accountId] }),
  });
}
