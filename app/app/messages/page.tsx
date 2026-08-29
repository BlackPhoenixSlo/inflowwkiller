"use client";

/**
 * /messages — the "Stuff" dashboard: the money surfaces in one place.
 *
 * The ROUTE stays /messages while the LABEL is Stuff. Renaming the path would
 * break every ?tab= / ?fan_id= deep link the stats page, the per-fan cards, the
 * assistant's click paths and operators' bookmarks already point at, and buys
 * nothing — the address bar is not the thing anyone reads.
 *
 * The tabs:
 *   • PPV  — outbound priced messages, list view with infinite scroll.
 *            Reads /admin/paid-messages (messages table only, no tx join).
 *   • Tips — tip transactions list. Reads /admin/tips-list directly off
 *            the transactions table (no view dependency — synthesis §2.9).
 *   • All  — all messages (inbound + outbound, free + paid). Same endpoint
 *            as PPV with `type=all&direction=out` defaults. Owns CSV export.
 *   • Customs — the owed-customs queue: a fan tipped for a voice note and has
 *            not been sent one. Moved here from its own nav entry, because it
 *            is money already TAKEN and belongs beside PPV and Tips rather
 *            than competing with them for a slot in the strip. Live and
 *            cross-account: it ignores the page's date range, since a debt
 *            does not stop being owed because it fell out of a 30-day
 *            window. `/customs` redirects in, so old links still land.
 *   • Posts / My Feed / Top Posts — the feed side of the same window.
 *
 * URL state: `?tab=ppv|tips|all|customs|posts|myfeed|top` + `?fan_id=<n>` —
 * the latter deep-links the All tab to a single fan (used from per-fan cards
 * and the stats page).
 *
 * Right rail: TopTippersCard, visible on every tab. Clicking a row sets
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
import CustomsTab from "@/components/messages/CustomsTab";
import MyFeedTab from "@/components/messages/MyFeedTab";
import PaidMessagesTab from "@/components/messages/PaidMessagesTab";
import PostsTab from "@/components/messages/PostsTab";
import TipsTab from "@/components/messages/TipsTab";
import TopPostsTab from "@/components/messages/TopPostsTab";
import TopTippersCard from "@/components/messages/TopTippersCard";
import { cn } from "@/lib/utils";
import { daysAgoISO, todayISO, toLocalIso } from "@/lib/dateRange";

type Tab = "ppv" | "tips" | "all" | "posts" | "myfeed" | "top" | "customs";

/** The strip, in order. Also the parser's allow-list and the table that
 *  components/assistant/answerLinks.ts copies — its drift guard reads the
 *  `key:` literals out of this file, so a renamed tab fails that suite
 *  instead of quietly sending a help-bot reader to the default tab. */
const TABS: Array<{ key: Tab; label: string }> = [
  { key: "ppv",     label: "PPV"       },
  { key: "tips",    label: "Tips"      },
  { key: "all",     label: "All"       },
  { key: "customs", label: "Customs"   },
  { key: "posts",   label: "Posts"     },
  { key: "myfeed",  label: "My Feed"   },
  { key: "top",     label: "Top Posts" },
];

const TAB_KEYS = new Set<string>(TABS.map((t) => t.key));

function parseTab(raw: string | null): Tab {
  return raw != null && TAB_KEYS.has(raw) ? (raw as Tab) : "ppv";
}

export default function MessagesPage() {
  // useSearchParams suspends in Next 14+; wrap the body in Suspense so the
  // page can render the static header while it resolves.
  return (
    <Suspense fallback={<div className="max-w-shell mx-auto p-3 sm:p-6 text-sm text-fg-dim">Loading…</div>}>
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
    <div className="max-w-shell mx-auto p-3 sm:p-6 space-y-5">
      <header className="flex flex-wrap items-end gap-4 justify-between">
        <div>
          <h1 className="text-2xl font-semibold mb-1">Stuff</h1>
          <p className="text-sm text-fg-dim">
            Paid messages (PPV), tips, posts and customs owed.
          </p>
        </div>
        <DateRangePicker from={from} to={to} preset={preset} onChange={handleRange} />
      </header>

      <div className="flex items-center gap-1 border-b border-border overflow-x-auto overflow-y-hidden md:overflow-visible no-scrollbar -mx-3 px-3 sm:mx-0 sm:px-0">
        {TABS.map((t) => (
          <TabButton key={t.key} active={tab === t.key} onClick={() => setTab(t.key)}>
            {t.label}
          </TabButton>
        ))}
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
          {tab === "customs" && <CustomsTab />}
          {tab === "posts" && <PostsTab />}
          {tab === "myfeed" && <MyFeedTab />}
          {tab === "top" && <TopPostsTab from={fromIso} to={toIso} />}
        </div>
        <aside className="space-y-3 order-first md:order-none">
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
        "px-4 py-2 text-sm font-medium -mb-px border-b-2 transition-colors shrink-0 whitespace-nowrap",
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
