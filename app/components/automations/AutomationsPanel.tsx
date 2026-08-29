"use client";

/**
 * AutomationsPanel — the operator's control surface over `automation_rules`.
 *
 * One row per rule: enable/disable toggle, kind + name, cadence, last-run
 * status, a "Run now" button (enqueue an immediate job), Edit, and Delete.
 * "New rule" / "Edit" open the inline RuleEditor. Account-scoped (mirrors the
 * Templates tab picker). The read-only AutomationRunsCard rides below for the
 * full run history.
 */

import dynamic from "next/dynamic";
import { useEffect, useMemo, useState } from "react";

import { Badge, Button, Card } from "@/components/ui/primitives";
import { cn } from "@/lib/utils";
import { fmtAgo, fmtEvery } from "@/lib/format";
import { AccountChips } from "@/components/AccountChips";
import { useActiveAccounts } from "@/hooks/useAccounts";
import {
  useAutomationKinds,
  useAutomationRules,
  useDeleteRule,
  useRunRuleNow,
  useUpdateRule,
  type AutomationRule,
} from "@/hooks/useAutomations";
import BrainPanel from "@/components/automations/BrainPanel";
import AutomationRunsCard from "@/components/stats/AutomationRunsCard";
import SettingsTransfer from "@/components/settings/SettingsTransfer";

// RuleEditor and ReadyMadePanel used to be static imports, which put them —
// and everything they pull in — into the chunk the browser must have before
// /automations can commit at all. That made this the heaviest route in the app
// (1.44 MB of JS against 873 KB for /stats), and until it lands the App Router
// keeps the PREVIOUS page on screen, so the click reads as ignored. Splitting
// these two took the route to 935 KB with the other routes byte-identical.
//
// Only these two, and for opposite reasons:
//   • RuleEditor already renders behind `(adding || editing)`, so an operator
//     who never opens the editor was paying ~1,000 lines plus its knob widgets
//     for a form they never see. Splitting it costs nothing at all.
//   • ReadyMadePanel drags FunnelEditor and FunnelMediaPane behind it (~2,500
//     lines together), and the bundle win holds wherever it sits on the page.
//     Its position has since moved, though: on desktop it still arrives below
//     the ~2000px Brain, but on phone it is now the FIRST card. So it carries a
//     `loading` placeholder that holds the card's own header while the chunk
//     lands, which keeps the first screen from being blank and then jumping. It
//     does NOT reserve the loaded panel's full height — the real one carries a
//     summary paragraph, an account picker and a tab strip — so some shift
//     remains; this trades a big jump for a small one.
// BrainPanel is deliberately NOT split: it is the first card at desktop, so
// deferring it would trade the reported symptom for a slower
// time-to-the-thing-you-came-for.
const RuleEditor = dynamic(
  () => import("@/components/automations/RuleEditor"), { ssr: false },
);
const ReadyMadePanel = dynamic(
  () => import("@/components/automations/ReadyMadePanel"),
  {
    ssr: false,
    // Built from the same Card + header shape the panel itself opens with, so
    // the reserved box is the panel's own top rather than a guessed height. The
    // title is repeated as a literal rather than imported: importing anything
    // from ReadyMadePanel here would pull it back into this chunk and undo the
    // split this block exists for.
    loading: () => (
      <Card className="p-4 space-y-3">
        <header>
          <h2 className="text-sm font-medium text-fg">Ready-made posts &amp; broadcasts</h2>
          <p className="text-xs text-fg-dim">Loading…</p>
        </header>
      </Card>
    ),
  },
);

function StatusBadge({ status }: { status: string }) {
  const color =
    status === "ok" ? "ok" : status === "error" ? "err" : status === "running" ? "warn" : "muted";
  return <Badge color={color}>{status === "running" ? "running…" : status}</Badge>;
}

export default function AutomationsPanel() {
  const accounts = useActiveAccounts();
  const [accountId, setAccountId] = useState<string | null>(null);

  // Default to the first session-backed account once they load.
  useEffect(() => {
    if (!accountId && accounts.length > 0) setAccountId(accounts[0].id);
  }, [accounts, accountId]);

  const kindsQ = useAutomationKinds();
  const rulesQ = useAutomationRules(accountId);
  const updateM = useUpdateRule(accountId);
  const deleteM = useDeleteRule(accountId);
  const runM = useRunRuleNow(accountId);

  const [editing, setEditing] = useState<AutomationRule | null>(null);
  const [adding, setAdding] = useState(false);
  const [rowErr, setRowErr] = useState<{ id: number; msg: string } | null>(null);

  const kinds = kindsQ.data ?? [];
  const rules = rulesQ.data ?? [];
  const enabledCount = useMemo(() => rules.filter((r) => r.is_enabled).length, [rules]);

  function closeEditor() {
    setEditing(null);
    setAdding(false);
  }

  async function toggle(rule: AutomationRule) {
    setRowErr(null);
    try {
      await updateM.mutateAsync({ id: rule.id, is_enabled: !rule.is_enabled });
    } catch (e) {
      setRowErr({ id: rule.id, msg: (e as Error)?.message || "Toggle failed" });
    }
  }

  async function runNow(rule: AutomationRule) {
    setRowErr(null);
    try {
      await runM.mutateAsync(rule.id);
    } catch (e) {
      setRowErr({ id: rule.id, msg: (e as Error)?.message || "Run failed" });
    }
  }

  async function remove(rule: AutomationRule) {
    setRowErr(null);
    if (!window.confirm(`Delete automation rule "${rule.name}"? This stops the schedule.`)) return;
    try {
      await deleteM.mutateAsync(rule.id);
      if (editing?.id === rule.id) closeEditor();
    } catch (e) {
      setRowErr({ id: rule.id, msg: (e as Error)?.message || "Delete failed" });
    }
  }

  // THE STACK, stated once — the four `order-*` values below are fragments of
  // this and are meaningless read alone:
  //
  //   phone   ready-made → rules → brain → runs
  //   md+     brain → ready-made → rules → runs
  //
  // Ready-made sits above the rules list at both widths: firing a post or a
  // broadcast is the errand most visits are for, while the rules list is the
  // power-user surface. The Brain leads at desktop but drops to third on phone,
  // where its ~2000px of always-expanded config would otherwise bury everything
  // else. `order-4` on the runs card is mandatory, not decorative: without it
  // that card sits at order:0 and floats above every ordered sibling at EVERY
  // breakpoint.
  return (
    <div className="flex flex-col gap-5">
      <div className="order-3 md:order-1">
        <BrainPanel />
      </div>

      <Card className="p-4 space-y-3 order-2 md:order-3">
        <header className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <h2 className="text-sm font-medium text-fg">Automation rules</h2>
            <p className="text-xs text-fg-dim">
              Enabled rules run on their schedule via the background executor.
              {rules.length > 0 && (
                <span className="ml-1 tabular-nums">
                  {enabledCount}/{rules.length} on.
                </span>
              )}
            </p>
          </div>
          <div className="flex items-center gap-2 flex-wrap">
            <SettingsTransfer
              accountId={accountId}
              nickname={accounts.find((a) => a.id === accountId)?.nickname}
            />
            <Button
              size="sm"
              variant="secondary"
              onClick={() => rulesQ.refetch()}
              disabled={rulesQ.isFetching}
            >
              <span className={cn("inline-block mr-1", rulesQ.isFetching && "animate-spin")}>↻</span>
              {rulesQ.isFetching ? "Refreshing…" : "Refresh"}
            </Button>
            <Button
              size="sm"
              variant="primary"
              onClick={() => { setEditing(null); setAdding(true); }}
              disabled={!accountId || kinds.length === 0}
            >
              + New rule
            </Button>
          </div>
        </header>

        <AccountChips
          accountId={accountId}
          onChange={(id) => { setAccountId(id); closeEditor(); }}
        />

        {(adding || editing) && accountId && (
          <RuleEditor
            // Remount when the target rule changes — the editor seeds its fields
            // from `editing` on mount only, so without a key, switching from one
            // rule's Edit to another's left the grid showing the first rule's
            // values (only the title, read from props, updated).
            key={editing?.id ?? "new"}
            accountId={accountId}
            kinds={kinds}
            editing={editing}
            onClose={closeEditor}
          />
        )}

        {rulesQ.isLoading && <div className="text-sm text-fg-dim">Loading…</div>}
        {rulesQ.isError && (
          <div className="text-sm text-err">
            {(rulesQ.error as Error)?.message || "Failed to load rules"}
          </div>
        )}

        {!rulesQ.isLoading && !rulesQ.isError && (
          rules.length === 0 ? (
            <div className="text-sm text-fg-dim py-2">
              No automation rules yet. Click <span className="text-fg">+ New rule</span> to create one.
            </div>
          ) : (
            <ul className="divide-y divide-border">
              {rules.map((r) => {
                const ago = fmtAgo(r.last_run?.started_at ?? null);
                return (
                  <li key={r.id} className="py-2.5 space-y-1">
                    <div className="flex flex-wrap items-center gap-3">
                      {/* Toggle */}
                      <input
                        type="checkbox"
                        checked={r.is_enabled}
                        onChange={() => toggle(r)}
                        disabled={updateM.isPending}
                        title={r.is_enabled ? "Disable" : "Enable"}
                        className="w-4 h-4 rounded accent-accent cursor-pointer shrink-0"
                      />
                      <div className="min-w-0 flex-1">
                        <div className="flex items-center gap-2 min-w-0">
                          <span className="text-sm font-medium text-fg truncate">{r.name}</span>
                          <span className="text-[11px] text-fg-dim font-mono shrink-0">{r.kind}</span>
                          {r.has_pending_job && <Badge color="warn">queued</Badge>}
                        </div>
                        <div className="text-[11px] text-fg-dim flex flex-wrap items-center gap-x-2 gap-y-0.5">
                          <span className="tabular-nums">{fmtEvery(r.every_seconds)}</span>
                          {r.last_run ? (
                            <>
                              <span>·</span>
                              <StatusBadge status={r.last_run.status} />
                              {ago && <span title={r.last_run.started_at ?? ""}>{ago}</span>}
                            </>
                          ) : (
                            <>
                              <span>·</span>
                              <span className="italic">never run</span>
                            </>
                          )}
                          {!r.is_enabled && <span className="text-fg-dim/70">· paused</span>}
                        </div>
                      </div>

                      {/* Actions — on phone they take their own full-width row
                       *  so "Delete" is not 6px from "Run now". */}
                      <div className="flex items-center gap-2 w-full md:w-auto md:gap-1.5 md:shrink-0">
                        <Button
                          size="sm"
                          variant="secondary"
                          onClick={() => runNow(r)}
                          disabled={runM.isPending}
                          className="flex-1 min-h-10 md:flex-none md:min-h-0"
                        >
                          Run now
                        </Button>
                        <Button
                          size="sm"
                          variant="ghost"
                          onClick={() => { setAdding(false); setEditing(r); }}
                          className="flex-1 min-h-10 md:flex-none md:min-h-0"
                        >
                          Edit
                        </Button>
                        <Button
                          size="sm"
                          variant="danger"
                          onClick={() => remove(r)}
                          disabled={deleteM.isPending}
                          className="flex-1 min-h-10 md:flex-none md:min-h-0"
                        >
                          Delete
                        </Button>
                      </div>
                    </div>

                    {r.last_run?.error_text && (
                      <div className="ml-7 text-[11px] text-err whitespace-pre-wrap break-words">
                        {r.last_run.error_text}
                      </div>
                    )}
                    {rowErr?.id === r.id && (
                      <div className="ml-7 text-[11px] text-err">{rowErr.msg}</div>
                    )}
                  </li>
                );
              })}
            </ul>
          )
        )}
      </Card>

      {/* Ready-made posts & broadcasts — fire-once actions, tabbed. */}
      <div className="order-1 md:order-2">
        <ReadyMadePanel />
      </div>

      {/* Full run history (read-only). */}
      <div className="order-4">
        <AutomationRunsCard />
      </div>
    </div>
  );
}
