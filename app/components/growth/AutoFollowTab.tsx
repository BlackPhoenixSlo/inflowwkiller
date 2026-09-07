"use client";

/**
 * AutoFollowTab — Growth → "Auto-follow / Auto-like".
 *
 * Trigger OnlyFans re-engagement by LIKING fans' recent messages or by
 * FOLLOWING fans back (both are notification-only nudges; never a DM). Saved
 * through the generic rules API with kind "auto_follow". Defaults to dry-run
 * so nothing acts until the operator confirms + enables.
 *
 *   action     like_messages · like_posts · follow · ping (all real; follow/ping money-gated)
 *   targets    recently-expired (follow only) · recently-active · a Smart List ·
 *              every fan on file (follow only — the backfill)
 *              (ping derives its own pool: fans quiet ≥ quiet_days)
 *   daily_cap  max actions per run
 *   min_days_between_pings  ONE per-fan cooldown ledger shared by follow AND
 *              ping (auto_follow._run_follow / _run_ping read the same key), so
 *              it is rendered and written for both — editing it on either
 *              action retunes the other.
 *
 * Follow targets FREE fan profiles by default — the engine price-checks every
 * fan and skips paid pages. NOT "can never spend money": `money_gate: false` is
 * a documented knob (`auto_follow._run_follow`), it has no checkbox here, and a
 * rule carrying it follows priced pages blind at this account's expense. The
 * last-run line says so in red when it happens (`lib/runStats.runStatsChunks`),
 * which is the only place this panel can. "Specific fan ids" was dropped from
 * the UI (operators pasted ids that went stale); the engine still honors
 * targets.source="fan_ids" for API callers.
 */

import { useEffect, useMemo, useState } from "react";

import { Button, Card, Input } from "@/components/ui/primitives";
import { errMsg } from "@/components/growth/_bits";
import { RunStats, isDryRun } from "@/lib/runStats";
import { boolKnob } from "@/lib/boolKnob";
import {
  useAutomationRules, useCreateRule, useUpdateRule, useRunRuleNow,
  type AutomationRule,
} from "@/hooks/useAutomations";
import { useSmartLists, type SmartList } from "@/hooks/useSmartLists";

const SELECT_CLS =
  "w-full bg-bg border border-border rounded-lg px-3 py-2 text-sm focus:outline-none focus:border-accent";

type Action = "like_messages" | "like_posts" | "follow" | "ping";
type Source = "expired" | "recent_active" | "smart_list" | "all_stored";

// Which pools an action may target (first entry = the UNRECOGNISED-value snap
// target). ruleToForm, the action-switch snap, and the Target dropdown all
// derive from it. "expired" is follow-only (OF's lapsed-subscriber list —
// nothing to like there); ping/like_posts derive their own pools and show no
// Target.
//
// ⚠️ THIS OWNS THE SOURCE AXIS ONLY. Adding a SOURCE is one edit here plus one
// `SOURCE_SPECS` entry the compiler demands. Adding an ACTION is not: it needs
// this table, an `<option>` in the Action select, and SIX unenforced
// `form.action === …` string compares — `targetsFor`, the `payload` memo's
// `quiet_days` and `min_days_between_pings` branches, the ping / like_posts
// render branches, and the follow-only cooldown field. A `Record<Action, …>` of
// specs (the shape `SOURCE_SPECS` is, one axis over) is what would make the
// compiler ask; nobody has needed a fifth action yet, so this note is the
// warning rather than the refactor. Do not read the SOURCE_SPECS docstring
// below as covering this axis — it does not.
const SOURCES_BY_ACTION: Record<Action, Source[]> = {
  follow: ["expired", "recent_active", "smart_list", "all_stored"],
  like_messages: ["recent_active", "smart_list"],
  like_posts: [],
  ping: [],
};

// What the ENGINE does when `targets.source` is ABSENT — which is NOT the same
// question as "what does an unrecognised value snap to", and reading them as one
// question is how the panel came to misreport a live backfill. `follow` goes
// through `auto_follow._source_list`, whose default has been "all_stored" since
// it shipped; every other action goes through `_resolve_targets`, whose default
// is recent_active. A rule saved as `{"action":"follow","dry_run":false}` with no
// targets at all IS a 3663-fan backfill, so that is what this panel must show —
// showing "expired" both lied about the running rule and made the next save
// narrow it to OF's short win-back list.
//
// ⚠️ Keep in step with auto_follow._source_list / _resolve_targets. Pinned from
// the OTHER side by `test_auto_follow.case_engine_default_pools_are_what_the_ui_claims`,
// which asserts these exact two answers against the engine — the only thing that
// can catch a drift across the language boundary.
//
// PARTIAL, and only the two actions that show a Target dropdown. `like_posts`
// has no Target and `ping` derives its own pool, so entries for them were two
// answers to a question nobody asks — the kind of dead mirror that goes stale
// unnoticed and then gets copied. An action with no entry falls back to
// `valid[0]` in `sourceFor`, the same narrow-pool answer an unrecognised source
// gets.
const ENGINE_DEFAULT_SOURCE: Partial<Record<Action, Source>> = {
  follow: "all_stored",
  like_messages: "recent_active",
};

type SetForm = <K extends keyof Form>(k: K, v: Form[K]) => void;

interface SourceSpec {
  /** The Target dropdown's option text. */
  label: string;
  /** The `targets` object a save writes for this source — the ONLY place the
   *  pairing is written, so a new source cannot land in the dropdown without
   *  one. This is what the old `if/else if/else` chain got wrong: its final
   *  unconditional `else` emitted `recent_active` for anything it had not been
   *  taught, so a forgotten branch compiled, tested green, and saved a
   *  wrong-pool rule. `Record<Source, …>` makes the compiler ask instead. */
  targets: (f: Form) => Record<string, unknown>;
  /** The cell beside the Target dropdown: this source's own input, or its hint.
   *  `ctx.eligible` is the engine's MEASURED remaining pool from the last run
   *  (`auto_follow._all_stored_eligible_count`) — no hint may invent an audience
   *  size, and one of them used to (`AUDIENCE = 3600`, one account's fan count
   *  asserted to every account).
   *
   *  It is null in three states, and the copy has to be true in all three: the
   *  rule has never run; the last run did not walk `all_stored` (the engine only
   *  counts that pool); or the relay predates the counter. It used to be null on
   *  every LIVE run as well — the engine computed it inside `if dry_run:` — which
   *  is why the copy below no longer tells the operator to do something a live
   *  rule cannot do. */
  extra?: (f: Form, set: SetForm,
           ctx: {
             smartLists: SmartList[];
             eligible: number | null;
             /** Was the run that MEASURED `eligible` a dry one? A dry tick stamps
              *  nothing, so its number is frozen and promising it falls beside it
              *  would be false. (The live copy says it falls with every fan the
              *  run REACHES, not "every run": `_follow_batch`'s `except` path
              *  stamps neither ledger nor `follow_examined_at` on purpose, so a
              *  tick whose whole head throws advances nothing.) */
             eligibleFromDryRun: boolean;
           }) => React.ReactNode;
}

/** Every follow/like pool: its label, the payload it writes, and the control or
 *  hint it shows. One entry per SOURCE, and the only place the source↔targets
 *  pairing is written — `Record<Source, …>` means the compiler demands the entry.
 *  This is the source axis only; see `SOURCES_BY_ACTION` for what adding an
 *  ACTION still costs. */
const SOURCE_SPECS: Record<Source, SourceSpec> = {
  expired: {
    label: "Recently-expired fans (win-back)",
    targets: () => ({ source: "expired" }),
    extra: () => (
      <div className="text-xs text-fg-dim self-end pb-2.5">
        OnlyFans’ own lapsed-subscriber list — the fans worth winning back.
      </div>
    ),
  },
  recent_active: {
    label: "Recently-active fans",
    targets: (f) => ({ source: "recent_active", days: f.days }),
    extra: (f, set) => (
      <label className="block space-y-1">
        <span className="text-[11px] uppercase tracking-wide text-fg-dim">Active within (days)</span>
        <Input type="number" min={1} value={f.days} onChange={(e) => set("days", Number(e.target.value))} />
      </label>
    ),
  },
  smart_list: {
    label: "A Smart List",
    targets: (f) => ({ source: "smart_list", smart_list_id: f.smartListId }),
    extra: (f, set, { smartLists }) => (
      <label className="block space-y-1">
        <span className="text-[11px] uppercase tracking-wide text-fg-dim">Smart List</span>
        <select className={SELECT_CLS} value={f.smartListId ?? ""}
          onChange={(e) => set("smartListId", e.target.value ? Number(e.target.value) : null)}>
          <option value="">— pick a segment —</option>
          {smartLists.map((l) => <option key={l.id} value={l.id}>{l.name}</option>)}
        </select>
      </label>
    ),
  },
  all_stored: {
    label: "Every fan on file (backfill)",
    targets: () => ({ source: "all_stored" }),
    extra: (f, _set, { eligible, eligibleFromDryRun }) => (
      <div className="text-xs text-fg-dim self-end pb-2.5">
        Every fan on file — current subs, old fans never followed, and fans who
        lapsed but are still stored. Each run follows at most{" "}
        <b>{Math.max(0, f.dailyCap)}</b> of them and looks at up to{" "}
        <b>{Math.max(0, f.dailyCap) * HEADROOM_FACTOR}</b> (a skipped fan is
        marked seen too, so it still moves the window along).{" "}
        {eligible === null ? (
          <>
            Run it once — dry or live — and this line will say how many fans are
            left to walk.
          </>
        ) : (
          <>
            The last run measured <b>{eligible.toLocaleString()}</b> still to
            walk — at {describeCadence(f.everyMinutes)} that is{" "}
            {drainHint(eligible, f.dailyCap, f.everyMinutes)}, depending on how
            many turn out to be follows rather than skips.{" "}
            {eligibleFromDryRun
              ? "That was a DRY run, which marks nobody as seen — the number will not move until this rule goes live."
              : "It falls with every fan the run reaches."}
          </>
        )}{" "}
        It does not then stop: the “min days between actions” stamp lapses and
        the table is walked again from the top, forever.
      </div>
    ),
  },
};

/** "every 4 hours" / "every 45 min" — the cadence in the operator's own words,
 *  derived from the field he just typed in rather than hardcoded (the hint used
 *  to say "4-hourly" on a rule set to run every 10 minutes). */
function describeCadence(everyMinutes: number): string {
  const m = Math.max(1, Math.round(everyMinutes));
  if (m % 60 !== 0) return `every ${m} min`;
  const h = m / 60;
  return h === 1 ? "hourly" : `every ${h} hours`;
}

// The engine oversizes each tick's pool so the per-fan gates cannot starve a
// run: `auto_follow._headroom(cap) = max(cap,1) * _HEADROOM_FACTOR`. That factor
// is what makes the drain a RANGE rather than a number — see drainHint.
const HEADROOM_FACTOR = 5;

/** How long the backfill takes to walk `audience` fans once — as a RANGE.
 *
 *  ⚠️ `daily_cap` bounds NOTIFICATIONS, not the walk. Each tick fetches
 *  `_headroom(cap) = cap × 5` fans and stamps `follow_examined_at` on every one
 *  it reaches, for all three `_EXAMINED_OUTCOMES` — a skip advances the window
 *  exactly like a follow does. So the table drains at somewhere between `cap`
 *  fans/tick (every fan followable, the cap binds first) and `cap × 5`
 *  fans/tick (every fan already-followed or gated, the pool size binds). The
 *  old hint modelled only the first and was therefore up to 5× too slow:
 *  measured at cap 30 / 300 fans, 3 ticks all-already-following vs 11 all-free,
 *  against a hint that predicted 10 for both.
 *
 *  `audience` is the engine's own measured `eligible` when the last run
 *  reported one; there is no fallback constant, because the one this replaced
 *  (`AUDIENCE = 3600`) was one production account's fan count asserted to every
 *  account, twice. With no measurement, `drainHint` renders nothing at all —
 *  see the caller. */
function drainHint(audience: number, dailyCap: number, everyMinutes: number): string {
  const runsPerDay = 1440 / Math.max(1, Math.round(everyMinutes));
  const cap = Math.max(0, Math.round(dailyCap));
  if (cap <= 0) return "never (max actions is 0)";
  const days = (perTick: number) => Math.max(1, Math.ceil(audience / (perTick * runsPerDay)));
  const fast = days(cap * HEADROOM_FACTOR);   // every fan skipped: the pool binds
  const slow = days(cap);                     // every fan followed: the cap binds
  const unit = (d: number) => (d === 1 ? "a day" : `${d} days`);
  return fast === slow ? unit(slow) : `${fast}–${slow} days`;
}

/** The stored `targets.source` this panel could NOT render, or null.
 *
 *  `sourceFor` snaps an unrecognised source to the action's narrow pool — which
 *  is the right SAFETY answer (it must never snap to the 3663-fan backfill) but
 *  a wrong DISPLAY answer, and since the fix that stopped an untouched save
 *  rewriting `targets`, a permanent one: a rule stored as
 *  `{"source":"fan_ids","fan_ids":[…]}` shows "Recently-expired fans" and keeps
 *  following the hand-named list forever, with nothing on screen admitting the
 *  panel is not describing the rule. That combination is money-shaped —
 *  `auto_follow`'s own header calls `money_gate:false` + `fan_ids` a RECURRING
 *  CHARGE — so the snap has to be visible.
 *
 *  Absent is NOT a snap: `sourceFor` shows the engine's own default for it, so
 *  screen and engine already agree and there is nothing to confess. */
function snappedSource(rule: AutomationRule | null, action: Action): string | null {
  const t = (rule?.payload?.targets ?? {}) as Record<string, unknown>;
  const raw = t.source;
  // A LIST is legal on the engine (`_source_list` stacks pools in priority
  // order) and has no dropdown, so it is a snap too — and one the operator is
  // most likely to have hand-written and to care about keeping.
  if (Array.isArray(raw)) return raw.map(String).join(" + ");
  if (typeof raw !== "string" || raw.trim() === "") return null;
  return SOURCES_BY_ACTION[action].includes(raw as Source) ? null : raw;
}

/** Valid source for `action`.
 *
 *  Two different questions, and collapsing them is the bug this shape exists to
 *  prevent:
 *    • ABSENT (`undefined` / `null` / "") — the rule never said. Show what the
 *      ENGINE will actually pick (`ENGINE_DEFAULT_SOURCE`), so the panel reports
 *      the run that is really happening and a save stamps it rather than
 *      changing it.
 *    • UNRECOGNISED ("fan_ids", dropped from the UI; anything legacy) — the rule
 *      said something this panel cannot render. Snap to the action's first
 *      listed pool, NEVER to the widest one. `snappedSource` above is what says
 *      so on screen; this function only decides which option to highlight. */
function sourceFor(action: Action, source?: string | null): Source {
  const valid = SOURCES_BY_ACTION[action];
  const fallback = valid[0] ?? "recent_active";
  if (source == null || source === "") {
    const engine = ENGINE_DEFAULT_SOURCE[action];
    return engine && valid.includes(engine) ? engine : fallback;
  }
  return valid.includes(source as Source) ? (source as Source) : fallback;
}

interface Form {
  enabled: boolean;
  action: Action;
  source: Source;
  days: number;
  smartListId: number | null;
  postIds: string;      // comma-separated (like_posts)
  quietDays: number;    // ping: fan counts as quiet after N days silent
  // ONE ledger, both actions: `min_days_between_pings` is the per-fan cooldown
  // for ping AND the backfill's progress marker for follow (it is also the
  // `follow_examined_at` window). Named for what it is rather than for the
  // action it was first written for.
  actionGapDays: number;
  dailyCap: number;
  dryRun: boolean;
  everyMinutes: number;
}

function ruleToForm(rule: AutomationRule | null): Form {
  const p = rule?.payload ?? {};
  const t = (p.targets ?? {}) as Record<string, unknown>;
  const action = (p.action as Action) || "like_messages";
  const every = rule?.every_seconds ? Math.max(1, Math.round(rule.every_seconds / 60)) : 240;
  return {
    enabled: rule?.is_enabled ?? false,
    action,
    source: sourceFor(action, t.source as string | undefined),
    days: typeof t.days === "number" ? t.days : 7,
    smartListId: typeof t.smart_list_id === "number" ? t.smart_list_id : null,
    postIds: Array.isArray(t.post_ids) ? (t.post_ids as number[]).join(", ") : "",
    quietDays: typeof p.quiet_days === "number" ? p.quiet_days : 7,
    actionGapDays: typeof p.min_days_between_pings === "number" ? p.min_days_between_pings : 14,
    dailyCap: typeof p.daily_cap === "number" ? p.daily_cap : 50,
    // default TRUE, and `boolKnob` (not `!== false`) because this is a
    // catalogued bool: one reader, same answer as the engine's `bool_knob`.
    dryRun: boolKnob(p.dry_run, true),
    everyMinutes: every,
  };
}

function parseIds(s: string): number[] {
  return s.split(/[,\s]+/).map((x) => Number(x.trim())).filter((n) => Number.isFinite(n) && n > 0);
}

export default function AutoFollowTab({ accountId }: { accountId: string | null }) {
  const rulesQ = useAutomationRules(accountId);
  const rule = (rulesQ.data ?? []).find((r) => r.kind === "auto_follow") ?? null;
  const smartListsQ = useSmartLists(accountId);

  const createM = useCreateRule(accountId);
  const updateM = useUpdateRule(accountId);
  const runM = useRunRuleNow(accountId);
  const busy = createM.isPending || updateM.isPending;

  const [form, setForm] = useState<Form>(ruleToForm(null));
  const [err, setErr] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  // Did the operator touch anything that decides the POOL (action, source, days,
  // Smart List, post ids)? Only then may a save rewrite `targets`. Untouched, the
  // stored object is left exactly as it is — see `save()`.
  const [targetsDirty, setTargetsDirty] = useState(false);

  useEffect(() => { setForm(ruleToForm(rule)); setTargetsDirty(false); }, [rule?.id]); // eslint-disable-line react-hooks/exhaustive-deps
  // Keyed off the SAVED action, not the form's: it describes what is stored.
  const snapped = snappedSource(rule, (rule?.payload?.action as Action) || form.action);
  const set: SetForm = (k, v) => setForm((f) => ({ ...f, [k]: v }));
  /** `set` for the fields that decide the pool — marks `targets` writable. */
  const setTarget: SetForm = (k, v) => { setTargetsDirty(true); set(k, v); };

  /** The `targets` object this form describes, or null for an action that has
   *  none (ping derives its own pool). */
  const targetsFor = (f: Form): Record<string, unknown> | null => {
    if (f.action === "ping") return null;
    if (f.action === "like_posts") return { post_ids: parseIds(f.postIds) };
    return SOURCE_SPECS[f.source].targets(f);
  };

  /** The scalar knobs this panel OWNS — never the whole payload, and never
   *  `targets`. Everything else on the rule (money_gate, hand-set
   *  targets.fan_ids, keys added by the typed rules editor) is preserved by the
   *  spread in `save()`.
   *
   *  `targets` is deliberately absent. It obeys a different rule from every
   *  other key here — replaced wholesale or not written at all, never merged —
   *  and putting it in this object meant `save()` had to spend three branches
   *  un-doing its own spread, reading `"targets" in …` off two different
   *  objects to work out what it had just done. It is applied once, positively,
   *  by `applyTargets()` below. */
  const payload = useMemo(() => {
    const p: Record<string, unknown> = {
      action: form.action, daily_cap: form.dailyCap, dry_run: form.dryRun,
    };
    if (form.action === "ping") p.quiet_days = form.quietDays;
    // ONE ledger for both: `_run_follow` reads `min_days_between_pings` as the
    // backfill's progress window and `_run_ping` reads it as the ping cooldown.
    // It was written for ping only, so a follow rule could never see or set the
    // knob that paces it — and editing the ping rule silently retuned the
    // backfill.
    if (form.action === "ping" || form.action === "follow") {
      p.min_days_between_pings = form.actionGapDays;
    }
    return p;
  }, [form]);

  /** Write the pool this form describes onto `p` — the ONLY place `targets` is
   *  decided.
   *
   *  Untouched pool → say nothing, so whatever the rule already stores survives
   *  verbatim (a source this panel had to infer, a hand-set `fan_ids` list).
   *  Touched → replace wholesale, because a source change from smart_list to
   *  expired must DROP `smart_list_id` or the engine resolves a pool the
   *  dropdown is not showing; and an action with no pool of its own (`ping`
   *  derives one) must drop the key entirely rather than leave the previous
   *  action's object behind, inert but ready to come back to life on a switch. */
  function applyTargets(p: Record<string, unknown>, dirty: boolean) {
    if (!dirty) return p;
    const t = targetsFor(form);
    if (t) p.targets = t;
    else delete p.targets;
    return p;
  }

  async function save() {
    setErr(null); setMsg(null);
    if (!accountId) return;
    const every_seconds = Math.max(60, Math.round(form.everyMinutes) * 60);
    try {
      if (rule) {
        // ⚠️ PATCH REPLACES the payload wholesale (automation_rules_api), and this
        // form holds four of its keys. Sending only those destroyed everything
        // else on the rule — `money_gate: false` reverted to True, a hand-set
        // `targets.fan_ids` vanished, and `min_days_between_pings` snapped back to
        // 14, retuning a running backfill. Spread first, exactly as
        // `BrainPanel.saveWelcome` does. `targets` rides in separately; the
        // spread never touches it, because `payload` no longer carries it.
        const merged = applyTargets(
          { ...(rule.payload ?? {}), ...payload },
          targetsDirty,
        );
        await updateM.mutateAsync({ id: rule.id, every_seconds, payload: merged, is_enabled: form.enabled });
      } else {
        // A brand-new rule has nothing to preserve, so its pool is always the
        // one the form describes.
        await createM.mutateAsync({
          account_id: accountId, kind: "auto_follow", name: "Auto-follow / Auto-like",
          every_seconds, payload: applyTargets({ ...payload }, true), is_enabled: form.enabled,
        });
      }
      // The stored targets now match the form, so the next save has nothing to
      // preserve.
      setTargetsDirty(false);
      setMsg("✓ Saved.");
    } catch (e) { setErr(errMsg(e, "Save failed")); }
  }

  async function runNow() {
    if (!rule) { setErr("Save the automation first."); return; }
    setErr(null); setMsg(null);
    // "Run now" executes the SAVED rule (rule.id), so the confirm + toast must
    // key off the SAVED payload's dry_run — NOT the (possibly unsaved) form,
    // which would otherwise say "nothing sent" while real likes fire.
    const savedDryRun = isDryRun(rule);
    const savedAction = rule.payload.action;
    const verb =
      savedAction === "ping" ? "UNFOLLOW + RE-FOLLOW real fans"
      : savedAction === "follow" ? "FOLLOW real fans"
      : "LIKE real messages";
    // ⚠️ MONEY. `money_gate` is what makes follow/ping read a fan's subscribe
    // price and skip the paid ones; with it off, every priced creator in the
    // pool is BOUGHT. `gated_follow`'s own docstring measures a 711-fan backfill
    // at ≈$177. There is no control for this knob anywhere in the app — it
    // arrives on a rule by hand or from a legacy row, and the panel's spread now
    // faithfully PRESERVES it — so this confirm is the only place it is ever put
    // in front of the operator. `BrainPanel.saveWelcome` gates its own
    // follow-back spend the same way, for the same reason.
    // A dry run spends nothing whatever the gate says, so the warning is scoped
    // to the run that actually acts — a spend warning on a preview is the kind
    // of false alarm that teaches an operator to skim past the real one.
    const moneyGateOff =
      !savedDryRun
      && (savedAction === "follow" || savedAction === "ping")
      && !boolKnob(rule.payload.money_gate, true);
    const spendWarning = moneyGateOff
      ? "\n\n⚠️ The price-check (money_gate) is also OFF on this rule, so fans are "
        + "followed WITHOUT reading their price first: every paid creator in the "
        + "pool CHARGES THIS ACCOUNT their subscription price. A full backfill of "
        + "711 fans has cost ≈$177."
      : "";
    if (!savedDryRun && !window.confirm(
      `Run now uses the LAST SAVED config, which has dry-run OFF — this will ${verb} on OnlyFans.${spendWarning}\n\nContinue?`,
    )) return;
    try {
      await runM.mutateAsync(rule.id);
      setMsg(savedDryRun
        ? "✓ Dry run queued (last saved config) — plans only, nothing sent."
        : "✓ Running now (last saved config) — actions land within ~30s.");
    } catch (e) { setErr(errMsg(e, "Run failed")); }
  }

  const last = rule?.last_run;

  return (
    <div className="space-y-5 max-w-2xl">
      <header>
        <h2 className="text-lg font-semibold">Auto-follow / Auto-like</h2>
        <p className="text-sm text-fg-dim">
          Trigger OnlyFans re-engagement by <b>liking fans’ recent messages</b>,{" "}
          <b>following fans back</b>, or <b>re-follow pinging</b> fans who went
          quiet — notification-only nudges, never a DM. Follow/ping{" "}
          <b>price-check every fan and skip paid pages</b>, so an ordinary rule
          spends nothing. Runs on a timer; <b>dry-run is on by default</b> so
          nothing acts until you confirm.
        </p>
      </header>

      <Card className="p-4 space-y-4">
        <div className="flex flex-wrap gap-4">
          <label className="flex items-center gap-2 text-sm cursor-pointer">
            <input type="checkbox" checked={form.enabled} onChange={(e) => set("enabled", e.target.checked)} />
            Enabled
          </label>
          <label className="flex items-center gap-2 text-sm cursor-pointer">
            <input type="checkbox" checked={form.dryRun} onChange={(e) => set("dryRun", e.target.checked)} />
            Dry run <span className="text-[11px] text-fg-dim">(plan only — never acts; may read OnlyFans to list the pool)</span>
          </label>
        </div>

        <div className="grid gap-3 sm:grid-cols-2">
          <label className="block space-y-1">
            <span className="text-[11px] uppercase tracking-wide text-fg-dim">Action</span>
            {/* "Like posts by id" is gone from the UI: pasting post ids is not
              *  something an operator can meaningfully do, and the only posts
              *  she could like are her own, which notifies nobody. The engine
              *  still supports the action for API callers. */}
            <select
              className={SELECT_CLS} value={form.action}
              onChange={(e) => {
                const action = e.target.value as Action;
                // The action decides which pools exist, so changing it is a pool
                // change — the save must be free to rewrite `targets`.
                setTargetsDirty(true);
                setForm((f) => ({ ...f, action, source: sourceFor(action, f.source) }));
              }}
            >
              <option value="like_messages">Like latest message (re-engage)</option>
              {/* Not "(win-back)" any more: since `all_stored` joined the Target
                *  dropdown this action covers the whole audience, and win-back is
                *  one of the four pools it can point at, not what it is. */}
              <option value="follow">Follow fans (pick who below)</option>
              <option value="ping">Re-follow ping (quiet fans)</option>
            </select>
          </label>
          <label className="block space-y-1">
            <span className="text-[11px] uppercase tracking-wide text-fg-dim">Max actions / run</span>
            <Input type="number" min={0} value={form.dailyCap}
              onChange={(e) => set("dailyCap", Number(e.target.value))} />
          </label>
        </div>

        {form.action === "ping" ? (
          <div className="space-y-2">
            <div className="grid gap-3 sm:grid-cols-2">
              <label className="block space-y-1">
                <span className="text-[11px] uppercase tracking-wide text-fg-dim">Quiet after (days)</span>
                <Input type="number" min={1} value={form.quietDays}
                  onChange={(e) => set("quietDays", Number(e.target.value))} />
              </label>
              <label className="block space-y-1">
                <span className="text-[11px] uppercase tracking-wide text-fg-dim">Min days between actions (per fan)</span>
                <Input type="number" min={1} value={form.actionGapDays}
                  onChange={(e) => set("actionGapDays", Number(e.target.value))} />
              </label>
            </div>
            <p className="text-xs text-fg-dim">
              A fan who chatted before but has been silent this long gets an
              unfollow + instant re-follow, so OnlyFans pings them with
              “started following you”. Each fan is pinged at most once per
              cooldown window; free profiles only. That cooldown is{" "}
              <b>the same ledger the follow action uses</b> — changing it here
              also re-paces a follow rule on this account.
            </p>
          </div>
        ) : form.action === "like_posts" ? (
          <label className="block space-y-1">
            <span className="text-[11px] uppercase tracking-wide text-fg-dim">Post ids (comma-separated)</span>
            <Input value={form.postIds} onChange={(e) => setTarget("postIds", e.target.value)} placeholder="123456, 234567" />
          </label>
        ) : (
          <div className="space-y-2">
            <div className="grid gap-3 sm:grid-cols-2">
              <label className="block space-y-1">
                <span className="text-[11px] uppercase tracking-wide text-fg-dim">Target</span>
                <select className={SELECT_CLS} value={form.source}
                  onChange={(e) => setTarget("source", e.target.value as Source)}>
                  {SOURCES_BY_ACTION[form.action].map((s) => (
                    <option key={s} value={s}>{SOURCE_SPECS[s].label}</option>
                  ))}
                </select>
              </label>
              {/* The source's own control or hint — from the SAME table entry that
                *  decides what `targets` it writes, so the two cannot disagree. */}
              {SOURCE_SPECS[form.source].extra?.(form, setTarget, {
                smartLists: smartListsQ.data ?? [],
                eligible: typeof rule?.last_run?.stats?.eligible === "number"
                  ? (rule.last_run.stats.eligible as number) : null,
                // The LAST RUN's own dry-run flag, not the checkbox's current
                // state: the number came from that tick, so that tick is what
                // decides whether it is standing still.
                eligibleFromDryRun: rule?.last_run?.stats?.dry_run === true,
              })}
            </div>
            {/* THE PANEL ADMITTING IT IS NOT DESCRIBING THE RULE. `sourceFor`
              *  snapped a stored source it cannot render, and an untouched save
              *  now (correctly) leaves `targets` alone — so without this the
              *  dropdown quietly shows the wrong pool forever. Cleared as soon as
              *  the operator picks a pool, because from then on the dropdown is
              *  the truth and the next save writes it. */}
            {snapped && !targetsDirty && (
              <p className="text-xs text-warn">
                This rule is stored with target <b>{snapped}</b>, which this panel
                has no control for — the dropdown above is showing the nearest
                option, not what the automation is doing. Leave it alone to keep
                the stored target; pick a Target and Save to replace it.
              </p>
            )}
            {form.action === "follow" && (
              <label className="block space-y-1 max-w-xs">
                <span className="text-[11px] uppercase tracking-wide text-fg-dim">Min days between actions (per fan)</span>
                <Input type="number" min={1} value={form.actionGapDays}
                  onChange={(e) => set("actionGapDays", Number(e.target.value))} />
                <span className="block text-[11px] text-fg-dim">
                  How long before a fan this rule already looked at is worth
                  looking at again. It is the backfill’s only progress marker —
                  and <b>the same ledger the re-follow ping uses</b>, so changing
                  it here re-paces a ping rule on this account too.
                </span>
              </label>
            )}
          </div>
        )}

        <label className="block space-y-1 max-w-xs">
          <span className="text-[11px] uppercase tracking-wide text-fg-dim">Run every (minutes)</span>
          <Input type="number" min={1} value={form.everyMinutes}
            onChange={(e) => set("everyMinutes", Number(e.target.value))} />
        </label>

        {err && <div className="text-sm text-err">{err}</div>}

        <div className="flex items-center gap-2 pt-1">
          <Button size="sm" onClick={save} disabled={busy || !accountId}>
            {rule ? "Save changes" : "Create automation"}
          </Button>
          {rule && (
            <Button size="sm" variant="ghost" onClick={runNow} disabled={runM.isPending}>
              {runM.isPending ? "Running…" : "Run now"}
            </Button>
          )}
        </div>

        {msg && <div className="text-sm text-accent border-t border-border pt-3">{msg}</div>}

        {rule && last && (
          <div className="text-xs text-fg-dim border-t border-border pt-3">
            Last run: <span className="text-fg">{last.status}</span>
            <RunStats stats={last.stats} />
            {last.started_at && ` · ${new Date(last.started_at).toLocaleString()}`}
            {last.error_text && <span className="text-err"> · {last.error_text}</span>}
          </div>
        )}
      </Card>
    </div>
  );
}
