"use client";

/**
 * VaultAiTab — Automations → "🧠 Vault AI".
 *
 * Operator surface for `account_ai_config.vault_ai_config_json`. v2 is built
 * around the PPV WEEK/MONTH ARC (service/vault_ppv_week.py): a story-shaped week
 * (soft → payoff, copy references yesterday) assembled from already-derived vault
 * content and repeated as exclusive waves across the month, every drop hitting
 * the feed AND DMs. The old describe-cadence / folder-taxonomy / daily-reminder
 * knobs were retired from this surface — describe still runs to DERIVE content,
 * it just isn't tuned here.
 *
 * Writes are PATCH-partial (server deep-merges). Suggest-only is a HARD contract:
 * the operator generates a preview and confirms — nothing here arms or sends.
 */

import { useEffect, useMemo, useState } from "react";
import { CalendarClock, Sparkles, Wand2 } from "lucide-react";

import { Button, Card } from "@/components/ui/primitives";
import {
  useVaultAiConfig,
  useSaveVaultAiConfig,
  useGenerateVaultPpvWeek,
  useVaultArcStatus,
  useApproveVaultArc,
  useCancelVaultArc,
  type VaultAiConfig,
  type VaultAiTier,
} from "@/hooks/useVaultAiConfig";
import { usePaidPage, PAID_PAGE_NOTE } from "@/hooks/usePaidPage";
import { cn } from "@/lib/utils";
import StickySaveBar, {
  SaveRow, type SaveControl,
} from "@/components/settings/StickySaveBar";

const INPUT_BASE =
  "bg-bg border border-border rounded-lg px-3 py-2 focus:outline-none focus:border-accent";
const INPUT = `${INPUT_BASE} text-sm max-md:text-base`;

const TIER_ORDER: VaultAiTier[] = ["safe", "suggestive", "explicit", "graphic", "unknown"];

function centsToDollars(c: number): number {
  return Math.round(c) / 100;
}
function dollarsToCents(d: number): number {
  return Math.round(d * 100);
}
function money(cents: number): string {
  if (!cents) return "free";
  return `$${(cents / 100).toFixed(2).replace(/\.00$/, "")}`;
}

function when(iso: string): string {
  const d = new Date(iso);
  return d.toLocaleString(undefined, {
    weekday: "short", hour: "numeric", minute: "2-digit",
  });
}

const CHANNEL_LABEL: Record<string, string> = {
  mass_ppv: "💬💰 mass PPV + feed",
  feed_paid: "📌💰 paid post",
  feed_free: "📌 free post",
  mass_free: "💬 free DM",
};

export default function VaultAiTab({ accountId }: { accountId: string | null }) {
  const cfgQ = useVaultAiConfig(accountId);
  const saveM = useSaveVaultAiConfig(accountId);
  const genM = useGenerateVaultPpvWeek(accountId);
  const arcQ = useVaultArcStatus(accountId);
  const armM = useApproveVaultArc(accountId);
  const cancelM = useCancelVaultArc(accountId);
  // A subscription page has no paid-post lane on OF, so the arc's feed_paid
  // channels are dropped server-side (vault_ppv_week.plan_week) — the combine
  // toggle would promise a wall drop that can never happen.
  const { isPaidPage } = usePaidPage(accountId);

  const [enabled, setEnabled] = useState(false);
  const [bands, setBands] = useState<Record<VaultAiTier, [number, number]>>({
    safe: [300, 800],
    suggestive: [500, 1500],
    explicit: [1000, 3000],
    graphic: [2000, 6000],
    unknown: [500, 1500],
  });
  const [tierLabels, setTierLabels] = useState<Record<VaultAiTier, string>>({
    safe: "Safe",
    suggestive: "Suggestive",
    explicit: "Explicit",
    graphic: "Graphic",
    unknown: "Unclassified",
  });
  // PPV arc knobs
  const [arcEnabled, setArcEnabled] = useState(false);
  const [weeks, setWeeks] = useState(4);
  const [combine, setCombine] = useState(true);
  const [inVoice, setInVoice] = useState(true);

  const cfg = cfgQ.data?.config;

  useEffect(() => {
    if (!cfg) return;
    setEnabled(!!cfg.enabled);
    setBands({ ...cfg.pricing.bands_by_tier });
    setTierLabels({ ...cfg.tier_labels });
    const p = cfg.ppv_week;
    setArcEnabled(!!p.enabled);
    setWeeks(p.weeks || 4);
    setCombine(p.combine_feed_and_dm);
    setInVoice(p.in_voice_copy);
  }, [cfg]);

  const dirty = useMemo(() => {
    if (!cfg) return false;
    if (enabled !== cfg.enabled) return true;
    for (const t of TIER_ORDER) {
      const [a, b] = cfg.pricing.bands_by_tier[t] ?? [0, 0];
      const [x, y] = bands[t] ?? [0, 0];
      if (a !== x || b !== y) return true;
      if ((cfg.tier_labels[t] ?? "") !== (tierLabels[t] ?? "")) return true;
    }
    const p = cfg.ppv_week;
    if (arcEnabled !== p.enabled) return true;
    if (weeks !== p.weeks) return true;
    if (combine !== p.combine_feed_and_dm) return true;
    if (inVoice !== p.in_voice_copy) return true;
    return false;
  }, [cfg, enabled, bands, tierLabels, arcEnabled, weeks, combine, inVoice]);

  const markDirty = () => {
    if (saveM.isSuccess || saveM.isError) saveM.reset();
  };

  const setBand = (t: VaultAiTier, idx: 0 | 1, dollars: number) => {
    markDirty();
    setBands((prev) => {
      const [lo, hi] = prev[t];
      const cents = dollarsToCents(Math.max(0, dollars));
      const next: [number, number] = idx === 0 ? [cents, hi] : [lo, cents];
      return { ...prev, [t]: next };
    });
  };

  const setLabel = (t: VaultAiTier, v: string) => {
    markDirty();
    setTierLabels((prev) => ({ ...prev, [t]: v }));
  };

  const onSave = () => {
    const patch: Partial<VaultAiConfig> = {
      enabled,
      pricing: { enabled: cfg?.pricing.enabled ?? true, bands_by_tier: bands },
      tier_labels: tierLabels,
      ppv_week: {
        enabled: arcEnabled,
        weeks: Math.max(1, Math.min(weeks || 4, 4)),
        combine_feed_and_dm: combine,
        in_voice_copy: inVoice,
      },
    };
    saveM.mutate(patch);
  };

  const onGenerate = () => genM.mutate({ weeks, use_llm: inVoice, combine });

  // The single moment suggest-only is spent. The server rebuilds the plan from
  // the same config the preview used, so what was read is what gets armed — but
  // the operator still has to say the number of drops out loud first.
  const onArm = () => {
    const n = genM.data?.summary;
    const drops = n ? (n.dm_sends ?? 0) + (n.feed_posts ?? 0) : weeks * 7;
    const span = weeks === 1 ? "week" : `${weeks} weeks`;
    if (
      !window.confirm(
        `Arm this ${span}?\n\n` +
          `~${drops} real sends will go out on their own days — paid posts on the ` +
          `feed and PPV messages in DMs, at ${money(n?.price_low_cents ?? 0)}–` +
          `${money(n?.price_high_cents ?? 0)}.\n\n` +
          `Each drop fires once. Nothing regenerates when the arc ends.`,
      )
    ) return;
    armM.mutate({ weeks, use_llm: inVoice, combine });
  };

  const onStandDown = () => {
    if (!window.confirm("Cancel every drop that hasn't fired yet? Sent drops stay sent."))
      return;
    cancelM.mutate();
  };

  const arc = arcQ.data;
  const armError =
    armM.error?.message?.includes("ppv_library_master_off")
      ? "Turn the PPV Library master switch on first — with it off every priced drop would silently skip."
      : armM.error?.message?.includes("vault_ai_off")
        ? "Turn Vault AI on and Save first."
        : armM.error?.message;

  // Written into a same-origin about:blank rather than opened as a blob: URL —
  // the page's thumbnails are root-relative relay routes (/admin/vault-ai/thumb),
  // and a blob: document has no origin to resolve them against, so every tile
  // would come up empty in the popup even though the iframe above shows them.
  const openFull = () => {
    const html = genM.data?.html;
    if (!html) return;
    const w = window.open("", "_blank");
    if (!w) return;
    w.document.open();
    w.document.write(html);
    w.document.close();
  };

  if (!accountId) return <div className="text-sm text-fg-dim">Pick an account above.</div>;
  if (cfgQ.isLoading || !cfg) return <div className="text-sm text-fg-dim">Loading…</div>;

  const sm = genM.data?.summary;

  /** THE tab's save control, built once and rendered twice — in flow at the
   *  bottom of the config Card, and again in the pinned bar at the end of the
   *  tab. One object, so gate, label and feedback cannot drift apart.
   *
   *  `canSave` is `dirty`, which is ALSO this tab's load gate: `dirty` returns
   *  false while `cfg` is undefined, so a save can never post the empty tier
   *  bands over a config that never arrived. */
  const save: SaveControl = {
    onSave,
    saving: saveM.isPending,
    canSave: dirty,
    saved: saveM.isSuccess && !dirty,
    error: saveM.isError ? (saveM.error?.message || "Save failed") : null,
  };
  /** Shown by both controls, so neither reads as if it is waiting on a click. */
  const noChanges = !dirty && !saveM.isSuccess ? (
    <span className="text-[11px] text-fg-dim/70">No unsaved changes.</span>
  ) : null;

  return (
    <div className="space-y-4 max-w-4xl">
      <Card className="p-4 space-y-6">
        <header className="flex items-center gap-2">
          <Sparkles size={16} className="text-accent" />
          <h3 className="text-sm font-medium">Vault AI — the PPV week &amp; month (suggest-only)</h3>
        </header>

        <p className="text-xs text-fg-dim leading-relaxed">
          Vault AI turns your <b>already-derived</b> vault into a story-shaped selling
          week — soft on Monday, escalating each day, the payoff on Saturday, and the
          copy on every day refers back to the day before. Repeated as fresh waves across
          the month, every drop hitting the <b>feed and DMs</b>. Generate a preview,
          then confirm — <b>nothing sends until you approve it</b>.
        </p>

        <label className="flex items-center gap-3 cursor-pointer">
          <input
            type="checkbox"
            className="h-4 w-4 accent-[var(--accent)]"
            checked={enabled}
            onChange={(e) => { markDirty(); setEnabled(e.target.checked); }}
          />
          <span className="text-sm">{enabled ? "Vault AI ON" : "Vault AI OFF"}</span>
        </label>

        <div className="rounded-md border border-border/60 bg-bg-elev-1/50 px-3 py-2 text-[11px] text-fg-dim/80 leading-relaxed">
          <b>Suggest-only.</b> The arc is a proposal — every drop lands in the Review
          queue before it can be armed or sent.
        </div>

        <fieldset disabled={!enabled} className="space-y-6" style={{ opacity: enabled ? 1 : 0.55 }}>

          {/* ── The PPV arc ─────────────────────────────────────────────── */}
          <div className="rounded-lg border border-border p-3 space-y-3">
            <label className="flex items-center gap-3 cursor-pointer">
              <input
                type="checkbox"
                className="h-4 w-4 accent-[var(--accent)]"
                checked={arcEnabled}
                onChange={(e) => { markDirty(); setArcEnabled(e.target.checked); }}
              />
              <span className="text-sm font-medium">PPV week / month arc</span>
            </label>
            <div className="flex flex-wrap gap-4">
              <label className="space-y-1 block">
                <div className="text-xs text-fg-dim">Span</div>
                <select
                  className={`${INPUT} w-40`}
                  value={weeks}
                  onChange={(e) => { markDirty(); setWeeks(Number(e.target.value)); }}
                >
                  <option value={1}>1 week</option>
                  <option value={2}>2 weeks</option>
                  <option value={3}>3 weeks</option>
                  <option value={4}>4 weeks (month)</option>
                </select>
              </label>
              <label className={cn("space-y-1 block self-end",
                                   isPaidPage && "opacity-40 pointer-events-none")}>
                <span className="flex items-center gap-2 text-xs cursor-pointer pb-2">
                  <input
                    type="checkbox"
                    className="h-4 w-4 accent-[var(--accent)]"
                    checked={combine && !isPaidPage}
                    disabled={isPaidPage}
                    onChange={(e) => { markDirty(); setCombine(e.target.checked); }}
                  />
                  Combine feed post + mass DM per drop
                </span>
              </label>
              <label className="space-y-1 block self-end">
                <span className="flex items-center gap-2 text-xs cursor-pointer pb-2">
                  <input
                    type="checkbox"
                    className="h-4 w-4 accent-[var(--accent)]"
                    checked={inVoice}
                    onChange={(e) => { markDirty(); setInVoice(e.target.checked); }}
                  />
                  Write captions in-voice (slower)
                </span>
              </label>
            </div>
            {isPaidPage && (
              <div className="text-[11px] text-fg-dim leading-relaxed">
                📌💰 {PAID_PAGE_NOTE} Every drop in the arc sells in DMs instead;
                the Sunday free post still goes on the wall.
              </div>
            )}
            <div className="text-[11px] text-fg-dim/70 leading-relaxed">
              In-voice copy is written through your PAINFUL_TEXTING framing (brevity +
              emotion). Off = fast template captions that still thread the week.
            </div>
          </div>

          {/* ── Pricing bands per tier ───────────────────────────────────── */}
          <div className="rounded-lg border border-border p-3 space-y-3">
            <div className="text-xs font-medium text-fg">
              PPV price bands by explicitness tier
            </div>
            <div className="text-[11px] text-fg-dim/70 leading-relaxed">
              Vision only classifies the tier (never invents a price). The arc picks a
              price inside the tier&apos;s band, climbing across the week and never going
              backward. Enter dollars — stored/sent as cents.
            </div>
            <div className="overflow-x-auto">
              <table className="text-xs border-collapse w-full">
                <thead>
                  <tr className="text-fg-dim">
                    <th className="text-left pr-2 font-normal">Tier</th>
                    <th className="text-left pr-2 font-normal">Label shown</th>
                    <th className="text-left pr-2 font-normal">Min $</th>
                    <th className="text-left pr-2 font-normal">Max $</th>
                  </tr>
                </thead>
                <tbody>
                  {TIER_ORDER.map((t) => {
                    const [lo, hi] = bands[t];
                    return (
                      <tr key={t} className="border-t border-border/40">
                        <td className="pr-2 py-1 font-mono text-fg-dim">{t}</td>
                        <td className="pr-2 py-1">
                          <input
                            type="text"
                            className={`${INPUT} w-36`}
                            value={tierLabels[t] ?? ""}
                            onChange={(e) => setLabel(t, e.target.value)}
                          />
                        </td>
                        <td className="pr-2 py-1">
                          <input
                            type="number" min={0} max={20000}
                            className={`${INPUT} w-20`}
                            value={centsToDollars(lo)}
                            onChange={(e) => setBand(t, 0, Number(e.target.value) || 0)}
                          />
                        </td>
                        <td className="pr-2 py-1">
                          <input
                            type="number" min={0} max={20000}
                            className={`${INPUT} w-20`}
                            value={centsToDollars(hi)}
                            onChange={(e) => setBand(t, 1, Number(e.target.value) || 0)}
                          />
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </div>
        </fieldset>

        <SaveRow {...save} className="hidden md:flex gap-3 pt-1">{noChanges}</SaveRow>
      </Card>

      {/* ── Generate + preview ───────────────────────────────────────────── */}
      <Card className="p-4 space-y-4">
        <header className="flex items-center gap-2">
          <Wand2 size={16} className="text-accent" />
          <h3 className="text-sm font-medium">Preview the arc</h3>
        </header>
        <p className="text-xs text-fg-dim leading-relaxed">
          Builds the {weeks === 1 ? "week" : `${weeks}-week`} arc from this account&apos;s
          described vault and renders it below. Read-only — nothing is armed or sent.
          {inVoice && " In-voice copy makes ~" + weeks * 7 + " model calls, so give it a moment."}
        </p>

        <div className="flex items-center gap-3 flex-wrap">
          <Button onClick={onGenerate} disabled={genM.isPending}>
            {genM.isPending ? "Generating…" : `Generate ${weeks === 1 ? "week" : "month"}`}
          </Button>
          {genM.data && (
            <Button onClick={openFull} variant="secondary">
              Open full preview ↗
            </Button>
          )}
          {dirty && (
            <span className="text-[11px] text-amber-500">
              Unsaved price-band changes won&apos;t show until you Save.
            </span>
          )}
          {genM.isError && (
            <span className="text-xs text-red-500">{genM.error?.message || "Generate failed"}</span>
          )}
        </div>

        {sm && (
          <div className="flex flex-wrap gap-x-6 gap-y-1 text-xs text-fg-dim">
            <span><b className="text-fg">{sm.media_bound}</b> media</span>
            {"dm_sends" in sm && <span><b className="text-fg">{sm.dm_sends}</b> mass DMs</span>}
            {"feed_posts" in sm && <span><b className="text-fg">{sm.feed_posts}</b> feed posts</span>}
            <span>
              <b className="text-fg">{money(sm.price_low_cents)}</b>→
              <b className="text-fg">{money(sm.price_high_cents)}</b> price arc
            </span>
            {!!sm.thin_days && <span className="text-amber-500">{sm.thin_days} thin day(s)</span>}
          </div>
        )}

        {genM.data && (
          <iframe
            title="PPV arc preview"
            srcDoc={genM.data.html}
            sandbox="allow-same-origin allow-popups"
            className="w-full h-[72vh] rounded-lg border border-border bg-white"
          />
        )}

        {/* ── Arm it ────────────────────────────────────────────────────── */}
        <div className="rounded-lg border border-amber-500/40 bg-amber-500/5 p-3 space-y-3">
          <div className="flex items-center gap-2">
            <CalendarClock size={15} className="text-amber-500" />
            <span className="text-sm font-medium">Let the arc run itself</span>
          </div>
          <p className="text-xs text-fg-dim leading-relaxed">
            Approve once and every day&apos;s drop leaves on its own day — Human Rhythm
            picks the hour, so it never lands on the same clock minute twice and never
            fires while the account would be asleep. Each drop fires <b>once</b>; when the arc
            runs out it stops and waits for you.
          </p>
          <div className="flex items-center gap-3 flex-wrap">
            <Button onClick={onArm} disabled={!genM.data || armM.isPending}>
              {armM.isPending ? "Arming…" : `Approve & run this ${weeks === 1 ? "week" : "month"}`}
            </Button>
            {!genM.data && (
              <span className="text-[11px] text-fg-dim/70">Generate a preview first.</span>
            )}
            {armError && <span className="text-xs text-red-500">{armError}</span>}
          </div>

          {arc?.active && (
            <div className="rounded-md border border-border/60 bg-bg-elev-1/50 p-2 space-y-2">
              <div className="flex items-center gap-3 flex-wrap text-xs">
                <span className="text-emerald-500 font-medium">● Armed</span>
                <span className="text-fg-dim">
                  <b className="text-fg">{arc.fired}</b> fired ·{" "}
                  <b className="text-fg">{arc.pending}</b> to go
                </span>
                {arc.next_at && (
                  <span className="text-fg-dim">next {when(arc.next_at)}</span>
                )}
                <button
                  onClick={onStandDown}
                  disabled={cancelM.isPending}
                  className="ml-auto text-red-500 hover:underline disabled:opacity-50"
                >
                  {cancelM.isPending ? "Cancelling…" : "Stand down"}
                </button>
              </div>
              <div className="overflow-x-auto">
                <table className="text-[11px] w-full border-collapse">
                  <tbody>
                    {(arc.drops ?? []).map((d) => (
                      <tr key={d.job_id} className="border-t border-border/40">
                        <td className="py-1 pr-3 text-fg-dim">{when(d.run_at)}</td>
                        <td className="py-1 pr-3">{CHANNEL_LABEL[d.channel] ?? d.channel}</td>
                        <td className="py-1 pr-3 text-fg-dim">{money(d.price_cents)}</td>
                        <td className="py-1 text-right">
                          {d.state === "pending" ? (
                            <span className="text-fg-dim/70">queued</span>
                          ) : d.state === "error" ? (
                            <span className="text-red-500">failed</span>
                          ) : (
                            <span className="text-emerald-500">sent</span>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </div>
      </Card>

      {/* The pinned twin. Was `hidden max-md:flex` — phones only — and then, on
       *  the first pass at un-gating it, a child of the config Card above.
       *  `sticky` resolves against its containing block, so from in there it
       *  stopped pinning the moment that Card scrolled past: the Save vanished
       *  for the whole "Preview the arc" Card below, which is ~40% of the tab
       *  and the one place that tells you unsaved price bands won't show. Last
       *  child of the OUTER container, which holds no other Save.
       *
       *  `hostPadding={0}`: that outer container is `max-w-4xl`, i.e. NARROWER
       *  than the ReadyMadePanel Card it sits in. A `-mx-4` would reach the
       *  Card's inner edge on the left and stop hundreds of px short of it on
       *  the right, running the border-t out mid-Card — the same asymmetry the
       *  nudge tabs' `max-w-2xl` columns have. */}
      <StickySaveBar {...save} hostPadding={0}>{noChanges}</StickySaveBar>
    </div>
  );
}
