"use client";

/**
 * PerEmployeeTable — sortable rollup of Employee × (optional) Account.
 *
 * Columns: Employee | Messages | PPVs | PPV Rev | Tip Rev | Total
 * Default sort: revenue DESC (matches the backend's own ordering).
 *
 * `by_account` toggles between two views:
 *   • on  (default) — expand each employee row to show per-account
 *                     subtotals from `per_account[]`
 *   • off            — show only the totals-per-employee row
 *
 * The synthetic "Unattributed" employee (employee_id=null) is rendered
 * with a muted highlight so orphan revenue (unlinked PPVs + tips) stays visible —
 * silently dropping it would make the totals not match per-model.
 */

import { Fragment, useMemo, useState } from "react";

import { Card } from "@/components/ui/primitives";
import { cn } from "@/lib/utils";
import { fmtCentsBlankZero } from "@/lib/format";
import { usePerEmployee, type PerEmployeeRow, type PerEmployeeAccountRow } from "@/hooks/useStats";

type SortKey = "display_name" | "messages_sent" | "ppv_conversions" | "revenue_cents";
type SortDir = "asc" | "desc";

interface Props {
  from: string | null;
  to: string | null;
}

// Since view rev 0042 the backend sends a real {ppv,tip} split for the
// "Unattributed" bucket too (orphan PPVs + orphan tips), so every row's
// kind cells render the same way. Note PPV+Tip can differ from Total by
// `custom`-kind cents (rare) — Total is the authoritative column.
function fmtKindCents(c: number): string {
  return fmtCentsBlankZero(c);
}

export default function PerEmployeeTable({ from, to }: Props) {
  const [byAccount, setByAccount] = useState(true);
  const [hideZeroRev, setHideZeroRev] = useState(true);
  const [sortKey, setSortKey] = useState<SortKey>("revenue_cents");
  const [sortDir, setSortDir] = useState<SortDir>("desc");
  const [expanded, setExpanded] = useState<Set<string>>(new Set());

  const q = usePerEmployee(from, to, byAccount);

  const sorted = useMemo<PerEmployeeRow[]>(() => {
    const rows = q.data?.employees ? [...q.data.employees] : [];
    rows.sort((a, b) => {
      const av: string | number = (a[sortKey] ?? (sortKey === "display_name" ? "" : 0)) as string | number;
      const bv: string | number = (b[sortKey] ?? (sortKey === "display_name" ? "" : 0)) as string | number;
      let cmp: number;
      if (typeof av === "string" || typeof bv === "string") {
        cmp = String(av).localeCompare(String(bv));
      } else {
        cmp = (av as number) - (bv as number);
      }
      return sortDir === "asc" ? cmp : -cmp;
    });
    return rows;
  }, [q.data, sortKey, sortDir]);

  // Default-on filter: chatters with zero revenue in the window crowd the
  // list and bury the people who actually earned. Footer count + "hidden"
  // pill keeps the omission discoverable.
  const visible = useMemo<PerEmployeeRow[]>(
    () => (hideZeroRev ? sorted.filter((r) => r.revenue_cents > 0) : sorted),
    [sorted, hideZeroRev],
  );
  const hiddenCount = sorted.length - visible.length;

  const toggleSort = (key: SortKey) => {
    if (key === sortKey) {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    } else {
      setSortKey(key);
      setSortDir(key === "display_name" ? "asc" : "desc");
    }
  };

  const empKey = (r: PerEmployeeRow) =>
    r.employee_id == null ? "__unattributed__" : `emp:${r.employee_id}`;

  const toggleExpand = (k: string) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(k)) next.delete(k); else next.add(k);
      return next;
    });
  };

  return (
    <Card className="p-0 overflow-hidden">
      <header className="flex items-center justify-between p-4 border-b border-border gap-4">
        <div>
          <h2 className="text-sm font-medium text-fg">Per-employee revenue</h2>
          <p className="text-xs text-fg-dim mt-0.5">
            Includes standalone tips attributed via 7-day lookback.
            {hideZeroRev && hiddenCount > 0 && (
              <span className="ml-1 text-fg-dim/80">
                · {hiddenCount} zero-revenue {hiddenCount === 1 ? "row" : "rows"} hidden
              </span>
            )}
          </p>
        </div>
        <div className="flex items-center gap-4 shrink-0">
          <label className="flex items-center gap-2 text-xs text-fg-dim">
            <input
              type="checkbox"
              checked={hideZeroRev}
              onChange={(e) => setHideZeroRev(e.target.checked)}
              className="accent-accent"
            />
            Hide $0 rows
          </label>
          <label className="flex items-center gap-2 text-xs text-fg-dim">
            <input
              type="checkbox"
              checked={!byAccount}
              onChange={(e) => setByAccount(!e.target.checked)}
              className="accent-accent"
            />
            Show totals only
          </label>
        </div>
      </header>

      {q.isError ? (
        <div className="p-4 text-sm text-err">
          {(q.error as Error)?.message || "Failed to load per-employee stats"}
        </div>
      ) : !q.isPending && visible.length === 0 ? (
        <div className="p-6 text-sm text-fg-dim text-center">
          {sorted.length === 0
            ? "No activity in this window."
            : "No employees with revenue in this window."}
          {sorted.length > 0 && hideZeroRev && (
            <button
              type="button"
              onClick={() => setHideZeroRev(false)}
              className="ml-2 text-accent hover:underline"
            >
              show {sorted.length} zero-revenue {sorted.length === 1 ? "row" : "rows"}
            </button>
          )}
        </div>
      ) : (
        <table
          className={cn(
            "w-full text-sm transition-opacity",
            q.isFetching && q.isPlaceholderData && "opacity-60",
          )}
        >
          <thead>
            <tr className="text-[11px] uppercase tracking-wide text-fg-dim border-b border-border">
              <HeaderCell label="Employee" k="display_name" curr={sortKey} dir={sortDir} onClick={toggleSort} align="left" />
              <HeaderCell label="Messages" k="messages_sent" curr={sortKey} dir={sortDir} onClick={toggleSort} align="right" />
              <HeaderCell label="PPVs"     k="ppv_conversions" curr={sortKey} dir={sortDir} onClick={toggleSort} align="right" />
              <th className="px-4 py-2 font-medium text-right select-none">PPV Rev</th>
              <th className="px-4 py-2 font-medium text-right select-none">Tip Rev</th>
              <HeaderCell label="Total"    k="revenue_cents" curr={sortKey} dir={sortDir} onClick={toggleSort} align="right" />
            </tr>
          </thead>
          <tbody>
            {q.isPending && Array.from({ length: 5 }).map((_, i) => (
              <tr key={`skeleton:${i}`} className="border-b border-border last:border-0">
                <td className="px-4 py-2.5 text-fg-dim">—</td>
                <td className="px-4 py-2.5 text-right tabular-nums text-fg-dim">—</td>
                <td className="px-4 py-2.5 text-right tabular-nums text-fg-dim">—</td>
                <td className="px-4 py-2.5 text-right tabular-nums text-fg-dim">—</td>
                <td className="px-4 py-2.5 text-right tabular-nums text-fg-dim">—</td>
                <td className="px-4 py-2.5 text-right tabular-nums text-fg-dim">—</td>
              </tr>
            ))}
            {visible.map((r) => {
              const key = empKey(r);
              const isOrphan = r.employee_id == null;
              const isOpen = byAccount && expanded.has(key);
              const sub = r.per_account ?? [];
              const expandable = byAccount && sub.length > 0;

              return (
                <Fragment key={key}>
                  <tr
                    onClick={expandable ? () => toggleExpand(key) : undefined}
                    className={cn(
                      "border-b border-border last:border-0",
                      isOrphan && "bg-warn/5",
                      expandable && "cursor-pointer hover:bg-bg-elev-1/40",
                    )}
                  >
                    <td className="px-4 py-2.5">
                      <div className="flex items-center gap-2">
                        {expandable && (
                          <span className="text-fg-dim text-xs w-3 inline-block">
                            {isOpen ? "▾" : "▸"}
                          </span>
                        )}
                        <span className={cn("font-medium", isOrphan ? "text-warn" : "text-fg")}>
                          {r.display_name ?? `Employee #${r.employee_id ?? "?"}`}
                        </span>
                        {isOrphan && (
                          <span className="text-[10px] uppercase tracking-wide text-warn/80 ml-1">
                            unattributed
                          </span>
                        )}
                      </div>
                    </td>
                    <td className="px-4 py-2.5 text-right tabular-nums">
                      {r.messages_sent.toLocaleString()}
                    </td>
                    <td className="px-4 py-2.5 text-right tabular-nums">
                      {r.ppv_conversions.toLocaleString()}
                    </td>
                    <td className="px-4 py-2.5 text-right tabular-nums">
                      {fmtKindCents(r.revenue_by_kind.ppv)}
                    </td>
                    <td
                      className={cn(
                        "px-4 py-2.5 text-right tabular-nums",
                        !isOrphan && r.revenue_by_kind.tip > 0 && "text-ok font-medium",
                      )}
                    >
                      {fmtKindCents(r.revenue_by_kind.tip)}
                    </td>
                    <td className="px-4 py-2.5 text-right tabular-nums font-medium">
                      {fmtCentsBlankZero(r.revenue_cents)}
                    </td>
                  </tr>
                  {isOpen && sub.map((s, i) => (
                    <SubRow key={`${key}:${s.account_id ?? "null"}:${i}`} sub={s} orphanParent={isOrphan} />
                  ))}
                </Fragment>
              );
            })}
          </tbody>
          {visible.length >= 1 && (
            <tfoot>
              <tr className="border-t border-border bg-bg-elev-1/30">
                <td className="px-4 py-2.5 text-[11px] uppercase tracking-wide text-fg-dim">
                  Total ({visible.length})
                </td>
                <td className="px-4 py-2.5 text-right tabular-nums font-medium">
                  {visible.reduce((s, r) => s + r.messages_sent, 0).toLocaleString()}
                </td>
                <td className="px-4 py-2.5 text-right tabular-nums font-medium">
                  {visible.reduce((s, r) => s + r.ppv_conversions, 0).toLocaleString()}
                </td>
                <td className="px-4 py-2.5 text-right tabular-nums font-medium">
                  {fmtCentsBlankZero(visible.reduce((s, r) => s + r.revenue_by_kind.ppv, 0))}
                </td>
                <td className="px-4 py-2.5 text-right tabular-nums font-medium">
                  {/* Every row (Unattributed included) carries a real by-kind
                      split since view rev 0042 — fold uniformly. PPV+Tip may
                      trail Total by `custom`-kind cents (rare). */}
                  {fmtCentsBlankZero(visible.reduce((s, r) => s + r.revenue_by_kind.tip, 0))}
                </td>
                <td className="px-4 py-2.5 text-right tabular-nums font-semibold">
                  {fmtCentsBlankZero(visible.reduce((s, r) => s + r.revenue_cents, 0))}
                </td>
              </tr>
            </tfoot>
          )}
        </table>
      )}
    </Card>
  );
}

function HeaderCell({
  label, k, curr, dir, onClick, align,
}: {
  label: string;
  k: SortKey;
  curr: SortKey;
  dir: SortDir;
  onClick: (k: SortKey) => void;
  align: "left" | "right";
}) {
  const active = k === curr;
  return (
    <th
      onClick={() => onClick(k)}
      className={cn(
        "px-4 py-2 font-medium cursor-pointer select-none",
        align === "right" ? "text-right" : "text-left",
        active && "text-fg",
      )}
    >
      <span className="inline-flex items-center gap-1">
        {label}
        {active && <span aria-hidden>{dir === "asc" ? "↑" : "↓"}</span>}
      </span>
    </th>
  );
}

function SubRow({ sub, orphanParent }: { sub: PerEmployeeAccountRow; orphanParent: boolean }) {
  return (
    <tr className="border-b border-border last:border-0 bg-bg-elev-1/30">
      <td className="px-4 py-1.5 pl-10 text-xs text-fg-dim">
        {sub.account_nickname ?? <span className="font-mono">{sub.account_id ?? "—"}</span>}
      </td>
      <td className="px-4 py-1.5 text-right tabular-nums text-xs text-fg-dim">
        {sub.messages_sent.toLocaleString()}
      </td>
      <td className="px-4 py-1.5 text-right tabular-nums text-xs text-fg-dim">
        {sub.ppv_conversions.toLocaleString()}
      </td>
      <td className="px-4 py-1.5 text-right tabular-nums text-xs text-fg-dim">
        {fmtKindCents(sub.revenue_by_kind.ppv)}
      </td>
      <td
        className={cn(
          "px-4 py-1.5 text-right tabular-nums text-xs",
          !orphanParent && sub.revenue_by_kind.tip > 0 ? "text-ok" : "text-fg-dim",
        )}
      >
        {fmtKindCents(sub.revenue_by_kind.tip)}
      </td>
      <td className="px-4 py-1.5 text-right tabular-nums text-xs text-fg-dim">
        {fmtCentsBlankZero(sub.revenue_cents)}
      </td>
    </tr>
  );
}
