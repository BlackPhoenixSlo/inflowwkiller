"use client";

/**
 * OverviewTab — Growth → "Overview".
 *
 * One glance at everything Growth is doing for this account: the two
 * growth-owned automations (auto_follow, promo_reactivate) with their live
 * state + last run, per-surface stat tiles (segments, links, campaigns), and
 * a footer list of every OTHER enabled automation so "what is running right
 * now?" has a single answer. Read-only — every card jumps to the tab (or
 * /automations) that owns the knobs.
 */

import Link from "next/link";

import { RunStats, isDryRun } from "@/components/growth/_bits";
import { Badge, Button, Card } from "@/components/ui/primitives";
import {
  useAutomationRules, type AutomationRule,
} from "@/hooks/useAutomations";
import { usePromotions, useTrackingLinks, useTrialLinks } from "@/hooks/useGrowth";
import { useSmartLists } from "@/hooks/useSmartLists";

/** The five feature tabs the overview can jump to (page.tsx adds "overview"). */
export type GrowthTab = "smart" | "trial" | "tracking" | "promotion" | "auto_follow";

const GROWTH_KINDS = new Set(["auto_follow", "promo_reactivate"]);

function fmtCadence(rule: AutomationRule): string {
  const daily = rule.trigger?.daily_at;
  if (daily && daily.length > 0) return `daily at ${daily.join(", ")}`;
  const s = rule.every_seconds ?? rule.trigger?.every_seconds;
  if (!s) return "no schedule";
  if (s % 3600 === 0) return `every ${s / 3600}h`;
  return `every ${Math.round(s / 60)}m`;
}

function fmtWhen(iso: string | null | undefined): string | null {
  if (!iso) return null;
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? null : d.toLocaleString();
}

/** LIVE / DRY RUN / OFF / not set up — dry_run defaults TRUE on both kinds. */
function ruleState(rule: AutomationRule | null): {
  label: string; color: "ok" | "warn" | "muted";
} {
  if (!rule) return { label: "not set up", color: "muted" };
  if (!rule.is_enabled) return { label: "OFF", color: "muted" };
  return isDryRun(rule)
    ? { label: "DRY RUN", color: "warn" }
    : { label: "LIVE", color: "ok" };
}

function AutomationCard({
  title, blurb, rule, extra, onOpen,
}: {
  title: string;
  blurb: string;
  rule: AutomationRule | null;
  /** Extra context under the blurb (e.g. "3 campaigns armed"). */
  extra?: string | null;
  onOpen: () => void;
}) {
  const state = ruleState(rule);
  const last = rule?.last_run;
  const nextDue = fmtWhen(rule?.next_due_at);

  return (
    <Card className="p-4 space-y-2">
      <div className="flex items-start justify-between gap-3">
        <div>
          <h3 className="text-sm font-semibold text-fg flex items-center gap-2">
            {title} <Badge color={state.color}>{state.label}</Badge>
          </h3>
          <p className="text-xs text-fg-dim mt-0.5">{blurb}</p>
          {extra && <p className="text-xs text-fg mt-0.5">{extra}</p>}
        </div>
        <Button size="sm" variant="ghost" onClick={onOpen}>Open →</Button>
      </div>

      <div className="text-xs text-fg-dim border-t border-border pt-2">
        {rule ? (
          <>
            <span>{fmtCadence(rule)}</span>
            {last && (
              <>
                <span> · last run <span className="text-fg">{last.status}</span></span>
                <RunStats stats={last.stats} />
                {fmtWhen(last.started_at) && <span> · {fmtWhen(last.started_at)}</span>}
                {last.error_text && <span className="text-err"> · {last.error_text}</span>}
              </>
            )}
            {!last && <span> · never run</span>}
            {rule.is_enabled && nextDue && <span> · next {nextDue}</span>}
          </>
        ) : (
          <span>Nothing configured yet — open the tab to set it up.</span>
        )}
      </div>
    </Card>
  );
}

function StatTile({
  label, value, sub, onOpen,
}: { label: string; value: string; sub?: string | null; onOpen: () => void }) {
  return (
    <button
      type="button"
      onClick={onOpen}
      className="text-left bg-panel border border-border rounded-2xl p-4 hover:border-accent transition-colors"
    >
      <div className="text-[11px] uppercase tracking-wide text-fg-dim">{label}</div>
      <div className="text-xl font-semibold text-fg mt-1">{value}</div>
      {sub && <div className="text-xs text-fg-dim mt-0.5">{sub}</div>}
    </button>
  );
}

export default function OverviewTab({
  accountId, onOpenTab,
}: { accountId: string | null; onOpenTab: (tab: GrowthTab) => void }) {
  const rulesQ = useAutomationRules(accountId);
  const promosQ = usePromotions(accountId);
  const trialQ = useTrialLinks(accountId);
  const trackingQ = useTrackingLinks(accountId);
  const smartQ = useSmartLists(accountId);

  const rules = rulesQ.data ?? [];
  const autoFollow = rules.find((r) => r.kind === "auto_follow") ?? null;
  const promoRearm = rules.find((r) => r.kind === "promo_reactivate") ?? null;
  // Every enabled rule Growth does NOT own — the "what else is running" list.
  const otherLive = rules.filter((r) => r.is_enabled && !GROWTH_KINDS.has(r.kind));

  const campaigns = promosQ.data?.campaigns ?? [];
  const promosActive = campaigns.filter((c) => c.status === "active").length;
  const promosArmed = campaigns.filter((c) => c.auto_reactivate).length;
  const reArms = campaigns.reduce((n, c) => n + (c.reactivate_count || 0), 0);

  const trialLinks = trialQ.data?.links ?? [];
  const trialLive = trialLinks.filter((l) => l.status === "active").length;
  const trialClaims = trialLinks.reduce((n, l) => n + (l.claim_counts || 0), 0);

  const trackingLinks = trackingQ.data ?? [];
  const trackingClicks = trackingLinks.reduce((n, l) => n + (l.click_count || 0), 0);

  const smartLists = smartQ.data ?? [];

  const loading =
    rulesQ.isLoading || promosQ.isLoading || trialQ.isLoading ||
    trackingQ.isLoading || smartQ.isLoading;

  return (
    <div className="space-y-5">
      <header>
        <h2 className="text-lg font-semibold">Overview</h2>
        <p className="text-sm text-fg-dim">
          Everything Growth is running for this account — automation state, last
          runs, and per-surface totals. Click anything to open its tab.
        </p>
      </header>

      {loading && <div className="text-sm text-fg-dim">Loading…</div>}

      <section className="space-y-3">
        <h3 className="text-[11px] uppercase tracking-wide text-fg-dim">Growth automations</h3>
        <div className="grid gap-3 lg:grid-cols-2">
          <AutomationCard
            title="❤️ Auto-follow / Auto-like"
            blurb="Likes fans’ recent messages or follows them back to trigger OnlyFans re-engagement."
            rule={autoFollow}
            onOpen={() => onOpenTab("auto_follow")}
          />
          <AutomationCard
            title="♻️ Keep promos running"
            blurb="Re-creates finished campaigns marked “Keep running”."
            rule={promoRearm}
            extra={
              promosArmed > 0
                ? `${promosArmed} campaign${promosArmed === 1 ? "" : "s"} armed` +
                  (reArms > 0 ? ` · re-armed ${reArms}× so far` : "")
                : "No campaign is armed yet."
            }
            onOpen={() => onOpenTab("promotion")}
          />
        </div>
      </section>

      <section className="space-y-3">
        <h3 className="text-[11px] uppercase tracking-wide text-fg-dim">At a glance</h3>
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <StatTile
            label="🎯 Smart Lists"
            value={String(smartLists.length)}
            sub={smartLists.length === 1 ? "saved segment" : "saved segments"}
            onOpen={() => onOpenTab("smart")}
          />
          <StatTile
            label="🎟️ Trial links"
            value={String(trialLive)}
            sub={`live · ${trialClaims} claim${trialClaims === 1 ? "" : "s"} total`}
            onOpen={() => onOpenTab("trial")}
          />
          <StatTile
            label="🔗 Tracking links"
            value={String(trackingLinks.length)}
            sub={`${trackingClicks} click${trackingClicks === 1 ? "" : "s"} total`}
            onOpen={() => onOpenTab("tracking")}
          />
          <StatTile
            label="📣 Promotions"
            value={String(promosActive)}
            sub={`active · ${promosArmed} armed to re-run`}
            onOpen={() => onOpenTab("promotion")}
          />
        </div>
      </section>

      <section className="space-y-3">
        <h3 className="text-[11px] uppercase tracking-wide text-fg-dim">
          Other automations on this account
        </h3>
        {otherLive.length === 0 ? (
          <p className="text-sm text-fg-dim">
            No other automation is enabled.{" "}
            <Link href="/automations" className="text-accent hover:underline">
              Open Automations →
            </Link>
          </p>
        ) : (
          <Card className="p-0 overflow-hidden">
            <ul className="divide-y divide-border">
              {otherLive.map((r) => (
                <li key={r.id} className="flex items-center justify-between gap-3 px-4 py-2.5 text-sm">
                  <div className="min-w-0">
                    <span className="text-fg">{r.name || r.kind}</span>
                    <span className="text-fg-dim text-xs"> · {r.kind} · {fmtCadence(r)}</span>
                  </div>
                  <div className="shrink-0 text-xs text-fg-dim">
                    {r.last_run
                      ? <>last <span className={r.last_run.error_text ? "text-err" : "text-fg"}>{r.last_run.status}</span></>
                      : "never run"}
                  </div>
                </li>
              ))}
            </ul>
            <div className="px-4 py-2.5 border-t border-border text-xs">
              <Link href="/automations" className="text-accent hover:underline">
                Manage in Automations →
              </Link>
            </div>
          </Card>
        )}
      </section>
    </div>
  );
}
