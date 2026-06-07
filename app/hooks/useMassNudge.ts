"use client";

/**
 * useMassNudge — roll-out hook for the mass_nudge automation.
 *
 * Mass Nudge keeps its whole config in the automation_rules payload (slots,
 * exclude/unsend, with_image) — no separate config store — so per-account
 * save/load goes through useAutomations (rules CRUD). This hook only adds the
 * "apply to many models at once" fan-out (create/enable the rule on each).
 */

import { useMutation, useQueryClient } from "@tanstack/react-query";

import { relay } from "@/lib/relay";
import type { NudgeSlots } from "@/hooks/useNudgeConfig";

export interface MassNudgeConfig {
  with_image?: boolean;
  online_only?: boolean;
  exclude_replied_hours?: number | null;
  exclude_inbound_hours?: number | null;
  unsend_after_hours?: number | null;
  slots?: NudgeSlots;
}

export interface MassNudgeBulkResult {
  applied: { account_id: string; rule: "created" | "updated" }[];
  count: number;
  enabled: boolean;
  every_seconds: number;
}

export interface MassNudgePreview {
  text: string;
  slot: string;
  media: number[];
  hour: number;
  lines: number;
}

/** Compose the broadcast for a chosen hour — text + image, no send. */
export function useMassNudgePreview() {
  return useMutation<
    MassNudgePreview,
    Error,
    { account_id: string; payload: MassNudgeConfig; hour?: number | null }
  >({
    mutationFn: (vars) => relay.post(`/admin/mass-nudge/preview`, vars),
  });
}

export function useMassNudgeBulk() {
  const qc = useQueryClient();
  return useMutation<
    MassNudgeBulkResult,
    Error,
    { account_ids: string[]; payload: MassNudgeConfig; enable?: boolean; every_seconds?: number }
  >({
    mutationFn: (vars) => relay.put(`/admin/mass-nudge/bulk`, vars),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["automation-rules"] }),
  });
}
