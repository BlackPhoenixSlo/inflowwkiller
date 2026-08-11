"use client";

/**
 * useAutomations — the data layer for the /automations control surface.
 *
 * Reads/writes the `automation_rules` table via the relay's
 * /admin/automation-rules/* routes. A "rule" is one automation_rules row: a
 * kind + an `every_seconds` cadence + a JSON `payload` (the per-run knob bag) +
 * an enabled flag. The executor materialises every enabled rule on its cadence;
 * `runNow` enqueues one immediate job, bypassing the timer.
 *
 * Per-account list (owner-only routes; a chatter session 403s). Mutations
 * invalidate the per-account list so the table reflects the new state.
 */

import {
  keepPreviousData,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";

import { relay, type RelayContext } from "@/lib/relay";

const KEY = "automation-rules";
const BG_CTX: RelayContext = { priority: "background" };

// ── Types (mirror automation_rules_api.py) ────────────────────────────

export interface KnobHint {
  key: string;
  type: "int" | "bool" | "str" | "ids" | "json";
  hint: string;
  /** Input widget the editor renders (derived from `type` server-side). */
  widget?: "number" | "switch" | "text" | "ids" | "json" | "select";
  min?: number;
  max?: number;
  default?: number | string | boolean;
  /** Allowed values for a "select"/str knob. */
  enum?: (string | number)[];
}

export interface AutomationKind {
  kind: string;
  label: string;
  /** false = action-style kind (does nothing on a bare timer without a payload). */
  recurring: boolean;
  summary: string;
  knobs: KnobHint[];
  /** When set, the editor builds this kind's whole payload with the named rich
   *  composer (instead of the per-knob fields). The knobs stay as the raw-JSON
   *  fallback. See automation_rules_api._CATALOG. */
  composer?: "mass_message" | "premade";
  /** Which control surface this kind belongs to: "rules" | "brain" | "ready_made"
   *  | "settings" | "internal". The /automations rule editor only lists "rules"
   *  kinds; everything else is driven from its own surface. */
  surface?: string;
  /** Suggested default cadence (seconds) for a NEW rule of this kind. The editor
   *  seeds the cadence field from it on kind-pick; absent → fall back to 300. */
  cadence_default_s?: number;
  /** One-line "good settings" example shown under the kind picker. */
  example?: string;
  /** Editor grouping for the kind dropdown: "advanced" → utility optgroup;
   *  absent / anything else → "core". */
  group?: string;
}

export interface RuleLastRun {
  status: string;
  started_at: string | null;
  completed_at: string | null;
  error_text: string | null;
  stats: Record<string, unknown> | null;
}

/** Rich trigger (mirrors automation_rules_api._validate_trigger). Exactly one
 *  of `every_seconds` (interval) or `daily_at` (clock times) drives cadence;
 *  `max_runs` optionally auto-disables the rule after N total runs. */
export interface Trigger {
  every_seconds?: number;
  daily_at?: string[];            // ["09:00","20:30"] in the local zone
  tz_offset_minutes?: number;     // minutes to add to UTC for local
  max_runs?: number;
}

export interface AutomationRule {
  id: number;
  account_id: string;
  name: string;
  kind: string;
  is_enabled: boolean;
  every_seconds: number | null;
  trigger: Trigger;
  payload: Record<string, unknown>;
  /** Opt-in quiet hours [start, end] in creator-local hours; null = off (24/7). */
  quiet_hours: number[] | null;
  created_at: string | null;
  last_run: RuleLastRun | null;
  next_due_at: string | null;
  has_pending_job: boolean;
}

// ── Reads ─────────────────────────────────────────────────────────────

export function useAutomationKinds() {
  return useQuery<AutomationKind[]>({
    queryKey: ["automation-kinds"],
    queryFn: async () => {
      const r = await relay.get<{ kinds: AutomationKind[] }>(
        "/admin/automation-kinds",
        BG_CTX,
      );
      return r.kinds ?? [];
    },
    // The catalog is process-static — refetch rarely.
    staleTime: 24 * 60 * 60_000,
    refetchOnWindowFocus: false,
  });
}

export function useAutomationRules(accountId: string | null) {
  return useQuery<AutomationRule[]>({
    queryKey: [KEY, accountId],
    enabled: !!accountId,
    queryFn: async () => {
      const r = await relay.get<{ rules: AutomationRule[] }>(
        `/admin/automation-rules?account_id=${encodeURIComponent(accountId!)}`,
        BG_CTX,
      );
      return r.rules ?? [];
    },
    // Poll so a running/last-run state refreshes without a manual reload.
    staleTime: 15_000,
    refetchInterval: 30_000,
    refetchOnWindowFocus: true,
    placeholderData: keepPreviousData,
  });
}

// ── Writes ────────────────────────────────────────────────────────────

export interface RuleDraft {
  account_id: string;
  kind: string;
  name?: string | null;
  every_seconds?: number;
  trigger?: Trigger;              // wins over every_seconds when present
  payload?: Record<string, unknown>;
  quiet_hours?: number[];         // [start, end] local hours; omit/[0,0] = off
  is_enabled?: boolean;
}

export interface RulePatch {
  name?: string;
  every_seconds?: number;
  trigger?: Trigger;
  payload?: Record<string, unknown>;
  quiet_hours?: number[];         // [0,0] clears, [start, end] sets
  is_enabled?: boolean;
}

export function useCreateRule(accountId: string | null) {
  const qc = useQueryClient();
  return useMutation<AutomationRule, Error, RuleDraft>({
    mutationFn: (draft) =>
      relay.post<AutomationRule>("/admin/automation-rules", draft),
    onSuccess: () => qc.invalidateQueries({ queryKey: [KEY, accountId] }),
  });
}

export function useUpdateRule(accountId: string | null) {
  const qc = useQueryClient();
  return useMutation<AutomationRule, Error, { id: number } & RulePatch>({
    mutationFn: ({ id, ...patch }) =>
      relay.patch<AutomationRule>(`/admin/automation-rules/${id}`, patch),
    onSuccess: () => qc.invalidateQueries({ queryKey: [KEY, accountId] }),
  });
}

export function useDeleteRule(accountId: string | null) {
  const qc = useQueryClient();
  return useMutation<unknown, Error, number>({
    mutationFn: (id) => relay.delete(`/admin/automation-rules/${id}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: [KEY, accountId] }),
  });
}

/** Compose-only preview of what an automation would send for one fan — text +
 *  chosen image id, NO send (mirrors /admin/automation-preview). send_welcome only. */
export interface AutomationPreviewResult {
  account_id: string;
  kind: string;
  text: string;
  /** send_welcome: the actual send shape — one entry per chat bubble (the image
   *  rides on bubble 1; bubble 2 is AI-restyled at send time). */
  bubbles?: string[];
  image: number | null;
  name: string;
  slot: string;
  /** send_followup only: which escalation step (1–3) was previewed. */
  step?: number;
  /** send_welcome only: whether bubble 2 is the real AI-restyled line (vs verbatim). */
  restyled?: boolean;
  /** send_welcome only: the restyle hit the daily AI cost cap → verbatim fallback. */
  cap_hit?: boolean;
  /** send_welcome only: this slot has an operator-pinned line — bubble 2 is that
   *  exact line (what will ship), not a fresh roll. */
  pinned?: boolean;
}

export function useAutomationPreview() {
  return useMutation<
    AutomationPreviewResult,
    Error,
    {
      account_id: string;
      kind: string;
      fan_id?: number | null;
      test_name?: string | null;
      /** send_welcome: run the real AI restyle so the preview matches what ships. */
      restyle?: boolean;
      /** send_welcome: pin a time-of-day slot (one of the 6 slot keys); null = now. */
      slot?: string | null;
      model?: string | null;
      /** send_welcome: unsaved on-screen draft config to preview against. */
      config?: Record<string, unknown> | null;
      /** send_welcome: Regenerate bypasses a pinned slot line to sample a fresh one. */
      ignore_pin?: boolean;
      /** send_welcome: 2nd bubble = day/time + location only (no activity line). */
      time_only?: boolean;
      /** send_welcome: operator question appended word-for-word as the 3rd bubble. */
      question?: string | null;
    }
  >({
    mutationFn: (vars) => relay.post("/admin/automation-preview", vars),
  });
}

/** Pin (line set) or unpin (line null/"") the approved welcome line for a slot, so
 *  send_welcome sends that exact line instead of re-rolling. Mirrors the "keep this
 *  one" flow in the Brain preview. */
export function useWelcomePin() {
  return useMutation<
    { account_id: string; slot: string; pinned: boolean; pins: Record<string, unknown> },
    Error,
    { account_id: string; slot: string; line?: string | null }
  >({
    mutationFn: (vars) => relay.post("/admin/welcome-pin", vars),
  });
}

export function useRunRuleNow(accountId: string | null) {
  const qc = useQueryClient();
  return useMutation<
    { enqueued_job_id: number; kind: string },
    Error,
    number
  >({
    mutationFn: (id) =>
      relay.post(`/admin/automation-rules/${id}/run-now`, {}),
    onSuccess: () => qc.invalidateQueries({ queryKey: [KEY, accountId] }),
  });
}
