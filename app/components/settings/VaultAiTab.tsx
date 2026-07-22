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
import { Sparkles, Wand2 } from "lucide-react";

import { Button, Card } from "@/components/ui/primitives";
import {
  useVaultAiConfig,
  useSaveVaultAiConfig,
  useGenerateVaultPpvWeek,
  type VaultAiConfig,
  type VaultAiTier,
} from "@/hooks/useVaultAiConfig";

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

export default function VaultAiTab({ accountId }: { accountId: string | null }) {
  const cfgQ = useVaultAiConfig(accountId);
  const saveM = useSaveVaultAiConfig(accountId);
  const genM = useGenerateVaultPpvWeek(accountId);

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

  const openFull = () => {
    const html = genM.data?.html;
    if (!html) return;
    const url = URL.createObjectURL(new Blob([html], { type: "text/html" }));
    window.open(url, "_blank");
    setTimeout(() => URL.revokeObjectURL(url), 60_000);
  };

  if (!accountId) return <div className="text-sm text-fg-dim">Pick an account above.</div>;
  if (cfgQ.isLoading || !cfg) return <div className="text-sm text-fg-dim">Loading…</div>;

  const sm = genM.data?.summary;

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
              <label className="space-y-1 block self-end">
                <span className="flex items-center gap-2 text-xs cursor-pointer pb-2">
                  <input
                    type="checkbox"
                    className="h-4 w-4 accent-[var(--accent)]"
                    checked={combine}
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

        <div className="hidden md:flex items-center gap-3 pt-1">
          <Button onClick={onSave} disabled={saveM.isPending || !dirty}>
            {saveM.isPending ? "Saving…" : "Save"}
          </Button>
          {saveM.isSuccess && !dirty && <span className="text-xs text-emerald-500">Saved ✓</span>}
          {saveM.isError && (
            <span className="text-xs text-red-500">{saveM.error?.message || "Save failed"}</span>
          )}
          {!dirty && !saveM.isSuccess && (
            <span className="text-[11px] text-fg-dim/70">No unsaved changes.</span>
          )}
        </div>

        <div className="hidden max-md:flex sticky bottom-0 z-20 -mx-4 px-4 py-3 items-center gap-3 flex-wrap bg-panel/95 backdrop-blur border-t border-border pb-[calc(env(safe-area-inset-bottom)+0.75rem)]">
          <Button onClick={onSave} disabled={saveM.isPending || !dirty}>
            {saveM.isPending ? "Saving…" : "Save"}
          </Button>
          {saveM.isSuccess && !dirty && <span className="text-xs text-emerald-500">Saved ✓</span>}
          {saveM.isError && (
            <span className="text-xs text-red-500">{saveM.error?.message || "Save failed"}</span>
          )}
        </div>
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
      </Card>
    </div>
  );
}
