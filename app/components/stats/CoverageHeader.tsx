"use client";

/**
 * CoverageHeader — top-of-page health number for attribution coverage.
 *
 * Traffic-light tiers per the plan:
 *   green  ≥ 95 %
 *   yellow 80–95 %
 *   red    < 80 %
 *
 * Click to expand a per-account breakdown. Outbound counts under the
 * window are shown in muted text below the big number so the user knows
 * whether the % is on a meaningful sample or near-zero traffic.
 */

import { useState } from "react";

import { Card } from "@/components/ui/primitives";
import { cn } from "@/lib/utils";
import { useAttributionCoverage, type CoverageAccountRow } from "@/hooks/useStats";

interface Props {
  from: string | null;
  to: string | null;
}

function tierColor(pct: number): { text: string; bg: string; label: string } {
  if (pct >= 95) return { text: "text-ok", bg: "bg-ok/15", label: "Healthy" };
  if (pct >= 80) return { text: "text-warn", bg: "bg-warn/15", label: "Watching" };
  return { text: "text-err", bg: "bg-err/15", label: "Action needed" };
}

export default function CoverageHeader({ from, to }: Props) {
  const [expanded, setExpanded] = useState(false);
  const q = useAttributionCoverage(from, to);

  const totalOutbound = q.data?.totals.outbound_total ?? 0;
  const pct = q.data?.totals.coverage_pct ?? 0;
  const tier = tierColor(pct);
  const accounts = q.data?.per_account ?? [];

  return (
    <Card className="p-0 overflow-hidden">
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        className="w-full text-left p-5 flex items-center gap-5 hover:bg-bg-elev-1/40 transition-colors"
        aria-expanded={expanded}
      >
        <div className={cn("w-16 h-16 rounded-2xl grid place-items-center", q.isPending ? "bg-bg-elev-1" : tier.bg)}>
          <span className={cn("text-2xl font-semibold tabular-nums", q.isPending ? "text-fg-dim" : tier.text)}>
            {q.isPending ? "—" : `${pct.toFixed(1)}%`}
          </span>
        </div>
        <div className="flex-1 min-w-0">
          <div className="text-sm font-medium text-fg">
            Attribution coverage
            {!q.isPending && (
              <span className={cn("ml-2 text-[11px] uppercase tracking-wide", tier.text)}>
                {tier.label}
              </span>
            )}
          </div>
          <div className="text-xs text-fg-dim mt-0.5">
            {q.isError ? (
              <span className="text-err">{(q.error as Error)?.message || "Failed to load"}</span>
            ) : q.isPending ? (
              <span className="text-fg-dim">click for per-account breakdown</span>
            ) : (
              <>
                {totalOutbound.toLocaleString()} outbound message{totalOutbound === 1 ? "" : "s"} in window
                {" · "}
                <span className="text-fg-dim">click for per-account breakdown</span>
              </>
            )}
          </div>
        </div>
        <div className="text-fg-dim text-sm select-none">{expanded ? "▴" : "▾"}</div>
      </button>

      {expanded && (
        <div className="border-t border-border">
          {q.isLoading ? (
            <div className="p-4 text-sm text-fg-dim">Loading per-account coverage…</div>
          ) : accounts.length === 0 ? (
            <div className="p-4 text-sm text-fg-dim">No outbound traffic in this window.</div>
          ) : (
            <div className="overflow-x-auto">
            <table
              className={cn(
                "w-full min-w-[560px] text-sm transition-opacity",
                q.isFetching && q.isPlaceholderData && "opacity-60",
              )}
            >
              <thead>
                <tr className="text-[11px] uppercase tracking-wide text-fg-dim border-b border-border">
                  <th className="text-left px-4 py-2 font-medium">Account</th>
                  <th className="text-right px-4 py-2 font-medium">Outbound</th>
                  <th className="text-right px-4 py-2 font-medium">Human</th>
                  <th className="text-right px-4 py-2 font-medium">Automation</th>
                  <th className="text-right px-4 py-2 font-medium">Unattributed</th>
                  <th className="text-right px-4 py-2 font-medium">Coverage</th>
                </tr>
              </thead>
              <tbody>
                {accounts.map((r) => <AccountRow key={r.account_id} row={r} />)}
              </tbody>
            </table>
            </div>
          )}
        </div>
      )}
    </Card>
  );
}

function AccountRow({ row }: { row: CoverageAccountRow }) {
  const tier = tierColor(row.coverage_pct);
  return (
    <tr className="border-b border-border last:border-0">
      <td className="px-4 py-2 text-xs">
        {row.display_name ? (
          <>
            <span className="text-fg">@{row.display_name}</span>
            <span className="ml-1 text-[10px] text-muted font-mono">{row.account_id}</span>
          </>
        ) : (
          <span className="text-fg-dim font-mono">{row.account_id}</span>
        )}
      </td>
      <td className="px-4 py-2 text-right tabular-nums">{row.outbound_total.toLocaleString()}</td>
      <td className="px-4 py-2 text-right tabular-nums">{row.attributed_to_human.toLocaleString()}</td>
      <td className="px-4 py-2 text-right tabular-nums">{row.attributed_to_automation.toLocaleString()}</td>
      <td className="px-4 py-2 text-right tabular-nums">{row.unattributed_count.toLocaleString()}</td>
      <td className={cn("px-4 py-2 text-right tabular-nums font-medium", tier.text)}>
        {row.coverage_pct.toFixed(1)}%
      </td>
    </tr>
  );
}
