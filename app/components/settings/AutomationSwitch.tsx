"use client";

/**
 * The `automation_rules` row behind a settings tab, in the two shapes a tab needs.
 *
 * A tab's "Enabled" box writes that engine's CONFIG blob. What makes the engine
 * actually tick is a rule row, and that row was born in a different panel — so an
 * account could have every box on a tab ticked and still send nothing, because no
 * rule ever ran it.
 *
 * `AutomationSwitchCard` — the row AS the switch, for a kind whose tab has no
 * config flag of its own to hang it on (welcome_chatter_for_info has no tab at
 * all; the rules list was the ONLY place it could be started):
 *
 *   on  → wake the kind's running-or-first rule, or CREATE one furnished from
 *         the catalog: its cadence and name, plus any create payload the kind
 *         declares (`_CREATE_PAYLOAD`) — which is deliberately NOT what the
 *         rules editor's form would have produced
 *   off → disable EVERY rule of the kind, so "off" honestly means nothing runs
 *
 * ⚠️ Unlike the config checkboxes around it this writes IMMEDIATELY — it edits a
 * different store, and there is no Save button on this tab that would carry it.
 *
 * `AutomationRuleBadge` — the row as a STATUS LIGHT, for a tab whose own Enabled
 * box already owns it. The AI Chatter tab carried a second checkbox here, next to
 * its Enabled box and indistinguishable from it; its save endpoint syncs the rule
 * now (`automation_rules_api.ensure_kind_rule`), so one box is the whole of on and
 * all that is left to show is whether the row is ticking.
 */

import { useMemo } from "react";
import { Loader2 } from "lucide-react";

import { Badge } from "@/components/ui/primitives";
import { fmtAgo, fmtEvery } from "@/lib/format";
import { cn } from "@/lib/utils";
import {
  useAutomationKinds, useAutomationRules, useSwitchKind,
  type AutomationRule,
} from "@/hooks/useAutomations";

/** The states a kind can be in for one account. One value, so the badge and the
 *  help line read the same source instead of each re-deriving it from
 *  `rule`/`on`/`loading` and drifting. `loading` and `unreadable` are both
 *  "we cannot say", kept apart because only one of them is still in progress. */
type RuleState = "loading" | "unreadable" | "running" | "parked" | "absent";

/** What the rule row IS. Split from the switch below so a surface that only
 *  reports (the AI Chatter header badge) does not carry two mutations and a
 *  `toggle` it can never call — the component's read-only contract is the
 *  hook's too. */
interface RuleView {
  /** Every rule of this kind on the account (legacy-named rows serialize under
   *  the canonical kind, so a pre-0062 DB matches here too — no duplicates). */
  rules: AutomationRule[];
  /** The rule the switch speaks for: the running one, else the first parked one. */
  rule: AutomationRule | null;
  state: RuleState;
  /** The kind's catalog label — the name a rule created here is given. Falls
   *  back to the raw kind slug, so `catalogued` says whether it is the real one. */
  kindLabel: string;
  /** What ticking an ABSENT kind on would create. Never mixed with the schedule
   *  of a rule that exists: a `daily_at` rule has no interval, and printing this
   *  number for it would name a cadence the rule does not run on. Only true when
   *  `catalogued`; otherwise it is the module's own fallback, not this kind's. */
  newRuleCadence: number;
  /** Whether the two fields above came from the catalog or from their fallbacks.
   *  The catalog is no longer part of the write gate, which is right — the client
   *  sends no cadence — but it left a window where the rules have loaded and the
   *  catalog has not, and copy that names a label and a schedule from fallbacks
   *  is copy that names a rule the server will not create. */
  catalogued: boolean;
  /** "This account's rules have not arrived" — every write is held until it
   *  clears. `isPlaceholderData` is the load-bearing half: the rules query keeps
   *  the PREVIOUS account's rows on screen while the new one loads, so a switch
   *  clicked in that window would have flipped a rule belonging to the account
   *  the operator just left. See the predicate for why neither a failed poll nor
   *  the kind catalog belongs in here. */
  settling: boolean;
}

interface AutomationSwitchState extends RuleView {
  busy: boolean;
  /** Cosmetic only — `toggle` enforces the same condition itself, so the write
   *  cannot happen even if a caller forgets to pass this to its input. */
  disabled: boolean;
  error: Error | null;
  toggle: (next: boolean) => void;
}

function useRuleView(accountId: string | null, kind: string): RuleView {
  const rulesQ = useAutomationRules(accountId);
  const kindsQ = useAutomationKinds();

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
  // "We do not know this account's rules": no rows have arrived, or the ones on
  // screen still belong to the account being switched away from. Both the
  // displayed state and the write gate hang off it.
  //
  // Written as data-presence rather than `isLoading`, which is what the card was
  // keyed on before: a read that FAILED is not a read that came back empty, and
  // under isLoading a failed first fetch settled with no rows, so the card
  // reported the kind "not set up yet" over a list it never managed to read and
  // offered a live checkbox beside whatever is actually there.
  //
  // A LATER poll failing is deliberately not in here, and data presence is how
  // that is expressed: a background failure DOES flip the query to `error` while
  // keeping the rows (query-core sets the status unconditionally and exposes
  // `isRefetchError = isError && hasData` for it), so keying this on `isSuccess`
  // would blank a card whose rows are on screen and still true.
  //
  // Nor is the kind catalog: it is fetched once with a 24h staleTime and never
  // refetched, so folding it in held the switch against rules that HAD been
  // read. It feeds only the label and the new-rule cadence, both of which have
  // their own fallback.
  const settling = rulesQ.data === undefined || rulesQ.isPlaceholderData;

  return {
    rules, rule, settling,
    // `unreadable` is settling too, but for a reason the operator can act on:
    // "still checking" would be a lie once the read has come back failed.
    state: rulesQ.isLoadingError
      ? "unreadable"
      : settling ? "loading" : running ? "running" : rule ? "parked" : "absent",
    kindLabel: meta?.label || kind,
    // 300s is the rules editor's own fallback for an uncatalogued kind.
    newRuleCadence: meta?.cadence_default_s ?? 300,
    catalogued: meta !== null,
  };
}

function useAutomationSwitch(
  accountId: string | null, kind: string,
): AutomationSwitchState {
  const view = useRuleView(accountId, kind);
  const switchM = useSwitchKind(accountId);

  // One request, whatever the rows look like: create the first, wake the parked
  // one, park every enabled one. That decision is the SERVER's — the same call
  // the AI Chatter tab's Save makes — so the two switches on this tab cannot
  // drift, and parking two rules can no longer half-fail between two requests.
  //
  // Still gated on `settling`: the rules query keeps the PREVIOUS account's rows
  // on screen while the new one loads, and a click in that window would switch
  // the account the operator just left.
  const toggle = (next: boolean) => {
    if (!accountId || view.settling) return;
    switchM.mutate({ kind, enable: next });
  };

  return {
    ...view,
    busy: switchM.isPending,
    disabled: !accountId || view.settling || switchM.isPending,
    error: switchM.error,
    toggle,
  };
}

/** The one badge both surfaces render, so "on" never says two different things.
 *  `busy` is the switch's spinner; the read-only badge has nothing to wait for. */
function RuleStateBadge({ view, busy = false }: { view: RuleView; busy?: boolean }) {
  if (busy) return <Loader2 size={13} className="animate-spin text-fg-dim" />;
  // Neither of the we-cannot-say states gets a badge: "not set up yet" over a
  // list that never loaded is the claim this whole predicate exists to prevent.
  if (view.state === "loading" || view.state === "unreadable") return null;
  if (view.state === "running") {
    return <Badge color="ok">on · {fmtEvery(view.rule?.every_seconds)}</Badge>;
  }
  return (
    <Badge color={view.state === "parked" ? "warn" : "muted"}>
      {view.state === "parked" ? "rule is off" : "not set up yet"}
    </Badge>
  );
}

/** Read-only status light for a kind's rule, for a tab that already OWNS the
 *  switch. The AI Chatter header carried a second checkbox for this row, sitting
 *  a few pixels from its own "Enabled" box and indistinguishable from it; the
 *  save endpoint now syncs the rule from `enabled`, so one box is the whole of
 *  on and what is left to show is whether the row is actually ticking. */
export function AutomationRuleBadge({
  accountId, kind, className,
}: {
  accountId: string | null;
  kind: string;
  className?: string;
}) {
  const view = useRuleView(accountId, kind);
  return (
    <span className={cn("flex items-center", className)}
      title={`The ${view.kindLabel} automation rule — the row the executor ticks.`}>
      <RuleStateBadge view={view} />
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
    unreadable: "Couldn't read this account's automation rules, so this can't "
      + "say whether the automation is set up. It will retry on its own.",
    running: `Runs on the background executor · ${
      ranAgo ? `last run ${ranAgo}` : "no run logged yet"}${
      sw.rule?.last_run?.status === "error" ? " (error)" : ""}`,
    parked: "Its rule is parked — ticking the box restarts it.",
    absent: sw.catalogued
      ? `Ticking the box creates the "${sw.kindLabel}" rule (${
          fmtEvery(sw.newRuleCadence)}) and starts it.`
      // Until the catalog answers, the label is the raw slug and the cadence is
      // a module fallback — naming either would describe a rule the server is
      // not going to create.
      : "Ticking the box creates this automation's rule and starts it.",
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
            <RuleStateBadge view={sw} busy={sw.busy} />
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
