"use client";

/** Small shared bits for the Growth tabs (Trial / Tracking / Promotion). */

import { useState } from "react";
import { Button, Input } from "@/components/ui/primitives";
import type { AutomationRule } from "@/hooks/useAutomations";

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
  return rule?.payload?.dry_run !== false;
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

/** Ordered, human-readable chunks for an automation run's stats bag. Zeroes
 *  are shown for the headline counters (liked/pinged/followed/re-created) and
 *  suppressed for the qualifier ones (stranded/refollowed/paid-skips). */
export function runStatsChunks(stats?: Record<string, unknown> | null): StatChunk[] {
  if (!stats) return [];
  const n = (k: string) => (typeof stats[k] === "number" ? (stats[k] as number) : null);
  const len = (k: string) => (Array.isArray(stats[k]) ? (stats[k] as unknown[]).length : null);
  const out: StatChunk[] = [];
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
  // price — only that some of these follows WILL be charged. Loudest chunk in
  // the row, and first, because it is the one the operator must not miss.
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
  // A follow run that walked its whole pool and notified NOBODY. Without this the
  // shape is `status ok · followed 0 · N candidates` — indistinguishable from a
  // quiet day, which is how a permanently pinned backfill window hid for six
  // days. `warn`, next to "already followed", because it is usually that counter
  // (or a head of paid/unreadable profiles) explaining the zero.
  if (stats.pool_exhausted === true) {
    out.push({ text: "whole pool checked, nobody new to notify", tone: "warn" });
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
  return out;
}

/** The " · chunk" spans for a last-run line, dry-run marker included. */
export function RunStats({ stats }: { stats?: Record<string, unknown> | null }) {
  return (
    <>
      {runStatsChunks(stats).map((c, i) => (
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
