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

import { useEffect, useMemo, useState } from "react";

import { Badge, Button, Card } from "@/components/ui/primitives";
import { cn } from "@/lib/utils";
import { useActiveAccounts } from "@/hooks/useAccounts";
import {
  useAutomationKinds,
  useAutomationRules,
  useDeleteRule,
  useRunRuleNow,
  useUpdateRule,
  type AutomationRule,
} from "@/hooks/useAutomations";
import RuleEditor from "@/components/automations/RuleEditor";
import ReadyMadePanel from "@/components/automations/ReadyMadePanel";
import BrainPanel from "@/components/automations/BrainPanel";
import AutomationRunsCard from "@/components/stats/AutomationRunsCard";

function timeAgo(iso: string | null): string | null {
  if (!iso) return null;
  const then = new Date(iso).getTime();
  if (!Number.isFinite(then)) return null;
  const secs = Math.round((Date.now() - then) / 1000);
  if (secs < 0) return "soon";
  if (secs < 60) return `${secs}s ago`;
  const mins = Math.round(secs / 60);
  if (mins < 60) return `${mins} min ago`;
  const hrs = Math.round(mins / 60);
  if (hrs < 24) return `${hrs} h ago`;
  return `${Math.round(hrs / 24)} d ago`;
}

function fmtEvery(secs: number | null): string {
  if (!secs || secs <= 0) return "—";
  if (secs % 3600 === 0) return `every ${secs / 3600} h`;
  if (secs % 60 === 0) return `every ${secs / 60} min`;
  return `every ${secs} s`;
}

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

  return (
    <div className="space-y-5">
      {/* Per-account Brain (persona / time lines + images / model / caps). */}
      <BrainPanel />

      <Card className="p-4 space-y-3">
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
          <div className="flex items-center gap-2">
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

        {/* Account picker */}
        {accounts.length > 1 && (
          <div className="flex items-center gap-1.5 flex-wrap">
            <span className="text-[10px] uppercase tracking-wide text-fg-dim mr-1">Account</span>
            {accounts.map((a) => {
              const active = accountId === a.id;
              return (
                <button
                  key={a.id}
                  type="button"
                  onClick={() => { setAccountId(a.id); closeEditor(); }}
                  className={cn(
                    "px-2.5 py-1 rounded-full text-xs border transition-colors",
                    active
                      ? "bg-accent text-white border-accent"
                      : "bg-bg-elev-1 text-fg-dim border-border hover:text-fg hover:border-fg-dim",
                  )}
                >
                  {a.nickname || a.id}
                </button>
              );
            })}
          </div>
        )}

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
                const ago = timeAgo(r.last_run?.started_at ?? null);
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

                      {/* Actions */}
                      <div className="flex items-center gap-1.5 shrink-0">
                        <Button
                          size="sm"
                          variant="secondary"
                          onClick={() => runNow(r)}
                          disabled={runM.isPending}
                        >
                          Run now
                        </Button>
                        <Button
                          size="sm"
                          variant="ghost"
                          onClick={() => { setAdding(false); setEditing(r); }}
                        >
                          Edit
                        </Button>
                        <Button
                          size="sm"
                          variant="danger"
                          onClick={() => remove(r)}
                          disabled={deleteM.isPending}
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

      {/* Ready-made posts & broadcasts — fire-once actions, tabbed, under the
       *  recurring-rules list. */}
      <ReadyMadePanel />

      {/* Full run history (read-only) lives right below the controls. */}
      <AutomationRunsCard />
    </div>
  );
}
