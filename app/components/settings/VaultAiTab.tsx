"use client";

/**
 * VaultAiTab — Automations → "🧠 Vault AI".
 *
 * Operator surface for `account_ai_config.vault_ai_config_json` (frozen shape:
 * plans/VAULT_AI_ACTIONS_CONTRACT.md §1). Owns only the knobs the operator
 * touches at v1: master enable, describe cadence + "Describe-all now" cap,
 * per-tier PPV price bands, folder taxonomy seed, and the daily-reminder card.
 *
 * Writes are PATCH-partial (server deep-merges over the stored blob); the API
 * always returns the full EFFECTIVE blob (defaults + overrides). Suggest-only
 * is a HARD contract constraint in v1 — shown, but not editable.
 */

import { useEffect, useMemo, useState } from "react";
import { Sparkles } from "lucide-react";

import { Button, Card } from "@/components/ui/primitives";
import {
  useVaultAiConfig,
  useSaveVaultAiConfig,
  type VaultAiConfig,
  type VaultAiTier,
} from "@/hooks/useVaultAiConfig";

/** Size-free base so the mono caption editors can append their OWN font size.
 *  These class strings are plain template concatenation (no twMerge), so a size
 *  baked into INPUT would sit alongside the caller's `text-[12px]` and the
 *  winner would depend on stylesheet order. */
const INPUT_BASE =
  "bg-bg border border-border rounded-lg px-3 py-2 focus:outline-none focus:border-accent";
const INPUT = `${INPUT_BASE} text-sm max-md:text-base`;

const TIER_ORDER: VaultAiTier[] = ["safe", "suggestive", "explicit", "graphic", "unknown"];

const UNSEEN_OPTIONS: Array<{ v: VaultAiConfig["daily_reminder"]["on_under_min_unseen"]; label: string }> = [
  { v: "use_fewer", label: "Send fewer images" },
  { v: "stop", label: "Skip today" },
  { v: "repeat_after_window", label: "Allow repeats after window" },
];

function centsToDollars(c: number): number {
  return Math.round(c) / 100;
}
function dollarsToCents(d: number): number {
  return Math.round(d * 100);
}

export default function VaultAiTab({ accountId }: { accountId: string | null }) {
  const cfgQ = useVaultAiConfig(accountId);
  const saveM = useSaveVaultAiConfig(accountId);

  const [enabled, setEnabled] = useState(false);
  const [cadenceHours, setCadenceHours] = useState(6);
  const [describeAllCap, setDescribeAllCap] = useState(80);
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
  const [taxonomyText, setTaxonomyText] = useState("");
  const [reminderEnabled, setReminderEnabled] = useState(false);
  const [reminderFolder, setReminderFolder] = useState("");
  const [reminderLinesText, setReminderLinesText] = useState("");
  const [reminderCooldown, setReminderCooldown] = useState(48);
  const [reminderImagesPerDay, setReminderImagesPerDay] = useState(2);
  const [reminderUnderMin, setReminderUnderMin] =
    useState<VaultAiConfig["daily_reminder"]["on_under_min_unseen"]>("use_fewer");

  const cfg = cfgQ.data?.config;

  useEffect(() => {
    if (!cfg) return;
    setEnabled(!!cfg.enabled);
    setCadenceHours(cfg.describe.cadence_hours);
    setDescribeAllCap(cfg.describe.describe_all_cap_percent);
    setBands({ ...cfg.pricing.bands_by_tier });
    setTierLabels({ ...cfg.tier_labels });
    setTaxonomyText((cfg.folders.taxonomy ?? []).join("\n"));
    const dr = cfg.daily_reminder;
    setReminderEnabled(!!dr.enabled);
    setReminderFolder(dr.folder_name ?? "");
    setReminderLinesText((dr.lines ?? []).join("\n"));
    setReminderCooldown(dr.per_fan_cooldown_hours);
    setReminderImagesPerDay(dr.images_per_day);
    setReminderUnderMin(dr.on_under_min_unseen);
  }, [cfg]);

  const dirty = useMemo(() => {
    if (!cfg) return false;
    if (enabled !== cfg.enabled) return true;
    if (cadenceHours !== cfg.describe.cadence_hours) return true;
    if (describeAllCap !== cfg.describe.describe_all_cap_percent) return true;
    for (const t of TIER_ORDER) {
      const [a, b] = cfg.pricing.bands_by_tier[t] ?? [0, 0];
      const [x, y] = bands[t] ?? [0, 0];
      if (a !== x || b !== y) return true;
      if ((cfg.tier_labels[t] ?? "") !== (tierLabels[t] ?? "")) return true;
    }
    const linesNow = reminderLinesText.split("\n").map((s) => s.trim()).filter(Boolean);
    const taxNow = taxonomyText.split("\n").map((s) => s.trim()).filter(Boolean);
    const linesOld = cfg.daily_reminder.lines ?? [];
    const taxOld = cfg.folders.taxonomy ?? [];
    if (linesNow.length !== linesOld.length || linesNow.some((v, i) => v !== linesOld[i])) return true;
    if (taxNow.length !== taxOld.length || taxNow.some((v, i) => v !== taxOld[i])) return true;
    if (reminderEnabled !== cfg.daily_reminder.enabled) return true;
    if (reminderFolder !== (cfg.daily_reminder.folder_name ?? "")) return true;
    if (reminderCooldown !== cfg.daily_reminder.per_fan_cooldown_hours) return true;
    if (reminderImagesPerDay !== cfg.daily_reminder.images_per_day) return true;
    if (reminderUnderMin !== cfg.daily_reminder.on_under_min_unseen) return true;
    return false;
  }, [
    cfg, enabled, cadenceHours, describeAllCap, bands, tierLabels, taxonomyText,
    reminderEnabled, reminderFolder, reminderLinesText, reminderCooldown,
    reminderImagesPerDay, reminderUnderMin,
  ]);

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
    const taxonomy = taxonomyText.split("\n").map((s) => s.trim()).filter(Boolean);
    const lines = reminderLinesText.split("\n").map((s) => s.trim()).filter(Boolean);
    const patch: Partial<VaultAiConfig> = {
      enabled,
      describe: {
        ...(cfg?.describe ?? {
          cadence_hours: 6, describe_all_cap_percent: 80, max_items_per_run: 40,
          images: true, videos: true,
        }),
        cadence_hours: Math.max(1, Math.min(cadenceHours || 6, 24 * 30)),
        describe_all_cap_percent: Math.max(0, Math.min(describeAllCap || 0, 100)),
      },
      pricing: {
        enabled: cfg?.pricing.enabled ?? true,
        bands_by_tier: bands,
      },
      tier_labels: tierLabels,
      folders: {
        ...(cfg?.folders ?? { mode: "internal", taxonomy: [], max_folders_per_item: 3 }),
        taxonomy,
      },
      daily_reminder: {
        ...(cfg?.daily_reminder ?? {
          enabled: false, auto_post: false, auto_mass_message: false,
          folder_name: "", lines: [], images_per_day: 2,
          on_under_min_unseen: "use_fewer", repeat_after_days: 30,
          per_fan_cooldown_hours: 48, daily_at: ["10:00"], tz_offset_minutes: 0,
        }),
        enabled: reminderEnabled,
        folder_name: reminderFolder.trim(),
        lines,
        images_per_day: Math.max(1, Math.min(reminderImagesPerDay || 1, 20)),
        per_fan_cooldown_hours: Math.max(0, Math.min(reminderCooldown || 0, 24 * 365)),
        on_under_min_unseen: reminderUnderMin,
      },
    };
    saveM.mutate(patch);
  };

  if (!accountId) return <div className="text-sm text-fg-dim">Pick an account above.</div>;
  if (cfgQ.isLoading || !cfg) return <div className="text-sm text-fg-dim">Loading…</div>;

  return (
    <Card className="p-4 space-y-6 max-w-3xl">
      <header className="flex items-center gap-2">
        <Sparkles size={16} className="text-accent" />
        <h3 className="text-sm font-medium">Vault AI — describe, tier-price, remind (suggest-only)</h3>
      </header>

      <p className="text-xs text-fg-dim leading-relaxed">
        The Vault AI describes new/changed vault media on a schedule, sorts it into
        an internal folder taxonomy, and proposes PPV drafts + a daily reminder
        card. <b>Nothing sends until you approve it</b> in the Review queue —
        that&apos;s a hard contract, not a checkbox.
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
        <b>Suggest-only</b> is locked ON in v1. Descriptions auto-apply (internal
        only, no send); folder / PPV / reminder actions always drop a review card
        first.
      </div>

      <fieldset disabled={!enabled} className="space-y-6" style={{ opacity: enabled ? 1 : 0.55 }}>

        {/* ── Describe cadence ─────────────────────────────────────────── */}
        <div className="rounded-lg border border-border p-3 space-y-3">
          <div className="text-xs font-medium text-fg">Describe cadence</div>
          <label className="flex items-center gap-2 text-xs flex-wrap">
            <span>Sweep every</span>
            <input
              type="number" min={1} max={24 * 30}
              className={`${INPUT} w-20`}
              value={cadenceHours}
              onChange={(e) => { markDirty(); setCadenceHours(Math.max(1, Number(e.target.value) || 1)); }}
            />
            <span>hours (new + changed vault items only)</span>
          </label>
          <label className="flex items-center gap-2 text-xs flex-wrap">
            <span>&quot;Describe all now&quot; may spend up to</span>
            <input
              type="number" min={0} max={100}
              className={`${INPUT} w-20`}
              value={describeAllCap}
              onChange={(e) => { markDirty(); setDescribeAllCap(Math.max(0, Math.min(Number(e.target.value) || 0, 100))); }}
            />
            <span>% of the daily LLM cap</span>
          </label>
          <div className="text-[11px] text-fg-dim/70">
            The one-click &quot;Describe-all now&quot; button on the Vault page will
            reserve at most this much of today&apos;s spend before deferring the
            rest to the next cadence tick.
          </div>
        </div>

        {/* ── Pricing bands per tier ───────────────────────────────────── */}
        <div className="rounded-lg border border-border p-3 space-y-3">
          <div className="text-xs font-medium text-fg">
            PPV price bands by explicitness tier
          </div>
          <div className="text-[11px] text-fg-dim/70 leading-relaxed">
            Vision only classifies the tier (never invents a price). The
            qualification logic then picks a price inside the tier&apos;s band.
            Enter dollars — stored/sent as cents.
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

        {/* ── Folder taxonomy seed ─────────────────────────────────────── */}
        <div className="rounded-lg border border-border p-3 space-y-2">
          <div className="text-xs font-medium text-fg">Folder taxonomy — operator seed</div>
          <div className="text-[11px] text-fg-dim/70 leading-relaxed">
            One folder name per line. Empty → the clusterer proposes folders
            from tags + tier. Folders stay <b>internal</b> until the OF vault-write
            wire is captured (see plan #1).
          </div>
          <textarea
            rows={5}
            className={`${INPUT_BASE} w-full font-mono text-sm text-[12px] max-md:text-base`}
            placeholder={"beach\nlingerie\nshower\ncosplay"}
            value={taxonomyText}
            onChange={(e) => { markDirty(); setTaxonomyText(e.target.value); }}
          />
        </div>

        {/* ── Daily reminder ───────────────────────────────────────────── */}
        <div className="rounded-lg border border-border p-3 space-y-3">
          <label className="flex items-center gap-3 cursor-pointer">
            <input
              type="checkbox"
              className="h-4 w-4 accent-[var(--accent)]"
              checked={reminderEnabled}
              onChange={(e) => { markDirty(); setReminderEnabled(e.target.checked); }}
            />
            <span className="text-sm font-medium">Daily reminder card</span>
          </label>
          <div className="text-[11px] text-fg-dim/70 leading-relaxed">
            Once a day, pick N unseen images from a folder + one random line and
            drop a review card. Approve → mass-broadcast minus MASSdmEXCLUDE
            (per-fan cooldown enforced).
          </div>
          <div className="flex flex-wrap gap-4">
            <label className="space-y-1 block">
              <div className="text-xs text-fg-dim">Folder name</div>
              <input
                type="text"
                className={`${INPUT} w-52`}
                placeholder="e.g. Beach"
                value={reminderFolder}
                onChange={(e) => { markDirty(); setReminderFolder(e.target.value); }}
              />
            </label>
            <label className="space-y-1 block">
              <div className="text-xs text-fg-dim">Images per day</div>
              <input
                type="number" min={1} max={20}
                className={`${INPUT} w-20`}
                value={reminderImagesPerDay}
                onChange={(e) => { markDirty(); setReminderImagesPerDay(Math.max(1, Math.min(Number(e.target.value) || 1, 20))); }}
              />
            </label>
            <label className="space-y-1 block">
              <div className="text-xs text-fg-dim">Per-fan cooldown (hours)</div>
              <input
                type="number" min={0} max={24 * 365}
                className={`${INPUT} w-20`}
                value={reminderCooldown}
                onChange={(e) => { markDirty(); setReminderCooldown(Math.max(0, Number(e.target.value) || 0)); }}
              />
            </label>
            <label className="space-y-1 block">
              <div className="text-xs text-fg-dim">When under min unseen</div>
              <select
                className={`${INPUT} w-52`}
                value={reminderUnderMin}
                onChange={(e) => { markDirty(); setReminderUnderMin(e.target.value as typeof reminderUnderMin); }}
              >
                {UNSEEN_OPTIONS.map((o) => (
                  <option key={o.v} value={o.v}>{o.label}</option>
                ))}
              </select>
            </label>
          </div>
          <div className="space-y-1">
            <div className="text-xs text-fg-dim">
              Prewritten lines — one per line. One is random-picked each day.
            </div>
            <textarea
              rows={4}
              className={`${INPUT_BASE} w-full font-mono text-sm text-[12px] max-md:text-base`}
              placeholder={"miss me? 🥵\nunlock this babe\nnew set just dropped"}
              value={reminderLinesText}
              onChange={(e) => { markDirty(); setReminderLinesText(e.target.value); }}
            />
          </div>
        </div>
      </fieldset>

      {/* Desktop keeps this in-flow row untouched; the phone gets the sticky
       *  twin below (outside the fieldset, last child of the Card). */}
      <div className="hidden md:flex md:mb-0 items-center gap-3 pt-1">
        <Button onClick={onSave} disabled={saveM.isPending || !dirty}>
          {saveM.isPending ? "Saving…" : "Save"}
        </Button>
        {saveM.isSuccess && !dirty && (
          <span className="text-xs text-emerald-500">Saved ✓</span>
        )}
        {saveM.isError && (
          <span className="text-xs text-red-500">{saveM.error?.message || "Save failed"}</span>
        )}
        {!dirty && !saveM.isSuccess && (
          <span className="text-[11px] text-fg-dim/70">No unsaved changes.</span>
        )}
      </div>

      {/* Phone-only sticky Save bar — `hidden` at >=768px so desktop renders
       *  nothing extra. `-mx-4 px-4` bleeds to this Card's own p-4 edges. */}
      <div className="hidden max-md:flex sticky bottom-0 z-20 -mx-4 px-4 py-3 items-center gap-3 flex-wrap bg-panel/95 backdrop-blur border-t border-border pb-[calc(env(safe-area-inset-bottom)+0.75rem)]">
        <Button onClick={onSave} disabled={saveM.isPending || !dirty}>
          {saveM.isPending ? "Saving…" : "Save"}
        </Button>
        {saveM.isSuccess && !dirty && (
          <span className="text-xs text-emerald-500">Saved ✓</span>
        )}
        {saveM.isError && (
          <span className="text-xs text-red-500">{saveM.error?.message || "Save failed"}</span>
        )}
      </div>
    </Card>
  );
}
