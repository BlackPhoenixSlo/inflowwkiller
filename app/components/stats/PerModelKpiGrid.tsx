"use client";

/**
 * PerModelKpiGrid — one card per model account with the headline KPIs.
 *
 * Fields:
 *   • Total revenue (cleared + pending; chips break down by kind, with
 *                    a small "·$Xp" badge for the pending portion)
 *   • New subs        (DISTINCT fan_id with kind='subscription'; tracks
 *                      pending so a new sub shows the moment it fires
 *                      instead of waiting ~7d for bank-clear)
 *   • LTV             (= total / new_subs; null when new_subs = 0)
 *   • Active subs     (point-in-time; null when backend can't read it)
 *   • ARPU            (= total / active; null when no active count)
 *
 * Nulls render as "—" — see plan §11.3 (active_subs may be null when
 * fans.subscription_expires_at is also empty). A warning badge sits at
 * the top of EVERY card when `active_subs_source === "unavailable"` so
 * the user knows why every model in the grid shows "—" for active/ARPU.
 */

import { Badge, Card } from "@/components/ui/primitives";
import { cn } from "@/lib/utils";
import { fmtCents, fmtDateShort, fmtInt } from "@/lib/format";
import { proxyImage } from "@/lib/relay";
import { useAccounts } from "@/hooks/useAccounts";
import {
  useAccountProfile,
  usePerModel,
  type PerModelRow,
  type PerModelResp,
} from "@/hooks/useStats";

interface Props {
  from: string | null;
  to: string | null;
}

/** Paid/free comes from OF's own profile when available (subscribePrice
 *  > 0 ≡ paid, isFree ≡ free). Falls back to "any sub/rebill in window"
 *  while the /me call is in flight or if it failed. */
function isPaidPage(m: PerModelRow, profile: import("@/hooks/useStats").OfProfile | null | undefined): boolean {
  if (profile) {
    if (typeof profile.subscribePrice === "number") return profile.subscribePrice > 0;
    if (typeof profile.isFree === "boolean") return !profile.isFree;
  }
  const k = m.revenue_by_kind;
  const p = m.pending_by_kind;
  return (k.subscription + k.rebill + p.subscription + p.rebill) > 0;
}

export default function PerModelKpiGrid({ from, to }: Props) {
  const q = usePerModel(from, to);
  const accountsQ = useAccounts();

  if (q.isError) {
    return (
      <div className="text-sm text-err">
        {(q.error as Error)?.message || "Failed to load per-model stats"}
      </div>
    );
  }

  const data: PerModelResp | undefined = q.data;
  const models = data?.per_model ?? [];
  const activeUnavail = data?.active_subs_source === "unavailable";

  // Cold-start path: paint a skeleton grid sized by useAccounts (which is
  // cached `staleTime: 3d`, so it lands well before /per-model). One empty
  // <ModelCard> placeholder per known account — the page renders <300ms
  // instead of waiting for the per-model query to settle.
  if (q.isPending) {
    const skeletons = accountsQ.data?.accounts ?? [];
    const count = skeletons.length || 3;
    return (
      <section className="space-y-3">
        <header className="flex items-center justify-between">
          <h2 className="text-sm font-medium text-fg">Per-model KPIs</h2>
          <span className="text-xs text-fg-dim">loading…</span>
        </header>
        <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3">
          {Array.from({ length: count }).map((_, i) => (
            <SkeletonModelCard
              key={skeletons[i]?.id ?? `skeleton:${i}`}
              account={skeletons[i] ?? null}
            />
          ))}
        </div>
      </section>
    );
  }

  if (models.length === 0) {
    return (
      <Card>
        <div className="text-sm text-fg-dim text-center py-6">
          No model activity in this window.
        </div>
      </Card>
    );
  }

  return (
    <section className="space-y-3">
      <header className="flex items-center justify-between">
        <h2 className="text-sm font-medium text-fg">Per-model KPIs</h2>
        <span className="text-xs text-fg-dim">
          {models.length} model{models.length === 1 ? "" : "s"}
        </span>
      </header>
      <div
        className={cn(
          "grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3 transition-opacity",
          q.isFetching && q.isPlaceholderData && "opacity-60",
        )}
      >
        {models.map((m) => (
          <ModelCard key={m.account_id} m={m} activeUnavail={activeUnavail} />
        ))}
      </div>
    </section>
  );
}

function SkeletonModelCard({ account }: { account: { id: string; nickname?: string | null } | null }) {
  const initial = account?.nickname?.trim()?.[0]?.toUpperCase() ?? "?";
  return (
    <Card className="p-4 space-y-3">
      <div className="flex items-start gap-3">
        <div
          aria-hidden
          className="w-10 h-10 rounded-full bg-bg-elev-1 border border-border grid place-items-center text-sm font-medium text-fg-dim shrink-0"
        >
          {initial}
        </div>
        <div className="min-w-0 flex-1">
          <div className="text-sm font-medium text-fg truncate">
            {account?.nickname || account?.id || "—"}
          </div>
          <div className="text-[10px] text-muted font-mono truncate">
            {account?.id ?? "—"}
          </div>
        </div>
      </div>
      <div>
        <div className="text-2xl font-semibold tabular-nums text-fg-dim">—</div>
        <div className="text-[11px] text-fg-dim mt-0.5">total revenue</div>
      </div>
      <dl className="grid grid-cols-2 gap-x-4 gap-y-2 pt-2 border-t border-border text-sm">
        <Kpi label="New subs"    value="—" muted />
        <Kpi label="Active subs" value="—" muted />
        <Kpi label="LTV"         value="—" muted />
        <Kpi label="ARPU"        value="—" muted />
        <Kpi label="Messages"    value="—" muted />
        <Kpi label="PPVs sold"   value="—" muted />
      </dl>
    </Card>
  );
}

function ModelCard({ m, activeUnavail }: { m: PerModelRow; activeUnavail: boolean }) {
  const kinds = m.revenue_by_kind;
  const pending = m.pending_by_kind;
  // Pull the model's OF profile (avatar + name) per card. React Query
  // dedupes by accountId so re-mounts don't re-fetch. Falls back to the
  // backend's `display_name` (accounts.nickname) and then the bare ID.
  const profile = useAccountProfile(m.account_id);
  const p = profile.data;
  // Hold the paid/free badge indeterminate until the OF profile query has
  // settled — otherwise the revenue-window fallback in isPaidPage paints a
  // guess that can flip once the real subscribePrice/isFree lands in.
  const paid = profile.isPending ? null : isPaidPage(m, p);
  const avatarSrc = proxyImage(
    p?.avatarThumbs?.c144 || p?.avatarThumbs?.c50 || p?.avatar || null,
    m.account_id,
  );
  const headline = p?.name || p?.username || m.display_name || m.account_id;
  const handle = p?.username ? `@${p.username}` : null;

  return (
    <Card className="p-4 space-y-3">
      <div className="flex items-start gap-3">
        <ModelAvatar src={avatarSrc} alt={headline} />
        <div className="min-w-0 flex-1">
          <div className="text-sm font-medium text-fg truncate flex items-center gap-2">
            <span className="truncate">{headline}</span>
            <Badge
              color={paid == null ? "muted" : paid ? "ok" : "muted"}
              title={
                paid == null
                  ? "Loading OF profile to determine paid/free…"
                  : p && typeof p.subscribePrice === "number"
                  ? (paid
                      ? `OF subscription price = $${p.subscribePrice.toFixed(2)} (paid page)`
                      : "OF profile reports subscription price = $0 (free page)")
                  : (paid
                      ? "Account has subscription revenue in window (paid)"
                      : "No subscription revenue in window — likely free")
              }
            >
              {paid == null ? "…" : paid ? "paid" : "free"}
            </Badge>
          </div>
          <div className="text-[10px] text-muted font-mono truncate">
            {handle ? <>{handle} · </> : null}{m.account_id}
          </div>
        </div>
        {activeUnavail && (
          <Badge color="warn" title="Backend couldn't read active subscription state — active subs and ARPU are not available">
            subs unavailable
          </Badge>
        )}
      </div>

      <div>
        <div className="text-2xl font-semibold tabular-nums text-fg">
          {fmtCents(m.total_revenue_cents)}
        </div>
        {m.pending_revenue_cents > 0 && (
          <div className="text-xs text-fg-dim mt-0.5 tabular-nums">
            incl. <span className="text-warn">{fmtCents(m.pending_revenue_cents)} pending</span>
            {m.pending_clears_by && ` · clears by ${fmtDateShort(m.pending_clears_by)}`}
          </div>
        )}
        <div className="text-[11px] text-fg-dim mt-0.5">total revenue</div>
      </div>

      {/* Revenue-by-kind chips. Hide kinds with zero on BOTH cleared and
          pending so the row stays terse. Pending appears as a dim "+$X"
          suffix when present — lets you see un-cleared revenue per kind. */}
      <div className="flex flex-wrap gap-1.5">
        <KindChip label="PPV"    cents={kinds.ppv}          pending={pending.ppv} />
        <KindChip label="Tip"    cents={kinds.tip}          pending={pending.tip} />
        <KindChip label="Sub"    cents={kinds.subscription} pending={pending.subscription} />
        <KindChip label="Rebill" cents={kinds.rebill}       pending={pending.rebill} />
        <KindChip label="Other"  cents={kinds.custom}       pending={pending.custom} />
      </div>

      <dl className="grid grid-cols-2 gap-x-4 gap-y-2 pt-2 border-t border-border text-sm">
        <Kpi label="New subs"    value={fmtInt(m.new_subs_count)} />
        <Kpi label="Active subs" value={fmtInt(m.active_subs_count)} muted={m.active_subs_count == null} />
        <Kpi label="LTV"         value={fmtCents(m.ltv_cents)}     muted={m.ltv_cents == null} />
        <Kpi label="ARPU"        value={fmtCents(m.arpu_cents)}    muted={m.arpu_cents == null} />
        <Kpi label="Messages"    value={fmtInt(m.messages_sent)} />
        <Kpi label="PPVs sold"   value={fmtInt(m.ppv_conversions)} />
      </dl>
    </Card>
  );
}

function ModelAvatar({ src, alt }: { src: string; alt: string }) {
  // 40px circle; falls back to an initial-letter chip when the OF /me
  // call hasn't resolved (or failed). `proxyImage` returns "" when src
  // is empty so the <img> never tries to load a bogus URL.
  const initial = alt && alt.trim() ? alt.trim()[0]?.toUpperCase() : "?";
  if (!src) {
    return (
      <div
        aria-hidden
        className="w-10 h-10 rounded-full bg-bg-elev-1 border border-border grid place-items-center text-sm font-medium text-fg-dim shrink-0"
      >
        {initial}
      </div>
    );
  }
  return (
    // eslint-disable-next-line @next/next/no-img-element
    <img
      src={src}
      alt={alt}
      className="w-10 h-10 rounded-full object-cover shrink-0 bg-bg-elev-1"
      loading="lazy"
      onError={(e) => {
        // OF avatar URLs are signed and rotate. If a stale cache hit
        // 403s, drop the broken image so the layout doesn't get a
        // browser-default "missing" glyph.
        (e.currentTarget as HTMLImageElement).style.visibility = "hidden";
      }}
    />
  );
}

function KindChip({ label, cents, pending }: { label: string; cents: number; pending: number }) {
  if (!cents && !pending) return null;
  const cleared = cents - pending;
  return (
    <span
      className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[11px] border border-border bg-bg-elev-1 text-fg-dim"
      title={pending > 0 ? `${fmtCents(cents)} total — ${fmtCents(cleared)} cleared, ${fmtCents(pending)} pending` : undefined}
    >
      <span className="text-fg-dim">{label}</span>
      <span className="tabular-nums text-fg">{fmtCents(cents)}</span>
      {pending > 0 && (
        <span className="tabular-nums text-warn" title={`${fmtCents(pending)} still pending`}>·{fmtCents(pending)}p</span>
      )}
    </span>
  );
}

function Kpi({ label, value, muted }: { label: string; value: string; muted?: boolean }) {
  return (
    <div>
      <dt className="text-[11px] uppercase tracking-wide text-fg-dim">{label}</dt>
      <dd className={cn("text-sm font-medium tabular-nums mt-0.5", muted ? "text-fg-dim" : "text-fg")}>
        {value}
      </dd>
    </div>
  );
}
