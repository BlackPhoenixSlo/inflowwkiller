"use client";

/**
 * Pixel-style recreation of a competitor's "Creator Reports" dashboard,
 * pinned to the bottom of the home page so anyone who wants to can
 * screenshot it as a joke. Interactive controls:
 *   • date range picker  — defaults to the last 30 days
 *   • period dropdown    — day / week / month (client-side bucketing)
 *   • net/gross dropdown — net = gross × 0.8 (OF's 20% cut)
 *   • filters dropdown   — multi-select models (all selected by default)
 *
 * Forces a light palette regardless of the app's dark/light mode so the
 * screenshot always looks like the original.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { keepPreviousData, useQueries, useQuery } from "@tanstack/react-query";

import PerModelKpiGrid from "@/components/stats/PerModelKpiGrid";
import { usePerModel, type PerModelRow } from "@/hooks/useStats";
import { relay, type RelayContext } from "@/lib/relay";

const BG_CTX: RelayContext = { priority: "background" };
const NET_RATIO = 0.8;

interface RevRow { day: string | null; kind: string | null; total_cents: number; count: number }
interface RevResp { rows: RevRow[] }

type Period = "day" | "week" | "month";
type Mode = "net" | "gross";
type Tab = "overview" | "creators";

/* Creator-table columns. Money columns format as USD; counts as integers.
 * Columns in `DEFAULT_COLS` match the screenshot; the rest are opt-in
 * "custom metrics" the user can toggle on. */
type TableCol =
  | "subscription"   // new sub + rebill combined
  | "rebill"         // rebill only (labelled "Recurring")
  | "new_sub"        // new subscription cents only
  | "tips"
  | "message"        // ppv
  | "pending"        // pending revenue (loading status)
  | "total"
  | "active_fans"
  | "new_subs_count"
  | "messages_sent"
  | "ppv_conversions"
  | "ltv"
  | "arpu"
  | "expired";

const ALL_COLS: TableCol[] = [
  "subscription", "rebill", "new_sub", "tips", "message", "pending",
  "total", "active_fans", "new_subs_count", "messages_sent", "ppv_conversions",
  "ltv", "arpu", "expired",
];
const DEFAULT_COLS: TableCol[] = ["subscription", "tips", "message", "total", "active_fans", "expired"];
const COL_LABEL: Record<TableCol, string> = {
  subscription:    "Subscription",
  rebill:          "Recurring",
  new_sub:         "New Sub",
  tips:            "Tips",
  message:         "Message",
  pending:         "Pending Rev",
  total:           "Total earnings",
  active_fans:     "Total active fans",
  new_subs_count:  "New subs (count)",
  messages_sent:   "Messages sent",
  ppv_conversions: "PPV conversions",
  ltv:             "LTV",
  arpu:            "ARPU",
  expired:         "Expired",
};
const COL_IS_MONEY: Record<TableCol, boolean> = {
  subscription: true, rebill: true, new_sub: true, tips: true, message: true,
  pending: true, total: true, ltv: true, arpu: true,
  active_fans: false, new_subs_count: false, messages_sent: false,
  ppv_conversions: false, expired: false,
};

function isoDay(d: Date): string {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const dd = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${dd}`;
}
function startOfDay(d: Date): Date { return new Date(d.getFullYear(), d.getMonth(), d.getDate(), 0, 0, 0, 0); }
function endOfDay(d: Date):   Date { return new Date(d.getFullYear(), d.getMonth(), d.getDate(), 23, 59, 59, 999); }
function addDays(d: Date, n: number): Date { const x = new Date(d); x.setDate(x.getDate() + n); return x; }
function daysBetween(a: Date, b: Date): number {
  const ms = endOfDay(b).getTime() - startOfDay(a).getTime();
  return Math.round(ms / 86_400_000);
}

function fmtUsd(cents: number): string {
  return (cents / 100).toLocaleString("en-US", { style: "currency", currency: "USD" });
}
function fmtUsdShort(cents: number): string {
  const d = cents / 100;
  if (Math.abs(d) >= 1000) return `$${d.toLocaleString("en-US", { maximumFractionDigits: 0 })}`;
  return `$${d.toFixed(2)}`;
}
function fmtRangeLabel(d: Date): string {
  const m = d.toLocaleString("en-US", { month: "short" });
  const day = String(d.getDate()).padStart(2, "0");
  return `${m}. ${day}, ${d.getFullYear()}`;
}
function fmtBucketLabel(d: Date, p: Period): string {
  if (p === "month") return d.toLocaleString("en-US", { month: "short", year: "2-digit" });
  if (p === "week")  return `${d.toLocaleString("en-US", { month: "short" })} ${d.getDate()}`;
  return d.toLocaleString("en-US", { month: "short", day: "numeric" });
}

/* ── kind bucketing ── */

type Bucket = "subscriptions" | "tips" | "posts" | "messages" | "streams" | "referrals" | "other";

export function bucketOf(kind: string | null): Bucket {
  if (!kind) return "other";
  if (kind === "subscription" || kind === "rebill") return "subscriptions";
  // Paid feed-post sales land as `ppv_post` (and post-tips as `tip_post`);
  // both are "Posts" in OF's vocab. Must precede the generic ppv→messages
  // catch-all below, or `ppv_post` gets swallowed into Messages.
  if (kind === "tip_post" || kind === "ppv_post") return "posts";
  if (kind === "tip_stream" || kind === "ppv_stream") return "streams";
  if (kind === "tip") return "tips";
  if (kind.startsWith("ppv")) return "messages"; // ppv (WS pump) + ppv_message
  return "other";
}

/* ── outside-click hook ── */

function useClickOutside<T extends HTMLElement>(onClose: () => void) {
  const ref = useRef<T | null>(null);
  useEffect(() => {
    function onDown(e: MouseEvent) {
      if (!ref.current) return;
      if (!ref.current.contains(e.target as Node)) onClose();
    }
    function onKey(e: KeyboardEvent) { if (e.key === "Escape") onClose(); }
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [onClose]);
  return ref;
}

/* ── main component ── */

export default function CompetitorReports() {
  /* state */
  const today = useMemo(() => startOfDay(new Date()), []);
  const initialFrom = useMemo(() => addDays(today, -29), [today]);
  const [from, setFrom] = useState<Date>(initialFrom);
  const [to, setTo] = useState<Date>(today);
  const [period, setPeriod] = useState<Period>("day");
  const [mode, setMode] = useState<Mode>("net");
  // null  ≡ "all models". Set ≡ explicit subset.
  const [selectedIds, setSelectedIds] = useState<Set<string> | null>(null);
  const [tab, setTab] = useState<Tab>("overview");
  const [visibleCols, setVisibleCols] = useState<Set<TableCol>>(() => new Set(DEFAULT_COLS));
  const ratio = mode === "net" ? NET_RATIO : 1;

  const fromIso = useMemo(() => startOfDay(from).toISOString(), [from]);
  const toIso   = useMemo(() => endOfDay(to).toISOString(),     [to]);

  /* day-axis */
  const dayDates = useMemo(() => {
    const n = Math.max(1, daysBetween(from, to) + 1);
    const out: Date[] = [];
    for (let i = 0; i < n; i++) out.push(addDays(from, i));
    return out;
  }, [from, to]);

  /* queries — per_model is always all-models so we have the full picker */
  const perModel = usePerModel(fromIso, toIso);
  const allModels = perModel.data?.per_model ?? [];

  /* effective model id list — when null, fall through with no account_id */
  const isAll = selectedIds === null;
  const effectiveIds: string[] = useMemo(() => {
    if (isAll) return ["__all__"];
    return Array.from(selectedIds!);
  }, [isAll, selectedIds]);

  /* one revenue query per effective id; "__all__" sends no account_id */
  const revByDayQueries = useQueries({
    queries: effectiveIds.map((id) => ({
      queryKey: ["competitor", "revenue-day", id, fromIso, toIso] as const,
      queryFn: () => {
        const qs = new URLSearchParams({ group_by: "day", from: fromIso, to: toIso });
        if (id !== "__all__") qs.set("account_id", id);
        return relay.get<RevResp>(`/admin/stats/revenue?${qs.toString()}`, BG_CTX);
      },
      staleTime: 60_000,
      placeholderData: keepPreviousData,
    })),
  });
  const revByKindQueries = useQueries({
    queries: effectiveIds.map((id) => ({
      queryKey: ["competitor", "revenue-kind", id, fromIso, toIso] as const,
      queryFn: () => {
        const qs = new URLSearchParams({ group_by: "kind", from: fromIso, to: toIso });
        if (id !== "__all__") qs.set("account_id", id);
        return relay.get<RevResp>(`/admin/stats/revenue?${qs.toString()}`, BG_CTX);
      },
      staleTime: 60_000,
      placeholderData: keepPreviousData,
    })),
  });

  /* aggregate day rows → map<isoDay, cents> */
  const dailyGross = useMemo(() => {
    const m = new Map<string, number>();
    for (const q of revByDayQueries) {
      for (const r of q.data?.rows ?? []) {
        if (!r.day) continue;
        m.set(r.day, (m.get(r.day) ?? 0) + r.total_cents);
      }
    }
    return dayDates.map((d) => Math.round((m.get(isoDay(d)) ?? 0) * ratio));
  }, [revByDayQueries, dayDates, ratio]);

  /* aggregate kind rows → buckets */
  const buckets = useMemo(() => {
    const out = { subscriptions: 0, tips: 0, posts: 0, messages: 0, streams: 0, referrals: 0, other: 0 };
    for (const q of revByKindQueries) {
      for (const r of q.data?.rows ?? []) out[bucketOf(r.kind)] += r.total_cents;
    }
    for (const k of Object.keys(out) as (keyof typeof out)[]) out[k] = Math.round(out[k] * ratio);
    return out;
  }, [revByKindQueries, ratio]);

  const totalCents = useMemo(
    () => Object.values(buckets).reduce((a, b) => a + b, 0),
    [buckets],
  );

  /* roll daily series into the chosen period */
  const { buckets: periodBuckets, labels: periodLabels } = useMemo(() => {
    if (period === "day") {
      return { buckets: dailyGross, labels: dayDates.map((d) => fmtBucketLabel(d, "day")) };
    }
    if (period === "week") {
      const out: number[] = [];
      const labs: string[] = [];
      for (let i = 0; i < dailyGross.length; i += 7) {
        const slice = dailyGross.slice(i, i + 7);
        out.push(slice.reduce((a, b) => a + b, 0));
        labs.push(fmtBucketLabel(dayDates[i], "week"));
      }
      return { buckets: out, labels: labs };
    }
    // month
    const byMonth = new Map<string, { total: number; first: Date }>();
    for (let i = 0; i < dailyGross.length; i++) {
      const d = dayDates[i];
      const key = `${d.getFullYear()}-${d.getMonth()}`;
      const cur = byMonth.get(key) ?? { total: 0, first: d };
      cur.total += dailyGross[i];
      byMonth.set(key, cur);
    }
    const entries = Array.from(byMonth.entries())
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([, v]) => v);
    return {
      buckets: entries.map((e) => e.total),
      labels: entries.map((e) => fmtBucketLabel(e.first, "month")),
    };
  }, [period, dailyGross, dayDates]);

  /* channel lines — split bucketed periods by overall kind share */
  const channelSeries = useMemo(() => {
    const t = totalCents || 1;
    const split = (frac: number) => periodBuckets.map((v) => Math.round(v * frac));
    return {
      messages:      split(buckets.messages      / t),
      subscriptions: split(buckets.subscriptions / t),
      tips:          split(buckets.tips          / t),
      posts:         split(buckets.posts         / t),
    };
  }, [periodBuckets, buckets, totalCents]);

  /* legend */
  const legend = useMemo(() => {
    const t = totalCents || 1;
    const pct = (c: number) => ((c / t) * 100).toFixed(2) + "%";
    return [
      { color: "#3b82f6", label: "Subscriptions", pct: pct(buckets.subscriptions), amt: fmtUsdShort(buckets.subscriptions) },
      { color: "#10b981", label: "Tips",          pct: pct(buckets.tips),          amt: fmtUsdShort(buckets.tips) },
      { color: "#ef4444", label: "Posts",         pct: pct(buckets.posts),         amt: fmtUsdShort(buckets.posts) },
      { color: "#f59e0b", label: "Messages",      pct: pct(buckets.messages),      amt: fmtUsdShort(buckets.messages) },
      { color: "#6366f1", label: "Referrals",     pct: pct(buckets.referrals),     amt: fmtUsdShort(buckets.referrals) },
      { color: "#a855f7", label: "Streams",       pct: pct(buckets.streams),       amt: fmtUsdShort(buckets.streams) },
    ];
  }, [buckets, totalCents]);

  /* creator table — filtered to selection */
  const visibleCreators = useMemo(() => {
    if (isAll) return allModels;
    return allModels.filter((m) => selectedIds!.has(m.account_id));
  }, [isAll, allModels, selectedIds]);

  const messageEarnings = useMemo(
    () => visibleCreators.reduce((a, m) => a + Math.round((m.revenue_by_kind?.ppv ?? 0) * ratio), 0),
    [visibleCreators, ratio],
  );
  const totalEarningsAcrossCreators = useMemo(
    () => visibleCreators.reduce((a, m) => a + Math.round(m.total_revenue_cents * ratio), 0),
    [visibleCreators, ratio],
  );

  // Column order = the canonical ALL_COLS order, filtered to the user's
  // selection. Keeps the table layout stable even when the user toggles
  // checkboxes in any order.
  const orderedCols = useMemo(
    () => ALL_COLS.filter((c) => visibleCols.has(c)),
    [visibleCols],
  );

  /* render */
  return (
    <div
      className="rounded-md overflow-hidden border"
      style={{
        background: "#ffffff",
        color: "#1f2937",
        borderColor: "#e5e7eb",
        fontFamily: "Inter, -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif",
      }}
    >
      <div className="px-6 pt-5 pb-6 text-[13px]">
        <div className="text-[15px] font-medium mb-3" style={{ color: "#111827" }}>Creator Reports</div>

        {/* Tabs + controls */}
        <div className="flex items-center justify-between border-b" style={{ borderColor: "#e5e7eb" }}>
          <div className="flex items-center gap-6">
            <button
              type="button"
              onClick={() => setTab("overview")}
              className="pb-2 text-[13px]"
              style={{
                color: tab === "overview" ? "#2563eb" : "#6b7280",
                fontWeight: tab === "overview" ? 500 : 400,
                borderBottom: tab === "overview" ? "2px solid #2563eb" : "2px solid transparent",
                marginBottom: "-1px",
                background: "none",
                cursor: "pointer",
              }}
            >
              Overview
            </button>
            <button
              type="button"
              onClick={() => setTab("creators")}
              className="pb-2 text-[13px]"
              style={{
                color: tab === "creators" ? "#2563eb" : "#6b7280",
                fontWeight: tab === "creators" ? 500 : 400,
                borderBottom: tab === "creators" ? "2px solid #2563eb" : "2px solid transparent",
                marginBottom: "-1px",
                background: "none",
                cursor: "pointer",
              }}
            >
              Creator performance
            </button>
          </div>

          <div className="flex items-center gap-2 pb-2">
            <DateRangePill from={from} to={to} onChange={(f, t) => { setFrom(f); setTo(t); }} />
            <SelectPill
              label={period === "day" ? "Shown by day" : period === "week" ? "Shown by week" : "Shown by month"}
              options={[
                { value: "day",   label: "Shown by day" },
                { value: "week",  label: "Shown by week" },
                { value: "month", label: "Shown by month" },
              ]}
              value={period}
              onChange={(v) => setPeriod(v as Period)}
            />
            <SelectPill
              label={mode === "net" ? "Net earnings" : "Gross earnings"}
              options={[
                { value: "net",   label: "Net earnings (×0.8)" },
                { value: "gross", label: "Gross earnings" },
              ]}
              value={mode}
              onChange={(v) => setMode(v as Mode)}
            />
            <FiltersPill
              models={allModels.map((m) => ({ id: m.account_id, name: m.display_name || m.account_id }))}
              selected={selectedIds}
              onChange={setSelectedIds}
            />
          </div>
        </div>

        {tab === "creators" && (
          <div className="mt-5">
            <PerModelKpiGrid from={fromIso} to={toIso} />
          </div>
        )}

        {tab === "overview" && <>
        {/* Earnings summary */}
        <div className="mt-5 mb-2 flex items-center gap-1.5">
          <span className="text-[13px] font-medium" style={{ color: "#111827" }}>Earnings summary</span>
          <Info />
        </div>

        <div className="grid grid-cols-4 gap-x-8 gap-y-4 pb-5 border-b" style={{ borderColor: "#f1f5f9" }}>
          <div className="row-span-2 flex items-start gap-3">
            <IconCircle color="#dbeafe"><RocketIcon /></IconCircle>
            <div>
              <div className="text-[12px]" style={{ color: "#2563eb" }}>Total earnings</div>
              <div className="text-[26px] font-semibold tracking-tight" style={{ color: "#2563eb" }}>
                {fmtUsd(totalCents)}
              </div>
            </div>
          </div>

          <SummaryCell label="Subscriptions" value={fmtUsd(buckets.subscriptions)} iconBg="#dcfce7"><BookmarkIcon /></SummaryCell>
          <SummaryCell label="Posts"        value={fmtUsd(buckets.posts)}         iconBg="#dcfce7"><DocIcon /></SummaryCell>
          <SummaryCell label="Messages"     value={fmtUsd(buckets.messages)}      iconBg="#ede9fe"><ChatIcon /></SummaryCell>

          <SummaryCell label="Tips"      value={fmtUsd(buckets.tips)}      iconBg="#ede9fe"><BulbIcon /></SummaryCell>
          <SummaryCell label="Referrals" value={fmtUsd(buckets.referrals)} iconBg="#fee2e2"><PeopleIcon /></SummaryCell>
          <SummaryCell label="Streams"   value={fmtUsd(buckets.streams)}   iconBg="#ede9fe"><StackIcon /></SummaryCell>
        </div>

        {/* Earnings trends */}
        <div className="mt-5 mb-3 flex items-center gap-1.5">
          <span className="text-[13px] font-medium" style={{ color: "#111827" }}>Earnings trends</span>
          <Info />
        </div>
        <TrendBars values={periodBuckets} labels={periodLabels} />

        {/* Earnings by channel */}
        <div className="mt-6 mb-3 flex items-center gap-1.5">
          <span className="text-[13px] font-medium" style={{ color: "#111827" }}>Earnings by channel</span>
          <Info />
        </div>
        <div className="grid grid-cols-[1fr_220px] gap-6">
          <ChannelLines labels={periodLabels} series={[
            { name: "messages",      color: "#f59e0b", values: channelSeries.messages },
            { name: "subscriptions", color: "#3b82f6", values: channelSeries.subscriptions },
            { name: "tips",          color: "#10b981", values: channelSeries.tips },
            { name: "posts",         color: "#ef4444", values: channelSeries.posts },
          ]} />
          <div className="flex flex-col gap-2 text-[12px]">
            {legend.map((l) => (
              <LegendRow key={l.label} color={l.color} label={l.label} pct={l.pct} amt={l.amt} />
            ))}
          </div>
        </div>

        {/* Creator statistics */}
        <div className="mt-6 flex items-center justify-between mb-3">
          <span className="text-[13px] font-medium" style={{ color: "#111827" }}>Creator statistics</span>
          <div className="flex items-center gap-2">
            <CustomMetricsPill visible={visibleCols} onChange={setVisibleCols} />
            <ExportPill onClick={() => downloadCsv({
              from, to, period, mode,
              modelLabel: isAll ? "all" : `${selectedIds!.size}/${allModels.length}`,
              buckets, totalCents,
              periodLabels, periodBuckets, channelSeries,
              creators: visibleCreators,
              cols: orderedCols,
              ratio,
            })} />
          </div>
        </div>

        <div className="grid grid-cols-4 gap-3 mb-4">
          <StatCard value={visibleCreators.length.toLocaleString()} label="Creators" />
          <StatCard value={fmtUsd(messageEarnings)}                  label="Message earnings" />
          <StatCard value={fmtUsd(totalEarningsAcrossCreators)}      label="Total earnings" />
          <StatCard value={fmtUsd(0)}                                 label="Refunded" />
        </div>

        {/* Table */}
        <CreatorTable
          creators={visibleCreators}
          cols={orderedCols}
          ratio={ratio}
        />
        </>}

        {/* Floating chat bubble */}
        <div style={{ position: "relative", marginTop: 12, height: 0 }}>
          <div
            style={{
              position: "absolute",
              right: 8,
              bottom: -8,
              width: 36, height: 36, borderRadius: 999,
              background: "#111827",
              boxShadow: "0 4px 12px rgba(0,0,0,0.18)",
              display: "flex", alignItems: "center", justifyContent: "center",
            }}
          >
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none">
              <path d="M21 12a8 8 0 01-11.4 7.2L3 21l1.8-6.6A8 8 0 1121 12z" stroke="#fff" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"/>
            </svg>
          </div>
        </div>
      </div>
    </div>
  );
}

/* ───────── interactive pills ───────── */

function PillTrigger({ open, accent, children, onClick }: {
  open?: boolean; accent?: boolean; children: React.ReactNode; onClick?: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md text-[12px] outline-none"
      style={{
        border: `1px solid ${open ? "#93c5fd" : accent ? "#bfdbfe" : "#e5e7eb"}`,
        background: "#fff",
        color: accent ? "#2563eb" : "#374151",
        boxShadow: open ? "0 0 0 3px rgba(59,130,246,0.15)" : "none",
        cursor: "pointer",
      }}
    >
      {children}
    </button>
  );
}

function PopoverBox({ children, anchor }: { children: React.ReactNode; anchor: "left" | "right" }) {
  return (
    <div
      style={{
        position: "absolute",
        top: "calc(100% + 6px)",
        [anchor]: 0,
        background: "#fff",
        border: "1px solid #e5e7eb",
        borderRadius: 8,
        boxShadow: "0 12px 32px rgba(0,0,0,0.08), 0 2px 6px rgba(0,0,0,0.04)",
        padding: 8,
        zIndex: 20,
        minWidth: 200,
      } as React.CSSProperties}
    >
      {children}
    </div>
  );
}

function DateRangePill({ from, to, onChange }: {
  from: Date; to: Date; onChange: (f: Date, t: Date) => void;
}) {
  const [open, setOpen] = useState(false);
  const ref = useClickOutside<HTMLDivElement>(() => setOpen(false));
  return (
    <div ref={ref} style={{ position: "relative" }}>
      <PillTrigger open={open} onClick={() => setOpen((v) => !v)}>
        <span style={{ color: "#111827" }}>{fmtRangeLabel(from)}</span>
        <span style={{ color: "#6b7280" }}>{"  →  "}</span>
        <span style={{ color: "#111827" }}>{fmtRangeLabel(to)}</span>
        <CalIcon />
      </PillTrigger>
      {open && (
        <PopoverBox anchor="right">
          <div className="flex flex-col gap-2 p-1" style={{ minWidth: 240 }}>
            <label className="flex items-center justify-between gap-3 text-[12px]" style={{ color: "#374151" }}>
              <span>From</span>
              <input
                type="date"
                value={isoDay(from)}
                max={isoDay(to)}
                onChange={(e) => {
                  const [y, m, d] = e.target.value.split("-").map(Number);
                  if (!y || !m || !d) return;
                  onChange(new Date(y, m - 1, d), to);
                }}
                style={dateInputStyle}
              />
            </label>
            <label className="flex items-center justify-between gap-3 text-[12px]" style={{ color: "#374151" }}>
              <span>To</span>
              <input
                type="date"
                value={isoDay(to)}
                min={isoDay(from)}
                onChange={(e) => {
                  const [y, m, d] = e.target.value.split("-").map(Number);
                  if (!y || !m || !d) return;
                  onChange(from, new Date(y, m - 1, d));
                }}
                style={dateInputStyle}
              />
            </label>
            <div className="flex items-center gap-1 mt-1">
              {[7, 30, 90].map((n) => (
                <button
                  key={n}
                  type="button"
                  onClick={() => {
                    const t = startOfDay(new Date());
                    onChange(addDays(t, -(n - 1)), t);
                  }}
                  className="text-[11px] px-2 py-1 rounded"
                  style={{ border: "1px solid #e5e7eb", color: "#374151", background: "#fff", cursor: "pointer" }}
                >
                  Last {n}d
                </button>
              ))}
            </div>
          </div>
        </PopoverBox>
      )}
    </div>
  );
}

interface SelectOpt { value: string; label: string }
function SelectPill({ label, options, value, onChange }: {
  label: string; options: SelectOpt[]; value: string; onChange: (v: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const ref = useClickOutside<HTMLDivElement>(() => setOpen(false));
  return (
    <div ref={ref} style={{ position: "relative" }}>
      <PillTrigger open={open} onClick={() => setOpen((v) => !v)}>
        <span>{label}</span>
        <Chev />
      </PillTrigger>
      {open && (
        <PopoverBox anchor="right">
          <div className="flex flex-col">
            {options.map((o) => {
              const active = o.value === value;
              return (
                <button
                  key={o.value}
                  type="button"
                  onClick={() => { onChange(o.value); setOpen(false); }}
                  className="text-left text-[12px] px-2 py-1.5 rounded"
                  style={{
                    background: active ? "#eff6ff" : "transparent",
                    color: active ? "#1d4ed8" : "#374151",
                    cursor: "pointer",
                  }}
                >
                  {o.label}
                </button>
              );
            })}
          </div>
        </PopoverBox>
      )}
    </div>
  );
}

function FiltersPill({ models, selected, onChange }: {
  models: { id: string; name: string }[];
  selected: Set<string> | null;
  onChange: (next: Set<string> | null) => void;
}) {
  const [open, setOpen] = useState(false);
  const ref = useClickOutside<HTMLDivElement>(() => setOpen(false));
  const isAll = selected === null;
  const count = isAll ? models.length : selected!.size;
  const label =
    isAll              ? "Filters" :
    count === 0        ? "Filters · none" :
    count === 1        ? `Filters · ${models.find((m) => selected!.has(m.id))?.name ?? "1"}` :
                         `Filters · ${count} models`;

  function toggle(id: string) {
    const next = new Set(isAll ? models.map((m) => m.id) : Array.from(selected!));
    if (next.has(id)) next.delete(id);
    else next.add(id);
    if (next.size === models.length) onChange(null);
    else onChange(next);
  }

  return (
    <div ref={ref} style={{ position: "relative" }}>
      <PillTrigger open={open} accent onClick={() => setOpen((v) => !v)}>
        <FilterIcon />
        <span>{label}</span>
      </PillTrigger>
      {open && (
        <PopoverBox anchor="right">
          <div style={{ minWidth: 240, maxHeight: 320, overflowY: "auto" }} className="flex flex-col">
            <div className="flex items-center justify-between px-2 py-1.5">
              <span className="text-[12px]" style={{ color: "#6b7280" }}>
                {isAll ? `All ${models.length} models` : `${count}/${models.length} selected`}
              </span>
              <div className="flex items-center gap-2">
                <button
                  type="button"
                  className="text-[11px]"
                  style={{ color: "#2563eb", background: "none", border: "none", cursor: "pointer", padding: 0 }}
                  onClick={() => onChange(null)}
                >
                  All
                </button>
                <button
                  type="button"
                  className="text-[11px]"
                  style={{ color: "#6b7280", background: "none", border: "none", cursor: "pointer", padding: 0 }}
                  onClick={() => onChange(new Set())}
                >
                  None
                </button>
              </div>
            </div>
            <div style={{ borderTop: "1px solid #f1f5f9", marginBottom: 2 }} />
            {models.length === 0 ? (
              <div className="text-[12px] px-2 py-2" style={{ color: "#9ca3af" }}>No models yet.</div>
            ) : models.map((m) => {
              const checked = isAll || selected!.has(m.id);
              return (
                <label
                  key={m.id}
                  className="flex items-center gap-2 text-[12px] px-2 py-1.5 rounded cursor-pointer"
                  style={{ color: "#374151" }}
                >
                  <input
                    type="checkbox"
                    checked={checked}
                    onChange={() => toggle(m.id)}
                    style={{ accentColor: "#2563eb" }}
                  />
                  <span style={{ flex: 1 }}>{m.name}</span>
                </label>
              );
            })}
          </div>
        </PopoverBox>
      )}
    </div>
  );
}

function CustomMetricsPill({ visible, onChange }: {
  visible: Set<TableCol>; onChange: (next: Set<TableCol>) => void;
}) {
  const [open, setOpen] = useState(false);
  const ref = useClickOutside<HTMLDivElement>(() => setOpen(false));
  const count = visible.size;
  const isDefault = count === DEFAULT_COLS.length && DEFAULT_COLS.every((c) => visible.has(c));
  const label =
    isDefault   ? "Custom metrics" :
    count === 0 ? "Custom metrics · none" :
                  `Custom metrics · ${count}`;

  function toggle(c: TableCol) {
    const next = new Set(visible);
    if (next.has(c)) next.delete(c);
    else next.add(c);
    onChange(next);
  }

  return (
    <div ref={ref} style={{ position: "relative" }}>
      <PillTrigger open={open} onClick={() => setOpen((v) => !v)}>
        <MetricsIcon />
        <span>{label}</span>
      </PillTrigger>
      {open && (
        <PopoverBox anchor="right">
          <div style={{ minWidth: 230, maxHeight: 360, overflowY: "auto" }} className="flex flex-col">
            <div className="flex items-center justify-between px-2 py-1.5">
              <span className="text-[12px]" style={{ color: "#6b7280" }}>
                Table columns · {count}/{ALL_COLS.length}
              </span>
              <div className="flex items-center gap-2">
                <button
                  type="button"
                  className="text-[11px]"
                  style={{ color: "#2563eb", background: "none", border: "none", cursor: "pointer", padding: 0 }}
                  onClick={() => onChange(new Set(DEFAULT_COLS))}
                >
                  Default
                </button>
                <button
                  type="button"
                  className="text-[11px]"
                  style={{ color: "#2563eb", background: "none", border: "none", cursor: "pointer", padding: 0 }}
                  onClick={() => onChange(new Set(ALL_COLS))}
                >
                  All
                </button>
                <button
                  type="button"
                  className="text-[11px]"
                  style={{ color: "#6b7280", background: "none", border: "none", cursor: "pointer", padding: 0 }}
                  onClick={() => onChange(new Set())}
                >
                  None
                </button>
              </div>
            </div>
            <div style={{ borderTop: "1px solid #f1f5f9", marginBottom: 2 }} />
            {ALL_COLS.map((c) => (
              <label
                key={c}
                className="flex items-center gap-2 text-[12px] px-2 py-1.5 rounded cursor-pointer"
                style={{ color: "#374151" }}
              >
                <input
                  type="checkbox"
                  checked={visible.has(c)}
                  onChange={() => toggle(c)}
                  style={{ accentColor: "#2563eb" }}
                />
                <span style={{ flex: 1 }}>{COL_LABEL[c]}</span>
              </label>
            ))}
          </div>
        </PopoverBox>
      )}
    </div>
  );
}

function ExportPill({ onClick }: { onClick: () => void }) {
  return (
    <PillTrigger accent onClick={onClick}>
      <ExportIcon />
      <span style={{ color: "#2563eb" }}>Export</span>
    </PillTrigger>
  );
}

/* ───────── creator-row column resolution ───────── */

function colNumericValue(m: PerModelRow, c: TableCol, ratio: number): number {
  const k = m.revenue_by_kind;
  switch (c) {
    case "subscription":    return Math.round(((k?.subscription ?? 0) + (k?.rebill ?? 0)) * ratio);
    case "rebill":          return Math.round((k?.rebill ?? 0) * ratio);
    case "new_sub":         return Math.round((k?.subscription ?? 0) * ratio);
    case "tips":            return Math.round((k?.tip ?? 0) * ratio);
    case "message":         return Math.round((k?.ppv ?? 0) * ratio);
    case "pending":         return Math.round((m.pending_revenue_cents ?? 0) * ratio);
    case "total":           return Math.round(m.total_revenue_cents * ratio);
    case "active_fans":     return m.active_subs_count ?? 0;
    case "new_subs_count":  return m.new_subs_count;
    case "messages_sent":   return m.messages_sent;
    case "ppv_conversions": return m.ppv_conversions;
    case "ltv":             return Math.round((m.ltv_cents ?? 0) * ratio);
    case "arpu":            return Math.round((m.arpu_cents ?? 0) * ratio);
    case "expired":         return 0;
  }
}

function colDisplay(m: PerModelRow, c: TableCol, ratio: number): string {
  const v = colNumericValue(m, c, ratio);
  if (COL_IS_MONEY[c]) {
    // ltv/arpu legitimately can be null when there's no data
    if ((c === "ltv" && m.ltv_cents == null) || (c === "arpu" && m.arpu_cents == null)) return "—";
    return fmtUsd(v);
  }
  return v.toLocaleString();
}

function colTemplateWidth(c: TableCol): string {
  // Money columns get a bit more room than count columns. Tweaked so the
  // default 6-column layout matches the screenshot proportions.
  if (c === "total" || c === "active_fans") return "1fr";
  if (COL_IS_MONEY[c]) return "0.9fr";
  return "0.7fr";
}

function CreatorTable({ creators, cols, ratio }: {
  creators: PerModelRow[]; cols: TableCol[]; ratio: number;
}) {
  // "Creator" is always the anchor column on the left.
  const template = ["1.2fr", ...cols.map(colTemplateWidth)].join(" ");

  return (
    <div className="rounded-md border overflow-hidden" style={{ borderColor: "#e5e7eb" }}>
      <div
        className="grid text-[12px] py-2.5 px-3"
        style={{
          gridTemplateColumns: template,
          color: "#6b7280",
          background: "#fafbfc",
          borderBottom: "1px solid #e5e7eb",
        }}
      >
        <span>Creator</span>
        {cols.map((c) => (
          <span key={c}>{COL_LABEL[c]} <SortArrows /></span>
        ))}
      </div>

      {creators.length === 0 ? (
        <div className="text-[12px] py-3 px-3" style={{ color: "#9ca3af" }}>
          No creators in window.
        </div>
      ) : creators.slice(0, 10).map((m) => (
        <div
          key={m.account_id}
          className="grid text-[12px] py-3 px-3 items-center"
          style={{
            gridTemplateColumns: template,
            color: "#111827",
            borderTop: "1px solid #f1f5f9",
          }}
        >
          <span>{m.display_name || m.account_id}</span>
          {cols.map((c) => (
            <span key={c}>{colDisplay(m, c, ratio)}</span>
          ))}
        </div>
      ))}
    </div>
  );
}

/* ───────── csv export ───────── */

interface CsvData {
  from: Date; to: Date;
  period: Period; mode: Mode;
  modelLabel: string;
  buckets: Record<Bucket, number>;
  totalCents: number;
  periodLabels: string[];
  periodBuckets: number[];
  channelSeries: { messages: number[]; subscriptions: number[]; tips: number[]; posts: number[] };
  creators: PerModelRow[];
  cols: TableCol[];
  ratio: number;
}

function csvField(v: string | number): string {
  const s = String(v);
  if (/[",\n]/.test(s)) return `"${s.replace(/"/g, '""')}"`;
  return s;
}
function csvRow(...fields: (string | number)[]): string {
  return fields.map(csvField).join(",");
}

function downloadCsv(d: CsvData) {
  const dollars = (c: number) => (c / 100).toFixed(2);
  const lines: string[] = [];

  lines.push(`# Creator Reports export`);
  lines.push(`# generated,${new Date().toISOString()}`);
  lines.push(`# date_from,${isoDay(d.from)}`);
  lines.push(`# date_to,${isoDay(d.to)}`);
  lines.push(`# period,${d.period}`);
  lines.push(`# mode,${d.mode}`);
  lines.push(`# models,${d.modelLabel}`);
  lines.push(``);

  lines.push(`# Earnings summary (USD)`);
  lines.push(csvRow("metric", "amount_usd"));
  lines.push(csvRow("Total earnings", dollars(d.totalCents)));
  lines.push(csvRow("Subscriptions", dollars(d.buckets.subscriptions)));
  lines.push(csvRow("Tips",          dollars(d.buckets.tips)));
  lines.push(csvRow("Posts",         dollars(d.buckets.posts)));
  lines.push(csvRow("Messages",      dollars(d.buckets.messages)));
  lines.push(csvRow("Referrals",     dollars(d.buckets.referrals)));
  lines.push(csvRow("Streams",       dollars(d.buckets.streams)));
  lines.push(``);

  lines.push(`# Earnings trends (per ${d.period}, USD)`);
  lines.push(csvRow("period", "total_usd"));
  for (let i = 0; i < d.periodLabels.length; i++) {
    lines.push(csvRow(d.periodLabels[i], dollars(d.periodBuckets[i])));
  }
  lines.push(``);

  lines.push(`# Earnings by channel (per ${d.period}, USD)`);
  lines.push(csvRow("period", "messages", "subscriptions", "tips", "posts"));
  for (let i = 0; i < d.periodLabels.length; i++) {
    lines.push(csvRow(
      d.periodLabels[i],
      dollars(d.channelSeries.messages[i] ?? 0),
      dollars(d.channelSeries.subscriptions[i] ?? 0),
      dollars(d.channelSeries.tips[i] ?? 0),
      dollars(d.channelSeries.posts[i] ?? 0),
    ));
  }
  lines.push(``);

  lines.push(`# Creators (columns reflect Custom metrics selection)`);
  const headerCells: string[] = ["account_id", "display_name"];
  for (const c of d.cols) headerCells.push(csvSlug(c));
  lines.push(headerCells.map(csvField).join(","));
  for (const m of d.creators) {
    const cells: (string | number)[] = [m.account_id, m.display_name ?? ""];
    for (const c of d.cols) {
      const v = colNumericValue(m, c, d.ratio);
      cells.push(COL_IS_MONEY[c] ? dollars(v) : v);
    }
    lines.push(cells.map(csvField).join(","));
  }

  const blob = new Blob([lines.join("\n")], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `creator-reports_${isoDay(d.from)}_${isoDay(d.to)}.csv`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 0);
}

function csvSlug(c: TableCol): string {
  const suffix = COL_IS_MONEY[c] ? "_usd" : "";
  return c + suffix;
}

const dateInputStyle: React.CSSProperties = {
  border: "1px solid #e5e7eb",
  borderRadius: 6,
  padding: "4px 6px",
  fontSize: 12,
  color: "#111827",
  background: "#fff",
  outline: "none",
};

/* ───────── primitives ───────── */

function IconCircle({ color, children }: { color: string; children: React.ReactNode }) {
  return (
    <div
      style={{
        width: 36, height: 36, borderRadius: 999,
        background: color,
        display: "flex", alignItems: "center", justifyContent: "center",
        flexShrink: 0,
      }}
    >
      {children}
    </div>
  );
}

function SummaryCell({ label, value, iconBg, children }: {
  label: string; value: string; iconBg: string; children: React.ReactNode;
}) {
  return (
    <div className="flex items-center justify-between gap-3">
      <div>
        <div className="text-[16px] font-semibold tracking-tight" style={{ color: "#111827" }}>{value}</div>
        <div className="text-[12px]" style={{ color: "#6b7280" }}>{label}</div>
      </div>
      <IconCircle color={iconBg}>{children}</IconCircle>
    </div>
  );
}

function StatCard({ value, label }: { value: string; label: string }) {
  return (
    <div className="rounded-md p-4 border flex items-start justify-between" style={{ borderColor: "#e5e7eb" }}>
      <div>
        <div className="text-[20px] font-semibold tracking-tight" style={{ color: "#111827" }}>{value}</div>
        <div className="text-[12px]" style={{ color: "#6b7280" }}>{label}</div>
      </div>
      <Info />
    </div>
  );
}

function LegendRow({ color, label, pct, amt }: { color: string; label: string; pct: string; amt: string }) {
  return (
    <div className="flex items-center gap-2">
      <span style={{ width: 8, height: 8, borderRadius: 999, background: color, display: "inline-block" }} />
      <span style={{ color: "#374151", flex: 1 }}>{label}</span>
      <span style={{ color: "#6b7280" }}>{pct}</span>
      <span style={{ color: "#111827", minWidth: 80, textAlign: "right" }}>{amt}</span>
    </div>
  );
}

/* ───────── charts ───────── */

function niceMax(maxValue: number, gridLines: number): number {
  if (maxValue <= 0) return gridLines;
  const raw = maxValue / gridLines;
  const exp = Math.floor(Math.log10(raw));
  const base = Math.pow(10, exp);
  const m = raw / base;
  const stepMult = m <= 1 ? 1 : m <= 2 ? 2 : m <= 5 ? 5 : 10;
  const step = stepMult * base;
  return step * gridLines;
}

function TrendBars({ values, labels }: { values: number[]; labels: string[] }) {
  const W = 1200, H = 220;
  const padL = 50, padR = 10, padT = 10, padB = 28;
  const innerW = W - padL - padR;
  const innerH = H - padT - padB;
  const peak = Math.max(1, ...values);
  const max = niceMax(peak, 6);
  const slot = innerW / Math.max(1, values.length);
  const barW = slot * 0.65;
  const ticks = 6;
  const labelStride = labels.length > 24 ? Math.ceil(labels.length / 24) : 1;

  return (
    <svg viewBox={`0 0 ${W} ${H}`} width="100%" style={{ display: "block" }}>
      {Array.from({ length: ticks + 1 }).map((_, i) => {
        const g = (max / ticks) * i;
        const y = padT + innerH - (g / max) * innerH;
        return (
          <g key={i}>
            <line x1={padL} y1={y} x2={W - padR} y2={y} stroke="#f1f5f9" strokeWidth={1} />
            <text x={padL - 6} y={y + 3} textAnchor="end" fontSize="10" fill="#9ca3af">
              {Math.round(g / 100).toLocaleString()}
            </text>
          </g>
        );
      })}
      {values.map((v, i) => {
        const h = (v / max) * innerH;
        const x = padL + i * slot + (slot - barW) / 2;
        const y = padT + innerH - h;
        return <rect key={i} x={x} y={y} width={barW} height={Math.max(0, h)} fill="#2563eb" rx={1} />;
      })}
      {labels.map((l, i) => {
        if (i % labelStride !== 0) return null;
        const x = padL + i * slot + slot / 2;
        return (
          <text key={i} x={x} y={H - 8} textAnchor="middle" fontSize="9" fill="#9ca3af">{l}</text>
        );
      })}
    </svg>
  );
}

interface LineSeries { name: string; color: string; values: number[] }
function ChannelLines({ labels, series }: { labels: string[]; series: LineSeries[] }) {
  const W = 900, H = 220;
  const padL = 50, padR = 10, padT = 10, padB = 28;
  const innerW = W - padL - padR;
  const innerH = H - padT - padB;
  let peak = 1;
  for (const s of series) for (const v of s.values) if (v > peak) peak = v;
  const max = niceMax(peak, 6);
  const ticks = 6;
  const xAt = (i: number) => padL + (i / Math.max(1, labels.length - 1)) * innerW;
  const yAt = (v: number) => padT + innerH - (v / max) * innerH;
  const labelStride = labels.length > 24 ? Math.ceil(labels.length / 24) : 1;

  return (
    <svg viewBox={`0 0 ${W} ${H}`} width="100%" style={{ display: "block" }}>
      {Array.from({ length: ticks + 1 }).map((_, i) => {
        const g = (max / ticks) * i;
        const y = yAt(g);
        return (
          <g key={i}>
            <line x1={padL} y1={y} x2={W - padR} y2={y} stroke="#f1f5f9" strokeWidth={1} />
            <text x={padL - 6} y={y + 3} textAnchor="end" fontSize="10" fill="#9ca3af">
              {Math.round(g / 100).toLocaleString()}
            </text>
          </g>
        );
      })}
      {series.map((s) => {
        const d = s.values
          .map((v, i) => `${i === 0 ? "M" : "L"}${xAt(i).toFixed(2)},${yAt(v).toFixed(2)}`)
          .join(" ");
        return (
          <g key={s.name}>
            <path d={d} fill="none" stroke={s.color} strokeWidth={1.6} strokeLinejoin="round" strokeLinecap="round" />
            {s.values.map((v, i) => (
              <circle key={i} cx={xAt(i)} cy={yAt(v)} r={1.6} fill={s.color} />
            ))}
          </g>
        );
      })}
      {labels.map((l, i) => {
        if (i % labelStride !== 0) return null;
        return (
          <text key={i} x={xAt(i)} y={H - 8} textAnchor="middle" fontSize="9" fill="#9ca3af">{l}</text>
        );
      })}
    </svg>
  );
}

/* ───────── icons ───────── */

function Chev() {
  return (
    <svg width="10" height="10" viewBox="0 0 24 24" fill="none">
      <path d="M6 9l6 6 6-6" stroke="#6b7280" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"/>
    </svg>
  );
}
function CalIcon() {
  return (
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" style={{ marginLeft: 6 }}>
      <rect x="3" y="5" width="18" height="16" rx="2" stroke="#6b7280" strokeWidth="1.6"/>
      <path d="M3 9h18M8 3v4M16 3v4" stroke="#6b7280" strokeWidth="1.6" strokeLinecap="round"/>
    </svg>
  );
}
function FilterIcon() {
  return (
    <svg width="12" height="12" viewBox="0 0 24 24" fill="none">
      <path d="M3 5h18M6 12h12M10 19h4" stroke="#2563eb" strokeWidth="1.8" strokeLinecap="round"/>
    </svg>
  );
}
function Info() {
  return (
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none">
      <circle cx="12" cy="12" r="9" stroke="#9ca3af" strokeWidth="1.5"/>
      <path d="M12 11v5M12 8h.01" stroke="#9ca3af" strokeWidth="1.8" strokeLinecap="round"/>
    </svg>
  );
}
function SortArrows() {
  return (
    <svg width="9" height="9" viewBox="0 0 24 24" fill="none" style={{ display: "inline", marginLeft: 4 }}>
      <path d="M8 9l4-4 4 4M8 15l4 4 4-4" stroke="#9ca3af" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"/>
    </svg>
  );
}
function RocketIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none">
      <path d="M14 6c4 1 5 5 4 9-4 1-8 0-9-4l5-5z" stroke="#2563eb" strokeWidth="1.6" strokeLinejoin="round"/>
      <path d="M9 11l-3 1-1 4 4-1 1-3M14 11a1 1 0 100-2 1 1 0 000 2z" stroke="#2563eb" strokeWidth="1.6" strokeLinejoin="round"/>
    </svg>
  );
}
function BookmarkIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none">
      <path d="M7 4h10v17l-5-3-5 3V4z" stroke="#16a34a" strokeWidth="1.6" strokeLinejoin="round"/>
    </svg>
  );
}
function BulbIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none">
      <path d="M9 18h6M10 21h4M12 3a6 6 0 014 10.5V16H8v-2.5A6 6 0 0112 3z" stroke="#7c3aed" strokeWidth="1.6" strokeLinejoin="round" strokeLinecap="round"/>
    </svg>
  );
}
function DocIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none">
      <path d="M7 3h7l4 4v14H7V3z" stroke="#16a34a" strokeWidth="1.6" strokeLinejoin="round"/>
      <path d="M14 3v4h4M9 12h6M9 16h6" stroke="#16a34a" strokeWidth="1.6" strokeLinecap="round"/>
    </svg>
  );
}
function PeopleIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none">
      <circle cx="9" cy="8" r="3" stroke="#dc2626" strokeWidth="1.6"/>
      <circle cx="17" cy="10" r="2.5" stroke="#dc2626" strokeWidth="1.6"/>
      <path d="M3 19c0-3 3-5 6-5s6 2 6 5M14 19c0-2 2-3.5 4-3.5s4 1.5 4 3.5" stroke="#dc2626" strokeWidth="1.6" strokeLinecap="round"/>
    </svg>
  );
}
function ChatIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none">
      <path d="M4 5h16v11H8l-4 4V5z" stroke="#7c3aed" strokeWidth="1.6" strokeLinejoin="round"/>
    </svg>
  );
}
function StackIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none">
      <path d="M4 7l8-4 8 4-8 4-8-4z" stroke="#7c3aed" strokeWidth="1.6" strokeLinejoin="round"/>
      <path d="M4 12l8 4 8-4M4 17l8 4 8-4" stroke="#7c3aed" strokeWidth="1.6" strokeLinejoin="round"/>
    </svg>
  );
}
function MetricsIcon() {
  return (
    <svg width="12" height="12" viewBox="0 0 24 24" fill="none">
      <path d="M4 20V10M10 20V4M16 20v-8M22 20h-22" stroke="#374151" strokeWidth="1.8" strokeLinecap="round"/>
    </svg>
  );
}
function ExportIcon() {
  return (
    <svg width="12" height="12" viewBox="0 0 24 24" fill="none">
      <path d="M12 3v12M7 8l5-5 5 5M5 21h14" stroke="#2563eb" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"/>
    </svg>
  );
}
