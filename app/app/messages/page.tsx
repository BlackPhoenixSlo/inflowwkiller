"use client";

/**
 * /messages — paid-message dashboard.
 *
 * Three tabs:
 *   • PPV  — outbound priced messages, list view with infinite scroll.
 *            Reads /admin/paid-messages (messages table only, no tx join).
 *   • Tips — tip transactions list. Reads /admin/tips-list directly off
 *            the transactions table (no view dependency — synthesis §2.9).
 *   • All  — all messages (inbound + outbound, free + paid). Same endpoint
 *            as PPV with `type=all&direction=out` defaults. Owns CSV export.
 *
 * URL state: `?tab=ppv|tips|all` + `?fan_id=<n>` — the latter deep-links
 * the All tab to a single fan (used from per-fan cards and the stats page).
 *
 * Right rail: TopTippersCard, visible on BOTH tabs. Clicking a row sets
 * `tippersPreset` which TipsTab adopts as its fan_query filter — and we
 * switch the active tab to Tips so the filtered list is visible right
 * away (a click from the PPV tab still means "show me this fan's tips").
 *
 * Date range owned at the page level (mirrors /stats). Defaults to last
 * 30 days. The picker is imported from components/stats/ — one extra
 * consumer doesn't justify lifting it (synthesis §5.5).
 */

import { Suspense, useCallback, useEffect, useMemo, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";

import DateRangePicker, { type RangePreset } from "@/components/stats/DateRangePicker";
import AllMessagesTab from "@/components/messages/AllMessagesTab";
import PaidMessagesTab from "@/components/messages/PaidMessagesTab";
import TipsTab from "@/components/messages/TipsTab";
import TopTippersCard from "@/components/messages/TopTippersCard";
import { cn } from "@/lib/utils";
import { daysAgoISO, todayISO, toLocalIso } from "@/lib/dateRange";

type Tab = "ppv" | "tips" | "all";

function parseTab(raw: string | null): Tab {
  if (raw === "tips" || raw === "all") return raw;
  return "ppv";
}

export default function MessagesPage() {
  // useSearchParams suspends in Next 14+; wrap the body in Suspense so the
  // page can render the static header while it resolves.
  return (
    <Suspense fallback={<div className="max-w-7xl mx-auto p-3 sm:p-6 text-sm text-fg-dim">Loading…</div>}>
      <MessagesPageInner />
    </Suspense>
  );
}

function MessagesPageInner() {
  const router = useRouter();
  const search = useSearchParams();

  const [from, setFrom] = useState<string | null>(daysAgoISO(30));
  const [to, setTo] = useState<string | null>(todayISO());
  const [preset, setPreset] = useState<RangePreset>("30d");
  const [tab, setTabState] = useState<Tab>(() => parseTab(search.get("tab")));
  const [fanId, setFanIdState] = useState<number | null>(() => {
    const raw = search.get("fan_id");
    const n = raw != null ? Number(raw) : NaN;
    return Number.isFinite(n) ? n : null;
  });

  // Mirror URL → state when the user navigates via browser back/forward.
  // We intentionally don't reverse-sync state → URL on every keystroke;
  // only the explicit tab/fan setters push back to the URL so a typing
  // user doesn't see history entries pile up.
  useEffect(() => {
    setTabState(parseTab(search.get("tab")));
    const raw = search.get("fan_id");
    const n = raw != null ? Number(raw) : NaN;
    setFanIdState(Number.isFinite(n) ? n : null);
  }, [search]);

  const updateUrl = useCallback(
    (next: { tab?: Tab; fanId?: number | null }) => {
      const qs = new URLSearchParams(search.toString());
      if (next.tab !== undefined) {
        if (next.tab === "ppv") qs.delete("tab");
        else qs.set("tab", next.tab);
      }
      if (next.fanId !== undefined) {
        if (next.fanId == null) qs.delete("fan_id");
        else qs.set("fan_id", String(next.fanId));
      }
      const qsStr = qs.toString();
      router.replace(qsStr ? `/messages?${qsStr}` : "/messages", { scroll: false });
    },
    [router, search],
  );

  const setTab = (t: Tab) => {
    setTabState(t);
    updateUrl({ tab: t });
  };
  const clearFanId = () => {
    setFanIdState(null);
    updateUrl({ fanId: null });
  };

  // Object-wrapped so re-setting the same username still bumps the
  // useEffect inside TipsTab. `value` is the username; `nonce` ensures
  // a fresh reference even when the user clicks the same row twice.
  const [tippersPreset, setTippersPreset] = useState<{
    value: string;
    nonce: number;
  } | null>(null);

  const handleRange = (f: string | null, t: string | null, p: RangePreset) => {
    setFrom(f);
    setTo(t);
    setPreset(p);
  };

  const fromIso = toLocalIso(from, false);
  const toIso = toLocalIso(to, true);

  const onSelectFan = (username: string) => {
    setTab("tips");
    setTippersPreset((prev) => ({
      value: username,
      nonce: (prev?.nonce ?? 0) + 1,
    }));
  };

  // Memoized so AllMessagesTab's `useMemo(params, …)` keeps a stable
  // reference when neither fanId nor the clearer changed.
  const onClearFanId = useMemo(() => () => clearFanId(), [clearFanId]);

  // The window label feeds TopTippersCard; matches the date-range preset
  // wording the user picked so the leaderboard doesn't lie about its
  // window when the picker shows "Last 30 days."
  const windowLabel = preset === "custom"
    ? from && to ? `${from} → ${to}` : "custom window"
    : presetLabel(preset);

  return (
    <div className="max-w-7xl mx-auto p-3 sm:p-6 space-y-5">
      <header className="flex flex-wrap items-end gap-4 justify-between">
        <div>
          <h1 className="text-2xl font-semibold mb-1">Messages</h1>
          <p className="text-sm text-fg-dim">
            Paid messages (PPV) and tips across the selected window.
          </p>
        </div>
        <DateRangePicker from={from} to={to} preset={preset} onChange={handleRange} />
      </header>

      <div className="flex items-center gap-1 border-b border-border">
        <TabButton active={tab === "ppv"} onClick={() => setTab("ppv")}>
          PPV
        </TabButton>
        <TabButton active={tab === "tips"} onClick={() => setTab("tips")}>
          Tips
        </TabButton>
        <TabButton active={tab === "all"} onClick={() => setTab("all")}>
          All
        </TabButton>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-[1fr_320px] gap-5">
        <div className="min-w-0">
          {tab === "ppv" && <PaidMessagesTab from={fromIso} to={toIso} />}
          {tab === "tips" && (
            <TipsTab
              from={fromIso}
              to={toIso}
              presetFanQuery={tippersPreset?.value ?? null}
              // The nonce isn't a prop on TipsTab itself; bumping the
              // key forces a remount on duplicate clicks so the preset
              // useEffect re-runs even when the same username is picked.
              key={`tips:${tippersPreset?.nonce ?? 0}`}
            />
          )}
          {tab === "all" && (
            <AllMessagesTab
              from={fromIso}
              to={toIso}
              fanId={fanId}
              onClearFanId={onClearFanId}
            />
          )}
        </div>
        <aside className="space-y-3">
          <TopTippersCard
            from={fromIso}
            to={toIso}
            onSelectFan={onSelectFan}
            windowLabel={windowLabel}
          />
        </aside>
      </div>
    </div>
  );
}

function presetLabel(p: RangePreset): string {
  switch (p) {
    case "7d":
      return "last 7 days";
    case "30d":
      return "last 30 days";
    case "custom":
      return "custom";
  }
}

function TabButton({
  active,
  disabled,
  onClick,
  title,
  children,
}: {
  active: boolean;
  disabled?: boolean;
  onClick: () => void;
  title?: string;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={disabled ? undefined : onClick}
      disabled={disabled}
      title={title}
      className={cn(
        "px-4 py-2 text-sm font-medium -mb-px border-b-2 transition-colors",
        active
          ? "border-accent text-fg"
          : "border-transparent text-fg-dim hover:text-fg",
        disabled && "opacity-50 cursor-not-allowed hover:text-fg-dim",
      )}
    >
      {children}
    </button>
  );
}
