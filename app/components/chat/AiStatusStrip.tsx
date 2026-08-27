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

import { useAiStatus, type AiState, type AiStatus } from "@/hooks/useAiStatus";
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
    "His last message was too thin to price against ('k', 'ok', 'lol'). The chat keeps going — it just won't put a number in front of noise.",
  we_spoke_last:
    "We spoke last. Selling into silence is the thing that doesn't work (0.67% vs 12.41% on a live thread), so the offer is PARKED and fires on his next real message.",
  stale: "His last message is too old for a live ask. Parked, not deleted.",
  declined: "He turned an offer down. The chat keeps going; no new price follows a 'no'.",
  tapped:
    "He ignored the last ask, so the ladder tapped out. It decays — he isn't retired forever.",
  hard_stop:
    "He threatened a chargeback or told the account to stop. Only an operator clearing skip_list brings him back.",
  offers_paused:
    "He said he's broke. Countering that with a cheaper offer is the one thing we never do — but if HE initiates a buy, it's honoured.",
  no_inbound: "He hasn't said anything yet.",
  rung_gap: "The last ask was seconds ago. No second price stacks on top of it.",
  near_bedtime:
    "Too close to the sleep window to OPEN a ladder — a hot sell abandoned at 01:58 and resumed at 09:00 is a disaster.",
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

/** "05:55" today, "Thu 05:55" otherwise.
 *
 *  `untilLabel` prints the clock alone, which is right for the pauses it was built
 *  for — those lift within the hour. The quota backoff runs to 72h, so a bare
 *  "05:55" would read as this morning and be wrong by up to three days. */
function whenLabel(iso: string | null): string | null {
  if (!iso) return null;
  const t = new Date(iso);
  if (Number.isNaN(t.getTime())) return null;
  const clock = t.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  const sameDay = t.toDateString() === new Date().toDateString();
  return sameDay ? clock : `${t.toLocaleDateString([], { weekday: "short" })} ${clock}`;
}

function money(cents: number | null): string {
  if (cents == null) return "";
  return `$${(cents / 100).toFixed(cents % 100 === 0 ? 0 : 2)}`;
}

/**
 * WHY his daily ceiling is the number it is. Only the terms that LIFTED it above the
 * stranger's ration are interesting — that attribution is the whole reason the gate
 * reports a reason instead of a boolean, and it exists nowhere else in the UI.
 */
const DAILY_WHY: Record<string, string> = {
  signal_lift: "Lifted above the base ration because he's showing a buying signal — a man reaching for his wallet is never the one to ration.",
  spend_lift: "Lifted above the base ration by what he SPENDS. Spending buys a bigger allowance, never a smaller one.",
  backoff_served: "He's over quota, but the silence has already been served — the chat is open again.",
  no_ladder: "Over quota with no backoff ladder configured, so nothing is being withheld.",
};

/** One decimal, trailing zero dropped: 3.6h, and 24h rather than 24.0h. The rung is
 *  jittered ±25%, so the raw float is a number like 3.5858745856403846. */
function hours(h: number): string {
  return `${Number(h.toFixed(1))}h`;
}

type Chip = { text: string; tone: string; title: string };

/**
 * The daily ceiling as one chip. Dispatches on the gate's OWN verdict name — the UI
 * does not re-derive "is he in his runway" from a null quota, because that would be
 * _quota_gate's exit order copied into a component.
 *
 * Deliberately does NOT restate the wait or the dry streak in its TEXT: when a hold is
 * being served the state badge beside it already says "Quiet for 19.4h · daily cap
 * reached (35/15)", and the same sentence twice on an 11px line is noise.
 * Both stay in the title, which is also what covers the case where a rhythm break or a
 * skip-list entry outranks the quota badge and suppresses it entirely.
 */
function dailyChip(d: NonNullable<AiStatus["cadence"]["daily"]>): Chip | null {
  if (d.reason === "off") return null;

  const dry = d.dry_days != null ? ` He hasn't paid in ${d.dry_days} days.` : "";
  // Only meaningful when there is a hold to NOT serve. On a fan who is nowhere near
  // his ceiling, "recorded, not served" describes nothing and reads as a warning.
  const shadow = d.held && !d.enforced
    ? " SHADOW — the hold was recorded, not served: the reply still went." : "";

  // Still being courted. Nearly every fan on a roster is here, so the chip has to earn
  // the space: the countdown to when the ceiling starts applying is the only thing
  // worth saying, and "no daily cap" — which is what this rendered before — said
  // nothing at all about the fan you were looking at.
  if (d.reason === "runway" && d.runway_left != null) {
    return {
      text: `📅 runway ${d.runway_left} left`,
      tone: "text-fg-dim",
      title:
        `No daily ceiling yet — ${d.runway_left} more replies are owed before one applies. ` +
        `84% of everyone who ever bought did it inside 25 replies, so a fan inside ` +
        `his runway is still being courted, not rationed.${shadow}`,
    };
  }
  if (d.quota == null) {
    return {
      text: "📅 no daily cap",
      tone: "text-emerald-400/80",
      title: `He spends enough that no daily ceiling applies. A whale is never rationed.${shadow}`,
    };
  }

  const wait = d.backoff_hours != null ? ` Quiet for ${hours(d.backoff_hours)} from the last reply — a manual message or a mass send does not move it.` : "";
  const why = DAILY_WHY[d.reason] ? ` ${DAILY_WHY[d.reason]}` : "";
  // Where he is on the ladder, drawn as the ladder. "4h · 12h · 24h · [72h]" says in
  // one glance what the number alone cannot: that it CYCLES, so the step after the
  // longest is the shortest, and that he walked every step to get where he is.
  const rungs = d.ladder_hours && d.rung != null
    ? d.ladder_hours.map((h, i) => (i === d.rung ? `[${hours(h)}]` : hours(h))).join(" · ")
    : null;
  // A ONE-rung ladder is not a cycle and must not be drawn as one — "Quiet steps: [4h],
  // then back to the first — after this one comes 4h" is three clauses saying 4h. A
  // recent buyer walks exactly that shape (quota_backoff_hours_recent_buyer), so this
  // is the common case, not an edge one.
  const step = rungs && d.ladder_hours && d.rung != null
    ? (d.ladder_hours.length === 1
        ? ` One quiet step, and it does not change: ${hours(d.ladder_hours[0])}.`
        : ` Quiet steps: ${rungs}, then back to the first — after this one comes ${hours(d.ladder_hours[(d.rung + 1) % d.ladder_hours.length])}.`)
    : "";
  const free = d.held ? whenLabel(d.free_at) : null;
  // "step 4 of 4", never "rung 4/4": both halves of that were jargon — the word, and a
  // fraction that reads like a score. The chip says WHERE on the ladder he is; the
  // badge beside it says how long the step lasts.
  const where = d.held && d.rung != null && d.ladder_hours && d.ladder_hours.length > 1
    ? ` · step ${d.rung + 1} of ${d.ladder_hours.length}` : "";
  // The counter and the hold can disagree, and only one way round: the quota day
  // TUMBLES — it opens on a reply of hers and closes after 12h of her silence — so it
  // usually rolls over under a fan who is mid-backoff. That is the moment someone reads
  // a hold beside "0/25" and calls the strip broken, so the chip says it in words.
  // ("today" was dropped from the text for the same reason: it is a calendar word for
  // a window that is not a calendar day.) Same predicate, same sentence, as the badge
  // that owns this state — `fan_status_copy.daily_quota_badge`.
  const rolled = d.held && d.used < d.quota
    ? " His count has already rolled over — a new day hands his ALLOWANCE back, never his wait, so the number reads fresh while the quiet stretch keeps running."
    : "";
  return {
    // The chip repeats the state badge's wake time on purpose: a rhythm break or a skip
    // row outranks the quota badge and suppresses it, and then this is the only place
    // the instant appears at all. Both print it the same way, so a reader who sees it
    // twice sees one answer twice rather than two answers to compare.
    text: `📅 ${d.used}/${d.quota} replies${where}` + (free ? ` → ${free}` : ""),
    tone: d.held ? "text-amber-400" : "text-fg-dim",
    title:
      `${d.used} of ${d.quota} replies used in the current day, which opens on the ` +
      `account's own reply and closes after 12h of its silence.${rolled}${dry}${wait}${step}${why} ` +
      `A purchase or a content ask lifts this immediately.${shadow}`,
  };
}

/**
 * The convo teaser: how many of HIS messages until the next one, and what it asks.
 *
 * Both halves were invisible. The cadence lives in `teaser_convo_after_fan_msgs` and
 * the price is decided at send time by a climb/soften/floor policy, so "when does she
 * tease him again, and for how much" could only be answered by reading the engine —
 * and the answer moves every time he types.
 */
function teaserChip(t: NonNullable<AiStatus["teaser"]>): Chip | null {
  if (!t.rungs) return null;
  const free = t.cents === 0 && t.cents_max === 0;
  // A jittered soften spans a range; the floor and free-bait branches do not. Showing
  // "$40–45" where the engine may send either beats quoting one end as though it were
  // decided, which is what a single sampled roll would have done.
  const ask = free
    ? "free"
    : t.cents == null
      ? "?"
      : t.cents_max != null && t.cents_max !== t.cents
        ? `${money(t.cents)}–${money(t.cents_max)}`
        : money(t.cents);
  const due = t.remaining <= 0;
  // Same rule as the quota chip: "price step 5 of 7", not "rung 5/7". The word is
  // house jargon, and this ladder is a DIFFERENT one — prices, not quiet time — so
  // reusing the term made two unrelated ladders read as one.
  const rung = t.rung != null ? ` · price step ${t.rung + 1} of ${t.rungs}` : "";
  return {
    text: `🎁 ${due ? "tease due" : `tease in ${t.remaining}`} · ${ask}`,
    tone: due ? "text-amber-400" : "text-fg-dim",
    title:
      `${t.msgs_since} of ${t.after} of HIS messages since the last tease${rung}. ` +
      (due ? "The next reply can carry it. " : "") +
      (t.softened
        ? "This ask is SOFTENED — the last tease didn't sell, so it decays toward his proven-spend floor and then alternates that with a free tease, rather than climbing. "
        : "This is the price step's own ask — it climbs only when a tease actually SELLS. ") +
      (t.adaptive ? "" : "Legacy mode: the price climbs on every send, sold or not. ") +
      "Teases are a separate stream from the catalog PPVs.",
  };
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

  // whenLabel, not untilLabel: the state badge carries the quota backoff too, and that
  // one runs to 72h — a bare "12:31 PM" on a three-day wait reads as this afternoon.
  const until = whenLabel(data.until);
  const daily = data.cadence.daily ? dailyChip(data.cadence.daily) : null;
  const teaser = data.teaser ? teaserChip(data.teaser) : null;
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

      {/* Language chip — only when she's NOT writing default English, so English
          accounts see no extra clutter. */}
      {data.language && data.language !== "en" && (
        <span className="text-fg-dim" title={
          data.language_source === "manual" ? "Language pinned for this fan"
          : data.language_source === "account" ? "Account default language"
          : "Auto-detected language"
        }>
          🌐 {data.language}{data.language_source === "manual" ? "·pinned" : ""}
        </span>
      )}

      {/* Everything past the state chip is desktop-only: a dozen chips that explain
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
        <span className="text-fg-dim" title="The info-gather chatter handed him to the upseller once he paid.">
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
        <span className="text-emerald-400/80" title="No cap — he just paid, so the chat keeps going.">
          💬 uncapped · {data.cadence.tier.replace(/_/g, " ")}
        </span>
      )}

      {/* The DAILY ceiling above the burst cap. The state badge names a hold at the
          moment it bites, but only then — this is the running counter, so "why is she
          getting quieter with him" is answerable BEFORE she goes silent. */}
      {daily && (
        <span className={daily.tone} title={daily.title}>{daily.text}</span>
      )}
      {teaser && (
        <span className={teaser.tone} title={teaser.title}>{teaser.text}</span>
      )}

      {/* She cannot randomly wander off mid-sell. */}
      {data.break_proof && (
        <span
          className="text-fg-dim"
          title="An ask went out in the last 30 min (or he just paid), so it can't wander off mid-sale. Past 30 min it's free to take a break again — and the next reply covers for it ('sorry was in the shower 🚿')."
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
        <span className="text-fg-dim" title="The ask is attached even when the model doesn't volunteer one.">
          ⚡ force-ask
        </span>
      )}

      </div>
    </div>
  );
}
