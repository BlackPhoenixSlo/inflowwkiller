"use client";

/**
 * /stats — read-only ops dashboard.
 *
 * Sections (in render order, revenue-first):
 *   1. Per-employee revenue table (sortable, per-account drill-down)
 *   2. Per-model KPI grid (revenue, subs, LTV, ARPU)
 *   3. Orphan-tips triage
 *   4. Unattributed-recent drawer (lazy)
 *   5. Attribution-coverage health header (click for per-account)
 *   6. Ingest health banner (click for per-account scan state)
 *
 * Date range is owned at the page level and threaded into every hook so
 * the sections stay in sync. Defaults to the last 30 days — the per-model
 * endpoint's own default — so the cards aren't empty on first load.
 */

import { useState } from "react";
import Link from "next/link";
import { useIsFetching } from "@tanstack/react-query";

import AutomationRunsCard from "@/components/stats/AutomationRunsCard";
import CoverageHeader from "@/components/stats/CoverageHeader";
import DateRangePicker, { type RangePreset } from "@/components/stats/DateRangePicker";
import IngestHealthBanner from "@/components/stats/IngestHealthBanner";
import OrphanTipsCard from "@/components/stats/OrphanTipsCard";
import PerAutomationTable from "@/components/stats/PerAutomationTable";
import PerEmployeeTable from "@/components/stats/PerEmployeeTable";
import PerModelKpiGrid from "@/components/stats/PerModelKpiGrid";
import UnattributedDrawer from "@/components/stats/UnattributedDrawer";
import UserDataTab from "@/components/stats/UserDataTab";
import { Button } from "@/components/ui/primitives";
import { cn } from "@/lib/utils";
import { fmtTimeOnly } from "@/lib/format";
import { useIngestHealth, useStatsRefresh } from "@/hooks/useStats";

function daysAgoISO(n: number): string {
  const d = new Date();
  d.setDate(d.getDate() - n);
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const dd = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${dd}`;
}
function todayISO(): string {
  const d = new Date();
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const dd = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${dd}`;
}

// Convert a bare YYYY-MM-DD (picker output) into a full UTC-instant ISO
// string with millisecond precision. `end=true` picks 23:59:59.999 local.
// Backend `_parse_date` (stats.py) converts the tzinfo back to UTC; this
// keeps a Pacific user's "today" boundary aligned with their wall clock.
// Anything that already has a time component is passed through.
function toLocalIso(date: string | null, end: boolean): string | null {
  if (!date) return null;
  if (date.includes("T")) return date;
  const parts = date.split("-").map(Number);
  if (parts.length !== 3 || parts.some((n) => !Number.isFinite(n))) return date;
  const [y, m, d] = parts;
  const local = end
    ? new Date(y, m - 1, d, 23, 59, 59, 999)
    : new Date(y, m - 1, d, 0, 0, 0, 0);
  return local.toISOString();
}

export default function StatsPage() {
  const [tab, setTab] = useState<"overview" | "user-data">("overview");
  const [from, setFrom] = useState<string | null>(daysAgoISO(30));
  const [to, setTo] = useState<string | null>(todayISO());
  const [preset, setPreset] = useState<RangePreset>("30d");
  const refresh = useStatsRefresh();
  // Latest ingest tick — used to caption the page with a "data through"
  // timestamp. Pending revenue is now baked into headline totals, so the
  // user needs visible signal of when the numbers were last refreshed.
  const health = useIngestHealth();
  const lastIngest = (health.data?.accounts ?? [])
    .map((a) => a.last_scan_at)
    .filter((s): s is string => !!s)
    .sort()
    .pop() ?? null;
  // Any in-flight stats query → show the refresh button as busy. Picks
  // up active-subs, per-model, per-employee, coverage, ingest-health.
  const fetching = useIsFetching({ queryKey: ["stats"] }) > 0;

  const handleRange = (f: string | null, t: string | null, p: RangePreset) => {
    setFrom(f);
    setTo(t);
    setPreset(p);
  };

  // Tz-aware boundaries — see toLocalIso() above. Passing UTC-instant ISO
  // strings (not bare YYYY-MM-DD) means a Pacific user's "today" tip is
  // visible in the "today" window instead of slipping to tomorrow in UTC.
  const fromIso = toLocalIso(from, false);
  const toIso = toLocalIso(to, true);

  return (
    <div className="max-w-7xl mx-auto p-3 sm:p-6 space-y-5">
      <header className="flex flex-wrap items-end gap-4 justify-between">
        <div>
          <h1 className="text-2xl font-semibold mb-1">Stats</h1>
          <p className="text-sm text-fg-dim">
            Per-employee + per-model performance over the selected window.
            {lastIngest && (
              <span className="ml-2 text-fg-dim/80">
                · data through {fmtTimeOnly(lastIngest)}
              </span>
            )}
          </p>
        </div>
        <div className="flex items-center gap-3">
          <Link
            href="/messages?tab=all"
            className="text-sm text-accent hover:underline whitespace-nowrap"
            title="Drill into individual messages — filter by fan, direction, type. Export CSV."
          >
            All messages…
          </Link>
          {tab === "overview" && (
            <DateRangePicker from={from} to={to} preset={preset} onChange={handleRange} />
          )}
          <Button
            type="button"
            variant="secondary"
            size="sm"
            onClick={() => refresh()}
            disabled={fetching}
            title={fetching ? "Refreshing…" : "Re-fetch all stats now"}
          >
            <span className={fetching ? "inline-block animate-spin" : "inline-block"}>⟳</span>
            <span className="ml-1">{fetching ? "Refreshing…" : "Refresh"}</span>
          </Button>
        </div>
      </header>

      <nav className="flex items-center gap-1 border-b border-border">
        {([
          { key: "overview", label: "Overview" },
          { key: "user-data", label: "User data" },
        ] as const).map((t) => (
          <button
            key={t.key}
            type="button"
            onClick={() => setTab(t.key)}
            className={cn(
              "px-3 py-1.5 text-sm rounded-t-md border-b-2 -mb-px transition-colors",
              tab === t.key
                ? "border-accent text-fg font-medium"
                : "border-transparent text-fg-dim hover:text-fg",
            )}
          >
            {t.label}
          </button>
        ))}
      </nav>

      {tab === "overview" ? (
        <>
          <PerEmployeeTable from={fromIso} to={toIso} />
          <PerAutomationTable from={fromIso} to={toIso} />
          <PerModelKpiGrid from={fromIso} to={toIso} />
          <OrphanTipsCard from={fromIso} to={toIso} />
          <UnattributedDrawer />
          <AutomationRunsCard />
          <CoverageHeader from={fromIso} to={toIso} />
          <IngestHealthBanner />
        </>
      ) : (
        <UserDataTab />
      )}
    </div>
  );
}
