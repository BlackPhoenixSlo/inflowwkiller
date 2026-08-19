"use client";

/**
 * AutomationSwitch — turn an automation KIND on for an account from the tab that
 * configures it, instead of hunting for it in the Automation rules list.
 *
 * A settings tab's own "Enabled" box only writes that engine's CONFIG blob. What
 * makes the engine actually tick is an `automation_rules` row, and that row is
 * born in a different panel — so an account can have every box on this tab ticked
 * and still send nothing, because no rule ever ran it. The info-gather chatter
 * (welcome_chatter_for_info) has no tab of its own at all: the rules list was the
 * ONLY place it could be switched on.
 *
 * This is that row, surfaced where the settings live:
 *
 *   on  → enable the kind's first rule, or CREATE one (catalog cadence, empty
 *         payload — the automation's own defaults, same as the rules editor)
 *   off → disable EVERY rule of the kind, so "off" honestly means nothing runs
 *
 * ⚠️ Unlike the config checkboxes around it this writes IMMEDIATELY — it edits a
 * different store, and there is no Save button on this tab that would carry it.
 */

import { useMemo } from "react";
import { Loader2 } from "lucide-react";

import { Badge } from "@/components/ui/primitives";
import { fmtAgo, fmtEvery } from "@/lib/format";
import { cn } from "@/lib/utils";
import {
  useAutomationKinds, useAutomationRules, useCreateRule, useUpdateRule,
  type AutomationRule,
} from "@/hooks/useAutomations";

/** The four states a kind can be in for one account. One value, so the badge and
 *  the help line read the same source instead of each re-deriving it from
 *  `rule`/`on`/`loading` and drifting. */
type RuleState = "loading" | "running" | "parked" | "absent";

interface AutomationSwitchState {
  /** Every rule of this kind on the account (legacy-named rows serialize under
   *  the canonical kind, so a pre-0062 DB matches here too — no duplicates). */
  rules: AutomationRule[];
  /** The rule the switch speaks for: the running one, else the first parked one. */
  rule: AutomationRule | null;
  state: RuleState;
  /** The kind's catalog label — the name a rule created here is given. */
  kindLabel: string;
  /** What ticking an ABSENT kind on would create. Never mixed with the schedule
   *  of a rule that exists: a `daily_at` rule has no interval, and printing this
   *  number for it would name a cadence the rule does not run on. */
  newRuleCadence: number;
  busy: boolean;
  /** Cosmetic only — `toggle` enforces the same condition itself, so the write
   *  cannot happen even if a caller forgets to pass this to its input. */
  disabled: boolean;
  error: Error | null;
  toggle: (next: boolean) => void;
}

function useAutomationSwitch(
  accountId: string | null, kind: string,
): AutomationSwitchState {
  const rulesQ = useAutomationRules(accountId);
  const kindsQ = useAutomationKinds();
  const createM = useCreateRule(accountId);
  const updateM = useUpdateRule(accountId);

  const rules = useMemo(
    () => (rulesQ.data ?? []).filter((r) => r.kind === kind),
    [rulesQ.data, kind],
  );
  const meta = useMemo(
    () => (kindsQ.data ?? []).find((k) => k.kind === kind) ?? null,
    [kindsQ.data, kind],
  );
  const running = rules.find((r) => r.is_enabled) ?? null;
  const rule = running ?? rules[0] ?? null;

  // "Not answering for this account yet." `isPlaceholderData` is the load-bearing
  // half: the rules query keeps the PREVIOUS account's rows on screen while the new
  // one loads, so a switch clicked in that window would have flipped a rule
  // belonging to the account the operator just left. The kind catalog is in here
  // too — without it a created rule silently lands on the 300s fallback cadence
  // instead of the kind's own.
  const settling = rulesQ.isLoading || rulesQ.isPlaceholderData || kindsQ.isLoading;

  const toggle = (next: boolean) => {
    if (!accountId || settling) return;
    // `mutate`, not `mutateAsync`: these writes are independent, so there is
    // nothing to sequence and no promise to own — react-query already holds the
    // pending/error state the UI reads below.
    if (!next) {
      // Off disables ALL of them: with two rules of one kind, parking just the
      // first would draw an unchecked box while the engine kept sending.
      rules.filter((r) => r.is_enabled)
        .forEach((r) => updateM.mutate({ id: r.id, is_enabled: false }));
    } else if (rule) {
      // On wakes the EXISTING rule (keeping its cadence, payload and quiet hours);
      // a second row of the same kind would just double the tick.
      updateM.mutate({ id: rule.id, is_enabled: true });
    } else {
      createM.mutate({
        account_id: accountId, kind,
        name: meta?.label || kind,
        // 300s is the rules editor's own fallback for an uncatalogued kind.
        every_seconds: meta?.cadence_default_s ?? 300,
        payload: {},                 // empty = the automation's shipped defaults
        is_enabled: true,
      });
    }
  };

  return {
    rules, rule,
    state: settling ? "loading" : running ? "running" : rule ? "parked" : "absent",
    kindLabel: meta?.label || kind,
    newRuleCadence: meta?.cadence_default_s ?? 300,
    busy: createM.isPending || updateM.isPending,
    disabled: !accountId || settling || createM.isPending || updateM.isPending,
    error: createM.error ?? updateM.error,
    toggle,
  };
}

/** The one badge both surfaces render, so "on" never says two different things. */
function RuleStateBadge({ sw }: { sw: AutomationSwitchState }) {
  if (sw.busy) return <Loader2 size={13} className="animate-spin text-fg-dim" />;
  if (sw.state === "loading") return null;
  if (sw.state === "running") {
    return <Badge color="ok">on · {fmtEvery(sw.rule?.every_seconds)}</Badge>;
  }
  return (
    <Badge color={sw.state === "parked" ? "warn" : "muted"}>
      {sw.state === "parked" ? "rule is off" : "not set up yet"}
    </Badge>
  );
}

/** Compact header pill — "is this engine actually scheduled?" next to the tab's
 *  own config checkbox, which only ever answered "is it configured on?". */
export function AutomationSwitch({
  accountId, kind, label = "Automation", className,
}: {
  accountId: string | null;
  kind: string;
  label?: string;
  className?: string;
}) {
  const sw = useAutomationSwitch(accountId, kind);
  return (
    <span className={cn("flex items-center gap-2", className)}>
      <label className="flex items-center gap-2 text-sm cursor-pointer"
        title={`The ${sw.kindLabel} automation rule. Saves immediately.`}>
        <input type="checkbox" checked={sw.state === "running"}
          disabled={sw.disabled} onChange={(e) => sw.toggle(e.target.checked)} />
        {label}
      </label>
      <RuleStateBadge sw={sw} />
      {sw.error && (
        <span className="text-[11px] text-err max-w-48 truncate"
          title={sw.error.message}>{sw.error.message || "failed"}</span>
      )}
    </span>
  );
}

/** The full block: the same switch with the sentence that says what turning it on
 *  actually starts, plus what the rule is doing. Written for a kind whose ONLY
 *  other home is the rules list. */
export function AutomationSwitchCard({
  accountId, kind, title, children, className,
}: {
  accountId: string | null;
  kind: string;
  title: string;
  children?: React.ReactNode;
  className?: string;
}) {
  const sw = useAutomationSwitch(accountId, kind);
  const ranAgo = fmtAgo(sw.rule?.last_run?.started_at);
  // Only what the badge does NOT already say: when it runs and how it went, or
  // what a tick would create.
  // `loading` gets its own line rather than falling through to the `absent` copy:
  // that branch claims the account has no rule, which is a claim we cannot make
  // until the query lands (and it flashed on every account switch).
  const HELP: Record<RuleState, string> = {
    loading: "Checking this account's automation rules…",
    running: `Runs on the background executor · ${
      ranAgo ? `last run ${ranAgo}` : "no run logged yet"}${
      sw.rule?.last_run?.status === "error" ? " (error)" : ""}`,
    parked: "Its rule is parked — ticking the box restarts it.",
    absent: `Ticking the box creates the "${sw.kindLabel}" rule (${
      fmtEvery(sw.newRuleCadence)}) and starts it.`,
  };

  return (
    <div className={cn(
      "rounded-md border border-border bg-bg-elev-1 px-3 py-2.5 space-y-2 text-sm",
      className,
    )}>
      <label className="flex items-start gap-2 cursor-pointer">
        <input type="checkbox" className="mt-0.5" checked={sw.state === "running"}
          disabled={sw.disabled} onChange={(e) => sw.toggle(e.target.checked)} />
        <span className="flex-1">
          <span className="flex items-center gap-2 flex-wrap">
            <span className="font-medium">{title}</span>
            <RuleStateBadge sw={sw} />
          </span>
          <span className="block text-fg-dim text-xs">{children}</span>
        </span>
      </label>
      <p className="text-[11px] text-fg-dim pl-6">
        {HELP[sw.state]} It is the same row the Automation rules list shows, and
        it saves the moment you tick it.
      </p>
      {sw.error && (
        <p className="text-xs text-err pl-6">
          {sw.error.message || "Could not change the rule"} — nothing was changed.
        </p>
      )}
      {sw.rules.length > 1 && (
        <p className="text-[11px] text-fg-dim pl-6">
          ⚠ {sw.rules.length} rules of this kind exist on this account — the switch drives
          the running one; the rest are in the Automation rules list.
        </p>
      )}
    </div>
  );
}
