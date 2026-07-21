/**
 * AiStatusStrip — one line under the fan's handle answering "what is the AI doing with
 * this fan, and if it's doing nothing, why, and until when".
 *
 * Before this, the only way to know was to SSH into prod and read four tables
 * (fans / skip_list / rhythm_state / ladder_state). That is how a 23-minute Human
 * Rhythm break on the hottest thread on the roster went unnoticed while a human closed
 * the sale by hand.
 *
 * The strip reports; it never decides. Every value comes from the engine's own state.
 */
"use client";

import { useAiStatus, type AiState } from "@/hooks/useAiStatus";
import { cn } from "@/lib/utils";

type Props = { accountId: string; fanId: number };

const TONE: Record<AiState, string> = {
  active: "text-emerald-400",
  paused: "text-amber-400",
  companion: "text-sky-400",
  blocked: "text-rose-400",
  off: "text-fg-dim",
};

const DOT: Record<AiState, string> = {
  active: "●",
  paused: "💤",
  companion: "💬",
  blocked: "🚫",
  off: "⭘",
};

/**
 * Plain English for the gate's refusal codes. A raw token like "low_information" in the
 * UI is just the code leaking — it tells you a name, not a reason.
 */
const GATE_WHY: Record<string, string> = {
  low_information:
    "His last message was too thin to price against ('k', 'ok', 'lol'). She keeps talking — she just won't put a number in front of noise.",
  we_spoke_last:
    "We spoke last. Selling into silence is the thing that doesn't work (0.67% vs 12.41% on a live thread), so the offer is PARKED and fires on his next real message.",
  stale: "His last message is too old for a live ask. Parked, not deleted.",
  declined: "He turned an offer down. She keeps talking; no new price follows a 'no'.",
  tapped:
    "He ignored the last ask, so the ladder tapped out. It decays — he isn't retired forever.",
  hard_stop:
    "He threatened a chargeback or told her to stop. Only an operator clearing skip_list brings him back.",
  offers_paused:
    "He said he's broke. Countering that with a cheaper offer is the one thing we never do — but if HE initiates a buy, it's honoured.",
  no_inbound: "He hasn't said anything yet.",
  rung_gap: "The last ask was seconds ago. She won't stack a second price on top of it.",
  near_bedtime:
    "Too close to her sleep window to OPEN a ladder — a hot sell abandoned at 01:58 and resumed at 09:00 is a disaster.",
  fan_daily_asks: "He's hit his daily limit on the number of asks.",
  fan_daily_cents: "He's hit his daily limit on how much he's been asked for.",
  account_hourly: "The whole account has hit its offers-per-hour ceiling.",
  account_daily: "The whole account has hit its offers-per-day ceiling.",
};

/** "→ 19:59" — when the pause lifts. Local time; the whole point is glanceability. */
function untilLabel(iso: string | null): string | null {
  if (!iso) return null;
  const t = new Date(iso);
  if (Number.isNaN(t.getTime())) return null;
  return t.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function money(cents: number | null): string {
  if (cents == null) return "";
  return `$${(cents / 100).toFixed(cents % 100 === 0 ? 0 : 2)}`;
}

export default function AiStatusStrip({ accountId, fanId }: Props) {
  const { data, isLoading, error } = useAiStatus(accountId, fanId);

  // Never fail SILENTLY. Returning null on error is how a broken strip becomes
  // indistinguishable from a healthy one with nothing to say — and "it renders nothing
  // and nobody knows why" is the exact bug class this component exists to kill.
  if (error) {
    return (
      <div className="text-[11px] leading-tight text-rose-400/80" title={String(error)}>
        ⚠ AI status unavailable
      </div>
    );
  }
  if (isLoading || !data) return null;

  const until = untilLabel(data.until);
  const engineName =
    data.engine === "ai_upseller" ? "AI Upseller"
    : data.engine === "ai_chatter" ? "AI Chatter"
    : "AI off";

  return (
    <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[11px] leading-tight">
      <span className="text-fg-dim">{data.engine === "none" ? "⭘" : "🤖"} {engineName}</span>

      <span className={cn("font-medium", TONE[data.state])} title={data.detail ?? undefined}>
        {DOT[data.state]} {data.label}
        {until ? ` → ${until}` : ""}
      </span>

      {/* Everything past the state chip is desktop-only: 11 chips that explain
          themselves through title= alone tower 8-10 lines high inside the phone
          header. `md:contents` keeps them DIRECT flex children at md, so the
          row's gaps are byte-identical to today. */}
      <div className="hidden md:contents">

      {/* A live unpaid ask — ours OR one a human chatter sent. This is the signal that
          makes the thread break-proof, so surfacing it explains the state above. */}
      {data.open_ask && (
        <span className="text-fg-dim" title={data.open_ask.by === "human" ? "Sent by a chatter" : "Sent by the AI"}>
          🔒 {money(data.open_ask.price_cents)} ask open
          {data.open_ask.by === "human" ? " (chatter)" : ""}
        </span>
      )}

      {data.graduated === "spent" && (
        <span className="text-fg-dim" title="of_ai_chat handed him to the upseller once he paid.">
          🤝 payer
        </span>
      )}

      {/* The reply budget. A CAP is deterministic — she goes quiet after N replies in a
          burst. It is NOT the random "she stepped away" break, which has a wake time
          instead. Showing them as one number would be a lie. */}
      {data.cadence.enabled && data.cadence.cap != null && (
        <span
          className={cn(data.cadence.stopped ? "text-amber-400" : "text-fg-dim")}
          title={
            `${data.cadence.used}/${data.cadence.cap} replies used in this burst ` +
            `(${data.cadence.tier} tier). 3-5 bubbles count as ONE reply. ` +
            `A fresh burst starts after ${data.cadence.resets_after_minutes} min of silence, ` +
            `or immediately if he shows a buying signal.`
          }
        >
          💬 {data.cadence.used}/{data.cadence.cap} replies
          {data.cadence.tier ? ` · ${data.cadence.tier.replace(/_/g, " ")}` : ""}
        </span>
      )}
      {data.cadence.enabled && data.cadence.cap == null && data.cadence.tier && (
        <span className="text-emerald-400/80" title="No cap — he just paid, so she keeps talking.">
          💬 uncapped · {data.cadence.tier.replace(/_/g, " ")}
        </span>
      )}

      {/* She cannot randomly wander off mid-sell. */}
      {data.break_proof && (
        <span
          className="text-fg-dim"
          title="An ask went out in the last 30 min (or he just paid), so she can't wander off mid-sale. Past 30 min she's free to take a break again — and she'll cover for it when she's back ('sorry babe was in the shower 🚿')."
        >
          🛡 can't wander off (30m)
        </span>
      )}

      {/* WHY she's talking but not selling. The single most-asked question about this
          bot, and until now it had no answer short of reading the code. */}
      {data.gate.enabled && data.gate.ok === false && (
        <span
          className="text-fg-dim"
          title={GATE_WHY[data.gate.why ?? ""] ?? "The qualification gate won't price him this turn."}
        >
          ⛔ won't price: {(data.gate.why ?? "blocked").replace(/_/g, " ")}
        </span>
      )}

      {data.offer_caps_ok === false && (
        <span className="text-fg-dim" title="Offer pacing: too many offers today, or too few of his messages since the last one.">
          ⛔ offer capped
        </span>
      )}

      {data.spend_7d.capped && (
        <span
          className="text-sky-400"
          title={`He's paid ${money(data.spend_7d.paid_cents)} in 7 days, at or past the account's cap. He isn't silenced — he stops being charged.`}
        >
          💸 7d spend cap
        </span>
      )}

      {/* Queued work. A deferred reply looked identical to silence before this. */}
      {data.next_action && (
        <span className="text-fg-dim" title={`A ${data.next_action.kind} job is queued for this fan.`}>
          ⏭ {data.next_action.kind} → {untilLabel(data.next_action.at)}
        </span>
      )}

      {data.llm.calls_24h > 0 && (
        <span
          className="text-fg-dim"
          title={`${data.llm.calls_24h} LLM calls on this fan in 24h · ${data.llm.model ?? "?"} · ${data.llm.cost_24h_cents.toFixed(2)}¢`}
        >
          🧠 {data.llm.calls_24h}/24h
        </span>
      )}

      {data.ladder.status !== "idle" && (
        <span className="text-fg-dim">ladder: {data.ladder.status} · rung {data.ladder.rung}</span>
      )}

      {data.force_ask && (
        <span className="text-fg-dim" title="She attaches the ask even when the model doesn't volunteer one.">
          ⚡ force-ask
        </span>
      )}

      </div>
    </div>
  );
}
