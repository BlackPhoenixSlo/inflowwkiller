"use client";

/**
 * useNudgeConfig — read/write account_ai_config.nudge_config_json for the
 * nudge_online automation (Settings → "Nudge online").
 *
 * The 60s detector cadence is an automation_rules row (managed via
 * useAutomations); THIS hook owns the rich config — content_mode, repeat_mode,
 * delays, caps, quiet hours and the per-slot tease pools — which both the
 * detector and the delayed fire-job read off account_ai_config. Owner-gated
 * relay routes; a partial config is fine (the automation merges over defaults).
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { relay, type RelayContext } from "@/lib/relay";

const KEY = "nudge-config";
const BG_CTX: RelayContext = { priority: "background" };

export interface NudgeSlotEntry {
  text: string[];
  image?: number[];
}
/** day-bucket ("default" | "weekend" | "friday" | …) → slot key → entry. */
export type NudgeSlots = Record<string, Record<string, NudgeSlotEntry>>;

export interface NudgeConfig {
  enabled?: boolean;
  // Two-part message model:
  //   nudge line — time-of-day greeting (+ image)
  //   info line  — Q&A / tease line from the fan's profile
  send_nudge_line?: boolean;
  nudge_line_text?: boolean;   // false → image-only opener
  nudge_line_image?: boolean;
  send_info_line?: boolean;
  info_line_mode?: "qa" | "tease" | "mix";
  info_line_image?: boolean;
  repeat_mode?: "new" | "same";
  delay_minutes?: number;
  jitter_minutes?: number;
  gap_minutes?: number;
  min_hours_between_nudges?: number;
  max_per_tick?: number;
  max_online_scan?: number;
  online_recent_minutes?: number;
  welcome_grace_hours?: number;
  active_convo_hours?: number;
  max_no_reply?: number;
  require_welcomed?: boolean;
  // Spend tier (cents; null/absent = off). Lifetime + a rolling N-day window.
  min_lifetime_spend_cents?: number | null;
  max_lifetime_spend_cents?: number | null;
  recent_spend_days?: number;
  min_recent_spend_cents?: number | null;
  max_recent_spend_cents?: number | null;
  quiet_hours?: [number, number];
  slots?: NudgeSlots;
}

interface NudgeConfigResponse {
  account_id: string;
  config: NudgeConfig;
  defaults: NudgeConfig;
}

export function useNudgeConfig(accountId: string | null) {
  return useQuery<NudgeConfigResponse>({
    queryKey: [KEY, accountId],
    enabled: !!accountId,
    queryFn: () =>
      relay.get<NudgeConfigResponse>(
        `/admin/nudge-config?account_id=${encodeURIComponent(accountId!)}`,
        BG_CTX,
      ),
    staleTime: 60_000,
    refetchOnWindowFocus: false,
  });
}

export function useSaveNudgeConfig(accountId: string | null) {
  const qc = useQueryClient();
  return useMutation<
    { account_id: string; config: NudgeConfig },
    Error,
    NudgeConfig
  >({
    mutationFn: (config) =>
      relay.put(`/admin/nudge-config`, { account_id: accountId, config }),
    onSuccess: () => qc.invalidateQueries({ queryKey: [KEY, accountId] }),
  });
}

export interface BulkApplyResult {
  applied: { account_id: string; rule: "created" | "updated" }[];
  count: number;
  enabled: boolean;
  config: NudgeConfig;
}

export interface NudgePreviewMessage {
  kind: "nudge" | "info" | string;
  text: string;
  media: number[];
}
export interface NudgePreviewResult {
  account_id: string;
  fan_id: number | null;
  slot: string;
  name: string;
  messages: NudgePreviewMessage[];
}

/** Preview the exact message list a nudge would send for a fan + hour — no send. */
export function useNudgePreview() {
  return useMutation<
    NudgePreviewResult,
    Error,
    { account_id: string; config: NudgeConfig; fan_id?: number | null; hour?: number | null }
  >({
    mutationFn: (vars) => relay.post(`/admin/nudge-config/preview`, vars),
  });
}

/** Apply one shared config to many models at once and (optionally) enable each
 *  model's 60s nudge rule. Text-only — the backend forces with_image off. */
export function useBulkApplyNudgeConfig() {
  const qc = useQueryClient();
  return useMutation<
    BulkApplyResult,
    Error,
    { account_ids: string[]; config: NudgeConfig; enable?: boolean }
  >({
    mutationFn: (vars) => relay.put(`/admin/nudge-config/bulk`, vars),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: [KEY] });
      qc.invalidateQueries({ queryKey: ["automation-rules"] });
    },
  });
}
