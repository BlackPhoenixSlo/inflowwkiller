import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { relay, type RelayContext } from "@/lib/relay";

const KEY = "vault-ai-config";
const BG_CTX: RelayContext = { priority: "background" };

/** Config shape frozen in plans/VAULT_AI_ACTIONS_CONTRACT.md §1. The API always
 *  returns the EFFECTIVE blob (defaults + operator overrides); PATCH deep-merges
 *  a partial and returns the new effective blob. */

export type VaultAiTier = "safe" | "suggestive" | "explicit" | "graphic" | "unknown";

export interface VaultAiModels {
  describe: string;
  escalation: string;
  caption: string;
  script: string;
  escalate_below_confidence: number;
}

export interface VaultAiDescribe {
  cadence_hours: number;
  describe_all_cap_percent: number;
  max_items_per_run: number;
  images: boolean;
  videos: boolean;
}

export interface VaultAiPricing {
  enabled: boolean;
  /** [min_cents, max_cents] per explicitness tier. */
  bands_by_tier: Record<VaultAiTier, [number, number]>;
}

export interface VaultAiFolders {
  /** "internal" until OF vault-writes are captured. */
  mode: "internal" | "of_mirror";
  taxonomy: string[];
  max_folders_per_item: number;
}

export interface VaultAiDailyReminder {
  enabled: boolean;
  auto_post: boolean;
  auto_mass_message: boolean;
  folder_name: string;
  lines: string[];
  images_per_day: number;
  on_under_min_unseen: "stop" | "use_fewer" | "repeat_after_window";
  repeat_after_days: number;
  per_fan_cooldown_hours: number;
  daily_at: string[];
  tz_offset_minutes: number;
}

/** The PPV week/month escalation arc (service/vault_ppv_week.py). */
export interface VaultAiPpvWeek {
  enabled: boolean;
  /** 1 = a single week, up to 4 = a month of exclusive waves. */
  weeks: number;
  /** Each drop = paid post (feed) + mass PPV (DM). */
  combine_feed_and_dm: boolean;
  /** Captions written via PAINFUL_TEXTING rather than the template. */
  in_voice_copy: boolean;
}

export interface VaultAiConfig {
  /** Master gate — nothing describes/proposes/sends until true. */
  enabled: boolean;
  /** HARD constraint in v1; UI shows it, cannot be turned off. */
  suggest_only: boolean;
  models: VaultAiModels;
  describe: VaultAiDescribe;
  pricing: VaultAiPricing;
  tier_labels: Record<VaultAiTier, string>;
  folders: VaultAiFolders;
  scoring: { story: boolean; tip: boolean };
  daily_reminder: VaultAiDailyReminder;
  ppv_week: VaultAiPpvWeek;
}

/** The generated arc — a self-contained HTML review page + rollup stats. */
export interface VaultPpvWeekResult {
  account_id: string;
  weeks: number;
  summary: Record<string, number>;
  coverage: Record<string, unknown>;
  html: string;
}

interface VaultAiConfigResponse {
  account_id: string;
  config: VaultAiConfig;
}

export function useVaultAiConfig(accountId: string | null) {
  return useQuery<VaultAiConfigResponse>({
    queryKey: [KEY, accountId],
    enabled: !!accountId,
    queryFn: () =>
      relay.get<VaultAiConfigResponse>(
        `/admin/account-ai-config/vault-ai?account_id=${encodeURIComponent(accountId!)}`,
        BG_CTX,
      ),
    staleTime: 60_000,
    refetchOnWindowFocus: false,
  });
}

/** PATCH is a deep-merge over stored — send only the keys you're changing.
 *  The response is the full new effective blob. */
export function useSaveVaultAiConfig(accountId: string | null) {
  const qc = useQueryClient();
  return useMutation<VaultAiConfigResponse, Error, Partial<VaultAiConfig>>({
    mutationFn: (patch) =>
      relay.patch(`/admin/account-ai-config/vault-ai`, { account_id: accountId, config: patch }),
    onSuccess: (data) => qc.setQueryData([KEY, accountId], data),
  });
}

/** Generate the PPV week/month arc for review. Read-only + suggest-only — nothing
 *  is armed or sent. `use_llm` writes captions in-voice (slower, ~7·weeks calls). */
export function useGenerateVaultPpvWeek(accountId: string | null) {
  return useMutation<
    VaultPpvWeekResult,
    Error,
    { weeks: number; use_llm: boolean; combine: boolean }
  >({
    mutationFn: ({ weeks, use_llm, combine }) =>
      relay.get<VaultPpvWeekResult>(
        `/admin/vault-ai/ppv-week?account_id=${encodeURIComponent(accountId!)}` +
          `&weeks=${weeks}&use_llm=${use_llm}&combine=${combine}`,
      ),
  });
}
