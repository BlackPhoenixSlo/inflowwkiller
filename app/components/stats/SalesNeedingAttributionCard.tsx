"use client";

/**
 * SalesNeedingAttributionCard — manual attribution for confirmed sales that
 * land in the per-employee view's "Unattributed" bucket.
 *
 * Broader than the old orphan-tips card: it covers BOTH standalone tips AND
 * message-linked PPVs whose scraped message carries no sender (the
 * duplicate-price cases the auto-rule deliberately leaves ambiguous — e.g. a
 * fan blasted the same $200 PPV by three chatters). Each row shows the
 * CANDIDATE chatters (the distinct 1:1 people who actually messaged that fan
 * around the sale) as one-click chips, plus a full-roster fallback. Picking one
 * POSTs to /admin/ingest/transactions/{id}/attribute; the view's COALESCE makes
 * it win, both this list and the per-employee table refetch.
 *
 * "Show assigned" (default off) reveals previously-assigned sales to reassign
 * or clear. Scoped to the page's date range and the viewer's visible accounts.
 */

import { useMemo, useState } from "react";

import { Button, Card } from "@/components/ui/primitives";
import { useEmployee } from "@/contexts/EmployeeContext";
import { useAccounts } from "@/hooks/useAccounts";
import { cn } from "@/lib/utils";
import { fmtCents, fmtDateTime } from "@/lib/format";
import {
  useAttributeTip,
  useSalesNeedingAttribution,
  type SaleNeedingAttribution,
} from "@/hooks/useStats";

interface Props {
  from: string | null;
  to: string | null;
}

function kindLabel(kind: string): string {
  if (kind.startsWith("ppv")) return "PPV";
  if (kind.startsWith("tip")) return "Tip";
  return kind;
}

export default function SalesNeedingAttributionCard({ from, to }: Props) {
  const [showAssigned, setShowAssigned] = useState(false);
  const q = useSalesNeedingAttribution({ from, to, includeAssigned: showAssigned });

  // Only surface sales for models the current viewer can see (principal-aware).
  const accountsQ = useAccounts();
  const visibleIds = useMemo(
    () => new Set((accountsQ.data?.accounts ?? []).map((a) => a.id)),
    [accountsQ.data],
  );
  // Fail closed until we know the viewer's scope.
  const rows = useMemo(
    () => (q.data?.rows ?? []).filter((r) => visibleIds.has(r.account_id)),
    [q.data, visibleIds],
  );
  const openRows = useMemo(
    () => rows.filter((r) => r.attributed_employee_id == null),
    [rows],
  );
  const assignedRows = useMemo(
    () => rows.filter((r) => r.attributed_employee_id != null),
    [rows],
  );
  const openTotal = openRows.reduce((s, r) => s + r.amount_cents, 0);
  const assignedTotal = assignedRows.reduce((s, r) => s + r.amount_cents, 0);

  return (
    <Card className="p-4 space-y-3">
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h2 className="text-sm font-medium text-fg">
            Sales needing attribution
            {openRows.length > 0 && (
              <> · <span className="tabular-nums">{fmtCents(openTotal)}</span></>
            )}
          </h2>
          <p className="text-xs text-fg-dim">
            Confirmed sales with no chatter credited — pick who closed it. Chips
            are the fans&apos; recent 1:1 chatters; use the dropdown for anyone
            else. Scoped to the page&apos;s date range.
          </p>
        </div>
        <div className="flex items-center gap-3 text-xs text-fg-dim">
          <span className="tabular-nums">
            {openRows.length} open
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
          {(q.error as Error)?.message || "Failed to load sales needing attribution"}
        </div>
      )}

      {!q.isLoading && !accountsQ.isLoading && !q.isError && (
        <div
          className={cn(
            "transition-opacity",
            q.isFetching && q.isPlaceholderData && "opacity-60",
          )}
        >
          {openRows.length > 0 ? (
            <ul className="divide-y divide-border">
              {openRows.map((r) => <SaleRow key={r.id} row={r} />)}
            </ul>
          ) : (
            <div className="text-sm text-fg-dim py-1">
              No sales awaiting attribution in this window.
            </div>
          )}
          {showAssigned && assignedRows.length > 0 && (
            <div className="pt-3 border-t border-border space-y-2">
              <div className="text-[11px] uppercase tracking-wide text-fg-dim">
                Previously assigned
              </div>
              <ul className="divide-y divide-border">
                {assignedRows.map((r) => <SaleRow key={r.id} row={r} />)}
              </ul>
            </div>
          )}
        </div>
      )}
    </Card>
  );
}

function SaleRow({ row }: { row: SaleNeedingAttribution }) {
  const { roster } = useEmployee();
  const mut = useAttributeTip();
  const [selected, setSelected] = useState<string>("");

  const isAssigned = row.attributed_employee_id != null;
  const choices = roster.filter((e) => e.is_active);
  const currentName = isAssigned
    ? row.attributed_employee_name
      ?? roster.find((e) => e.id === row.attributed_employee_id)?.display_name
      ?? `Employee ${row.attributed_employee_id}`
    : null;

  const assignTo = (eid: number) =>
    mut.mutate({ txId: row.id, employeeId: eid }, { onSuccess: () => setSelected("") });
  const assignFromSelect = () => {
    if (selected === "") return;
    assignTo(Number(selected));
  };
  const clear = () => mut.mutate({ txId: row.id, employeeId: null });

  // Candidate chip ids we render as quick-picks — dedupe against nothing;
  // the backend already returns distinct senders, newest first.
  const candidates = row.candidate_chatters ?? [];

  return (
    <li className="py-2.5 flex flex-col gap-2 md:flex-row md:flex-wrap md:items-center md:gap-x-3">
      {/* `md:contents` dissolves this wrapper at ≥768px so the three meta
          spans stay direct flex items of the row — the desktop layout is
          the same three children in the same order. */}
      <div className="flex items-center gap-2 md:contents">
        <span className="text-sm font-semibold tabular-nums text-fg w-auto md:w-20">
          {fmtCents(row.amount_cents)}
        </span>
        <span className="text-[10px] uppercase tracking-wide text-fg-dim w-auto md:w-8">
          {kindLabel(row.kind)}
        </span>
        <span
          className="text-xs text-fg-dim w-auto md:w-28 truncate"
          title={row.occurred_at ?? ""}
        >
          {fmtDateTime(row.occurred_at)}
        </span>
      </div>
      <span
        className="text-xs text-fg-dim w-full md:w-auto md:flex-1 md:min-w-[8rem] truncate"
        title={row.description ?? ""}
      >
        {row.description || `fan ${row.fan_id ?? "?"} · ${row.account_id}`}
      </span>

      {isAssigned ? (
        <>
          <span className="text-xs text-ok truncate max-w-[10rem]">→ {currentName}</span>
          <Button
            type="button" variant="ghost" size="sm"
            onClick={clear} disabled={mut.isPending}
            title="Clear the manual attribution (returns this sale to the list)"
          >
            {mut.isPending ? "…" : "Clear"}
          </Button>
        </>
      ) : (
        <div className="flex flex-wrap items-center gap-1.5">
          {candidates.map((c) => (
            <button
              key={c.employee_id}
              type="button"
              onClick={() => assignTo(c.employee_id)}
              disabled={mut.isPending}
              title={c.last_at ? `last chatted ${fmtDateTime(c.last_at)}` : undefined}
              className="text-xs min-h-[40px] px-3 py-2 md:min-h-0 md:px-2 md:py-1 rounded-full border border-accent/40 bg-accent/10 text-accent hover:bg-accent/20 disabled:opacity-50"
            >
              {c.name || `Employee ${c.employee_id}`}
            </button>
          ))}
          <select
            className="text-xs min-h-[40px] px-3 py-2 md:min-h-0 md:px-2 md:py-1 bg-bg-elev-1 border border-border rounded text-fg"
            value={selected}
            onChange={(e) => setSelected(e.target.value)}
            disabled={mut.isPending}
          >
            <option value="">{candidates.length ? "Other…" : "Assign to…"}</option>
            {choices.map((e) => (
              <option key={e.id} value={String(e.id)}>{e.display_name}</option>
            ))}
          </select>
          {selected !== "" && (
            <Button
              type="button" variant="secondary" size="sm"
              onClick={assignFromSelect} disabled={mut.isPending}
            >
              {mut.isPending ? "…" : "Assign"}
            </Button>
          )}
        </div>
      )}
    </li>
  );
}
