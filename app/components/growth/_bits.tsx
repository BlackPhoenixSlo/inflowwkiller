"use client";

/** Small shared bits for the Growth tabs (Trial / Tracking / Promotion), plus
 *  the last-run line every automation surface renders.
 *
 *  ⚠️ `RunStats` is NOT growth-only any more — the Brain's welcome card uses it
 *  too. Different kinds emit DISJOINT stats bags (`send_welcome` and
 *  `auto_follow` share exactly the follow-back keys and nothing else), so it
 *  dispatches on `kind` to a per-kind chunk builder. Do NOT add a welcome key to
 *  `runStatsChunks`: a formatter that answers for every kind ends up answering
 *  for none, which is how a welcome tick with 9 errors rendered as a bare "ok".
 *
 *  That rule now holds in the code as well as in this comment. `runStatsChunks`
 *  used to call `followBackChunks` itself — the one welcome key it knew — and
 *  the call was DEAD (`follow_back` reaches neither kind that renders this
 *  formatter, `auto_follow` or `promo_reactivate`), while being the only path
 *  the follow-back lane's own tests drove. The call is gone and those tests run
 *  through `welcomeStatsChunks`, which is what production runs. */

import { useState } from "react";
import { Button, Input } from "@/components/ui/primitives";
import type { AutomationRule } from "@/hooks/useAutomations";
import { boolKnob } from "@/lib/boolKnob";

export function fmtUsd(cents: number): string {
  return `$${(Math.max(0, cents) / 100).toFixed(2)}`;
}

/** Is this rule still in dry-run (planning only, sending nothing)?
 *
 *  The default is INVERTED — every growth automation defaults `dry_run` TRUE, so
 *  an ABSENT key means dry run and only an explicit `false` means live. That is
 *  exactly the kind of default that drifts when it's re-derived per component, and
 *  drifting it fails toward sending for real. One home. `payload` is already typed
 *  `Record<string, unknown>` on AutomationRule, so no cast is needed to read it. */
export function isDryRun(rule: AutomationRule | null | undefined): boolean {
  return boolKnob(rule?.payload?.dry_run, true);
}

/** Message off a thrown value, with a fallback — collapses the
 *  `(e as Error)?.message || "…"` cast every Growth create/save catch repeated. */
export function errMsg(e: unknown, fallback: string): string {
  return (e as Error)?.message || fallback;
}

export function Field({
  label, children,
}: { label: string; children: React.ReactNode }) {
  return (
    <label className="block space-y-1">
      <span className="text-[11px] uppercase tracking-wide text-fg-dim">{label}</span>
      {children}
    </label>
  );
}

export function NumInput({
  value, onChange, min = 0, className,
}: { value: number; onChange: (v: number) => void; min?: number; className?: string }) {
  // Hold the raw keystrokes. Binding the number straight to the input let a
  // field sitting at 0 render "040" once you typed 40 in front of it, and made
  // the box impossible to clear while typing. The draft keeps typing natural;
  // blur normalises it back to the real number.
  const [draft, setDraft] = useState<string | null>(null);
  return (
    <Input
      type="number" min={min} className={className}
      value={draft ?? String(value)}
      onChange={(e) => {
        const raw = e.target.value;
        setDraft(raw);
        const n = Number(raw);
        if (raw !== "" && Number.isFinite(n)) onChange(n);
      }}
      onBlur={() => setDraft(null)}
    />
  );
}

// ── Automation last-run stats ─────────────────────────────────────────
//
// Every Growth surface that shows an automation's last run (AutoFollowTab,
// PromoReactivateCard, the Overview cards) renders the same stats bag. One
// formatter here so a new stat is added in exactly one place — the per-site
// conditional chains this replaces had already drifted apart once.

interface StatChunk {
  text: string;
  tone?: "err" | "warn";
}

/** The send_welcome FOLLOW-BACK lane's chunks — the second place in this app
 *  that spends real money, and until now the only automation lane with no run
 *  feedback anywhere in the UI at all. `send_welcome` emits five counters and the
 *  two resolved knobs; nothing read any of them, so an operator could not tell a
 *  free follow from a purchased subscription, or a lane that was working from one
 *  that had been silently off since it shipped.
 *
 *  Reported ONLY when `follow_back` says the lane was switched ON. The counters
 *  are present on every welcome run, so a bag of zeroes is what a switched-off
 *  lane and a broken one both look like — and reporting "followed back 0" on
 *  every tick of an account that never turned the lane on is the kind of
 *  permanent zero operators learn to stop reading.
 *
 *  ⚠️ ON is not RAN. `send_welcome` follows nobody on a dry run
 *  (`if ctx.follow_back and not ctx.dry_run`), so on a dry tick every counter
 *  here is a structural zero and the gate says what WOULD happen, not what did.
 *  This is the same distinction BrainPanel's `welcomeSpendsNow` draws for the
 *  panel warning, and it has to be drawn in both places or the red "paid
 *  subscribers WILL be charged" lands on a run that cannot spend a cent.
 *
 *  ⚠️ The gate-off chunk mirrors `money_gate`'s TONE, deliberately: it is the
 *  same class of fact (follows fired without reading a price) on the same
 *  operator's screen, and two different voices for it would make the quieter one
 *  look like the smaller problem. It is not the smaller problem — auto_follow's
 *  ungated run is a plan the operator asked for by pressing Run now, and this
 *  one happens on a timer.
 *
 *  It does NOT mirror its position, and the comment used to claim it did. This
 *  builder is called from `welcomeStatsChunks` only, where two facts outrank it:
 *  `errors` (a tick that threw is worse news than a tick that spent) and the
 *  `welcomed N` headline the card is opened to read. `err` is what stops it
 *  being skimmed past; the column it sits in is not. */
function followBackChunks(
  stats: Record<string, unknown>,
  n: (k: string) => number | null,
): StatChunk[] {
  // Absent (an older relay) is NOT off — it is "this run cannot say", and
  // inventing a lane report from five zeroes it did not vouch for is the same
  // mistake `would_follow` made. Silence until the relay ships the knob.
  if (stats.follow_back !== true) return [];
  const out: StatChunk[] = [];
  // A dry tick ran no lane at all: the five counters below are structural
  // zeroes, not measurements, so say the one true thing and stop. Without this
  // the row read "gate OFF — paid subscribers WILL be charged · followed back 0"
  // on a run whose own `· dry run` marker was three chunks to the right.
  if (stats.dry_run === true) {
    out.push(stats.follow_back_gate === false
      ? { text: "follow-back gate off — a live run would follow blind", tone: "warn" }
      : { text: "follow-back would run (dry)", tone: "warn" });
    return out;
  }
  const back = n("followed_back") ?? 0;
  const already = n("follow_back_already") ?? 0;
  const paid = n("follow_back_paid_skipped") ?? 0;
  const noPrice = n("follow_back_no_price") ?? 0;
  const failed = n("follow_back_errors") ?? 0;
  if (stats.follow_back_gate === false) {
    // ⚠️ MONEY, first and loudest. Nothing read these fans' prices, so nothing
    // here can name the cost — only that some of these follows bought a
    // subscription. `back` is the count of follows actually fired, which is the
    // exact number that went out unpriced.
    out.push({
      text: back
        ? `follow-back gate OFF — ${back} follow-backs charged blind`
        : "follow-back gate OFF — paid subscribers WILL be charged",
      tone: "err",
    });
  }
  // The headline, zeroes included — but only on a tick that welcomed somebody.
  // On a quiet tick every counter is 0 because there was nobody to follow, and
  // that is not news; on a tick that sent welcomes, a 0 is worth a look and the
  // qualifiers beside it say why.
  const sent = n("welcomes_sent");
  if (sent || back || already || paid || noPrice || failed) {
    out.push({ text: `followed back ${back}`, tone: back === 0 ? "warn" : undefined });
  }
  if (already) out.push({ text: `${already} already followed back` });
  if (paid) out.push({ text: `${paid} paid subs not followed back` });
  if (noPrice) out.push({ text: `${noPrice} follow-back prices unreadable` });
  if (failed) out.push({ text: `${failed} follow-backs failed`, tone: "err" });
  return out;
}

/** Ordered, human-readable chunks for an automation run's stats bag. Zeroes
 *  are shown for the headline counters (liked/pinged/followed/re-created) and
 *  suppressed for the qualifier ones (stranded/refollowed/paid-skips). */
export function runStatsChunks(stats?: Record<string, unknown> | null): StatChunk[] {
  if (!stats) return [];
  const n = (k: string) => (typeof stats[k] === "number" ? (stats[k] as number) : null);
  const len = (k: string) => (Array.isArray(stats[k]) ? (stats[k] as unknown[]).length : null);
  const out: StatChunk[] = [];
  // FIRST and loudest, exactly as in `welcomeStatsChunks` — and for the same
  // reason, on the surface where it costs money. `auto_follow` emits `errors`
  // from all four of its actions and `promo_reactivate` from its only one, and
  // NOTHING rendered it: a follow tick that threw on 12 fans printed
  // `last run ok · followed 0 · 20 candidates`, which is the line a healthy
  // quiet day prints. The welcome card was given this chunk one pass ago under a
  // comment saying "`errors` rendering nowhere is what made 'ok' a lie", and the
  // identical hole was left standing here, on the paid lane.
  //
  // Ahead of the money-gate chunk: a run we could not READ is a worse fact than
  // a run we read without a price check, and the gate chunk keeps its `err` tone
  // so neither is skimmed past.
  const errors = n("errors");
  if (errors) out.push({ text: `${errors} failed`, tone: "err" });
  // A DRY RUN's headline is what it would actually do, not how big the pool
  // was. `candidates` alone read as "8 ready to go" on a rule whose gate
  // refused all eight, and it said that for six days.
  //
  // Gated on `no_price_skipped`, a counter ONLY the gate-running preview emits.
  // An older relay fills `would_follow` with the raw, un-gated pool, so
  // rendering it as "8 would notify" would restate the very lie this replaced —
  // with more confidence than the wording it replaced. Until that relay ships,
  // say nothing rather than something wrong.
  //
  // NOT `examined`: the money_gate-off preview emits `examined` too, so keying
  // off it rendered an ungated run — one that will BUY every priced profile it
  // reaches — as an ordinary gated plan, just missing its money-skip chunks.
  // An absent "paid profiles skipped" reads as "there were none", not "the
  // check is switched off", which is the opposite of the truth.
  const ungated = stats.money_gate === false;
  const gated = n("no_price_skipped") !== null;
  const wouldNotify = gated || ungated ? (len("would_follow") ?? len("would_ping")) : null;
  // ⚠️ MONEY. The ungated run reads no profile, so nothing here can name the
  // price — only that some of these follows WILL be charged. `err` tone and
  // ahead of every plan chunk, because it is the one the operator must not
  // miss; only the error count outranks it (see above).
  if (ungated) {
    const charged = n("unpriced_follows") ?? wouldNotify ?? n("followed");
    out.push({
      text: charged !== null
        ? `money gate OFF — ${charged} follows will be charged`
        : "money gate OFF — paid profiles WILL be charged",
      tone: "err",
    });
  }
  if (wouldNotify !== null) {
    out.push({
      text: `${wouldNotify} would notify`,
      tone: wouldNotify === 0 ? "warn" : undefined,
    });
  }
  const liked = n("liked");
  if (liked !== null) out.push({ text: `liked ${liked}` });
  const pinged = n("pinged");
  if (pinged !== null) out.push({ text: `pinged ${pinged}` });
  const stranded = n("stranded");
  if (stranded) out.push({ text: `${stranded} stranded (unfollowed, re-follow failed)`, tone: "err" });
  const followed = n("followed");
  if (followed !== null) out.push({ text: `followed ${followed}` });
  const refollowed = n("refollowed");
  if (refollowed) out.push({ text: `re-followed ${refollowed}` });
  const already = n("already_following");
  if (already) out.push({ text: `${already} already followed`, tone: "warn" });
  // A follow run that walked everything it looked at and notified NOBODY. Without
  // this the shape is `status ok · followed 0 · N candidates` — indistinguishable
  // from a quiet day, which is how a permanently pinned backfill window hid for
  // six days. `warn`, next to "already followed", because it is usually that
  // counter (or a head of paid/unreadable profiles) explaining the zero.
  //
  // ⚠️ "in this batch", NOT "the whole pool". The engine computes
  // `pool_exhausted` over the HEADROOM-TRUNCATED slice (`auto_follow._run_follow`
  // takes `[:_headroom(cap)]`, i.e. cap × 5 fans), so on a 3663-fan backfill it
  // means "this tick's window notified nobody" and says nothing about the table.
  // The old copy ("whole pool checked") was false on every truncated tick — and
  // once `eligible` started rendering below, the two chunks contradicted each
  // other in the same line on any healthy draining tick: "whole pool checked …
  // 200 eligible still to walk". The narrower claim is true in BOTH states, so
  // the chunk keeps its job and the operator is not taught to distrust it.
  if (stats.pool_exhausted === true) {
    out.push({ text: "nobody new to notify in this batch", tone: "warn" });
  }
  const paid = n("paid_profile_skipped");
  if (paid) out.push({ text: `${paid} paid profiles skipped` });
  const noPrice = n("no_price_skipped");
  if (noPrice) out.push({ text: `${noPrice} price unreadable, skipped` });
  const reactivated = n("reactivated");
  if (reactivated !== null) out.push({ text: `re-created ${reactivated}` });
  const due = n("due");
  if (due !== null) out.push({ text: `${due} due` });
  if (stats.skipped === "of_unreachable") {
    out.push({ text: "OnlyFans unreachable, stood down", tone: "warn" });
  }
  const candidates = n("candidates");
  const examined = n("examined");
  if (candidates !== null) {
    // Say when only part of the pool was checked, so a partial look is never
    // read as a full one.
    out.push({
      text: examined !== null && examined < candidates
        ? `${examined} of ${candidates} candidates checked`
        : `${candidates} candidates`,
    });
  }
  // How many fans the backfill still has to get through, emitted by `all_stored`
  // on BOTH paths. `20 candidates` on a 3663-fan account said nothing about the
  // 3643 behind them.
  //
  // ⚠️ The two paths mean DIFFERENT things by it and the copy has to say which.
  // A preview does not stamp `follow_examined_at`, so it re-reads the same head
  // every tick and this number never moves — that is the standing-still report
  // it was added for, and it earns `warn`. A LIVE tick stamps every fan it
  // reaches, so the same number is progress and warning about it would be
  // crying wolf. (It was emitted on the dry path ONLY until R3-4, which is why
  // this used to be one sentence.)
  const dry = stats.dry_run === true;
  const eligible = n("eligible");
  // ⚠️ `eligible > candidates` is only a valid suppression on a SINGLE-source
  // run. `candidates` is the merged pool across every source on the rule, while
  // `eligible` counts the `all_stored` backfill alone — auto_follow emits it
  // only when that source is in the list — so on a stacked rule the two measure
  // different populations and the comparison could hide the number on exactly
  // the rule whose progress is hardest to see. Stacked runs therefore always
  // show it, and say which pool it counts.
  const stacked = typeof stats.source === "string" && stats.source.includes(",");
  if (eligible !== null && (stacked || (candidates !== null && eligible > candidates))) {
    const pool = stacked ? `${eligible} eligible in the backfill pool` : `${eligible} eligible`;
    out.push({
      text: dry
        ? `${pool} — a dry run re-checks this same head each tick`
        : `${pool} still to walk`,
      tone: dry ? "warn" : undefined,
    });
  }
  return out;
}

/** Ordered chunks for a `send_welcome` run's stats bag.
 *
 *  Its OWN set, not a branch inside `runStatsChunks`. The two bags overlap on
 *  the follow-back keys and on nothing else: `runStatsChunks` knows `liked`,
 *  `followed`, `candidates`, `pool_exhausted`; a welcome run emits
 *  `welcomes_sent`, `errors`, `skipped_*`, `cap_hit`, `aborted_on_reply`. Run
 *  the welcome bag through the growth formatter and every chunk misses — a tick
 *  that welcomed 5 rendered only its follow-back chunks, and a tick with NINE
 *  ERRORS rendered the empty string, so the card read "Last run: ok".
 *
 *  Order is worst-news-first: errors, then the headline, then the qualifiers
 *  that explain a low headline. `welcomes_sent` shows its zero (a welcome
 *  automation that sent nothing is the thing an operator opens this card to
 *  find out about); the qualifiers are suppressed at zero. */
export function welcomeStatsChunks(stats?: Record<string, unknown> | null): StatChunk[] {
  if (!stats) return [];
  const n = (k: string) => (typeof stats[k] === "number" ? (stats[k] as number) : null);
  const out: StatChunk[] = [];
  // FIRST and loudest. The executor finalises a paced welcome run as `ok` even
  // when every fan in the batch threw (one fan's escape must not kill the other
  // bursts — send_welcome's own §A3 note says so), so `errors` is the ONLY place
  // a failed tick is visible. It rendering nowhere is what made "ok" a lie.
  const errors = n("errors");
  if (errors) out.push({ text: `${errors} failed`, tone: "err" });
  const sent = n("welcomes_sent");
  if (sent !== null) {
    out.push({ text: `welcomed ${sent}`, tone: sent === 0 ? "warn" : undefined });
  }
  const fresh = n("new_subscribers");
  if (fresh !== null) out.push({ text: `${fresh} new subs` });
  out.push(...followBackChunks(stats, n));
  // Why a tick welcomed fewer fans than it saw. Each is a different decision and
  // an operator chasing "why did nobody get a welcome" needs the one that fired.
  const skips: [string, string][] = [
    ["skipped_existing", "already welcomed"],
    ["skipped_cooldown", "on cooldown"],
    ["skipped_guard", "outside the new-sub window"],
    ["skipped_restricted", "restricted from automations"],
    ["skipped_audience", "outside the audience"],
    ["skipped_locked", "locked by another tick"],
  ];
  for (const [key, label] of skips) {
    const v = n(key);
    if (v) out.push({ text: `${v} ${label}` });
  }
  if (stats.cap_hit === true) out.push({ text: "daily LLM cap hit", tone: "warn" });
  if (stats.batch_capped === true) out.push({ text: "batch cap hit", tone: "warn" });
  // stop_on_reply (§C) — inert with the knob off, so only the non-zeroes show.
  const aborted = n("aborted_on_reply");
  if (aborted) out.push({ text: `${aborted} stopped, he replied` });
  const handoff = n("handoff_enqueued");
  if (handoff) out.push({ text: `${handoff} handed to the chat engine` });
  const guardOff = n("reply_guard_off");
  if (guardOff) out.push({ text: `${guardOff} sent without the reply guard`, tone: "warn" });
  return out;
}

/** The " · chunk" spans for a last-run line, dry-run marker included.
 *
 *  `kind` picks the chunk builder. It defaults to the growth bag because every
 *  Growth-tab caller predates the welcome card; a NEW surface should pass its
 *  kind explicitly and add a builder rather than inherit a formatter that does
 *  not know its keys. */
export function RunStats(
  { stats, kind }: { stats?: Record<string, unknown> | null; kind?: "send_welcome" },
) {
  const chunks = kind === "send_welcome" ? welcomeStatsChunks(stats) : runStatsChunks(stats);
  return (
    <>
      {chunks.map((c, i) => (
        <span key={i} className={c.tone === "err" ? "text-err" : c.tone === "warn" ? "text-warn" : undefined}>
          {" · "}{c.text}
        </span>
      ))}
      {stats?.dry_run ? <span> · dry run</span> : null}
    </>
  );
}

/** Danger button that guards a delete behind a native confirm. Every Growth
 *  list row (trial / tracking / redirect / promo / segment) deleted through the
 *  same confirm→mutate→disabled-while-pending shape; this is that shape, once. */
export function ConfirmDeleteButton({
  confirm, onConfirm, pending, label = "Delete",
}: {
  confirm: string;
  onConfirm: () => void;
  pending?: boolean;
  label?: string;
}) {
  return (
    <Button
      size="sm" variant="danger" disabled={pending}
      onClick={() => { if (window.confirm(confirm)) onConfirm(); }}
    >
      {label}
    </Button>
  );
}

/** Copy-to-clipboard button that flashes "Copied". */
export function CopyButton({ text, label = "Copy" }: { text: string; label?: string }) {
  const [done, setDone] = useState(false);
  return (
    <Button
      size="sm" variant="ghost"
      onClick={async () => {
        try {
          await navigator.clipboard?.writeText(text);
          setDone(true);
          setTimeout(() => setDone(false), 1200);
        } catch { /* clipboard blocked */ }
      }}
      title={text}
    >
      {done ? "✓ Copied" : label}
    </Button>
  );
}
