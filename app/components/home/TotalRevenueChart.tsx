"use client";

/**
 * TotalRevenueChart — home-page summary of cleared+pending revenue summed
 * across every model in the account roster, bucketed by day.
 *
 * Reads /admin/stats/revenue?group_by=day (no account_id filter ≡ all
 * models). Renders a compact SVG bar chart so we don't pull in a chart
 * library — same approach the rest of the stats surface takes.
 *
 * The chart is non-blocking: the home page renders immediately with a
 * skeleton, the chart fades in once data arrives. Range presets 7d/30d/90d
 * are local state — picking a new range refetches in the background while
 * the previous data stays visible (keepPreviousData).
 */

import { useMemo, useState } from "react";
import { keepPreviousData, useQuery } from "@tanstack/react-query";

import { Card } from "@/components/ui/primitives";
import { cn } from "@/lib/utils";
import { fmtCents, fmtDateShort } from "@/lib/format";
import { relay, type RelayContext } from "@/lib/relay";

const BG_CTX: RelayContext = { priority: "background" };

type RangeKey = "7d" | "30d" | "90d";
const RANGES: { key: RangeKey; label: string; days: number }[] = [
  { key: "7d",  label: "7d",  days: 7  },
  { key: "30d", label: "30d", days: 30 },
  { key: "90d", label: "90d", days: 90 },
];

interface RevenueRow { day: string | null; kind: string | null; total_cents: number; count: number }
interface RevenueResp { rows: RevenueRow[] }

function daysAgoIso(n: number): string {
  const d = new Date();
  d.setDate(d.getDate() - n);
  const local = new Date(d.getFullYear(), d.getMonth(), d.getDate(), 0, 0, 0, 0);
  return local.toISOString();
}
function endOfTodayIso(): string {
  const d = new Date();
  const local = new Date(d.getFullYear(), d.getMonth(), d.getDate(), 23, 59, 59, 999);
  return local.toISOString();
}

/** Pad a sparse day series to one row per day in the window so the chart
 *  shows zero-revenue days as gaps instead of squashing the timeline. */
function fillDays(rows: RevenueRow[], days: number): { day: string; cents: number }[] {
  const byDay = new Map<string, number>();
  for (const r of rows) if (r.day) byDay.set(r.day, r.total_cents);
  const out: { day: string; cents: number }[] = [];
  const now = new Date();
  for (let i = days - 1; i >= 0; i--) {
    const d = new Date(now.getFullYear(), now.getMonth(), now.getDate() - i);
    const y = d.getFullYear();
    const m = String(d.getMonth() + 1).padStart(2, "0");
    const dd = String(d.getDate()).padStart(2, "0");
    const key = `${y}-${m}-${dd}`;
    out.push({ day: key, cents: byDay.get(key) ?? 0 });
  }
  return out;
}

export default function TotalRevenueChart() {
  const [range, setRange] = useState<RangeKey>("30d");
  const cfg = RANGES.find((r) => r.key === range)!;
  const from = useMemo(() => daysAgoIso(cfg.days - 1), [cfg.days]);
  const to = useMemo(() => endOfTodayIso(), []);

  const q = useQuery<RevenueResp>({
    queryKey: ["stats", "revenue-total", range],
    queryFn: () => {
      const qs = new URLSearchParams({ group_by: "day", from, to });
      return relay.get<RevenueResp>(`/admin/stats/revenue?${qs.toString()}`, BG_CTX);
    },
    staleTime: 30_000,
    refetchOnWindowFocus: true,
    placeholderData: keepPreviousData,
  });

  const series = useMemo(() => fillDays(q.data?.rows ?? [], cfg.days), [q.data?.rows, cfg.days]);
  const totalCents = useMemo(() => series.reduce((a, b) => a + b.cents, 0), [series]);
  const peakCents = useMemo(() => series.reduce((m, b) => Math.max(m, b.cents), 0), [series]);

  return (
    <Card className="p-6">
      <div className="flex items-start justify-between mb-4 gap-4">
        <div>
          <div className="text-xs uppercase tracking-wider text-muted mb-1">
            Total revenue · all models · {cfg.label}
          </div>
          <div className="text-3xl font-semibold tabular-nums">
            {q.isLoading && !q.data ? <span className="text-fg-dim">…</span> : fmtCents(totalCents)}
          </div>
        </div>
        <div className="flex items-center gap-1 bg-bg-elev-1 border border-border rounded-lg p-0.5">
          {RANGES.map((r) => (
            <button
              key={r.key}
              type="button"
              onClick={() => setRange(r.key)}
              className={cn(
                "px-2.5 py-1 text-xs rounded-md transition-colors",
                range === r.key
                  ? "bg-bg-elev-2 text-fg"
                  : "text-fg-dim hover:text-fg",
              )}
            >
              {r.label}
            </button>
          ))}
        </div>
      </div>

      {q.isError ? (
        <div className="text-sm text-err">
          {(q.error as Error)?.message || "Failed to load revenue"}
        </div>
      ) : (
        <RevenueBars series={series} peak={peakCents} loading={q.isLoading && !q.data} />
      )}
    </Card>
  );
}

interface BarsProps {
  series: { day: string; cents: number }[];
  peak: number;
  loading: boolean;
}

function RevenueBars({ series, peak, loading }: BarsProps) {
  // Render-time geometry. Width is intrinsic via viewBox + preserveAspectRatio
  // so the SVG scales fluidly. The first/last labels render below the axis.
  const W = 600;
  const H = 140;
  const padX = 4;
  const padY = 8;
  const innerW = W - padX * 2;
  const innerH = H - padY * 2;
  const slotW = series.length > 0 ? innerW / series.length : innerW;
  const barW = Math.max(2, slotW * 0.7);

  return (
    <div className="relative">
      <svg
        viewBox={`0 0 ${W} ${H}`}
        preserveAspectRatio="none"
        className="w-full h-32"
        role="img"
        aria-label="Daily revenue across all models"
      >
        {series.map((d, i) => {
          const h = peak > 0 ? (d.cents / peak) * innerH : 0;
          const x = padX + i * slotW + (slotW - barW) / 2;
          const y = padY + (innerH - h);
          const isZero = d.cents === 0;
          return (
            <g key={d.day}>
              <rect
                x={x}
                y={y}
                width={barW}
                height={Math.max(h, isZero ? 1 : 1)}
                rx={1}
                className={isZero ? "fill-border" : "fill-accent"}
              >
                <title>{`${fmtDateShort(d.day)} — ${fmtCents(d.cents)}`}</title>
              </rect>
            </g>
          );
        })}
      </svg>
      {loading && (
        <div className="absolute inset-0 flex items-center justify-center text-xs text-fg-dim">
          loading…
        </div>
      )}
      <div className="flex justify-between text-[10px] text-fg-dim mt-1 px-1">
        <span>{series.length > 0 ? fmtDateShort(series[0].day) : ""}</span>
        <span>{series.length > 0 ? fmtDateShort(series[series.length - 1].day) : ""}</span>
      </div>
    </div>
  );
}
