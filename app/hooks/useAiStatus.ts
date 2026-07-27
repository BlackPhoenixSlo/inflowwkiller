/**
 * useAiStatus — what the AI is doing with THIS fan, and if nothing, why.
 *
 * Backs the strip under the chat header. The server evaluates the gates in the same
 * order ai_chatter.run() does and returns the FIRST one that fires, so the strip names
 * the thing that actually stopped her rather than merely a thing that is true.
 *
 * Polled, not pushed: the state is mostly timers elapsing (a rhythm break waking, a
 * cooldown expiring), which no event ever fires for.
 */
import { useQuery } from "@tanstack/react-query";

import { relay } from "@/lib/relay";

export type AiState = "active" | "paused" | "companion" | "blocked" | "off";

/**
 * The daily quota's verdict names, mirroring the `QUOTA_*` constants in
 * service/automations/_leash.py. Widened with `string` at the use site: the engine may
 * name a new outcome before the UI knows it, and an unlisted reason must fall back to
 * the plain counter, never blank the chip.
 */
export type DailyReason =
  | "held" | "runway" | "under_quota" | "unlimited"
  | "signal_lift" | "spend_lift" | "backoff_served" | "no_ladder" | "off";

export type AiStatus = {
  state: AiState;
  label: string;
  detail: string | null;
  until: string | null; // ISO — when the pause lifts
  engine: "ai_chatter" | "ai_upseller" | "none";
  graduated: string | null; // of_ai_chat handed him off (e.g. "spent")
  ladder: { status: string; rung: number };
  /** The language she writes to this fan (resolved), + where it came from. */
  language: string;
  language_source: string;
  open_ask: { price_cents: number | null; at: string | null; by: "ai" | "human" } | null;
  last_paid_at: string | null;
  force_ask: boolean;
  /** The reply budget — deterministic, and NOT the same thing as a break. */
  cadence: {
    enabled: boolean;
    tier?: "baseline" | "buying_signal" | "no_signal" | "pic_sent" | "post_purchase" | string;
    used?: number; // replies this burst (3-5 bubbles = 1 reply)
    cap?: number | null; // null = uncapped
    left?: number | null;
    stopped?: boolean;
    resets_after_minutes?: number;
    last_message_at?: string | null;
    /** Item 21c — the DAILY ceiling that sits above the burst cap. Absent while
     *  cadence is off. Unlike the cap, a hold HAS a clock. */
    daily?: {
      /** The gate's own verdict name — dispatch on THIS, never re-derive the state
       *  from the numbers. The exit order of `_quota_gate` is not the UI's to know. */
      reason: DailyReason | string;
      used: number;
      quota: number | null; // null = no ceiling reaches him (runway, or a whale)
      runway_left: number | null; // replies before the ceiling starts applying
      held: boolean;
      enforced: boolean; // false = shadow — recorded, but she still replied
      backoff_hours: number | null; // how long she stays quiet once held
      dry_days: number | null; // since his last money event (or first contact)
    };
  };
  offer_caps_ok: boolean | null;
  /** A live ask or a fresh purchase → she cannot randomly wander off mid-sell. */
  break_proof: boolean;
  /** Work already queued for this fan — a deferred reply is pending, not silence. */
  next_action: { kind: string; at: string } | null;
  /** May she put a PRICE in front of him this turn, and if not, what refused. */
  gate: { enabled: boolean; ok?: boolean; why?: string | null };
  /** The 7-day spend brake: past the cap he stops being CHARGED (not silenced). */
  spend_7d: { paid_cents: number; cap_cents: number | null; capped: boolean };
  /** Is she even thinking about him. cost is in CENTS (the column is millicents). */
  llm: {
    last_at: string | null;
    model: string | null;
    purpose: string | null;
    calls_24h: number;
    cost_24h_cents: number;
  };
};

export function useAiStatus(accountId: string | null, fanId: number | null) {
  return useQuery<AiStatus>({
    queryKey: ["ai-status", accountId, fanId],
    enabled: !!accountId && !!fanId,
    queryFn: () => relay.get(`/admin/fans/${accountId}/${fanId}/ai-status`),
    // A break can lapse at any second and nothing emits an event when it does.
    refetchInterval: 15_000,
    staleTime: 10_000,
  });
}
