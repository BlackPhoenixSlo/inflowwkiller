"use client";

/**
 * /growth — the Growth control surface (Infloww-gap features).
 *
 * Owner-facing. Six sub-tabs share one account selector:
 *   Smart Lists · Trial Links · Tracking Links · Promotion · Auto-follow · Overview
 * Growth owns its own automations: promo re-arm sits on the Promotion tab with
 * the campaigns it acts on, auto-follow has its own tab. Neither is in
 * /automations. Overview is the read-only rollup of all of it.
 */

import { useEffect, useState } from "react";

import { cn } from "@/lib/utils";
import { useActiveAccounts } from "@/hooks/useAccounts";
import { useTabParam } from "@/hooks/useTabParam";
import SmartListsTab from "@/components/growth/SmartListsTab";
import TrialLinksTab from "@/components/growth/TrialLinksTab";
import TrackingLinksTab from "@/components/growth/TrackingLinksTab";
import PromotionTab from "@/components/growth/PromotionTab";
import AutoFollowTab from "@/components/growth/AutoFollowTab";
import OverviewTab, { type GrowthTab } from "@/components/growth/OverviewTab";

type Tab = GrowthTab | "overview";
const TABS: Array<{ key: Tab; label: string }> = [
  { key: "smart", label: "🎯 Smart Lists" },
  { key: "trial", label: "🎟️ Trial Links" },
  { key: "tracking", label: "🔗 Tracking Links" },
  { key: "promotion", label: "📣 Promotion" },
  { key: "auto_follow", label: "❤️ Auto-follow" },
  { key: "overview", label: "📊 Overview" },
];

const isTab = (v: string): v is Tab => TABS.some((t) => t.key === v);

export default function GrowthPage() {
  const accounts = useActiveAccounts();
  const [accountId, setAccountId] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab>("smart");
  // Deep link from the help assistant: /growth?tab=auto_follow.
  useTabParam("tab", isTab, setTab);

  useEffect(() => {
    if (!accountId && accounts.length > 0) setAccountId(accounts[0].id);
  }, [accounts, accountId]);

  return (
    <div className="max-w-shell mx-auto p-3 sm:p-6 space-y-5">
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold mb-1">Growth</h1>
          <p className="text-sm text-fg-dim">
            Saved fan segments, free-trial &amp; tracking links, and profile-promo
            campaigns — the acquisition &amp; targeting toolkit.
          </p>
        </div>
        {accounts.length > 1 && (
          <label className="flex items-center gap-2 text-sm">
            <span className="text-fg-dim">Account</span>
            <select
              className="bg-bg border border-border rounded-lg px-3 py-2 text-sm focus:outline-none focus:border-accent"
              value={accountId ?? ""}
              onChange={(e) => setAccountId(e.target.value)}
            >
              {accounts.map((a) => (
                <option key={a.id} value={a.id}>{a.nickname || a.id}</option>
              ))}
            </select>
          </label>
        )}
      </header>

      <div className="flex items-center gap-1 border-b border-border overflow-x-auto no-scrollbar">
        {TABS.map((t) => (
          <button
            key={t.key}
            type="button"
            onClick={() => setTab(t.key)}
            className={cn(
              "shrink-0 whitespace-nowrap px-3 py-2 text-sm rounded-t-md border-b-2 -mb-px transition-colors",
              tab === t.key
                ? "border-accent text-fg font-medium"
                : "border-transparent text-fg-dim hover:text-fg",
            )}
          >
            {t.label}
          </button>
        ))}
      </div>

      {!accountId ? (
        <div className="text-sm text-fg-dim">No account connected yet.</div>
      ) : (
        <>
          {tab === "smart" && <SmartListsTab accountId={accountId} />}
          {tab === "trial" && <TrialLinksTab accountId={accountId} />}
          {tab === "tracking" && <TrackingLinksTab accountId={accountId} />}
          {tab === "promotion" && <PromotionTab accountId={accountId} />}
          {tab === "auto_follow" && <AutoFollowTab accountId={accountId} />}
          {tab === "overview" && <OverviewTab accountId={accountId} onOpenTab={setTab} />}
        </>
      )}
    </div>
  );
}
