"use client";

/**
 * OrphanTipsCard — surface for manually attributing tips that fall out of
 * the per-employee view's "Unattributed" bucket.
 *
 * A tip lands here when its 7-day standalone lookback found no
 * chatter-attributed outbound message to the fan and no manual override
 * is set. Picking an employee POSTs to
 * /admin/ingest/transactions/{id}/attribute, which sets
 * transactions.attributed_employee_id; the view's COALESCE makes it win
 * over the lookback heuristic. Both the orphan list and the per-employee
 * table refetch — the row disappears, totals shift to the picked
 * chatter.
 *
 * Date range comes from the page-level filter so the list scopes to the
 * same window as the per-employee table.
 *
 * "Show assigned" toggle (default off) reveals previously-assigned tips
 * so they can be reassigned to a different chatter or cleared.
 */

import { useMemo, useState } from "react";

import { Button, Card } from "@/components/ui/primitives";
import { useEmployee } from "@/contexts/EmployeeContext";
import { useAccounts } from "@/hooks/useAccounts";
import { cn } from "@/lib/utils";
import { fmtCents, fmtDateTime } from "@/lib/format";
import {
  useAttributeTip,
  useOrphanTips,
  type OrphanTipRow,
} from "@/hooks/useStats";

interface Props {
  from: string | null;
  to: string | null;
}

export default function OrphanTipsCard({ from, to }: Props) {
  const [showAssigned, setShowAssigned] = useState(false);
  const q = useOrphanTips({ from, to, includeAssigned: showAssigned });

  // Only surface tips for models the current viewer can actually see.
  // useAccounts() is already principal-aware: owners get every account,
  // chatters get only their granted accounts. The orphan-tips endpoint
  // pools tips across all of an owner's models, so without this filter a
  // chatter (or any viewer) would see tips for models outside their scope.
  const accountsQ = useAccounts();
  const visibleIds = useMemo(
    () => new Set((accountsQ.data?.accounts ?? []).map((a) => a.id)),
    [accountsQ.data],
  );

  // Fail closed: until we know which models the viewer can see, filter
  // everything out rather than briefly leaking out-of-scope tips.
  const rows = useMemo(
    () => (q.data?.rows ?? []).filter((r) => visibleIds.has(r.account_id)),
    [q.data, visibleIds],
  );
  const orphanRows = useMemo(
    () => rows.filter((r) => r.attributed_employee_id == null),
    [rows],
  );
  const assignedRows = useMemo(
    () => rows.filter((r) => r.attributed_employee_id != null),
    [rows],
  );

  const orphanTotal = orphanRows.reduce((s, r) => s + r.amount_cents, 0);
  const assignedTotal = assignedRows.reduce((s, r) => s + r.amount_cents, 0);

  // Always render so the "Show assigned" toggle is reachable. Owners want
  // to be able to clear / reassign previously-attributed tips even when
  // the orphan queue is empty — hiding the card stranded that workflow.

  return (
    <Card className="p-4 space-y-3">
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h2 className="text-sm font-medium text-fg">
            Orphan tips
            {orphanRows.length > 0 && (
              <> · <span className="tabular-nums">{fmtCents(orphanTotal)}</span></>
            )}
          </h2>
          <p className="text-xs text-fg-dim">
            Tips with no chatter-attributed outbound message in the 7-day
            lookback. Scoped to the page&apos;s date range.
          </p>
        </div>
        <div className="flex items-center gap-3 text-xs text-fg-dim">
          <span className="tabular-nums">
            {orphanRows.length} orphan
            {showAssigned && assignedRows.length > 0 && (
              <> · {assignedRows.length} assigned ({fmtCents(assignedTotal)})</>
            )}
          </span>
          <label className="flex items-center gap-1.5 cursor-pointer select-none">
            <input
              type="checkbox"
              className="accent-accent"
              checked={showAssigned}
              onChange={(e) => setShowAssigned(e.target.checked)}
            />
            Show assigned
          </label>
        </div>
      </header>

      {(q.isLoading || accountsQ.isLoading) && (
        <div className="text-sm text-fg-dim">Loading…</div>
      )}
      {q.isError && (
        <div className="text-sm text-err">
          {(q.error as Error)?.message || "Failed to load orphan tips"}
        </div>
      )}

      {!q.isLoading && !accountsQ.isLoading && !q.isError && (
        <div
          className={cn(
            "transition-opacity",
            q.isFetching && q.isPlaceholderData && "opacity-60",
          )}
        >
          {orphanRows.length > 0 ? (
            <ul className="divide-y divide-border">
              {orphanRows.map((r) => (
                <OrphanRow key={r.id} row={r} />
              ))}
            </ul>
          ) : (
            <div className="text-sm text-fg-dim py-1">
              No orphan tips in this window.
            </div>
          )}
          {showAssigned && assignedRows.length > 0 && (
            <div className="pt-3 border-t border-border space-y-2">
              <div className="text-[11px] uppercase tracking-wide text-fg-dim">
                Previously assigned
              </div>
              <ul className="divide-y divide-border">
                {assignedRows.map((r) => (
                  <OrphanRow key={r.id} row={r} />
                ))}
              </ul>
            </div>
          )}
        </div>
      )}
    </Card>
  );
}

function OrphanRow({ row }: { row: OrphanTipRow }) {
  const { roster } = useEmployee();
  const mut = useAttributeTip();
  const [selected, setSelected] = useState<string>("");

  const isAssigned = row.attributed_employee_id != null;
  const choices = roster.filter((e) => e.is_active);
  // Server returns the canonical display_name (joined from the employees
  // table). Fall back to the roster only if the server didn't ship it,
  // and to a generic label as a last resort.
  const currentName = isAssigned
    ? row.attributed_employee_name
      ?? roster.find((e) => e.id === row.attributed_employee_id)?.display_name
      ?? `Employee ${row.attributed_employee_id}`
    : null;

  const assign = () => {
    const eid = selected === "" ? null : Number(selected);
    if (eid === null) return;
    mut.mutate(
      { txId: row.id, employeeId: eid },
      { onSuccess: () => setSelected("") },
    );
  };
  const clear = () => {
    mut.mutate({ txId: row.id, employeeId: null });
  };

  return (
    <li className="py-2.5 flex flex-wrap items-center gap-3">
      <span className="text-sm font-semibold tabular-nums text-fg w-20">
        {fmtCents(row.amount_cents)}
      </span>
      <span
        className="text-xs text-fg-dim w-32 truncate"
        title={row.occurred_at ?? ""}
      >
        {fmtDateTime(row.occurred_at)}
      </span>
      <span
        className="text-xs text-fg-dim flex-1 min-w-0 truncate"
        title={row.description ?? ""}
      >
        {row.description || `fan ${row.fan_id ?? "?"} · ${row.account_id}`}
      </span>
      {isAssigned ? (
        <>
          <span className="text-xs text-ok truncate max-w-[10rem]">
            → {currentName}
          </span>
          <Button
            type="button"
            variant="ghost"
            size="sm"
            onClick={clear}
            disabled={mut.isPending}
            title="Clear the manual attribution (returns this tip to the orphan list)"
          >
            {mut.isPending ? "…" : "Clear"}
          </Button>
        </>
      ) : (
        <>
          <select
            className="text-xs bg-bg-elev-1 border border-border rounded px-2 py-1 text-fg"
            value={selected}
            onChange={(e) => setSelected(e.target.value)}
            disabled={mut.isPending}
          >
            <option value="">Assign to…</option>
            {choices.map((e) => (
              <option key={e.id} value={String(e.id)}>
                {e.display_name}
              </option>
            ))}
          </select>
          <Button
            type="button"
            variant="secondary"
            size="sm"
            onClick={assign}
            disabled={selected === "" || mut.isPending}
          >
            {mut.isPending ? "…" : "Assign"}
          </Button>
        </>
      )}
    </li>
  );
}
