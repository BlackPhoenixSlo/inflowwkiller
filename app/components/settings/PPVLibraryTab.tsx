"use client";

/**
 * PPVLibraryTab — Automations → "💸 PPV Library".
 *
 * Build ~20 premade PPVs once. Each = vault media + a caption pool + a base price
 * + free-teaser picks + "sends per week". On Save the backend upserts one `ppv_send`
 * rule per enabled PPV (cadence = a week ÷ sends-per-week); when a rule fires,
 * `ppv_send` fans the SAME media out to spend×recency fan segments at a per-segment
 * price (base × matrix multiplier), rotating the preview + caption per send.
 *
 * Writes account_ai_config.ppv_library_config_json via /admin/ppv-library-config.
 */

import { useEffect, useState } from "react";
import { DollarSign, Image as ImageIcon, Copy } from "lucide-react";

import { Button, Card } from "@/components/ui/primitives";
import { VaultPicker } from "@/components/chat/VaultPicker";
import { cn } from "@/lib/utils";
import { proxyImage } from "@/lib/relay";
import {
  usePpvLibraryConfig,
  useSavePpvLibraryConfig,
  usePpvPreview,
  type PpvItem,
  type PriceMatrix,
} from "@/hooks/usePpvLibraryConfig";

const INPUT =
  "bg-bg border border-border rounded-lg px-3 py-2 text-sm focus:outline-none focus:border-accent";

const POOL_LABELS: Record<string, string> = {
  intro_new: "New fan (soft intro)",
  standard_active: "Regular (standard)",
  vip_whale: "VIP / whale",
  winback_dormant: "Win-back (discount)",
  teaser_free: "Free teaser",
  photoset_striptease: "Photo set / strip tease",
  video_ppv: "Video",
  followup_nonunlocker: "Follow-up (didn't open)",
  bundle_anchor: "Bundle / big drop",
  bundle_long: "Big bundle — long text",
  flash_discount: "Flash sale (short, % off)",
  exclusive_list: "Exclusive list (long, not-a-mass-dm)",
  intimate_reveal: "Intimate reveal",
};

// Plain-word send cadence (maps to sends_per_week). A non-standard stored value
// shows as its own "N× / week" option so nothing is lost on load.
const CADENCE_OPTIONS: Array<{ v: number; label: string }> = [
  { v: 14, label: "Twice a day" },
  { v: 7, label: "Every day" },
  { v: 3, label: "Every few days" },
  { v: 2, label: "Twice a week" },
  { v: 1, label: "Once a week" },
];
function cadenceLabel(spw: number): string {
  return CADENCE_OPTIONS.find((o) => o.v === spw)?.label ?? `${spw}× / week`;
}

// One-click starter presets so "build 20" doesn't mean re-typing every field.
const TEMPLATES: Array<{ emoji: string; label: string; patch: Partial<PpvItem> }> = [
  { emoji: "🍑", label: "Weekly tease", patch: { name: "Weekly tease", caption_pool_key: "standard_active", base_price_cents: 1500, sends_per_week: 3, resend_monthly: false, exclude_buyers: true } },
  { emoji: "🐳", label: "Whale drop", patch: { name: "Whale drop", caption_pool_key: "vip_whale", base_price_cents: 6000, sends_per_week: 1, resend_monthly: false, exclude_buyers: true } },
  { emoji: "💔", label: "Win-back", patch: { name: "Win-back", caption_pool_key: "winback_dormant", base_price_cents: 900, sends_per_week: 2, resend_monthly: false, exclude_buyers: false } },
];

function newId(): string {
  return "ppv_" + Math.random().toString(36).slice(2, 8);
}

// OF rejects any priced message under $3.00 → floor at $3.99 (keeps .99 styling).
const PRICE_FLOOR = 399;
function roundTo99(cents: number): number {
  const dollars = Math.round(cents / 100);
  const c = dollars < 1 ? 99 : dollars * 100 - 1;
  return Math.max(PRICE_FLOOR, Math.min(c, 20000));
}

function money(cents: number): string {
  return cents % 100 === 0 ? `$${cents / 100}` : `$${(cents / 100).toFixed(2)}`;
}

// {was} anchor = ~4x what they pay (display only, no floor/ceiling) so a caption's
// "was X now Y" always reads like a real ~75% off deal. Mirrors _anchor_price.
function anchor99(nowCents: number): number {
  const dollars = Math.max(1, Math.round((nowCents * 4) / 100));
  return dollars * 100 - 1;
}

// Cheapest → priciest cell for a base price, straight off the live matrix.
function priceRange(baseCents: number, matrix: PriceMatrix): [number, number] {
  let lo = Infinity;
  let hi = 0;
  for (const s of matrix.spend_bands) {
    for (const r of matrix.recency_bands) {
      const c = roundTo99(baseCents * s.mult * r.mult);
      lo = Math.min(lo, c);
      hi = Math.max(hi, c);
    }
  }
  return [Number.isFinite(lo) ? lo : baseCents, hi || baseCents];
}

function blankPpv(): PpvItem {
  return {
    id: newId(),
    name: "",
    media_ids: [],
    caption_pool_key: "standard_active",
    base_price_cents: 2500,
    preview_options: [],
    sends_per_week: 3,
    resend_monthly: false,
    exclude_buyers: true,
    enabled: true,
  };
}

const DISCOUNT_CAPTION = "{off} off today babe 🙈 was {was} now just {now}";

export default function PPVLibraryTab({ accountId }: { accountId: string | null }) {
  const cfgQ = usePpvLibraryConfig(accountId);
  const saveM = useSavePpvLibraryConfig(accountId);
  const previewM = usePpvPreview(accountId);

  const [enabled, setEnabled] = useState(false);
  const [ppvs, setPpvs] = useState<PpvItem[]>([]);
  // which card's vault picker is open (idx) — one picker now; previews are ⭐ taps
  const [picker, setPicker] = useState<number | null>(null);
  // thumbnails captured at pick time so the ⭐ grid shows real images this session
  const [mediaThumbs, setMediaThumbs] = useState<Record<number, string>>({});
  // creator-local quiet window
  const [quietOn, setQuietOn] = useState(false);
  const [quietStart, setQuietStart] = useState(23);
  const [quietEnd, setQuietEnd] = useState(8);
  // per-account caps (0 = no limit)
  const [capDay, setCapDay] = useState(0);
  const [capWeek, setCapWeek] = useState(0);
  const [capMonth, setCapMonth] = useState(0);
  // which card's audience preview is showing
  const [previewFor, setPreviewFor] = useState<string | null>(null);
  // bulk one-liner import: which card's paste box is open + its text
  const [importIdx, setImportIdx] = useState<number | null>(null);
  const [importText, setImportText] = useState("");

  const pools = cfgQ.data?.pools ?? Object.keys(POOL_LABELS);
  const captionPools = cfgQ.data?.caption_pools ?? {};
  const matrix = cfgQ.data?.matrix;

  useEffect(() => {
    const c = cfgQ.data?.config;
    if (!c) return;
    setEnabled(!!c.enabled);
    setPpvs((c.ppvs ?? []).map((p) => ({ ...blankPpv(), ...p })));
    const qh = c.quiet_hours;
    if (Array.isArray(qh) && qh.length === 2) {
      setQuietOn(true);
      setQuietStart(qh[0]);
      setQuietEnd(qh[1]);
    } else {
      setQuietOn(false);
    }
    const caps = c.ppv_caps ?? {};
    setCapDay(caps.per_day ?? 0);
    setCapWeek(caps.per_week ?? 0);
    setCapMonth(caps.per_month ?? 0);
  }, [cfgQ.data]);

  if (!accountId) return <div className="text-sm text-fg-dim">Pick an account above.</div>;
  if (cfgQ.isLoading) return <div className="text-sm text-fg-dim">Loading…</div>;

  const markDirty = () => {
    if (saveM.isSuccess || saveM.isError) saveM.reset();
  };
  const setPpv = (i: number, patch: Partial<PpvItem>) => {
    markDirty();
    setPpvs((ps) => ps.map((p, j) => (j === i ? { ...p, ...patch } : p)));
  };
  const addPpv = () => {
    markDirty();
    setPpvs((ps) => [...ps, blankPpv()]);
  };
  const addTemplate = (patch: Partial<PpvItem>) => {
    markDirty();
    setPpvs((ps) => [...ps, { ...blankPpv(), ...patch }]);
  };
  const dupPpv = (i: number) => {
    markDirty();
    setPpvs((ps) => {
      const src = ps[i];
      const copy: PpvItem = { ...src, id: newId(), name: src.name ? `${src.name} (copy)` : "" };
      return [...ps.slice(0, i + 1), copy, ...ps.slice(i + 1)];
    });
  };
  const removePpv = (i: number) => {
    markDirty();
    setPpvs((ps) => ps.filter((_, j) => j !== i));
  };
  // ⭐ a picked item = show it FREE as a teaser (preview_options is always ⊆ media)
  const togglePreview = (i: number, id: number) => {
    markDirty();
    setPpvs((ps) => ps.map((p, j) => {
      if (j !== i) return p;
      const cur = p.preview_options ?? [];
      return { ...p, preview_options: cur.includes(id) ? cur.filter((x) => x !== id) : [...cur, id] };
    }));
  };
  // each caption is its own box (a box can be one line OR a long multi-line caption)
  const setCaption = (i: number, ci: number, val: string) => {
    markDirty();
    setPpvs((ps) => ps.map((p, j) =>
      j === i ? { ...p, caption_texts: (p.caption_texts ?? []).map((c, k) => (k === ci ? val : c)) } : p));
  };
  const addCaption = (i: number) => {
    markDirty();
    setPpvs((ps) => ps.map((p, j) =>
      j === i ? { ...p, caption_texts: [...(p.caption_texts ?? []), ""] } : p));
  };
  const addDiscountCaption = (i: number) => {
    markDirty();
    setPpvs((ps) => ps.map((p, j) =>
      j === i ? { ...p, caption_texts: [...(p.caption_texts ?? []), DISCOUNT_CAPTION] } : p));
  };
  const removeCaption = (i: number, ci: number) => {
    markDirty();
    setPpvs((ps) => ps.map((p, j) =>
      j === i ? { ...p, caption_texts: (p.caption_texts ?? []).filter((_, k) => k !== ci) } : p));
  };
  // paste a list, one caption per line → each becomes its own box
  const importLines = (text: string) => text.split("\n").map((s) => s.trim()).filter(Boolean);
  const bulkImport = (i: number) => {
    const lines = importLines(importText);
    if (lines.length) {
      markDirty();
      setPpvs((ps) => ps.map((p, j) =>
        j === i ? { ...p, caption_texts: [...(p.caption_texts ?? []), ...lines] } : p));
    }
    setImportIdx(null);
    setImportText("");
  };

  const buildConfig = () => ({
    enabled,
    quiet_hours: quietOn ? ([quietStart, quietEnd] as [number, number]) : null,
    ppv_caps: {
      ...(capDay > 0 ? { per_day: capDay } : {}),
      ...(capWeek > 0 ? { per_week: capWeek } : {}),
      ...(capMonth > 0 ? { per_month: capMonth } : {}),
    },
    ppvs: ppvs.map((p) => ({
      ...p,
      name: (p.name ?? "").trim(),
      base_price_cents: Math.max(PRICE_FLOOR, Math.min(p.base_price_cents || PRICE_FLOOR, 20000)),
      sends_per_week: Math.max(1, Math.min(p.sends_per_week || 1, 14)),
    })),
  });

  const incomplete = ppvs.filter((p) => p.enabled && p.media_ids.length === 0).length;

  return (
    <Card className="p-4 space-y-5 max-w-3xl">
      <header className="flex items-center gap-2">
        <DollarSign size={16} className="text-accent" />
        <h3 className="text-sm font-medium">PPV Library (premade PPVs, auto-sent by fan group)</h3>
      </header>

      <p className="text-xs text-fg-dim leading-relaxed">
        Build your PPVs once. For each, set how often to send it. The system sends the
        {" "}<b>same media</b> to every fan group at a <b>different price</b> (big spenders pay
        more, quiet/never-paid fans pay less to pull them back), picks a fresh preview +
        caption each time, and can re-send monthly. No fan is messaged twice within 6h.
      </p>

      <label className="flex items-center gap-3 cursor-pointer">
        <input
          type="checkbox"
          className="h-4 w-4 accent-[var(--accent)]"
          checked={enabled}
          onChange={(e) => { markDirty(); setEnabled(e.target.checked); }}
        />
        <span className="text-sm">{enabled ? "Library ON" : "Library OFF"}</span>
      </label>

      <label className="flex items-center gap-2 text-xs cursor-pointer flex-wrap">
        <input
          type="checkbox"
          className="h-3.5 w-3.5 accent-[var(--accent)]"
          checked={quietOn}
          onChange={(e) => { markDirty(); setQuietOn(e.target.checked); }}
        />
        <span>Quiet hours — never send between</span>
        <input
          type="number" min={0} max={23} disabled={!quietOn}
          className={`${INPUT} w-16`} value={quietStart}
          onChange={(e) => { markDirty(); setQuietStart(Math.max(0, Math.min(Number(e.target.value) || 0, 23))); }}
        />
        <span>and</span>
        <input
          type="number" min={0} max={23} disabled={!quietOn}
          className={`${INPUT} w-16`} value={quietEnd}
          onChange={(e) => { markDirty(); setQuietEnd(Math.max(0, Math.min(Number(e.target.value) || 0, 23))); }}
        />
        <span className="text-fg-dim">(creator-local hour, 0-23)</span>
      </label>

      {/* Per-account send caps. 0 = no limit. A hit cap holds a PPV and releases
          one when the window frees. */}
      <div className="rounded-lg border border-border p-3 space-y-2">
        <div className="text-xs font-medium text-fg">Max PPVs sent (whole account) — 0 = no limit</div>
        <div className="flex flex-wrap gap-4">
          {([
            ["per day", capDay, setCapDay],
            ["per week", capWeek, setCapWeek],
            ["per month", capMonth, setCapMonth],
          ] as const).map(([label, val, set]) => (
            <label key={label} className="space-y-1 block">
              <div className="text-xs text-fg-dim">{label}</div>
              <input
                type="number" min={0} max={10000}
                className={`${INPUT} w-20`}
                value={val}
                onChange={(e) => { markDirty(); set(Math.max(0, Math.min(Number(e.target.value) || 0, 10000))); }}
              />
            </label>
          ))}
        </div>
        <div className="text-[11px] text-fg-dim/70">
          <b>Spreads sends evenly</b> across the window — e.g. 2 per day = at most one every
          12h (24h ÷ 2), 3 per day = every 8h. The spacing restarts from the last send. A
          PPV that comes due too soon waits its turn, then goes out.
        </div>
      </div>

      <fieldset disabled={!enabled} className="space-y-4" style={{ opacity: enabled ? 1 : 0.5 }}>
        {ppvs.length === 0 && (
          <div className="text-xs text-fg-dim italic">No PPVs yet — add your first below.</div>
        )}

        {ppvs.map((p, i) => (
          <div key={p.id} className="rounded-lg border border-border bg-bg-elev-1 p-3 space-y-3">
            <div className="flex items-center gap-2 flex-wrap">
              <input
                type="text"
                className={`${INPUT} w-48`}
                placeholder="PPV name (your label)"
                value={p.name ?? ""}
                onChange={(e) => setPpv(i, { name: e.target.value })}
              />
              <label className="flex items-center gap-1.5 text-xs cursor-pointer">
                <input
                  type="checkbox"
                  className="h-3.5 w-3.5 accent-[var(--accent)]"
                  checked={p.enabled}
                  onChange={(e) => setPpv(i, { enabled: e.target.checked })}
                />
                {p.enabled ? "On" : "Off"}
              </label>
              <button
                type="button"
                onClick={() => dupPpv(i)}
                className="ml-auto text-fg-dim hover:text-accent text-xs px-1 inline-flex items-center gap-1"
                title="Duplicate this PPV"
              >
                <Copy size={13} /> Duplicate
              </button>
              <button
                type="button"
                onClick={() => removePpv(i)}
                className="text-fg-dim hover:text-err text-sm px-1"
                title="Remove PPV"
              >
                ✕
              </button>
            </div>

            {/* content: ONE picker, then ⭐ a thumbnail to make it a free teaser */}
            <div className="space-y-2">
              <div className="flex items-center gap-2 flex-wrap">
                <Button size="sm" variant="secondary" onClick={() => setPicker(i)}>
                  <ImageIcon size={13} /> {p.media_ids.length ? `${p.media_ids.length} item(s)` : "Pick content"}
                </Button>
                <span className="text-[11px] text-fg-dim">
                  {p.media_ids.length === 0
                    ? "the photos/video fans pay to unlock"
                    : "tap ⭐ = show that one FREE as a teaser (rotates daily)"}
                </span>
              </div>
              {p.media_ids.length > 0 && (
                <div className="flex flex-wrap gap-2">
                  {p.media_ids.map((id, mi) => {
                    const isPrev = (p.preview_options ?? []).includes(id);
                    const thumb = mediaThumbs[id];
                    return (
                      <button
                        key={id}
                        type="button"
                        onClick={() => togglePreview(i, id)}
                        title={isPrev ? "Free teaser — tap to lock" : "Locked — tap to show free"}
                        className={cn(
                          "relative w-14 h-14 rounded-md overflow-hidden border-2 bg-bg",
                          isPrev ? "border-accent" : "border-border",
                        )}
                      >
                        {thumb ? (
                          // eslint-disable-next-line @next/next/no-img-element
                          <img src={thumb} alt="" className="w-full h-full object-cover" />
                        ) : (
                          <div className="w-full h-full grid place-items-center text-[10px] text-fg-dim">#{mi + 1}</div>
                        )}
                        <span className="absolute top-0.5 right-0.5 text-[12px] drop-shadow-[0_1px_2px_rgba(0,0,0,0.8)]">
                          {isPrev ? "⭐" : "☆"}
                        </span>
                      </button>
                    );
                  })}
                </div>
              )}
              {p.media_ids.length > 0 && (p.preview_options ?? []).length === 0 && (
                <div className="text-[11px] text-fg-dim/70">No teaser starred — fans see only a locked message (tap a ⭐ to show one free).</div>
              )}
            </div>

            <div className="flex flex-wrap gap-4">
              {/* caption pool */}
              <label className="space-y-1 block">
                <div className="text-xs text-fg-dim">Caption style</div>
                <select
                  className={`${INPUT} w-52`}
                  value={p.caption_pool_key}
                  onChange={(e) => setPpv(i, { caption_pool_key: e.target.value })}
                >
                  {pools.map((k) => (
                    <option key={k} value={k}>{POOL_LABELS[k] ?? k}</option>
                  ))}
                </select>
              </label>
              {/* base price */}
              <label className="space-y-1 block">
                <div className="text-xs text-fg-dim">Base price</div>
                <div className="flex items-center gap-1">
                  <span className="text-xs text-fg-dim">$</span>
                  <input
                    type="number"
                    min={4}
                    max={200}
                    className={`${INPUT} w-24`}
                    value={Math.round(p.base_price_cents / 100)}
                    onChange={(e) =>
                      setPpv(i, { base_price_cents: Math.max(PRICE_FLOOR, Math.min((Number(e.target.value) || 0) * 100, 20000)) })
                    }
                  />
                </div>
              </label>
              {/* how often (plain words) */}
              <label className="space-y-1 block">
                <div className="text-xs text-fg-dim">How often</div>
                <select
                  className={`${INPUT} w-40`}
                  value={p.sends_per_week}
                  onChange={(e) => setPpv(i, { sends_per_week: Number(e.target.value) || 1 })}
                >
                  {!CADENCE_OPTIONS.some((o) => o.v === p.sends_per_week) && (
                    <option value={p.sends_per_week}>{p.sends_per_week}× / week</option>
                  )}
                  {CADENCE_OPTIONS.map((o) => (
                    <option key={o.v} value={o.v}>{o.label}</option>
                  ))}
                </select>
              </label>
              {/* resend monthly */}
              <label className="flex items-end gap-1.5 text-xs cursor-pointer pb-2">
                <input
                  type="checkbox"
                  className="h-3.5 w-3.5 accent-[var(--accent)]"
                  checked={p.resend_monthly}
                  onChange={(e) => setPpv(i, { resend_monthly: e.target.checked })}
                />
                Also resend monthly
              </label>
              {/* skip already-bought */}
              <label className="flex items-end gap-1.5 text-xs cursor-pointer pb-2">
                <input
                  type="checkbox"
                  className="h-3.5 w-3.5 accent-[var(--accent)]"
                  checked={p.exclude_buyers !== false}
                  onChange={(e) => setPpv(i, { exclude_buyers: e.target.checked })}
                />
                Skip fans who already bought it
              </label>
            </div>

            {/* plain-language summary of what this card will do */}
            {matrix && (
              <div className="text-[11px] text-fg-dim">
                📣 Goes out <b>{cadenceLabel(p.sends_per_week).toLowerCase()}</b> · fans pay{" "}
                <b>{money(priceRange(p.base_price_cents, matrix)[0])}–{money(priceRange(p.base_price_cents, matrix)[1])}</b>
                {previewFor === p.id && previewM.data ? <> · ~{previewM.data.total_fans} fans</> : null}
              </div>
            )}

            {/* own captions — ONE BOX = ONE caption (short one-liner OR a long message) */}
            <div className="block space-y-1.5">
              <div className="text-xs text-fg-dim">
                Your own captions — <b>one box = one caption</b> (a box can be a short one-liner
                <i> or</i> a long multi-line message). Leave all empty to use the style pool above.
              </div>
              {(p.caption_texts ?? []).map((cap, ci) => (
                <div key={ci} className="flex items-start gap-1.5">
                  <textarea
                    rows={Math.max(2, cap.split("\n").length)}
                    className={`${INPUT} w-full font-mono text-[12px]`}
                    placeholder={ci === 0
                      ? "short: unlock this babe 🙈\n…or a long one:\n🌟 just for u 🌟\n\n{off} off today, was {was} now {now}"
                      : "another caption…"}
                    value={cap}
                    onChange={(e) => setCaption(i, ci, e.target.value)}
                  />
                  <button
                    type="button"
                    onClick={() => removeCaption(i, ci)}
                    className="text-fg-dim hover:text-err text-sm px-1 pt-2"
                    title="Remove caption"
                  >
                    ✕
                  </button>
                </div>
              ))}
              <div className="flex items-center gap-2 flex-wrap">
                <Button size="sm" variant="secondary" onClick={() => addCaption(i)}>+ Add caption</Button>
                <Button size="sm" variant="secondary" onClick={() => addDiscountCaption(i)}>+ Discount caption</Button>
                <Button
                  size="sm" variant="secondary"
                  onClick={() => { setImportIdx(importIdx === i ? null : i); setImportText(""); }}
                >
                  {importIdx === i ? "Close paste box" : "Paste many (1 per line)"}
                </Button>
              </div>
              {importIdx === i && (
                <div className="space-y-1">
                  <textarea
                    rows={5}
                    className={`${INPUT} w-full font-mono text-[12px]`}
                    placeholder={"unlock this one babe 🙈\nlast chance, dont leave me hangin\nokay im obsessed with this set"}
                    value={importText}
                    onChange={(e) => setImportText(e.target.value)}
                  />
                  <Button size="sm" onClick={() => bulkImport(i)} disabled={importLines(importText).length === 0}>
                    Add {importLines(importText).length} caption(s)
                  </Button>
                </div>
              )}
              <div className="text-[11px] text-fg-dim/70">
                {(p.caption_texts ?? []).filter((t) => t.trim()).length || 0} caption(s) — one is random-picked each send.
                Price tokens (auto-filled): <span className="font-mono">{"{now}"}</span> their price ·{" "}
                <span className="font-mono">{"{was}"}</span> old price (≈4× higher) ·{" "}
                <span className="font-mono">{"{off}"}</span> the % off.
              </div>
              <CaptionPreview
                lines={
                  (p.caption_texts ?? []).filter((t) => t.trim()).length
                    ? (p.caption_texts ?? []).filter((t) => t.trim())
                    : (captionPools[p.caption_pool_key] ?? [])
                }
                baseCents={p.base_price_cents}
              />
            </div>

            {matrix && <PriceGrid baseCents={p.base_price_cents} matrix={matrix} />}

            <div className="flex items-center gap-2 flex-wrap">
              <Button
                size="sm" variant="secondary"
                onClick={() => { setPreviewFor(p.id); previewM.mutate(p.base_price_cents); }}
                disabled={previewM.isPending}
              >
                {previewM.isPending && previewFor === p.id ? "Checking…" : "Preview audience"}
              </Button>
              {previewFor === p.id && previewM.data && (
                <span className="text-[11px] text-fg-dim">
                  {previewM.data.total_fans} fans →{" "}
                  {previewM.data.cells.map((c) => `${c.cell} ${c.recipients}@$${c.price}`).join(" · ") || "none"}
                </span>
              )}
              {previewFor === p.id && previewM.isError && (
                <span className="text-[11px] text-red-500">{previewM.error?.message || "preview failed"}</span>
              )}
            </div>

            {p.enabled && p.media_ids.length === 0 && (
              <div className="text-[11px] text-warn">Pick at least one piece of content or this PPV won&apos;t send.</div>
            )}
          </div>
        ))}

        <div className="flex items-center gap-2 flex-wrap">
          <Button size="sm" variant="secondary" onClick={addPpv}>+ Add PPV</Button>
          <span className="text-[11px] text-fg-dim">or start from:</span>
          {TEMPLATES.map((t) => (
            <Button key={t.label} size="sm" variant="secondary" onClick={() => addTemplate(t.patch)}>
              {t.emoji} {t.label}
            </Button>
          ))}
        </div>
      </fieldset>

      {/* What it all means */}
      <details className="rounded-lg border border-border p-3 text-xs text-fg-dim leading-relaxed">
        <summary className="cursor-pointer text-fg font-medium">What do these settings mean?</summary>
        <ul className="mt-2 space-y-1.5 list-disc pl-4">
          <li><b>+ Add PPV / templates / Duplicate</b> — start blank, start from a ready preset, or copy a PPV you already built (to make 20 fast).</li>
          <li><b>Content + ⭐ teaser</b> — pick the photos/video the fan pays to unlock. Then tap ⭐ on any of them to show that one FREE as a teaser (rotates daily so resends look fresh). No star = fully locked.</li>
          <li><b>Caption style</b> — a ready-made wording group; one is random-picked each send. Styles range from short &amp; sweet to long multi-paragraph (&quot;big bundle&quot;, &quot;exclusive list&quot;). <b>Your own captions</b> override it — <b>one box = one caption</b>, <b>+ Discount caption</b> drops in a price-token example, <b>Paste many</b> turns a pasted list into one caption each. Hit <b>Preview captions</b> to read them.</li>
          <li><b>Price words in captions</b> — type <span className="font-mono">{"{now}"}</span> for what that fan pays, <span className="font-mono">{"{was}"}</span> for an old price (auto ≈4× higher, so it always looks like a deal), and <span className="font-mono">{"{off}"}</span> for the % off. They fill in by themselves per fan group.</li>
          <li><b>Base price</b> — your starting price. Each fan group then pays base × their multiplier (whales more, quiet/never-paid less). The line above each card shows the range.</li>
          <li><b>How often</b> — how frequently this PPV goes out. It keeps re-sending to fans who haven&apos;t bought, with a fresh preview each time.</li>
          <li><b>Also resend monthly</b> — fire this PPV again ~30 days later, on top of the normal cadence.</li>
          <li><b>Skip fans who already bought it</b> — don&apos;t re-pitch content a fan already owns (recommended ON).</li>
          <li><b>Quiet hours</b> — never send between these hours (creator-local), e.g. while you sleep.</li>
          <li><b>Max PPVs per day/week/month</b> — a whole-account speed limit that <b>spreads sends evenly</b> (counts PPV sends, not single messages). E.g. 2/day = one about every 12h; a PPV due too soon waits its turn. 0 = no limit.</li>
          <li><b>Library ON/OFF</b> — the master switch. OFF stops everything instantly.</li>
        </ul>
      </details>

      <div className="flex items-center gap-3 pt-1">
        <Button onClick={() => saveM.mutate(buildConfig())} disabled={saveM.isPending}>
          {saveM.isPending ? "Saving…" : "Save"}
        </Button>
        {saveM.isSuccess && !saveM.isPending && <span className="text-xs text-emerald-500">Saved ✓</span>}
        {saveM.isError && <span className="text-xs text-red-500">{saveM.error?.message || "Save failed"}</span>}
        {enabled && incomplete > 0 && (
          <span className="text-xs text-warn">{incomplete} enabled PPV(s) have no content yet.</span>
        )}
      </div>

      {picker !== null && accountId && (
        <VaultPicker
          open
          onClose={() => setPicker(null)}
          accountId={accountId}
          fanId={null}
          initialSelectedIds={ppvs[picker]?.media_ids ?? []}
          onConfirm={(media) => {
            const ids = media.map((m) => m.id);
            setMediaThumbs((prev) => {
              const next = { ...prev };
              for (const m of media) {
                const raw = m.files?.thumb?.url || m.files?.squarePreview?.url || m.files?.preview?.url || null;
                if (raw) next[m.id] = proxyImage(raw, accountId);
              }
              return next;
            });
            const idx = picker;
            setPpvs((ps) => ps.map((p, j) =>
              j === idx
                ? { ...p, media_ids: ids, preview_options: (p.preview_options ?? []).filter((x) => ids.includes(x)) }
                : p));
            markDirty();
            setPicker(null);
          }}
        />
      )}
    </Card>
  );
}

/** Show the actual caption lines (custom or the chosen style pool) with the
 *  {now}/{was}/{off} discount tokens filled in, so the operator can preview wording. */
function CaptionPreview({ lines, baseCents }: { lines: string[]; baseCents: number }) {
  const [open, setOpen] = useState(false);
  const fill = (l: string) => {
    const now = roundTo99(baseCents * 0.5 * 0.55); // cheapest-cell example
    const was = anchor99(now);
    const off = was > now ? Math.round((1 - now / was) * 100) : 0;
    return l
      .replace(/\{was\}/g, money(was))
      .replace(/\{now\}/g, money(now))
      .replace(/\{off\}/g, `${off}%`);
  };
  return (
    <div className="pt-1">
      <button
        type="button"
        className="text-[11px] text-accent hover:underline"
        onClick={() => setOpen((o) => !o)}
      >
        {open ? "Hide example captions" : `Preview captions (${lines.length})`}
      </button>
      {open && (
        <ul className="mt-1 space-y-2 text-[11px] text-fg-dim border border-border rounded-md p-2 bg-bg-elev-1 max-w-xl">
          {lines.length === 0 ? (
            <li className="italic">No captions in this style yet.</li>
          ) : (
            lines.map((l, i) => (
              <li key={i} className="whitespace-pre-line border-b border-border/40 last:border-0 pb-2 last:pb-0">
                {fill(l)}
              </li>
            ))
          )}
        </ul>
      )}
    </div>
  );
}

/** Tiny preview of what each fan group pays for this PPV's base price. */
function PriceGrid({ baseCents, matrix }: { baseCents: number; matrix: PriceMatrix }) {
  const spendLabel: Record<string, string> = { whale: "Whale", mid: "Medium", low: "Small", free: "Never-paid" };
  const recencyLabel: Record<string, string> = { hot: "Just bought", warm: "This week", cool: "Cooling", quiet: "Gone quiet" };
  return (
    <div className="overflow-x-auto">
      <table className="text-[11px] border-collapse">
        <thead>
          <tr className="text-fg-dim">
            <th className="text-left pr-2 font-normal"> </th>
            {matrix.spend_bands.map((s) => (
              <th key={s.name} className="px-2 font-normal">{spendLabel[s.name] ?? s.name}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {matrix.recency_bands.map((r) => (
            <tr key={r.name}>
              <td className="text-fg-dim pr-2">{recencyLabel[r.name] ?? r.name}</td>
              {matrix.spend_bands.map((s) => (
                <td key={s.name} className="px-2 text-center text-fg tabular-nums">
                  {money(roundTo99(baseCents * s.mult * r.mult))}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
