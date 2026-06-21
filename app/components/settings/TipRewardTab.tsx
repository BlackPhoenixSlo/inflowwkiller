"use client";

/**
 * TipRewardTab — Automations → "🎁 Tip Reward".
 *
 * Per-account config for the `tip_reward` automation: when a fan tips, send them
 * vault images they haven't received yet. Writes account_ai_config.
 * tip_reward_config_json via /admin/tip-reward-config (mirrors AutoreplyTab).
 *
 * The rule, in plain terms:
 *   • COUNT  = tip ÷ "$ per image", clamped between min and max.
 *   • FOLDER = picked by a TIER — the highest tier whose threshold the fan has
 *     reached, where the threshold is checked against max(this tip, the fan's
 *     tip total over the last N hours). So a fan who has tipped past a tier in
 *     the window keeps that tier even on a small follow-up tip.
 *   • Images are FREE and only ever ones this fan has never been sent.
 *
 * Layout: two independent feature SECTIONS (ASK / REWARD), each with its own
 * on/off switch in its header. The body of each section only renders when its
 * switch is on, so the tab reads as two cards instead of one long column.
 */

import { useEffect, useMemo, useState } from "react";
import { Gift, FolderOpen, MessageCircle } from "lucide-react";

import { Button, Card } from "@/components/ui/primitives";
import { VaultFolderPicker } from "@/components/settings/VaultFolderPicker";
import {
  useTipRewardConfig,
  useSaveTipRewardConfig,
  type TipRewardConfig,
} from "@/hooks/useTipRewardConfig";

const INPUT =
  "bg-bg border border-border rounded-lg px-3 py-2 text-sm focus:outline-none focus:border-accent";

/** A tier as the form holds it. Folders are the vault folder NAMES this tier
 *  draws from — picked visually, the same string[] shape tip_reward stores. */
interface TierForm {
  name: string;
  minDollars: number;
  folders: string[];
}

function tiersToForm(tiers: TipRewardConfig["tiers"]): TierForm[] {
  return (tiers ?? []).map((t) => ({
    name: t.name ?? "",
    minDollars: Math.round((t.min_basis_cents ?? 0) / 100),
    folders: (t.folders ?? []).filter((f) => f.trim()),
  }));
}

export default function TipRewardTab({ accountId }: { accountId: string | null }) {
  const cfgQ = useTipRewardConfig(accountId);
  const saveM = useSaveTipRewardConfig(accountId);

  const eff: TipRewardConfig = useMemo(() => {
    const d = cfgQ.data?.defaults ?? {};
    const c = cfgQ.data?.config ?? {};
    return { ...d, ...c };
  }, [cfgQ.data]);

  const [enabled, setEnabled] = useState(false);
  const [alwaysReward, setAlwaysReward] = useState(false);
  const [dollarsPerImage, setDollarsPerImage] = useState(10);
  const [minImages, setMinImages] = useState(1);
  const [maxImages, setMaxImages] = useState(5);
  const [windowHours, setWindowHours] = useState(72);
  const [caption, setCaption] = useState("");
  const [tiers, setTiers] = useState<TierForm[]>([]);
  const [pickerTier, setPickerTier] = useState<number | null>(null);
  // ASK side (independent of the delivery `enabled` above): default ON, no set price.
  const [askEnabled, setAskEnabled] = useState(true);
  const [askAmount, setAskAmount] = useState<string>(""); // "" → ask with no fixed price
  const [askTemplate, setAskTemplate] = useState("");

  useEffect(() => {
    setEnabled(!!eff.enabled);
    setAlwaysReward(!!eff.always_reward);
    setDollarsPerImage(eff.dollars_per_image ?? 10);
    setMinImages(eff.min_images ?? 1);
    setMaxImages(eff.max_images ?? 5);
    setWindowHours(eff.window_hours ?? 72);
    setCaption(eff.caption ?? "");
    setTiers(tiersToForm(eff.tiers));
    setAskEnabled(eff.ask_enabled !== false); // default ON when unset
    setAskAmount(eff.ask_amount_dollars == null ? "" : String(eff.ask_amount_dollars));
    setAskTemplate(eff.ask_template ?? "");
  }, [eff]);

  if (!accountId) return <div className="text-sm text-fg-dim">Pick an account above.</div>;
  if (cfgQ.isLoading) return <div className="text-sm text-fg-dim">Loading…</div>;

  // Any user edit makes the "Saved ✓"/error feedback stale — clear it so the
  // indicator only ever reflects the currently-persisted state.
  const markDirty = () => {
    if (saveM.isSuccess || saveM.isError) saveM.reset();
  };

  const setTier = (i: number, patch: Partial<TierForm>) => {
    markDirty();
    setTiers((ts) => ts.map((t, j) => (j === i ? { ...t, ...patch } : t)));
  };
  const addTier = () => {
    markDirty();
    setTiers((ts) => [...ts, { name: "", minDollars: 0, folders: [] }]);
  };
  const removeTier = (i: number) => {
    markDirty();
    setTiers((ts) => ts.filter((_, j) => j !== i));
  };

  function buildConfig(): TipRewardConfig {
    const askAmtTrim = askAmount.trim();
    return {
      enabled,
      always_reward: alwaysReward,
      dollars_per_image: dollarsPerImage,
      min_images: minImages,
      max_images: maxImages,
      window_hours: windowHours,
      caption: caption.trim(),
      // ASK side — null amount means "ask for a tip without naming a price".
      ask_enabled: askEnabled,
      ask_amount_dollars: askAmtTrim === "" ? null : Math.max(1, Math.round(Number(askAmtTrim) || 0)),
      ask_template: askTemplate.trim(),
      tiers: tiers
        // a tier with no folders does nothing — drop it on save
        .map((t) => ({
          name: t.name.trim(),
          min_basis_cents: Math.max(0, Math.round(t.minDollars * 100)),
          folders: t.folders.map((f) => f.trim()).filter(Boolean),
        }))
        .filter((t) => t.folders.length > 0),
    };
  }

  const noFolders = tiers.every((t) => t.folders.length === 0);

  return (
    <Card className="p-5 space-y-4 max-w-2xl">
      <header className="flex items-center gap-2">
        <Gift size={16} className="text-accent" />
        <h3 className="text-sm font-medium">Tip Reward</h3>
      </header>

      <p className="text-xs text-fg-dim leading-relaxed">
        Two independent switches: get the AI to <b>ask</b> fans for a tip when they
        beg to see content, and automatically <b>reward</b> fans who tip with free
        vault photos &amp; videos. Either one works on its own.
      </p>

      {/* ── ASK ───────────────────────────────────────────────────────────
          The AI can ask for a tip even when reward delivery is off. */}
      <Section
        icon={<MessageCircle size={15} />}
        title="Ask fans to tip for content"
        subtitle="When a fan asks to see something in chat (“can i see…”, “send me something”), the AI teases “tip me and I’ll send you something” instead of a normal reply."
        toggle={
          <Toggle
            checked={askEnabled}
            onChange={(v) => {
              markDirty();
              setAskEnabled(v);
            }}
          />
        }
      >
        {askEnabled && (
          <label className="block space-y-1">
            <div className="text-xs font-medium text-fg">Suggested amount (optional)</div>
            <div className="flex items-center gap-2">
              <span className="text-xs text-fg-dim">$</span>
              <input
                type="number"
                min={1}
                max={10000}
                placeholder="no set amount"
                className={`${INPUT} w-40`}
                value={askAmount}
                onChange={(e) => {
                  markDirty();
                  setAskAmount(e.target.value);
                }}
              />
            </div>
            <div className="text-[11px] text-fg-dim/70">
              Leave blank (recommended) so she asks for a tip without naming a price.
              Set a number only if you want her to suggest a specific amount.
            </div>
          </label>
        )}
      </Section>

      {/* ── REWARD ─────────────────────────────────────────────────────────
          Delivery side: send free vault media to fans who tip. */}
      <Section
        icon={<Gift size={15} />}
        title="Reward tippers with vault media"
        subtitle={`Send free photos & videos the fan hasn’t seen yet — about one item per $${dollarsPerImage} tipped (capped). Bigger tippers can be routed to nicer folders.`}
        toggle={
          <Toggle
            checked={enabled}
            onChange={(v) => {
              markDirty();
              setEnabled(v);
            }}
          />
        }
      >
        {enabled && (
          <div className="space-y-5">
            {/* How many images */}
            <div className="space-y-2">
              <div className="text-xs font-medium text-fg">How many to send</div>
              <div className="flex flex-wrap gap-4">
                <NumField
                  label="$ per image" hint="1 image for every this many dollars"
                  value={dollarsPerImage} min={1} max={10000} suffix="$"
                  onChange={(n) => { markDirty(); setDollarsPerImage(n); }}
                />
                <NumField
                  label="Minimum" hint="floor for any tip (even under $/image)"
                  value={minImages} min={0} max={50}
                  onChange={(n) => { markDirty(); setMinImages(n); }}
                />
                <NumField
                  label="Maximum (cap)" hint="a whale tip never sends more than this"
                  value={maxImages} min={1} max={50}
                  onChange={(n) => { markDirty(); setMaxImages(n); }}
                />
              </div>
            </div>

            {/* Caption */}
            <label className="block space-y-1">
              <div className="text-xs font-medium text-fg">Caption (optional)</div>
              <input
                type="text"
                className={`${INPUT} w-full`}
                placeholder="omg thank you babe 🥰 here's something just for you…"
                value={caption}
                onChange={(e) => {
                  markDirty();
                  setCaption(e.target.value);
                }}
              />
              <div className="text-[11px] text-fg-dim/70">
                Sent as the message text with the images. Leave blank for images only.
              </div>
            </label>

            {/* Always reward — override the open-offer standdown */}
            <label className="flex items-start gap-3 cursor-pointer">
              <input
                type="checkbox"
                className="h-4 w-4 mt-0.5 accent-[var(--accent)]"
                checked={alwaysReward}
                onChange={(e) => {
                  markDirty();
                  setAlwaysReward(e.target.checked);
                }}
              />
              <span className="space-y-0.5">
                <span className="block text-sm">Always reward, even mid-sale</span>
                <span className="block text-[11px] text-fg-dim/80 leading-relaxed">
                  By default, when the AI has an open paid (PPV) offer with a fan, a tip
                  is treated as payment toward that offer and the reward stands down so
                  the fan doesn&apos;t get free media on top of what they paid for. Turn
                  this on to <b>always</b> send the reward anyway — the paid offer is
                  still credited, the fan just also gets the thank-you media.
                </span>
              </span>
            </label>

            {/* Tiers */}
            <div className="space-y-2">
              <div className="text-xs font-medium text-fg">Folder tiers</div>
              <p className="text-[11px] text-fg-dim/80 leading-relaxed">
                The fan&apos;s tier is the highest one whose threshold they&apos;ve reached,
                measured by the bigger of <i>this tip</i> and their tip total over the last{" "}
                <b>{windowHours}h</b>. Pick the vault folder(s) each tier draws from — both
                images and videos inside are eligible. A tier with no folders is ignored.
              </p>

              <div className="space-y-2">
                {tiers.map((t, i) => (
                  <div
                    key={i}
                    className="rounded-lg border border-border bg-bg-elev-1 p-3 space-y-2"
                  >
                    <div className="flex items-center gap-2 flex-wrap">
                      <input
                        type="text"
                        className={`${INPUT} w-32`}
                        placeholder="tier name"
                        value={t.name}
                        onChange={(e) => setTier(i, { name: e.target.value })}
                      />
                      <span className="text-xs text-fg-dim">applies at</span>
                      <div className="flex items-center gap-1">
                        <span className="text-xs text-fg-dim">$</span>
                        <input
                          type="number"
                          min={0}
                          className={`${INPUT} w-24`}
                          value={t.minDollars}
                          onChange={(e) =>
                            setTier(i, { minDollars: Math.max(0, Number(e.target.value) || 0) })
                          }
                        />
                        <span className="text-xs text-fg-dim">+</span>
                      </div>
                      <button
                        type="button"
                        onClick={() => removeTier(i)}
                        className="ml-auto text-fg-dim hover:text-err text-sm px-1"
                        title="Remove tier"
                      >
                        ✕
                      </button>
                    </div>
                    <div className="flex flex-wrap items-center gap-1.5">
                      {t.folders.length === 0 && (
                        <span className="text-[11px] text-fg-dim italic">No folders yet.</span>
                      )}
                      {t.folders.map((nm) => (
                        <span
                          key={nm}
                          className="inline-flex items-center gap-1 text-xs bg-bg-elev-1 border border-border rounded-full px-2 py-0.5"
                        >
                          {nm}
                          <button
                            type="button"
                            onClick={() =>
                              setTier(i, { folders: t.folders.filter((f) => f !== nm) })
                            }
                            className="text-fg-dim hover:text-err leading-none"
                            title="Remove folder"
                          >
                            ×
                          </button>
                        </span>
                      ))}
                      <Button
                        size="sm"
                        variant="secondary"
                        onClick={() => setPickerTier(i)}
                      >
                        <FolderOpen size={13} /> Pick from vault
                      </Button>
                    </div>
                  </div>
                ))}
              </div>

              <Button size="sm" variant="secondary" onClick={addTier}>
                + Add tier
              </Button>
            </div>
          </div>
        )}
      </Section>

      {/* Save + feedback */}
      <div className="flex items-center gap-3 pt-1">
        <Button onClick={() => saveM.mutate(buildConfig())} disabled={saveM.isPending}>
          {saveM.isPending ? "Saving…" : "Save"}
        </Button>
        {saveM.isSuccess && !saveM.isPending && (
          <span className="text-xs text-emerald-500">Saved ✓</span>
        )}
        {saveM.isError && (
          <span className="text-xs text-red-500">{saveM.error?.message || "Save failed"}</span>
        )}
        {enabled && noFolders && (
          <span className="text-xs text-warn">
            No folders set — nothing will send until you pick a vault folder.
          </span>
        )}
      </div>

      {/* Vault folder picker for the active tier. */}
      {pickerTier !== null && (
        <VaultFolderPicker
          open
          accountId={accountId}
          initialSelected={tiers[pickerTier]?.folders ?? []}
          onClose={() => setPickerTier(null)}
          onConfirm={(folders) => setTier(pickerTier, { folders })}
        />
      )}
    </Card>
  );
}

/** A bordered feature section with a title, optional subtitle, an on/off switch
 *  in the header, and a body that the caller only renders when the switch is on. */
function Section({
  icon, title, subtitle, toggle, children,
}: {
  icon: React.ReactNode;
  title: string;
  subtitle?: string;
  toggle: React.ReactNode;
  children?: React.ReactNode;
}) {
  return (
    <section className="rounded-xl border border-border bg-bg-elev-1/40 overflow-hidden">
      <header className="flex items-start gap-3 p-3.5">
        <div className="text-accent mt-0.5 shrink-0">{icon}</div>
        <div className="min-w-0 flex-1">
          <div className="text-sm font-medium">{title}</div>
          {subtitle && (
            <div className="text-[11px] text-fg-dim/80 leading-relaxed mt-0.5">{subtitle}</div>
          )}
        </div>
        <div className="shrink-0 mt-0.5">{toggle}</div>
      </header>
      {children && (
        <div className="border-t border-border p-3.5 space-y-4">{children}</div>
      )}
    </section>
  );
}

/** A pill on/off switch. Lighter-touch than a checkbox for a section header. */
function Toggle({
  checked, onChange,
}: {
  checked: boolean;
  onChange: (v: boolean) => void;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      onClick={() => onChange(!checked)}
      className={`relative inline-flex h-5 w-9 shrink-0 items-center rounded-full transition-colors ${
        checked ? "bg-accent" : "bg-border"
      }`}
      title={checked ? "On" : "Off"}
    >
      <span
        className={`inline-block h-4 w-4 transform rounded-full bg-white shadow transition-transform ${
          checked ? "translate-x-4" : "translate-x-0.5"
        }`}
      />
    </button>
  );
}

function NumField({
  label, hint, value, onChange, min = 0, max = 100000, step = 1, suffix,
}: {
  label: string;
  hint?: string;
  value: number;
  min?: number;
  max?: number;
  step?: number;
  suffix?: string;
  onChange: (n: number) => void;
}) {
  return (
    <label className="space-y-1 block">
      <div className="text-xs text-fg-dim">{label}</div>
      <div className="flex items-center gap-2">
        {suffix === "$" && <span className="text-xs text-fg-dim">$</span>}
        <input
          type="number"
          min={min}
          max={max}
          step={step}
          className={`${INPUT} w-24`}
          value={value}
          onChange={(e) =>
            onChange(Math.max(min, Math.min(Number(e.target.value) || 0, max)))
          }
        />
        {suffix && suffix !== "$" && <span className="text-xs text-fg-dim">{suffix}</span>}
      </div>
      {hint && <div className="text-[11px] text-fg-dim/70">{hint}</div>}
    </label>
  );
}
