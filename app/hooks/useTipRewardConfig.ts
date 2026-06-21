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
