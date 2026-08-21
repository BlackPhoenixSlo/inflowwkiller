"use client";

/**
 * PPVLibraryTab — Automations → "💸 PPV Library".
 *
 * Build ~20 premade PPVs once. Each = vault media + a caption pool + a base price
 * + free-teaser picks + "sends per week". On Save the backend upserts ONE `ppv_send`
 * rule for the whole account; each hourly tick, the backend rotator picks whichever
 * enabled PPV is due again (a week ÷ its sends-per-week since its last send) — so
 * the per-PPV ✓ ticks here are the only per-PPV switch. A firing send fans the SAME
 * media out to spend×recency fan segments at a per-segment price (base × matrix
 * multiplier), rotating the preview + caption per send.
 *
 * Writes account_ai_config.ppv_library_config_json via /admin/ppv-library-config.
 */

import { useEffect, useMemo, useState } from "react";
import { DollarSign, Image as ImageIcon, Copy } from "lucide-react";

import { Button, Card } from "@/components/ui/primitives";
import { EditRawJsonButton } from "./JsonConfigModal";
import { VaultPicker } from "@/components/chat/VaultPicker";
import { useVaultMediaByIds } from "@/hooks/useVaultMediaByIds";
import { useReorder } from "@/hooks/useReorder";
import { usePaidPage, PAID_PAGE_NOTE } from "@/hooks/usePaidPage";
import { cn } from "@/lib/utils";
import { proxyImage } from "@/lib/relay";
import { BroadcastAudiencePicker } from "@/components/audience/BroadcastAudiencePicker";
import { DEFAULT_BROADCAST_LISTS } from "@/components/audience/fanLists";
import {
  usePpvLibraryConfig,
  useSavePpvLibraryConfig,
  useSuggestPpvs,
  type PpvSuggestions,
  usePpvPreview,
  usePostPpvToFeed,
  usePreviewPpvToFeed,
  type PpvItem,
  type PriceMatrix,
  type PreviewPpvToFeedResult,
} from "@/hooks/usePpvLibraryConfig";

/** Size-free base so the mono caption editors can append their OWN font size.
 *  These class strings are plain template concatenation (no twMerge), so a size
 *  baked into INPUT would sit alongside the caller's `text-[12px]` and the
 *  winner would depend on stylesheet order. */
const INPUT_BASE =
  "bg-bg border border-border rounded-lg px-3 py-2 focus:outline-none focus:border-accent";
const INPUT = `${INPUT_BASE} text-sm max-md:text-base`;

// Feed-post caption styles (public voice) — keys mirror PPV_FEED_CAPTION_POOLS.
const FEED_POOL_LABELS: Record<string, string> = {
  feed_new_drop: "New drop",
  feed_flash_sale: "Flash sale (% off)",
  feed_bundle_drop: "Bundle drop",
  feed_teaser: "Teaser",
  feed_video_drop: "Video drop",
  feed_photoset: "Photo set",
};

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
  { emoji: "💔", label: "Win-back", patch: { name: "Win-back", caption_pool_key: "winback_dormant", base_price_cents: 900, sends_per_week: 2, resend_monthly: false } },
];

function newId(): string {
  return "ppv_" + Math.random().toString(36).slice(2, 8);
}

// OF rejects any priced message under $3.00 / over $200 — the HARD limits and the
// defaults for the operator's Min/Max price limits (mirrors _PRICE_FLOOR_CENTS).
const PRICE_FLOOR = 300;
const PRICE_CEIL = 20000;
// Round to X.99, then clamp into the operator's [min, max] — a clamped price sits
// EXACTLY on the bound (mirrors backend round_to_99). Bounds are REQUIRED so no
// call site can silently show an unclamped price.
function roundTo99(cents: number, minC: number, maxC: number): number {
  const dollars = Math.round(cents / 100);
  const c = dollars < 1 ? 99 : dollars * 100 - 1;
  return Math.max(minC, Math.min(c, maxC));
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
function priceRange(baseCents: number, matrix: PriceMatrix, minC: number, maxC: number): [number, number] {
  let lo = Infinity;
  let hi = 0;
  for (const s of matrix.spend_bands) {
    for (const r of matrix.recency_bands) {
      const c = roundTo99(baseCents * s.mult * r.mult, minC, maxC);
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
    feed_enabled: true,
  };
}

const DISCOUNT_CAPTION = "{off} off today babe 🙈 was {was} now just {now}";

// House-default send caps — canonical copy lives in ppv_send._DEFAULT_PPV_CAPS
// and rides in on the API's `defaults` payload; this is the ONE client-side
// fallback literal (fetch-error edge only). Values and prose both render from
// the merged `capDef` below, so a server-side default bump shows up here.
const CAP_DEFAULTS = { per_day: 2, per_week: 14, per_month: 60 };

export default function PPVLibraryTab({ accountId }: { accountId: string | null }) {
  const cfgQ = usePpvLibraryConfig(accountId);
  const saveM = useSavePpvLibraryConfig(accountId);
  const previewM = usePpvPreview(accountId);
  const postM = usePostPpvToFeed(accountId);
  const previewFeedM = usePreviewPpvToFeed(accountId);
  // A subscription page has no paid-post lane on OF, and a feed PPV is always
  // priced — so every feed control here is dead for it (the relay refuses too).
  const { isPaidPage } = usePaidPage(accountId);
  // which "Post to feed" is in flight: a PPV id, or "__random__" for the bottom button
  const [postFor, setPostFor] = useState<string | null>(null);
  // the generated candidate for the GLOBAL "post fresh PPV" flow: Generate → this
  // card (name/caption/price/counts) → Post to feed / Regenerate
  const [feedCandidate, setFeedCandidate] = useState<PreviewPpvToFeedResult | null>(null);

  const [enabled, setEnabled] = useState(false);
  const [ppvs, setPpvs] = useState<PpvItem[]>([]);
  const suggestM = useSuggestPpvs(accountId);
  // Generated-but-not-yet-added proposals, and which of them are ticked.
  const [suggested, setSuggested] = useState<PpvSuggestions | null>(null);
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [suggestNote, setSuggestNote] = useState("");
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
  // "send to everyone" broadcast (default ON) + the optional pause
  const [reachAll, setReachAll] = useState(true);
  // null = "never configured" → the historical fans + following. An EMPTY SET is
  // a real operator choice and must survive as one; see buildConfig below.
  const [bcastInclude, setBcastInclude] = useState<Set<string> | null>(null);
  const [bcastExclude, setBcastExclude] = useState<Set<string>>(new Set());
  const [spendCapOn, setSpendCapOn] = useState(false);
  const [spendCapDollars, setSpendCapDollars] = useState(400);
  const [pauseOn, setPauseOn] = useState(false);
  const [pauseHours, setPauseHours] = useState(12);
  // global price limits (DOLLARS in the UI, cents on the wire) — defaults $3/$200
  const [priceMin, setPriceMin] = useState(3);
  const [priceMax, setPriceMax] = useState(200);
  // "your Max is below what your best content is worth" — shown ONCE per visit,
  // then dismissed either way (raised or kept). Nagging on every render is how a
  // warning gets trained out of an operator's eye.
  const [bandDismissed, setBandDismissed] = useState(false);
  // which card's audience preview is showing
  const [previewFor, setPreviewFor] = useState<string | null>(null);
  // bulk one-liner import: which card's paste box is open + its text
  const [importIdx, setImportIdx] = useState<number | null>(null);
  const [importText, setImportText] = useState("");

  // Resolve SAVED media ids → thumbnails so the grid shows real images after a
  // RELOAD. mediaThumbs only holds items picked this session (from the picker's
  // onConfirm); a reload restores media_ids from config but no thumbs, so the
  // tiles fell back to "#N". Fan out a by-id vault read per stored id (cached,
  // 404-tolerant) and prefer the session cache when present.
  const allMediaIds = useMemo(
    () => Array.from(new Set(ppvs.flatMap((p) => p.media_ids))),
    [ppvs],
  );
  const resolvedById = useVaultMediaByIds(accountId, allMediaIds);
  const thumbFor = (id: number): string => {
    if (mediaThumbs[id]) return mediaThumbs[id];
    const f = resolvedById[id]?.files;
    const raw = f?.thumb?.url || f?.squarePreview?.url || f?.preview?.url || null;
    return raw ? proxyImage(raw, accountId) : "";
  };

  // Top of the priciest clip's derived band, in whole dollars, capped at OF's $200
  // ceiling. 0 (no content / no evidence) ⇒ no warning.
  const bandMaxDollars = Math.min(
    Math.ceil((cfgQ.data?.content_band_max_cents ?? 0) / 100),
    Math.round((cfgQ.data?.price_ceil_cents ?? PRICE_CEIL) / 100),
  );

  // Displayed house caps: the API-served defaults win, CAP_DEFAULTS only fills
  // in when the payload is missing — the single displayed truth for both the
  // input fallbacks and the header/footer prose.
  const capDef = useMemo(() => {
    // Merge key-by-key, keeping only real numbers — a null/absent wire value
    // must fall back to CAP_DEFAULTS, never shadow it through the spread.
    const wire = cfgQ.data?.defaults?.ppv_caps ?? {};
    const num = (v: unknown, d: number) => (typeof v === "number" ? v : d);
    return {
      per_day: num(wire.per_day, CAP_DEFAULTS.per_day),
      per_week: num(wire.per_week, CAP_DEFAULTS.per_week),
      per_month: num(wire.per_month, CAP_DEFAULTS.per_month),
    };
  }, [cfgQ.data]);

  const pools = cfgQ.data?.pools ?? Object.keys(POOL_LABELS);
  const captionPools = cfgQ.data?.caption_pools ?? {};
  const feedPools = cfgQ.data?.feed_pools ?? Object.keys(FEED_POOL_LABELS);
  const feedCaptionPools = cfgQ.data?.feed_caption_pools ?? {};
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
    // No stored caps = the runtime applies the house default (2/14/60 → one
    // blast per 12h) — show THAT, not 0s, so the tab reflects what actually
    // runs. A stored dict is authoritative per key (explicit 0 = off).
    const caps = c.ppv_caps;
    setCapDay(caps ? caps.per_day ?? 0 : capDef.per_day);
    setCapWeek(caps ? caps.per_week ?? 0 : capDef.per_week);
    setCapMonth(caps ? caps.per_month ?? 0 : capDef.per_month);
    setReachAll(c.reach_all ?? true);
    // Array vs not — the ONLY distinction that matters here. A stored [] means
    // "reach nobody new" and must hydrate as an empty Set; null/absent means
    // never configured and must hydrate as null so the default still applies.
    // (Don't reach for a truthiness test: [] is truthy in JS, so it happens to
    // work and reads like it doesn't.)
    setBcastInclude(Array.isArray(c.broadcast_lists)
                    ? new Set(c.broadcast_lists) : null);
    setBcastExclude(new Set((c.broadcast_exclude_lists ?? []).map(String)));
    const cap = c.max_lifetime_spend_cents ?? 0;
    setSpendCapOn(cap > 0);
    if (cap > 0) setSpendCapDollars(Math.round(cap / 100));
    const ph = c.pause_hours ?? 0;
    setPauseOn(ph > 0);
    if (ph > 0) setPauseHours(ph);
    // backend `defaults` blob doesn't carry these keys — fall back to 3/200 here
    setPriceMin(Math.round((c.price_min_cents ?? PRICE_FLOOR) / 100));
    setPriceMax(Math.round((c.price_max_cents ?? PRICE_CEIL) / 100));
  }, [cfgQ.data, capDef]);

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
  // Build a week of bundles from the vault. THREE steps, deliberately: the
  // button only GENERATES (into `suggested`, nothing touched), the operator
  // ticks which bundles they want, and only "Add" writes them into the editor —
  // where they still have to be Saved, and then armed one by one. Suggestions
  // are drafts (enabled:false, feed_enabled:false) at every stage, so no path
  // through this can start a send.
  const buildFromVault = () => {
    setSuggested(null);
    suggestM.mutate(undefined, {
      onSuccess: (res) => {
        setSuggested(res);
        // Everything ticked by default except the thin ones — those are the
        // bundles the vault could not fill, and they are the ones worth a look
        // before they go in.
        setPicked(new Set(res.ppvs.filter((p) => !res.notes[p.id]?.thin).map((p) => p.id)));
      },
    });
  };

  // Commit the ticked bundles. Re-running REPLACES a previous suggestion rather
  // than duplicating it: ids are derived from the slot, and the send ledger +
  // rotator track each PPV by that id.
  const addSuggested = () => {
    if (!suggested) return;
    const take = suggested.ppvs.filter((p) => picked.has(p.id));
    if (!take.length) return;
    markDirty();
    setPpvs((ps) => {
      const incoming = new Map(take.map((p) => [p.id, p]));
      const kept = ps.map((p) => {
        const fresh = incoming.get(p.id);
        if (!fresh) return p;
        incoming.delete(p.id);
        // Keep whatever the operator already tuned on this row (price, cadence,
        // captions); refresh only the content. Never re-arm a row.
        return { ...p, media_ids: fresh.media_ids,
                 preview_options: fresh.preview_options };
      });
      return [...kept, ...[...incoming.values()].map((p) => ({ ...blankPpv(), ...p }))];
    });
    setSuggestNote(
      `Added ${take.length} bundle${take.length === 1 ? "" : "s"}, all switched OFF. ` +
      "Review the content, press Save, then turn on the ones you want.",
    );
    setSuggested(null);
    setPicked(new Set());
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
  // ✕ drop one piece of content from this set. It must come out of
  // preview_options too: OF requires previews ⊆ media_files and rejects the send
  // with "Wrong preview" otherwise, so removing a starred teaser has to unstar it.
  const removeMedia = (i: number, id: number) => {
    markDirty();
    setPpvs((ps) => ps.map((p, j) => {
      if (j !== i) return p;
      return {
        ...p,
        media_ids: p.media_ids.filter((x) => x !== id),
        preview_options: (p.preview_options ?? []).filter((x) => x !== id),
      };
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
  // feed-post captions (used ONLY by "Post to feed") — same one-box-per-caption shape
  const setFeedCaption = (i: number, ci: number, val: string) => {
    markDirty();
    setPpvs((ps) => ps.map((p, j) =>
      j === i ? { ...p, feed_captions: (p.feed_captions ?? []).map((c, k) => (k === ci ? val : c)) } : p));
  };
  const addFeedCaption = (i: number) => {
    markDirty();
    setPpvs((ps) => ps.map((p, j) =>
      j === i ? { ...p, feed_captions: [...(p.feed_captions ?? []), ""] } : p));
  };
  const removeFeedCaption = (i: number, ci: number) => {
    markDirty();
    setPpvs((ps) => ps.map((p, j) =>
      j === i ? { ...p, feed_captions: (p.feed_captions ?? []).filter((_, k) => k !== ci) } : p));
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

  // Effective price limits in CENTS (ordered, hard-clamped) — shared by the save
  // payload and every displayed price, so the grid always matches what would send.
  const minCents = Math.max(PRICE_FLOOR, Math.min((priceMin || 3) * 100, PRICE_CEIL));
  const maxCents = Math.max(minCents, Math.min((priceMax || 200) * 100, PRICE_CEIL));

  // Live translation of the caps into the gap the runtime enforces — each cap
  // becomes window ÷ N and the WIDEST gap wins (mirror of ppv_send._cap_release),
  // so the operator sees "every ~6h 43m" instead of doing the math per field.
  const capGapH = Math.max(
    capDay > 0 ? 24 / capDay : 0,
    capWeek > 0 ? 168 / capWeek : 0,
    capMonth > 0 ? 720 / capMonth : 0,
  );
  const fmtGap = (h: number) => {
    let hh = Math.floor(h);
    let mm = Math.round((h - hh) * 60);
    if (mm === 60) { hh += 1; mm = 0; }
    return mm ? `${hh}h ${mm}m` : `${hh}h`;
  };

  const buildConfig = () => ({
    enabled,
    reach_all: reachAll,
    // ⚠️ buildConfig is a KEY LITERAL and the server validator rebuilds a fresh
    // dict from it — anything absent here is DROPPED on every save from this
    // tab. New config keys must be added in BOTH places or they silently vanish
    // the first time an operator touches an unrelated switch.
    broadcast_lists: bcastInclude === null ? null : Array.from(bcastInclude),
    broadcast_exclude_lists: Array.from(bcastExclude).map(Number).filter(Number.isFinite),
    max_lifetime_spend_cents: spendCapOn ? Math.max(0, spendCapDollars) * 100 : 0,
    pause_hours: pauseOn ? Math.max(1, Math.min(pauseHours || 1, 168)) : 0,
    quiet_hours: quietOn ? ([quietStart, quietEnd] as [number, number]) : null,
    // Always all three keys, zeros included — an absent-key blob runs the house
    // default (2/14/60), so explicit 0s are the only way "no limit" survives a save.
    ppv_caps: { per_day: capDay, per_week: capWeek, per_month: capMonth },
    price_min_cents: minCents,
    price_max_cents: maxCents,
    ppvs: ppvs.map((p) => ({
      ...p,
      name: (p.name ?? "").trim(),
      // hard OF limits ONLY — the Min/Max above clamp at SEND time, never the
      // authored base (a tightened max must not permanently rewrite prices)
      base_price_cents: Math.max(PRICE_FLOOR, Math.min(p.base_price_cents || PRICE_FLOOR, PRICE_CEIL)),
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

      {/* Global price limits: runtime clamp on every computed send/post price. */}
      <div className="rounded-lg border border-border p-3 space-y-2">
        <div className="text-xs font-medium text-fg">Price limits — every PPV &amp; post</div>
        <div className="flex items-center gap-2 text-xs flex-wrap">
          <span>Min $</span>
          <input
            type="number" min={3} max={200}
            className={`${INPUT} w-20`} value={priceMin}
            onChange={(e) => { markDirty(); setPriceMin(Math.max(0, Math.min(Number(e.target.value) || 0, 200))); }}
          />
          <span>Max $</span>
          <input
            type="number" min={3} max={200}
            className={`${INPUT} w-20`} value={priceMax}
            onChange={(e) => { markDirty(); setPriceMax(Math.max(0, Math.min(Number(e.target.value) || 0, 200))); }}
          />
        </div>
        <div className="text-[11px] text-fg-dim/70">
          Every price this library sends is kept between these — the per-group tier prices,
          the everyone-broadcast, and feed posts. Your <b>Base prices stay as typed</b>; the
          limits apply at send time ($3–$200 hard range, min ≤ max enforced on save).
          These are also the limits the AI Seller keeps to when it prices a clip in chat.
        </div>
        {/* One-shot: the operator's Max is the ceiling on every 1:1 quote too
            (upsell.next_price clamps into it), so a Max below what his best content
            is worth truncates it silently. Say it once, offer the fix, then shut up. */}
        {bandMaxDollars > priceMax && !bandDismissed && (
          <div className="flex items-center gap-2 flex-wrap text-[11px] text-warn border border-warn/30 bg-warn/5 rounded-md px-2.5 py-2">
            <span>
              Your max is ${priceMax}, but some clips are worth more. Raise it?
            </span>
            <Button
              size="sm" variant="secondary"
              onClick={() => { markDirty(); setPriceMax(bandMaxDollars); setBandDismissed(true); }}
            >
              Yes, raise to ${bandMaxDollars}
            </Button>
            <Button size="sm" variant="ghost" onClick={() => setBandDismissed(true)}>
              Keep ${priceMax}
            </Button>
          </div>
        )}
      </div>

      {/* How the 1:1 seller and the blast stay out of each other's way. Read-only —
          there is no gate toggle here: the same qualification gate on a BROADCAST
          would delete the broadcast (its job is reaching fans who aren't replying). */}
      <div className="text-[11px] text-fg-dim/80 leading-relaxed rounded-lg border border-border p-3">
        While the AI is selling to a fan 1:1, the mass PPV skips him — so he never gets
        the same clip twice.
      </div>

      {/* Send to everyone: also broadcast to fans + following at the default price. */}
      <div className="rounded-lg border border-border p-3 space-y-2">
        <label className="flex items-center gap-3 cursor-pointer">
          <input
            type="checkbox"
            className="h-4 w-4 accent-[var(--accent)]"
            checked={reachAll}
            onChange={(e) => { markDirty(); setReachAll(e.target.checked); }}
          />
          <span className="text-sm font-medium">Send to everyone (fans + following)</span>
        </label>
        <div className="text-[11px] text-fg-dim/80 leading-relaxed">
          {reachAll
            ? <>After the per-tier sends to fans we know (whales pay more, cold fans less), it fires <b>one broadcast</b> at the <b>default price (your Base price)</b> — reaching silent/uncached subs the tiers can&apos;t see. <b>Every fan already in the system is excluded</b>, whales included: they got this at their own tier price, so nobody is reached twice and no big spender is ever sent the cheap version.</>
            : <>Only fans already in the system get it (priced by tier). Silent/uncached subscribers are <b>not</b> reached.</>}
        </div>
        {reachAll && accountId && (
          <div className="pt-2 border-t border-border/60">
            <BroadcastAudiencePicker
              accountId={accountId}
              include={bcastInclude ?? new Set(DEFAULT_BROADCAST_LISTS)}
              exclude={bcastExclude}
              onToggleInclude={(id) => {
                markDirty();
                setBcastInclude((prev) => {
                  const next = new Set(prev ?? DEFAULT_BROADCAST_LISTS);
                  if (next.has(id)) next.delete(id); else next.add(id);
                  return next;
                });
              }}
              onToggleExclude={(id) => {
                markDirty();
                setBcastExclude((prev) => {
                  const next = new Set(prev);
                  if (next.has(id)) next.delete(id); else next.add(id);
                  return next;
                });
              }}
            />
          </div>
        )}
        <label className="flex items-center gap-2 text-xs cursor-pointer flex-wrap pt-2 border-t border-border/60 mt-2">
          <input
            type="checkbox"
            className="h-3.5 w-3.5 accent-[var(--accent)]"
            checked={spendCapOn}
            onChange={(e) => { markDirty(); setSpendCapOn(e.target.checked); }}
          />
          <span>Never mass-PPV a fan who has spent over</span>
          <span className="text-fg-dim">$</span>
          <input
            type="number" min={1} max={1000000} disabled={!spendCapOn}
            className={`${INPUT} w-20`} value={spendCapDollars}
            onChange={(e) => { markDirty(); setSpendCapDollars(Math.max(1, Number(e.target.value) || 1)); }}
          />
          <span>lifetime {spendCapOn ? "" : "(off)"}</span>
        </label>
        <div className="text-[11px] text-fg-dim/80 leading-relaxed">
          Pulls big spenders out of this lane completely — <b>no tier send and no
          broadcast</b> — so they stay the closer&apos;s to handle 1:1. A fan we have
          no spend history for is <b>still sent to</b>. Takes effect on the{" "}
          <b>next run</b>, not on sends already queued.
          {spendCapOn && (
            <> Heads-up: lifetime spend counts <b>everything</b> — subscriptions and
            tips, not just content. A fan on $10/month for {Math.max(1, Math.round(spendCapDollars / 10))} months
            reaches ${spendCapDollars} without ever buying a PPV. Use Preview to see
            who this drops before you save.</>
          )}
        </div>
        <label className="flex items-center gap-2 text-xs cursor-pointer flex-wrap pt-1">
          <input
            type="checkbox"
            className="h-3.5 w-3.5 accent-[var(--accent)]"
            checked={pauseOn}
            onChange={(e) => { markDirty(); setPauseOn(e.target.checked); }}
          />
          <span>Pause — skip fans we already talked to in the last</span>
          <input
            type="number" min={1} max={168} disabled={!pauseOn}
            className={`${INPUT} w-16`} value={pauseHours}
            onChange={(e) => { markDirty(); setPauseHours(Math.max(1, Math.min(Number(e.target.value) || 1, 168))); }}
          />
          <span>hours {pauseOn ? "" : "(off)"}</span>
        </label>
        <div className="text-[11px] text-fg-dim/80 leading-relaxed">
          <b>Per-fan</b> — different from the account-wide caps above, which only space
          the blasts themselves. At send time this builds an <b>exclude list</b>: every
          fan <b>we</b> messaged inside the window — by anything (AI chatter, welcome,
          follow-ups, nudges, other blasts) — or who <b>wrote to us</b>, is dropped from
          this blast&apos;s audience. ⚠️ On a busy account almost everyone has some contact
          within 12h, so this can gut the audience (−86% measured live). Fans mid-purchase
          or in a hot 1:1 chat are skipped automatically anyway — most accounts should
          leave this off.
        </div>
      </div>

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

      {/* Per-account send caps. Default 2/day · 14/week · 60/month (12h spacing);
          0 = no limit. A hit cap holds a PPV and releases it when the window frees. */}
      <div className="rounded-lg border border-border p-3 space-y-2">
        <div className="text-xs font-medium text-fg">
          Max PPVs sent (whole account) — default {capDef.per_day} / {capDef.per_week} / {capDef.per_month} · 0 = no limit
        </div>
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
          <b>Spacing between blasts — whole account.</b> Each cap becomes a minimum gap
          (window ÷ N: 2/day = every 12h, 25/wk = every ~6h 43m) and the <b>strictest one
          wins</b>. A PPV that comes due too soon isn&apos;t dropped — it waits for its slot,
          then goes out. Only these Library blasts count toward the gap; 1:1 selling,
          welcomes and nudges don&apos;t (the Pause below handles those).{" "}
          {capGapH > 0
            ? <>Your current setting → <b>at most one blast every ~{fmtGap(capGapH)}</b>.</>
            : <><b>All three at 0 → no spacing</b> — several PPVs can blast the same fan
              minutes apart.</>}{" "}
          Default {capDef.per_day}/day · {capDef.per_week}/wk · {capDef.per_month}/mo (= every 12h).
        </div>
      </div>

      {/* Deliberately OUTSIDE the disabled fieldset below: a disabled <fieldset>
          disables every control inside it, so keeping this here would mean the
          operator had to ARM the library just to populate it. Building is
          read-only and every suggestion is a draft, so it is safe with the
          master switch off — which is exactly when you'd want it. */}
      <div className="flex items-center gap-2 flex-wrap">
        <Button size="sm" variant="secondary" disabled={suggestM.isPending}
          title="Read the described vault and propose a week of bundles — content, ⭐ teasers, price, cadence and caption style. Proposes only; you pick what to add, and everything arrives switched OFF."
          onClick={buildFromVault}>
          ✨ {suggestM.isPending ? "Building…" : "Build a week from vault"}
        </Button>
        <span className="text-[11px] text-fg-dim">
          proposes bundles from your vault — nothing is added or sent until you say so
        </span>
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
              <label className="flex items-center gap-1.5 text-xs cursor-pointer"
                     title="Send this PPV as a paid message to fans on a schedule (priced by fan group)">
                <input
                  type="checkbox"
                  className="h-3.5 w-3.5 accent-[var(--accent)]"
                  checked={p.enabled}
                  onChange={(e) => setPpv(i, { enabled: e.target.checked })}
                />
                Send as PPV messages
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

            {/* Feed delivery — a SEPARATE enable from the PPV messages above. "Post to
                feed" turns on feed posting for this PPV (the button + the random picker +
                auto-post); leave "Send as PPV messages" off for a feed-only PPV. */}
            <div className="flex items-center gap-2 flex-wrap rounded-md bg-bg/40 px-2 py-1.5">
              <label className="flex items-center gap-1.5 text-xs cursor-pointer font-medium"
                     title="Make this PPV available to post to your public feed (independent of the message send)">
                <input
                  type="checkbox"
                  className="h-3.5 w-3.5 accent-[var(--accent)]"
                  checked={p.feed_enabled !== false}
                  onChange={(e) => setPpv(i, { feed_enabled: e.target.checked })}
                />
                Post to feed
              </label>
              <Button
                size="sm" variant="secondary"
                onClick={() => { setPostFor(p.id); postM.mutate(p.id); }}
                disabled={postM.isPending || p.media_ids.length === 0
                          || p.feed_enabled === false || isPaidPage}
                title={isPaidPage ? PAID_PAGE_NOTE
                       : "Post this PPV to your feed now as a paid post at its base price"}
              >
                {postM.isPending && postFor === p.id ? "Posting…" : "Post to feed now"}
              </Button>
              <label className={cn("flex items-center gap-1.5 text-xs cursor-pointer",
                                   (p.feed_enabled === false || isPaidPage)
                                     && "opacity-40 pointer-events-none")}>
                <input
                  type="checkbox"
                  className="h-3.5 w-3.5 accent-[var(--accent)]"
                  checked={(p.also_post_to_feed ?? false) && p.feed_enabled !== false
                           && !isPaidPage}
                  disabled={p.feed_enabled === false || isPaidPage}
                  onChange={(e) => setPpv(i, { also_post_to_feed: e.target.checked })}
                />
                Also auto-post to feed with each send
              </label>
              {isPaidPage && (
                <span className="text-[11px] text-fg-dim">{PAID_PAGE_NOTE}</span>
              )}
              {postFor === p.id && postM.isSuccess && (
                <span className="text-[11px] text-emerald-500">
                  Posted @ ${postM.data.price} ✓{postM.data.used_feed_caption ? " (feed caption)" : ""}
                  {postM.data.preview_count ? ` · ${postM.data.preview_count} free preview` : ""}
                </span>
              )}
              {postFor === p.id && postM.isError && (
                <span className="text-[11px] text-red-500">{postM.error?.message || "post failed"}</span>
              )}
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
                    const thumb = thumbFor(id);
                    return (
                      <div
                        key={id}
                        className={cn(
                          "group relative w-14 h-14 rounded-md overflow-hidden border-2 bg-bg",
                          isPrev ? "border-accent" : "border-border",
                        )}
                      >
                        <button
                          type="button"
                          onClick={() => togglePreview(i, id)}
                          title={isPrev ? "Free teaser — tap to lock" : "Locked — tap to show free"}
                          className="block w-full h-full"
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
                        {/* On touch there is no hover, so `opacity-0` made this
                         *  remove button invisible yet still hit-testable. Below
                         *  768px it is always visible and thumb-sized; >=768px
                         *  keeps the exact hover-reveal it has today. */}
                        <button
                          type="button"
                          onClick={() => removeMedia(i, id)}
                          title="Remove this from the set"
                          aria-label="Remove this from the set"
                          className="absolute top-0 left-0 w-4 h-4 max-md:w-6 max-md:h-6 grid place-items-center rounded-br-md
                                     bg-black/60 text-white text-[10px] max-md:text-[11px] leading-none
                                     opacity-0 max-md:opacity-100 group-hover:opacity-100 focus:opacity-100 hover:bg-red-600"
                        >
                          ✕
                        </button>
                      </div>
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
                    min={3}
                    max={200}
                    className={`${INPUT} w-24`}
                    value={Math.round(p.base_price_cents / 100)}
                    onChange={(e) =>
                      setPpv(i, { base_price_cents: Math.max(PRICE_FLOOR, Math.min((Number(e.target.value) || 0) * 100, PRICE_CEIL)) })
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
              {/* Was a checkbox ("Skip fans who already bought it"). It is now a
                  STATEMENT, because ownership is a fact about the fan, not a policy:
                  the guard runs on every priced send and cannot be turned off. Turning
                  it off re-charged fans for clips they already owned — 4 fans in prod,
                  one of them $200 twice. A checkbox that no longer does anything is
                  worse than no checkbox. */}
              <div className="flex items-end gap-1.5 text-xs text-fg-dim pb-2">
                <span aria-hidden>✓</span>
                Never re-sold to a fan who already bought it
              </div>
            </div>

            {/* plain-language summary of what this card will do */}
            {matrix && (
              <div className="text-[11px] text-fg-dim">
                📣 Goes out <b>{cadenceLabel(p.sends_per_week).toLowerCase()}</b> · fans pay{" "}
                <b>{money(priceRange(p.base_price_cents, matrix, minCents, maxCents)[0])}–{money(priceRange(p.base_price_cents, matrix, minCents, maxCents)[1])}</b>
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
                    className={`${INPUT_BASE} w-full font-mono text-sm text-[12px] max-md:text-base`}
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
                    className={`${INPUT_BASE} w-full font-mono text-sm text-[12px] max-md:text-base`}
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
                minCents={minCents}
                maxCents={maxCents}
              />
            </div>

            {/* Feed-post captions — used ONLY by "Post to feed" (public voice).
                Empty = the feed post reuses the message captions / style pool above. */}
            <details className="block rounded-lg border border-border/60 p-2.5">
              <summary className="cursor-pointer text-xs text-fg-dim">
                Feed-post captions{" "}
                <span className="text-fg-dim/60">
                  ({(p.feed_captions ?? []).filter((t) => t.trim()).length || "none"})
                </span>{" "}
                — optional, public-feed voice
              </summary>
              <div className="mt-2 space-y-1.5">
                <div className="text-[11px] text-fg-dim/70 leading-relaxed">
                  Used when this PPV goes to the <b>feed</b> (the <b>Post to feed</b> button or
                  auto-post). A feed post is public, so avoid 1:1 lines like &quot;just for u&quot;.
                  <b> Priority:</b> your own feed captions below → the <b>Feed caption style</b> →
                  otherwise it reuses the message captions/style above. Price tokens{" "}
                  (<span className="font-mono">{"{now}"}</span> /{" "}
                  <span className="font-mono">{"{was}"}</span> /{" "}
                  <span className="font-mono">{"{off}"}</span>) work here too.
                </div>
                <label className="flex items-center gap-2 text-xs flex-wrap">
                  <span className="text-fg-dim">Feed caption style (auto-picked)</span>
                  <select
                    className={`${INPUT} w-52`}
                    value={p.feed_caption_pool_key ?? ""}
                    onChange={(e) => setPpv(i, { feed_caption_pool_key: e.target.value })}
                  >
                    <option value="">— none (use my captions / message style) —</option>
                    {feedPools.map((k) => (
                      <option key={k} value={k}>{FEED_POOL_LABELS[k] ?? k}</option>
                    ))}
                  </select>
                </label>
                {(p.feed_caption_pool_key ?? "") && !(p.feed_captions ?? []).some((t) => t.trim()) && (
                  <CaptionPreview
                    lines={feedCaptionPools[p.feed_caption_pool_key as string] ?? []}
                    baseCents={p.base_price_cents}
                    minCents={minCents}
                    maxCents={maxCents}
                  />
                )}
                <div className="text-[11px] text-fg-dim/60">Or write your own feed captions (these win over the style):</div>
                {(p.feed_captions ?? []).map((cap, ci) => (
                  <div key={ci} className="flex items-start gap-1.5">
                    <textarea
                      rows={Math.max(2, cap.split("\n").length)}
                      className={`${INPUT_BASE} w-full font-mono text-sm text-[12px] max-md:text-base`}
                      placeholder={"🔥 new set just dropped 🔥 unlock below babe — {off} off today, was {was} now {now}"}
                      value={cap}
                      onChange={(e) => setFeedCaption(i, ci, e.target.value)}
                    />
                    <button
                      type="button"
                      onClick={() => removeFeedCaption(i, ci)}
                      className="text-fg-dim hover:text-err text-sm px-1 pt-2"
                      title="Remove feed caption"
                    >
                      ✕
                    </button>
                  </div>
                ))}
                <Button size="sm" variant="secondary" onClick={() => addFeedCaption(i)}>
                  + Add feed caption
                </Button>
              </div>
            </details>

            {matrix && <PriceGrid baseCents={p.base_price_cents} matrix={matrix} minCents={minCents} maxCents={maxCents} />}

            <div className="flex items-center gap-2 flex-wrap">
              <Button
                size="sm" variant="secondary"
                onClick={() => { setPreviewFor(p.id); previewM.mutate({ basePriceCents: p.base_price_cents, priceMinCents: minCents, priceMaxCents: maxCents,
                  // Send the DRAFT cap (0 when the box is unticked) so the
                  // readout answers "who does this drop" for the number
                  // currently on screen — the tab's copy promises exactly that,
                  // and omitting it would silently preview the SAVED cap.
                  maxLifetimeSpendCents: spendCapOn ? Math.max(0, spendCapDollars) * 100 : 0 }); }}
                disabled={previewM.isPending}
              >
                {previewM.isPending && previewFor === p.id ? "Checking…" : "Preview audience"}
              </Button>
              {previewFor === p.id && previewM.data && (
                <span className="text-[11px] text-fg-dim">
                  {previewM.data.total_fans} fans →{" "}
                  {previewM.data.cells.map((c) => `${c.cell} ${c.recipients}@$${c.price}`).join(" · ") || "none"}
                  {/* Name the gate whenever it shaped this number. Without it a
                      capped preview and an empty account look identical. */}
                  {!!previewM.data.spend_cap_cents && (
                    <span className="text-warn">
                      {" "}· excludes fans over ${Math.round(previewM.data.spend_cap_cents / 100)} lifetime
                    </span>
                  )}
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

      {suggestM.isError && (
        <div className="text-[11px] text-warn">Couldn&apos;t build: {suggestM.error.message}</div>
      )}
      {suggestNote && <div className="text-[11px] text-fg-dim">{suggestNote}</div>}

      {suggested && (
        <fieldset className="rounded-lg border border-border p-3 space-y-2">
          <legend className="px-1 text-sm font-medium">
            Proposed week — nothing added yet
          </legend>
          <div className="text-[11px] text-fg-dim">
            {suggested.summary.media_bound} vault items across {suggested.ppvs.length} bundles
            {" · "}{suggested.summary.pools_used} caption styles
            {" · "}{suggested.summary.sends_per_week} sends/week if you enable them all.
            Tick what you want, then Add. Everything arrives switched OFF.
          </div>
          {suggested.ppvs.map((p) => {
            const n = suggested.notes[p.id];
            const on = picked.has(p.id);
            return (
              <label key={p.id}
                className="flex items-start gap-2 rounded border border-border p-2 cursor-pointer">
                <input type="checkbox" checked={on} className="mt-1"
                  onChange={() => setPicked((cur) => {
                    const next = new Set(cur);
                    if (next.has(p.id)) next.delete(p.id); else next.add(p.id);
                    return next;
                  })} />
                <span className="min-w-0">
                  <span className="text-sm">
                    {p.name} <span className="text-fg-dim">— {money(p.base_price_cents)}</span>
                    {" "}<span className="text-fg-dim">· {p.sends_per_week}×/wk</span>
                  </span>
                  <span className="block text-[11px] text-fg-dim">
                    {p.media_ids.length} media ({n?.photos ?? 0} photo / {n?.videos ?? 0} video)
                    {" · "}{p.preview_options.length} free teaser
                    {" · style "}{p.caption_pool_key}
                    {n?.preview_unsafe ? " · ⛔ no free teaser possible — add a tame photo" : ""}
                    {n?.thin ? " · ⚠️ under 10 — the vault is thin here" : ""}
                    {n?.reused ? ` · ${n.reused} reused from another bundle` : ""}
                  </span>
                  {n?.why && <span className="block text-[11px] text-fg-dim">{n.why}</span>}
                </span>
              </label>
            );
          })}
          <div className="flex items-center gap-2">
            <Button size="sm" onClick={addSuggested} disabled={picked.size === 0}>
              Add {picked.size} to library
            </Button>
            <Button size="sm" variant="ghost"
              onClick={() => { setSuggested(null); setPicked(new Set()); }}>
              Discard
            </Button>
          </div>
        </fieldset>
      )}

      {/* What it all means */}
      <details className="rounded-lg border border-border p-3 text-xs text-fg-dim leading-relaxed">
        <summary className="cursor-pointer text-fg font-medium">What do these settings mean?</summary>
        <ul className="mt-2 space-y-1.5 list-disc pl-4">
          <li><b>+ Add PPV / templates / Duplicate</b> — start blank, start from a ready preset, or copy a PPV you already built (to make 20 fast).</li>
          <li><b>✨ Build a week from vault</b> — reads your described vault and proposes a week of bundles: content, ⭐ teasers, price, cadence and caption style, each on the style written for its job. It only <b>proposes</b> — you tick what you want, press <b>Add</b>, then <b>Save</b>. Everything arrives <b>switched OFF</b>, so nothing can send until you turn it on. Running it again refreshes the content of bundles it made before instead of duplicating them, and keeps any price/caption you changed.</li>
          <li><b>Content + ⭐ teaser</b> — pick the photos/video the fan pays to unlock. Then tap ⭐ on any of them to show that one FREE as a teaser (rotates daily so resends look fresh). No star = fully locked. <span className="max-md:hidden">Hover a thumbnail and hit the </span><span className="hidden max-md:inline">Tap the </span><b>✕</b> in its top-left corner to drop that one piece from the set (if it was the starred teaser, it un-stars too).</li>
          <li><b>Caption style</b> — a ready-made wording group; one is random-picked each send. Styles range from short &amp; sweet to long multi-paragraph (&quot;big bundle&quot;, &quot;exclusive list&quot;). <b>Your own captions</b> override it — <b>one box = one caption</b>, <b>+ Discount caption</b> drops in a price-token example, <b>Paste many</b> turns a pasted list into one caption each. Hit <b>Preview captions</b> to read them.</li>
          <li><b>Price words in captions</b> — type <span className="font-mono">{"{now}"}</span> for what that fan pays, <span className="font-mono">{"{was}"}</span> for an old price (auto ≈4× higher, so it always looks like a deal), and <span className="font-mono">{"{off}"}</span> for the % off. They fill in by themselves per fan group.</li>
          <li><b>Base price</b> — your starting price. Each fan group then pays base × their multiplier (whales more, quiet/never-paid less). The line above each card shows the range.</li>
          <li><b>How often</b> — how frequently this PPV goes out. It keeps re-sending to fans who haven&apos;t bought, with a fresh preview each time.</li>
          <li><b>Also resend monthly</b> — fire this PPV again ~30 days later, on top of the normal cadence.</li>
          <li><b>Skip fans who already bought it</b> — don&apos;t re-pitch content a fan already owns (recommended ON).</li>
          <li><b>Quiet hours</b> — never send between these hours (creator-local), e.g. while you sleep.</li>
          <li><b>Max PPVs per day/week/month</b> — a whole-account speed limit that <b>spreads sends evenly</b> (counts PPV sends, not single messages). E.g. 2/day = one about every 12h; a PPV due too soon waits its turn. 0 = no limit.</li>
          <li><b>Send to everyone (fans + following)</b> — after pricing the fans we know by tier, it sends <b>one broadcast to fans + following at the Base price</b> (everyone already messaged is excluded, so no doubles). This is how a PPV reaches silent/uncached subs — the whole free-page list, not just the few hundred cached.</li>
          <li><b>Pause</b> — don&apos;t re-message the same fan for X hours. <b>Off by default</b> (send to everyone). Turn it on + set the hours if you want breathing room between touches.</li>
          <li><b>Library ON/OFF</b> — the master switch. OFF stops everything instantly.</li>
        </ul>
      </details>

      {/* Generate → preview → confirm: post a fresh random PPV to the feed now (paid, at its base price). */}
      <div className="rounded-lg border border-border p-3 space-y-2">
        <div className="flex items-center gap-2 flex-wrap">
          <Button
            size="sm" variant="secondary"
            onClick={() => {
              setPostFor(null);
              previewFeedM.mutate(undefined, { onSuccess: (data) => setFeedCandidate(data) });
            }}
            disabled={previewFeedM.isPending || isPaidPage || ppvs.filter((p) => p.feed_enabled !== false && p.media_ids.length).length === 0}
            title={isPaidPage ? PAID_PAGE_NOTE : undefined}
          >
            {previewFeedM.isPending ? "Generating…" : feedCandidate ? "Regenerate" : "Generate a fresh PPV to post →"}
          </Button>
          {previewFeedM.isError && (
            <span className="text-xs text-red-500">{previewFeedM.error?.message || "generate failed"}</span>
          )}
          {isPaidPage && (
            <span className="text-xs text-fg-dim">{PAID_PAGE_NOTE}</span>
          )}
        </div>

        {feedCandidate && (
          <FeedCandidateCard
            candidate={feedCandidate}
            thumbFor={thumbFor}
            posting={postM.isPending && postFor === "__random__"}
            regenerating={previewFeedM.isPending}
            postSuccess={postFor === "__random__" && postM.isSuccess ? postM.data.price : null}
            postError={postFor === "__random__" && postM.isError ? (postM.error?.message || "post failed") : null}
            onPost={(mediaFiles, previews) => {
              setPostFor("__random__");
              postM.mutate(
                { ppvId: feedCandidate.ppv_id, caption: feedCandidate.caption, mediaFiles, previews },
                { onSuccess: () => setFeedCandidate(null) },
              );
            }}
            onRegenerate={() =>
              previewFeedM.mutate(undefined, { onSuccess: (data) => setFeedCandidate(data) })
            }
          />
        )}

        <div className="text-[11px] text-fg-dim/70 leading-relaxed">
          Picks one of your <b>feed-enabled</b> PPVs (a fresh caption each time, skips the one
          posted last), shows it here so you can check it, then posts it to your <b>feed</b> as a
          paid post at its <b>base price</b> — the same &quot;normal&quot; price your messages start from.
        </div>
      </div>

      {/* With ~20 PPV cards this Save sits ~15,000px down the scroll, so below
       *  768px the row pins to the bottom of the viewport. Every sticky/paint
       *  utility is `max-md:`-scoped, so at >=768px this is still exactly
       *  `flex items-center gap-3 pt-1` in normal flow. `-mx-4 px-4` bleeds to
       *  the host Card's own p-4 (note: this Card is p-4, not p-5). */}
      <div className="flex items-center gap-3 pt-1 max-md:sticky max-md:bottom-0 max-md:z-20 max-md:flex-wrap max-md:-mx-4 max-md:px-4 max-md:py-3 max-md:pb-[calc(env(safe-area-inset-bottom)+0.75rem)] max-md:bg-panel/95 max-md:backdrop-blur max-md:border-t max-md:border-border">
        <Button onClick={() => saveM.mutate(buildConfig())} disabled={saveM.isPending}>
          {saveM.isPending ? "Saving…" : "Save"}
        </Button>
        {saveM.isSuccess && !saveM.isPending && <span className="text-xs text-emerald-500">Saved ✓</span>}
        {saveM.isError && <span className="text-xs text-red-500">{saveM.error?.message || "Save failed"}</span>}
        {enabled && incomplete > 0 && (
          <span className="text-xs text-warn">{incomplete} enabled PPV(s) have no content yet.</span>
        )}
        <div className="ml-auto">
          <EditRawJsonButton surface="ppv-library-config" accountId={accountId} />
        </div>
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

/** Interactive candidate card for the "Generate a fresh PPV to post" flow.
 *  Thumbnail grid of the candidate's media: drag to reorder, ✕ to exclude,
 *  ⭐ to mark a free preview. Emits the chosen media order + preview subset to
 *  the post-now call. `previews` is always kept a subset of the included set. */
function FeedCandidateCard({
  candidate, thumbFor, posting, regenerating, postSuccess, postError, onPost, onRegenerate,
}: {
  candidate: PreviewPpvToFeedResult;
  thumbFor: (id: number) => string;
  posting: boolean;
  regenerating: boolean;
  postSuccess: number | null;
  postError: string | null;
  onPost: (mediaFiles: number[], previews: number[]) => void;
  onRegenerate: () => void;
}) {
  // Ordered id list + which are included + which are free previews. Re-seeded
  // whenever a fresh candidate object arrives — i.e. on every Generate/Regenerate,
  // even if the SAME ppv_id comes back (single feed-enabled PPV, or a random
  // repeat: the preview endpoint never advances last_posted_ppv_id). Keying on
  // the candidate OBJECT (a new reference per preview response) resets the
  // operator's edits each time; keying on ppv_id would leave them stale.
  const [order, setOrder] = useState<number[]>(candidate.media_ids);
  const [included, setIncluded] = useState<Set<number>>(() => new Set(candidate.media_ids));
  const [previews, setPreviews] = useState<Set<number>>(() => new Set(candidate.previews));

  useEffect(() => {
    setOrder(candidate.media_ids);
    setIncluded(new Set(candidate.media_ids));
    setPreviews(new Set(candidate.previews));
  }, [candidate]);

  const reorder = useReorder(order, setOrder, (id) => id);

  const toggleIncluded = (id: number) => {
    setIncluded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) {
        next.delete(id);
        // Dropping a tile removes it from previews (subset invariant).
        setPreviews((pp) => {
          if (!pp.has(id)) return pp;
          const np = new Set(pp);
          np.delete(id);
          return np;
        });
      } else {
        next.add(id);
      }
      return next;
    });
  };

  const togglePreview = (id: number) => {
    if (!included.has(id)) return; // previews ⊆ included
    setPreviews((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const mediaFiles = order.filter((id) => included.has(id));
  const previewIds = [...previews].filter((id) => mediaFiles.includes(id));
  const canPost = mediaFiles.length > 0;

  return (
    <div className="rounded-lg border border-border bg-bg-subtle p-3 space-y-2">
      <div className="text-xs font-medium">{candidate.name || candidate.ppv_id}</div>
      <div className="text-[12px] text-fg-dim whitespace-pre-wrap">{candidate.caption}</div>

      <div className="text-[11px] text-fg-dim/70">
        ${candidate.price} · {mediaFiles.length} media · {previewIds.length} free preview
        {previewIds.length === 1 ? "" : "s"}
        {" "}<span className="text-fg-dim/50">— drag to reorder · ✕ to skip · ⭐ = free teaser</span>
      </div>

      {order.length > 0 && (
        <div className="flex flex-wrap gap-2">
          {order.map((id, idx) => {
            const isIncl = included.has(id);
            const isPrev = previews.has(id) && isIncl;
            const thumb = thumbFor(id);
            const isDragging = reorder.draggingIndex === idx;
            const isDragOver = reorder.dragOverIndex === idx && !isDragging;
            return (
              <div
                key={id}
                draggable
                onDragStart={(e) => reorder.onDragStart(e, idx)}
                onDragOver={(e) => reorder.onDragOver(e, idx)}
                onDrop={(e) => reorder.onDrop(e, idx)}
                onDragEnd={reorder.onDragEnd}
                tabIndex={0}
                onKeyDown={(e) => {
                  if (e.key === "ArrowLeft") { e.preventDefault(); reorder.onKeyboardMove(idx, -1); }
                  else if (e.key === "ArrowRight") { e.preventDefault(); reorder.onKeyboardMove(idx, 1); }
                }}
                className={cn(
                  "relative w-14 h-14 rounded-md overflow-hidden border-2 bg-bg select-none",
                  "cursor-grab active:cursor-grabbing focus:outline-none focus:ring-2 focus:ring-accent",
                  isPrev ? "border-accent" : "border-border",
                  !isIncl && "opacity-30",
                  isDragging && "opacity-40 border-accent",
                  isDragOver && "ring-2 ring-accent",
                )}
              >
                {thumb ? (
                  // eslint-disable-next-line @next/next/no-img-element
                  <img src={thumb} alt="" draggable={false} className="w-full h-full object-cover pointer-events-none" />
                ) : (
                  <div className="w-full h-full grid place-items-center text-[10px] text-fg-dim">#{idx + 1}</div>
                )}
                {/* ⭐ free-preview toggle (only meaningful when included) */}
                <button
                  type="button"
                  onClick={() => togglePreview(id)}
                  disabled={!isIncl}
                  title={isPrev ? "Free teaser — tap to lock" : "Locked — tap to show free"}
                  className="absolute top-0.5 left-0.5 text-[12px] drop-shadow-[0_1px_2px_rgba(0,0,0,0.8)] disabled:opacity-40"
                >
                  {isPrev ? "⭐" : "☆"}
                </button>
                {/* ✕ exclude/include toggle */}
                <button
                  type="button"
                  onClick={() => toggleIncluded(id)}
                  title={isIncl ? "Skip this one (don't post)" : "Include this one"}
                  className="absolute top-0.5 right-0.5 w-4 h-4 rounded-full bg-black/70 text-white grid place-items-center text-[10px] opacity-90"
                  aria-label={isIncl ? "Exclude" : "Include"}
                >
                  {isIncl ? "×" : "+"}
                </button>
              </div>
            );
          })}
        </div>
      )}

      <div className="flex items-center gap-2 flex-wrap">
        <Button
          size="sm"
          onClick={() => onPost(mediaFiles, previewIds)}
          disabled={posting || !canPost}
        >
          {posting ? "Posting…" : "Post to feed"}
        </Button>
        <Button size="sm" variant="secondary" onClick={onRegenerate} disabled={regenerating}>
          {regenerating ? "Generating…" : "Regenerate"}
        </Button>
        {postSuccess !== null && (
          <span className="text-xs text-emerald-500">Posted @ ${postSuccess} ✓</span>
        )}
        {postError && <span className="text-xs text-red-500">{postError}</span>}
        {!canPost && (
          <span className="text-[11px] text-warn">Include at least one item to post.</span>
        )}
      </div>
    </div>
  );
}

/** Show the actual caption lines (custom or the chosen style pool) with the
 *  {now}/{was}/{off} discount tokens filled in, so the operator can preview wording. */
function CaptionPreview({ lines, baseCents, minCents, maxCents }:
  { lines: string[]; baseCents: number; minCents: number; maxCents: number }) {
  const [open, setOpen] = useState(false);
  const fill = (l: string) => {
    const now = roundTo99(baseCents * 0.5 * 0.55, minCents, maxCents); // cheapest-cell example
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
function PriceGrid({ baseCents, matrix, minCents, maxCents }:
  { baseCents: number; matrix: PriceMatrix; minCents: number; maxCents: number }) {
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
                  {money(roundTo99(baseCents * s.mult * r.mult, minCents, maxCents))}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
