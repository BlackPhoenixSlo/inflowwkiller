"use client";

/**
 * sellerShared — pieces shared by the two seller tabs:
 *   • 🤖 AI Chatter  (ScriptsTab)  — how the account talks / who it talks to / its pacing
 *   • 💰 AI Upseller (UpsellerTab) — the selling brain + content
 *
 * Both tabs read and write the SAME `ai_chatter_config_json` (the save REPLACES the
 * whole blob, it does not merge). The container renders one tab at a time — a tab
 * switch unmounts the other — so each mount reloads fresh config and there is no
 * cross-tab clobber, PROVIDED both tabs save the FULL sparse config with identical
 * logic. `useSellerConfig` is that single source of save truth: it always seeds the
 * two authoritative keys (`sleep_window`, `script_pack_overrides`) from the loaded
 * config, so a tab that doesn't render those editors still carries them through
 * unchanged on save.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { BookOpen, ChevronDown, FolderOpen, Image as ImageIcon, Plus, Save, Trash2 } from "lucide-react";

import { Badge, Button, Card, Textarea } from "@/components/ui/primitives";
import { VaultFolderPicker } from "@/components/settings/VaultFolderPicker";
import { VaultPicker } from "@/components/chat/VaultPicker";
import { MediaPreviewModal } from "@/components/settings/MediaPreviewModal";
import { useMediaCache } from "@/hooks/useMediaCache";
import { mirrorThumbSrc } from "@/hooks/useVaultCache";
import { proxyImage, type VaultMedia } from "@/lib/relay";
import { cn } from "@/lib/utils";
import { useSaveStyleConfig, useStyleConfig } from "@/hooks/useStyleConfig";
import {
  useAiChatterConfig, useDeleteScript, useImportFolder, usePasteImport,
  useSaveAiChatterConfig, useSaveScriptItems, useUpsertScript,
  type AiChatterConfig, type CatalogItemT, type CatalogScriptT, type GhostStage,
  type PaceBand,
} from "@/hooks/useCatalog";

export const INPUT =
  "bg-bg border border-border rounded-lg px-2 py-1.5 text-sm max-md:text-base focus:outline-none focus:border-accent";
export const TH = "text-left text-[11px] uppercase tracking-wide text-fg-dim px-2 py-1";
export const TD = "px-2 py-1 align-top";

export const dollars = (cents: number) => Math.round((cents || 0) / 100);

/* ── Human Rhythm helpers ──────────────────────────────────────────── */

/** "03:00" → "3am" (the operator reads clock time, not a 24h string). */
export function fmtClock(hhmm: string): string {
  const [h, m] = String(hhmm || "").split(":");
  const hour = Number(h);
  if (!Number.isFinite(hour)) return hhmm;
  const min = Number(m) || 0;
  const suffix = hour < 12 ? "am" : "pm";
  const h12 = hour % 12 === 0 ? 12 : hour % 12;
  return min ? `${h12}:${String(min).padStart(2, "0")}${suffix}` : `${h12}${suffix}`;
}

/** The creator timezones this agency actually books. Any zone already stored on the
 *  account is appended, so a hand-set value can never be silently dropped by the UI. */
/** "-8/-7" — the zone's winter/summer UTC offsets ("-3" alone when it doesn't
 *  observe DST). Computed via Intl at Jan 1 / Jul 1 so labels never go stale. */
export function zoneOffsets(tz: string): string {
  const at = (d: Date) => {
    const v = new Intl.DateTimeFormat("en-US", { timeZone: tz, timeZoneName: "shortOffset" })
      .formatToParts(d)
      .find((p) => p.type === "timeZoneName")?.value ?? "GMT";
    return v.replace("GMT", "") || "+0";
  };
  try {
    const y = new Date().getFullYear();
    const jan = at(new Date(Date.UTC(y, 0, 1)));
    const jul = at(new Date(Date.UTC(y, 6, 1)));
    return jan === jul ? jan : `${jan}/${jul}`;
  } catch {
    return "";
  }
}

/** Dropdown label: "America/Vancouver" → "America Vancouver (-8/-7)". */
export function zoneLabel(z: string): string {
  const off = zoneOffsets(z);
  const name = z.replace(/_/g, " ");
  return off ? `${name} (${off})` : name;
}

export const TIMEZONES: string[] = [
  "America/Los_Angeles", "America/Denver", "America/Phoenix", "America/Chicago",
  "America/New_York", "America/Toronto", "America/Vancouver", "America/Mexico_City",
  "America/Bogota", "America/Lima", "America/Santiago", "America/Sao_Paulo",
  "America/Argentina/Buenos_Aires", "Europe/London", "Europe/Dublin", "Europe/Lisbon",
  "Europe/Madrid", "Europe/Paris", "Europe/Amsterdam", "Europe/Berlin", "Europe/Rome",
  "Europe/Prague", "Europe/Warsaw", "Europe/Ljubljana", "Europe/Budapest",
  "Europe/Bucharest", "Europe/Athens", "Europe/Kyiv", "Europe/Moscow", "Europe/Istanbul",
  "Africa/Lagos", "Africa/Johannesburg", "Asia/Dubai", "Asia/Karachi", "Asia/Kolkata",
  "Asia/Bangkok", "Asia/Jakarta", "Asia/Singapore", "Asia/Manila", "Asia/Hong_Kong",
  "Asia/Tokyo", "Asia/Seoul", "Australia/Perth", "Australia/Brisbane",
  "Australia/Sydney", "Pacific/Auckland",
];

/** Text-area text → the stored line list (blank rows dropped). */
export const linesOf = (text: string): string[] =>
  text.split("\n").map((l) => l.trim()).filter(Boolean);
export const sameLines = (a: string[], b: string[]) =>
  a.length === b.length && a.every((l, i) => l === b[i]);

function thumbUrl(m: VaultMedia | undefined, accountId: string | null): string | null {
  const raw = m?.files?.thumb?.url || m?.files?.squarePreview?.url || m?.files?.preview?.url || null;
  return raw ? proxyImage(raw, accountId) : null;
}

export const NEW_ITEM: CatalogItemT = {
  kind: "video", label: "", description_for_ai: "", media_ids: [],
  preview_media_ids: [], duration_sec: null, price_cents: 0,
  tip_unlock_cents: 0, is_free_teaser: false, tags: [], enabled: true,
};

/** Previews to keep after the media set changes: those still present, plus — for an
 *  image SET of 2+ pieces with nothing marked yet — the FIRST as the free peek. That's
 *  the standard way to sell a set (show one, lock the rest), and operators routinely
 *  attach up to ~8, so it should work without hand-toggling a preview. */
export function defaultPreviews(kind: string, ids: number[], current: number[]): number[] {
  const kept = current.filter((p) => ids.includes(p));
  if (kind === "image_set" && ids.length >= 2 && kept.length === 0) return [ids[0]];
  return kept;
}

/* ── media thumbnail ───────────────────────────────────────────────── */

export function MediaThumb({ id, accountId, size = 36, dim, selected, onClick, onRemove }: {
  id: number;
  accountId: string | null;
  size?: number;
  dim?: boolean;
  selected?: boolean;
  onClick?: (m: VaultMedia | undefined) => void;
  /** Drop this media from the row. Renders an × in the top-left corner. */
  onRemove?: () => void;
}) {
  const cache = useMediaCache();
  const media = cache.get(id);
  const isVideo = media?.type === "video";
  const style = { width: size, height: size };
  const ring = selected ? "ring-2 ring-accent border-accent" : "border-border";

  // Source chain, tried in order until one loads:
  //   1. the vault thumb CACHE (`/admin/vault-ai/thumb`) — permanent, on-disk,
  //      signature-free. The reason "not all images load": the OF CDN url is
  //      IP+time-signed and 403s once it expires (the broken-tile bug), and it
  //      also needs the media METADATA in the browser cache, which isn't always
  //      primed. The cache endpoint needs only the id and never expires.
  //   2. the live OF CDN url via the image proxy — the fallback for a media id
  //      that isn't in the vault mirror (so the cache 404s).
  // On the last error we drop to the id placeholder rather than a broken tile.
  const cacheSrc = accountId ? mirrorThumbSrc(accountId, id) : null;
  const cdnSrc = thumbUrl(media, accountId);
  const chain = [cacheSrc, cdnSrc].filter(Boolean) as string[];
  const [errIdx, setErrIdx] = useState(0);
  const src = chain[errIdx] ?? null;   // null once every candidate has failed

  const inner = !src ? (
    <button type="button" style={style} title={`vault ${id}`}
      onClick={() => onClick?.(media)}
      className={cn("grid place-items-center rounded border bg-bg-elev-1 text-[8px] text-fg-dim overflow-hidden",
        ring, dim && "opacity-40")}>
      {String(id).slice(-4)}
    </button>
  ) : (
    <button type="button" style={style} title={`vault ${id}${isVideo ? " · video" : ""}`}
      onClick={() => onClick?.(media)}
      className={cn("relative rounded overflow-hidden border hover:border-accent",
        ring, dim && "opacity-40")}>
      <img src={src} alt="" className="w-full h-full object-cover"
        onError={() => setErrIdx((n) => n + 1)} />
      {isVideo && (
        <span className="absolute inset-0 grid place-items-center bg-black/25 text-white text-[10px]">▶</span>
      )}
    </button>
  );
  if (!onRemove) return inner;
  // A sibling, never a child: a <button> inside a <button> is invalid HTML and
  // React will not deliver the inner click reliably. The wrapper is
  // `inline-flex` so it keeps the bare thumb's layout inside the flex strips.
  return (
    <span className="relative inline-flex">
      {inner}
      {/* Same affordance as the mass-send tabs, mirrored to the LEFT so it
          never lands on the preview toggle that sits beside these strips. */}
      <button type="button" title="Remove from this item"
        onClick={(e) => { e.stopPropagation(); onRemove(); }}
        className="absolute -top-1 -left-1 z-10 bg-panel border border-border rounded-full size-4 grid place-items-center text-[10px] leading-none hover:text-err">
        ×
      </button>
    </span>
  );
}

/* ── per-item editable row ─────────────────────────────────────────── */

function ItemRow({ it, accountId, onChange, onRemove, onPickMedia, onPreview }: {
  it: CatalogItemT;
  accountId: string | null;
  onChange: (patch: Partial<CatalogItemT>) => void;
  onRemove: () => void;
  onPickMedia: () => void;
  onPreview: (m: VaultMedia) => void;
}) {
  const { request } = useMediaCache();
  useEffect(() => {
    if (it.media_ids.length) request(it.media_ids);
  }, [it.media_ids, request]);

  const setMedia = (ids: number[]) =>
    onChange({ media_ids: ids, preview_media_ids: defaultPreviews(it.kind, ids, it.preview_media_ids) });
  const togglePreview = (id: number) =>
    onChange({ preview_media_ids: it.preview_media_ids.includes(id)
      ? it.preview_media_ids.filter((p) => p !== id)
      : [...it.preview_media_ids, id] });
  const allUnlocked = it.media_ids.length > 0
    && it.media_ids.every((id) => it.preview_media_ids.includes(id));
  return (
    <tr className="border-t border-border/60">
      <td className={TD}>
        <select className={INPUT} value={it.kind}
          onChange={(e) => onChange({ kind: e.target.value as CatalogItemT["kind"] })}>
          <option value="video">video</option>
          <option value="image">image</option>
          <option value="image_set">image set</option>
        </select>
      </td>
      <td className={TD}>
        <input className={`${INPUT} w-36`} value={it.label ?? ""}
          placeholder="ASS TEASE…"
          onChange={(e) => onChange({ label: e.target.value })} />
      </td>
      <td className={`${TD} w-full`}>
        <Textarea rows={2} value={it.description_for_ai ?? ""}
          className="w-full text-sm"
          placeholder="What the fan SEES, present tense — the AI may only claim what's written here"
          onChange={(e) => onChange({ description_for_ai: e.target.value })} />
      </td>
      <td className={TD}>
        <div className="space-y-1">
          {it.media_ids.length > 0 && (
            <div className="flex flex-wrap gap-1">
              {it.media_ids.map((id) => (
                <MediaThumb key={id} id={id} accountId={accountId}
                  onClick={(m) => m && onPreview(m)}
                  onRemove={() => setMedia(it.media_ids.filter((m) => m !== id))} />
              ))}
            </div>
          )}
          <div className="flex items-center gap-1">
            <input className={`${INPUT} w-28`}
              value={it.media_ids.join(",")}
              placeholder="vault ids"
              onChange={(e) => setMedia(
                e.target.value.split(",").map((x) => parseInt(x.trim(), 10))
                  .filter((n) => Number.isFinite(n)),
              )} />
            <button type="button" onClick={onPickMedia} title="Pick from vault"
              className="text-fg-dim hover:text-accent p-1 rounded border border-border hover:border-accent">
              <ImageIcon size={14} />
            </button>
          </div>
          {it.media_ids.length === 0 && (
            <div className="text-[10px] text-amber-400">no media → not offerable</div>
          )}
        </div>
      </td>
      <td className={TD}>
        {it.media_ids.length === 0 ? (
          <span className="text-[10px] text-fg-dim">pick media first</span>
        ) : (
          <div className="space-y-1"
            title="Tap a media to send it UNLOCKED as the free teaser on a PPV (subset of media)">
            <div className="flex flex-wrap gap-1">
              {it.media_ids.map((id) => (
                <MediaThumb key={id} id={id} accountId={accountId} size={28}
                  selected={it.preview_media_ids.includes(id)}
                  dim={!it.preview_media_ids.includes(id)}
                  onClick={() => togglePreview(id)} />
              ))}
            </div>
            {allUnlocked && (
              <div className="text-[10px] text-amber-400">all media unlocked → fan pays for nothing</div>
            )}
          </div>
        )}
      </td>
      <td className={TD}>
        <input type="number" className={`${INPUT} w-16`} min={0}
          value={dollars(it.tip_unlock_cents)}
          onChange={(e) => onChange({ tip_unlock_cents: (parseInt(e.target.value || "0", 10) || 0) * 100 })} />
      </td>
      <td className={TD}>
        <input type="number" className={`${INPUT} w-16`} min={0}
          value={dollars(it.price_cents)}
          onChange={(e) => onChange({ price_cents: (parseInt(e.target.value || "0", 10) || 0) * 100 })} />
      </td>
      <td className={`${TD} text-center`}>
        <input type="checkbox" checked={it.is_free_teaser}
          onChange={(e) => onChange({ is_free_teaser: e.target.checked })} />
      </td>
      <td className={`${TD} text-xs text-fg-dim whitespace-nowrap`}>
        {it.stats ? `${it.stats.delivered}/${it.stats.offers}` : "0/0"}
      </td>
      <td className={TD}>
        <button className="text-fg-dim hover:text-red-400" onClick={onRemove}
          title="remove item">
          <Trash2 size={14} />
        </button>
      </td>
    </tr>
  );
}

export function ItemsTable({ items, setItems, accountId }: {
  items: CatalogItemT[];
  setItems: (items: CatalogItemT[]) => void;
  accountId: string | null;
}) {
  const { prime } = useMediaCache();
  const [pickRow, setPickRow] = useState<number | null>(null);
  const [preview, setPreview] = useState<VaultMedia | null>(null);

  const patch = (i: number, p: Partial<CatalogItemT>) =>
    setItems(items.map((x, j) => (j === i ? { ...x, ...p } : x)));
  return (
    <>
      <div className="overflow-x-auto">
      <table className="w-full min-w-[760px] text-sm">
        <thead>
          <tr>
            <th className={TH}>kind</th><th className={TH}>label</th>
            <th className={TH}>what the fan sees</th><th className={TH}>media</th>
            <th className={TH}>previews</th>
            <th className={TH}>tip $</th><th className={TH}>ppv $</th>
            <th className={TH}>free</th><th className={TH}>sold</th><th className={TH} />
          </tr>
        </thead>
        <tbody>
          {items.map((it, i) => (
            <ItemRow key={it.id ?? `new-${i}`} it={it} accountId={accountId}
              onChange={(p) => patch(i, p)}
              onRemove={() => setItems(items.filter((_, j) => j !== i))}
              onPickMedia={() => setPickRow(i)}
              onPreview={(m) => setPreview(m)} />
          ))}
        </tbody>
      </table>
      </div>

      <VaultPicker open={pickRow !== null} onClose={() => setPickRow(null)}
        accountId={accountId}
        initialSelectedIds={pickRow !== null ? items[pickRow]?.media_ids ?? [] : []}
        onConfirm={(picked) => {
          if (pickRow !== null) {
            const ids = picked.map((m) => m.id);
            prime(picked);
            patch(pickRow, {
              media_ids: ids,
              preview_media_ids: defaultPreviews(
                items[pickRow]?.kind ?? "image_set", ids, items[pickRow]?.preview_media_ids ?? []),
            });
          }
          setPickRow(null);
        }} />

      {preview && (
        <MediaPreviewModal media={preview} accountId={accountId}
          onClose={() => setPreview(null)} />
      )}
    </>
  );
}

/* ── one script editor card ────────────────────────────────────────── */

export function ScriptCard({ accountId, sc }: { accountId: string; sc: CatalogScriptT }) {
  const upsertM = useUpsertScript(accountId);
  const deleteM = useDeleteScript(accountId);
  const saveItemsM = useSaveScriptItems(accountId);
  const importM = useImportFolder(accountId);
  const pasteM = usePasteImport(accountId);

  const [name, setName] = useState(sc.name);
  const [theme, setTheme] = useState(sc.theme ?? "");
  const [status, setStatus] = useState(sc.status);
  const [items, setItems] = useState<CatalogItemT[]>(sc.items);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [pasteOpen, setPasteOpen] = useState(false);
  const [pasteText, setPasteText] = useState("");

  const lastMeta = useRef({ name: sc.name, theme: sc.theme ?? "", status: sc.status });
  const itemsKey = JSON.stringify(sc.items);
  const lastItemsKey = useRef(itemsKey);
  useEffect(() => {
    if (itemsKey !== lastItemsKey.current) {
      setItems(sc.items);
      lastItemsKey.current = itemsKey;
    }
  }, [itemsKey, sc.items]);
  useEffect(() => {
    const next = { name: sc.name, theme: sc.theme ?? "", status: sc.status };
    if (next.name !== lastMeta.current.name) setName(next.name);
    if (next.theme !== lastMeta.current.theme) setTheme(next.theme);
    if (next.status !== lastMeta.current.status) setStatus(next.status);
    lastMeta.current = next;
  }, [sc.name, sc.theme, sc.status]);

  const missingDesc = items.filter((i) => !(i.description_for_ai ?? "").trim()).length;
  const missingMedia = items.filter((i) => i.media_ids.length === 0).length;

  return (
    <Card className="p-4 space-y-3">
      <div className="flex items-center gap-2 flex-wrap">
        <input className={`${INPUT} font-medium w-48`} value={name}
          onChange={(e) => setName(e.target.value)} />
        <select className={INPUT} value={status}
          onChange={(e) => setStatus(e.target.value as CatalogScriptT["status"])}>
          <option value="draft">draft</option>
          <option value="enabled">enabled (sellable)</option>
          <option value="disabled">disabled</option>
        </select>
        <Button size="sm" variant="ghost"
          onClick={() => upsertM.mutate({ id: sc.id, name, theme, status })}>
          <Save size={14} className="mr-1" /> Save script
        </Button>
        <div className="flex-1" />
        <Button size="sm" variant="ghost" onClick={() => setPickerOpen(true)}>
          <FolderOpen size={14} className="mr-1" /> Import folder
        </Button>
        <Button size="sm" variant="ghost" onClick={() => setPasteOpen((v) => !v)}>
          Paste table
        </Button>
        <Button size="sm" variant="ghost"
          className="text-red-400"
          onClick={() => { if (confirm(`Delete script "${sc.name}" and its items?`)) deleteM.mutate(sc.id); }}>
          <Trash2 size={14} />
        </Button>
      </div>

      <Textarea rows={2} value={theme} className="w-full text-sm"
        placeholder="Theme (shown to the AI, never fans): setting, outfit, arc…"
        onChange={(e) => setTheme(e.target.value)} />

      {pasteOpen && (
        <div className="space-y-2 border border-border rounded-lg p-2">
          <Textarea rows={6} value={pasteText} className="w-full text-xs font-mono"
            placeholder={"Paste the Notion table:\n| Label | No | Video | Duration |\n| 1st [ASS TEASE] | 3 | Mirror selfie video… | 1 min |"}
            onChange={(e) => setPasteText(e.target.value)} />
          <Button size="sm" disabled={!pasteText.trim() || pasteM.isPending}
            onClick={() => pasteM.mutate({ scriptId: sc.id, table: pasteText },
              { onSuccess: () => { setPasteText(""); setPasteOpen(false); } })}>
            Import rows
          </Button>
        </div>
      )}

      <ItemsTable items={items} setItems={setItems} accountId={accountId} />

      <div className="flex items-center gap-2 flex-wrap">
        <Button size="sm" variant="ghost"
          onClick={() => setItems([...items, { ...NEW_ITEM }])}>
          <Plus size={14} className="mr-1" /> Add item
        </Button>
        <Button size="sm" disabled={saveItemsM.isPending}
          onClick={() => saveItemsM.mutate({ scriptId: sc.id, items })}>
          <Save size={14} className="mr-1" /> Save items ({items.length})
        </Button>
        {missingDesc > 0 && (
          <Badge className="bg-amber-500/15 text-amber-400">
            ⚠ {missingDesc} without description
          </Badge>
        )}
        {missingMedia > 0 && (
          <Badge className="bg-amber-500/15 text-amber-400">
            ⚠ {missingMedia} without media
          </Badge>
        )}
        {saveItemsM.isSuccess && <span className="text-xs text-green-400">saved ✓</span>}
      </div>

      <VaultFolderPicker open={pickerOpen} onClose={() => setPickerOpen(false)}
        accountId={accountId} initialSelected={[]}
        onConfirm={(names) => {
          setPickerOpen(false);
          if (names[0]) importM.mutate({ scriptId: sc.id, folder: names[0] });
        }} />
    </Card>
  );
}

/* ── Human Rhythm — how long a reply waits before it goes ──────────── */

/** Ticking any Rhythm SUB-MODE also turns Rhythm itself on. A ticked box that
 *  does nothing because its parent is off is a bug report waiting to be filed,
 *  and there are three of these now — the rule belongs in one place rather than
 *  re-derived (and re-commented) at every checkbox. Unticking is deliberately
 *  NOT the inverse: dropping a sub-mode must not switch the whole lane off. */
type RhythmSubMode = "rhythm_no_sleep" | "rhythm_pace_buckets" | "rhythm_ghost_enabled";

const rhythmSubMode = (key: RhythmSubMode, on: boolean): Partial<AiChatterConfig> =>
  ({ [key]: on, ...(on ? { rhythm_enabled: true } : {}) });

/** The shipped bands, shown when the account hasn't authored its own. Mirrors
 *  rhythm.PACE_BUCKETS — if that moves, this caption is the thing that lies. */
export const SHIPPED_PACE: PaceBand[] = [
  { pct: 85, up_to_min: 2 },
  { pct: 10, up_to_min: 6 },
  { pct: 4, up_to_min: 15 },
  { pct: 1, up_to_min: 60 },
];

/** "2 min" / "1 h 30" — the band edges read as durations, not decimals. */
function fmtMin(m: number): string {
  if (!Number.isFinite(m) || m <= 0) return "0";
  if (m < 60) return `${+m.toFixed(m < 1 ? 2 : 0)} min`;
  const h = Math.floor(m / 60);
  const rest = Math.round(m - h * 60);
  return rest ? `${h} h ${rest}` : `${h} h`;
}

/** The reply-time curve: how often a reply is fast, and how often it isn't.
 *  Rows are CUT POINTS — each band starts where the one above it ended — so the
 *  editor cannot produce a gap or an overlap, only an out-of-order edge (flagged). */
function PaceCurveEditor({ cfg, set }: {
  cfg: AiChatterConfig;
  set: (p: Partial<AiChatterConfig>) => void;
}) {
  const rows: PaceBand[] = cfg.rhythm_pace_curve?.length
    ? cfg.rhythm_pace_curve : SHIPPED_PACE;
  const on = !!cfg.rhythm_pace_buckets;
  const total = rows.reduce((a, r) => a + (Number(r.pct) || 0), 0);
  // The server normalises, so 99 or 101 is not an error — but it does mean the
  // number in the box is not the number it runs. Show both rather than pretend.
  const skewed = Math.abs(total - 100) > 0.01 && total > 0;
  const outOfOrder = rows.some((r, i) =>
    !(r.up_to_min > 0) || (i > 0 && r.up_to_min <= rows[i - 1].up_to_min));

  const write = (next: PaceBand[]) => set({ rhythm_pace_curve: next });
  const edit = (i: number, patch: Partial<PaceBand>) =>
    write(rows.map((r, j) => (j === i ? { ...r, ...patch } : r)));

  return (
    <div className="pl-6 space-y-2">
      <label className="flex items-start gap-2 cursor-pointer">
        <input type="checkbox" className="mt-0.5" checked={on}
          onChange={(e) => set(rhythmSubMode("rhythm_pace_buckets", e.target.checked))} />
        <span className="text-xs">
          <span className="font-medium text-fg">Use my own reply-time curve</span>
          <span className="block text-fg-dim">
            Off, replies follow the timing curve measured from this agency's own chat
            archive — the same one for every account. On, they use the percentages
            below. Only affects ordinary replies — the pause after
            a gif and the first few messages of a new chat keep their own timing.
          </span>
        </span>
      </label>

      <div className={cn("space-y-1.5", !on && "opacity-50 pointer-events-none")}>
        <div className="flex items-center gap-2 text-[11px] text-fg-dim">
          <span className="w-16">Share</span>
          <span>replies within</span>
        </div>
        {rows.map((r, i) => {
          const from = i === 0 ? 0 : rows[i - 1].up_to_min;
          const share = total > 0 ? (Number(r.pct) || 0) / total : 0;
          return (
            <div key={i} className="flex items-center gap-2 text-xs">
              <div className="flex items-center gap-1">
                <input type="number" min={0} max={100} step="any"
                  className={`${INPUT} w-14 text-right`}
                  value={r.pct}
                  onChange={(e) => edit(i, { pct: Number(e.target.value) })} />
                <span className="text-fg-dim">%</span>
              </div>
              <span className="text-fg-dim w-20 shrink-0">
                {from > 0 ? `${fmtMin(from)} –` : "under"}
              </span>
              <input type="number" min={0} step="any"
                className={`${INPUT} w-16 text-right`}
                value={r.up_to_min}
                onChange={(e) => edit(i, { up_to_min: Number(e.target.value) })} />
              <span className="text-fg-dim">min</span>
              {skewed && (
                <span className="text-fg-dim/70">
                  → runs as {(share * 100).toFixed(1)}%
                </span>
              )}
              {rows.length > 1 && (
                <button type="button" className="text-fg-dim hover:text-red-400 ml-auto"
                  title="Remove this band"
                  onClick={() => write(rows.filter((_, j) => j !== i))}>
                  <Trash2 size={13} />
                </button>
              )}
            </div>
          );
        })}

        <div className="flex items-center gap-3 pt-0.5">
          <button type="button" className="text-[11px] text-accent hover:underline"
            onClick={() => write([...rows, {
              pct: 1,
              up_to_min: (rows[rows.length - 1]?.up_to_min ?? 0) * 2 || 2,
            }])}>
            + band
          </button>
          <button type="button" className="text-[11px] text-fg-dim hover:underline"
            onClick={() => set({ rhythm_pace_curve: null })}>
            reset to 85 / 10 / 4 / 1
          </button>
          <span className={cn("text-[11px] ml-auto",
            skewed ? "text-amber-400" : "text-fg-dim")}>
            {total.toFixed(total % 1 ? 1 : 0)}%
            {skewed && " — scaled to 100 when it runs"}
          </span>
        </div>

        {outOfOrder && (
          <div className="text-[11px] text-red-400">
            Each row&apos;s minutes must be larger than the row above it — this curve
            won&apos;t save.
          </div>
        )}
      </div>
    </div>
  );
}

/* ── Typing pacing — the gaps BETWEEN the bubbles of one reply ─────────── */

/** How she TYPES, as opposed to when she answers (which is everything above).
 *
 *  Lives behind its own Advanced disclosure because none of it is a decision the
 *  operator has to make to run the account — the defaults are fitted numbers, and
 *  the copy's job is to make the one knob that matters (`drift_pct`) legible
 *  without asking anyone to read a histogram. */
function TypingPaceEditor({ cfg, set }: {
  cfg: AiChatterConfig;
  set: (p: Partial<AiChatterConfig>) => void;
}) {
  const on = !!cfg.pacing_enabled;
  const pct = cfg.pacing_drift_pct ?? 30;
  const cap = cfg.pacing_drift_cap_s ?? 90;
  const thinkOn = cfg.pacing_think_gaps !== false;

  return (
    <div className="pl-6 space-y-2">
      <label className="flex items-start gap-2 cursor-pointer">
        <input type="checkbox" className="mt-0.5" checked={on}
          onChange={(e) => set({ pacing_enabled: e.target.checked })} />
        <span className="text-xs">
          <span className="font-medium text-fg">Type like a person, not a bot</span>
          <span className="block text-fg-dim">
            When a reply comes out as several texts in a row, vary the gaps between
            them and sometimes stop for a moment. Off, every gap is just how long the
            text takes to type — which is why almost none of them are longer than 20
            seconds, where a real person&apos;s often are.
          </span>
        </span>
      </label>

      <details className={cn(!on && "opacity-50 pointer-events-none")}>
        <summary className="cursor-pointer select-none text-[11px] text-accent hover:underline">
          Advanced
        </summary>
        <div className="mt-2 space-y-2.5 text-xs">
          <div className="flex items-center gap-2">
            <input type="number" min={0} max={100} step={5}
              className={`${INPUT} w-16 text-right`}
              value={pct}
              onChange={(e) => set({ pacing_drift_pct: Number(e.target.value) })} />
            <span className="text-fg-dim">
              % of texts that get a pause first — the creator got distracted
            </span>
          </div>
          <div className="text-fg-dim/70 pl-1">
            The setting that actually matters. Pausing a <em>little</em> before every
            text reads worse than pausing a lot before a few — it just moves the
            problem. 0 turns the pauses off.
          </div>

          <div className="flex items-center gap-2">
            <input type="number" min={0} max={105} step={5}
              className={`${INPUT} w-16 text-right`}
              value={cap}
              onChange={(e) => set({ pacing_drift_cap_s: Number(e.target.value) })} />
            <span className="text-fg-dim">seconds — the longest such pause</span>
          </div>
          <div className="text-fg-dim/70 pl-1">
            Capped at 105s: past that the reply has to be handed to the scheduler
            instead of waited out, which is a different thing entirely.
          </div>

          <label className="flex items-start gap-2 cursor-pointer pt-0.5">
            <input type="checkbox" className="mt-0.5" checked={thinkOn}
              onChange={(e) => set({ pacing_think_gaps: e.target.checked })} />
            <span>
              <span className="text-fg">Let &ldquo;is typing…&rdquo; flicker</span>
              <span className="block text-fg-dim">
                Drop the typing bubble for a few seconds mid-text, like someone who
                stopped to think. Costs no extra time — today it runs solid for
                exactly as long as the wait, every single time, which is the giveaway.
              </span>
            </span>
          </label>
        </div>
      </details>
    </div>
  );
}

/* ── The ghost cycle — whole days she doesn't answer him ───────────────── */

/** The shipped cycle, shown when the account hasn't authored its own. Mirrors
 *  _ghost.DEFAULT_CYCLE — if that moves, this is the thing that lies. */
const SHIPPED_GHOST: GhostStage[] = [
  { chat_days: 3, ghost_days: 1 },
  { chat_days: 4, ghost_days: 2 },
  { chat_days: 5, ghost_days: 2.5 },
];

/** "1 day" / "2.5 days" / "12 h" — a stage reads as a duration, not a decimal. */
function fmtDays(d: number): string {
  if (!Number.isFinite(d) || d <= 0) return "0";
  if (d < 1) return `${Math.round(d * 24)} h`;
  return `${+d.toFixed(2)} ${d === 1 ? "day" : "days"}`;
}

/** Chat a while, go dark a while, repeat. Per fan — each one's schedule runs off
 *  his OWN first message, so the roster never goes quiet on the same day. */
function GhostCycleEditor({ cfg, set }: {
  cfg: AiChatterConfig;
  set: (p: Partial<AiChatterConfig>) => void;
}) {
  const rows: GhostStage[] = cfg.rhythm_ghost_cycle?.length
    ? cfg.rhythm_ghost_cycle : SHIPPED_GHOST;
  const on = !!cfg.rhythm_ghost_enabled;
  const chat = rows.reduce((a, r) => a + (Number(r.chat_days) || 0), 0);
  const dark = rows.reduce((a, r) => a + (Number(r.ghost_days) || 0), 0);
  const total = chat + dark;
  const negative = rows.some((r) =>
    !(Number(r.chat_days) >= 0) || !(Number(r.ghost_days) >= 0));

  const write = (next: GhostStage[]) => set({ rhythm_ghost_cycle: next });
  const edit = (i: number, patch: Partial<GhostStage>) =>
    write(rows.map((r, j) => (j === i ? { ...r, ...patch } : r)));

  return (
    <div className="pl-6 space-y-2">
      <label className="flex items-start gap-2 cursor-pointer">
        <input type="checkbox" className="mt-0.5" checked={on}
          onChange={(e) => set(rhythmSubMode("rhythm_ghost_enabled", e.target.checked))} />
        <span className="text-xs">
          <span className="font-medium text-fg">
            Go quiet for whole days — the account has a life
          </span>
          <span className="block text-fg-dim">
            After a stretch of chatting, the account stops answering him for a day or
            two, then comes back. <b>Per fan</b>, on his own schedule — a good spender
            goes a day with no reply and wants one more. Everything else keeps running:
            this only silences the 1:1 AI chat.
          </span>
        </span>
      </label>

      <div className={cn("space-y-1.5", !on && "opacity-50 pointer-events-none")}>
        {rows.map((r, i) => (
          <div key={i} className="flex items-center gap-2 text-xs">
            <span className="text-fg-dim w-10 shrink-0">chat</span>
            <input type="number" min={0} step="any"
              className={`${INPUT} w-16 text-right`}
              value={r.chat_days}
              onChange={(e) => edit(i, { chat_days: Number(e.target.value) })} />
            <span className="text-fg-dim">days, then quiet</span>
            <input type="number" min={0} step="any"
              className={`${INPUT} w-16 text-right`}
              value={r.ghost_days}
              onChange={(e) => edit(i, { ghost_days: Number(e.target.value) })} />
            <span className="text-fg-dim">days</span>
            {rows.length > 1 && (
              <button type="button" className="text-fg-dim hover:text-red-400 ml-auto"
                title="Remove this stage"
                onClick={() => write(rows.filter((_, j) => j !== i))}>
                <Trash2 size={13} />
              </button>
            )}
          </div>
        ))}

        <div className="flex items-center gap-3 pt-0.5">
          <button type="button" className="text-[11px] text-accent hover:underline"
            onClick={() => write([...rows, {
              chat_days: rows[rows.length - 1]?.chat_days ?? 3,
              ghost_days: rows[rows.length - 1]?.ghost_days ?? 1,
            }])}>
            + stage
          </button>
          <button type="button" className="text-[11px] text-fg-dim hover:underline"
            onClick={() => set({ rhythm_ghost_cycle: null })}>
            reset to 3/1 · 4/2 · 5/2.5
          </button>
          <span className="text-[11px] text-fg-dim ml-auto">
            {total > 0
              ? `repeats every ${fmtDays(total)} — quiet ${fmtDays(dark)} of it `
                + `(${Math.round((dark / total) * 100)}%)`
              : "every stage is 0 — this won't save"}
          </span>
        </div>

        {negative && (
          <div className="text-[11px] text-red-400">
            Days can&apos;t be negative — this cycle won&apos;t save.
          </div>
        )}

        <div className="text-[11px] text-fg-dim/70">
          A fan mid-sale is never left hanging: a live offer or a payment cancels his
          quiet stretch and restarts his cycle. Editing the numbers re-reads where every
          fan already is, so a save can move someone into or out of a quiet day at once.
        </div>
      </div>
    </div>
  );
}

export function RhythmSection({ cfg, set, tz, setTz, utcOffset, derived, effective,
                                houseDefault = ["02:00", "06:00"] }: {
  cfg: AiChatterConfig;
  set: (p: Partial<AiChatterConfig>) => void;
  tz: string;
  setTz: (v: string) => void;
  utcOffset: number;
  derived: [string, string];
  effective: [string, string];
  /** rhythm.DEFAULT_SLEEP, handed back by the server so the label and the engine
   *  can't drift apart. */
  houseDefault?: [string, string];
}) {
  const [advanced, setAdvanced] = useState(false);
  const tzKnown = !!tz.trim() || utcOffset !== 0;
  const noSleep = !!cfg.rhythm_no_sleep;
  // No-sleep mode needs no timezone, so it can be enabled with none set.
  const canEnable = tzKnown || noSleep;
  const override = cfg.sleep_window ?? null;
  const sleepSource = cfg.rhythm_sleep_source ?? "default";
  const zones = tz && !TIMEZONES.includes(tz) ? [tz, ...TIMEZONES] : TIMEZONES;

  const setWindow = (i: 0 | 1, v: string) => {
    const base: [string, string] = override ?? [houseDefault[0], houseDefault[1]];
    const next: [string, string] = i === 0 ? [v, base[1]] : [base[0], v];
    set({ sleep_window: next });
  };

  return (
    <div className="rounded-md border border-border bg-bg-elev-1 px-3 py-2.5 space-y-3 text-sm">
      <label className={cn("flex items-start gap-2",
        canEnable ? "cursor-pointer" : "cursor-not-allowed opacity-60")}>
        <input type="checkbox" className="mt-0.5" checked={!!cfg.rhythm_enabled}
          disabled={!canEnable}
          onChange={(e) => set({ rhythm_enabled: e.target.checked })} />
        <span>
          <span className="font-medium">Human Rhythm — replies land like a person&apos;s, not a bot&apos;s</span>
          <span className="block text-fg-dim text-xs">
            Fast when you&apos;re mid-conversation, slower when it&apos;s quiet, and the account
            steps away sometimes.
          </span>
        </span>
      </label>

      {/* No-sleep mode: hot/cold/busy pacing + short breaks, no overnight sleep, no tz. */}
      <label className="flex items-start gap-2 cursor-pointer pl-6">
        <input type="checkbox" className="mt-0.5" checked={noSleep}
          onChange={(e) => set(rhythmSubMode("rhythm_no_sleep", e.target.checked))} />
        <span className="text-xs">
          <span className="font-medium text-fg">No sleep — just gets busy (no timezone needed)</span>
          <span className="block text-fg-dim">
            Keeps the hot/cold pacing and short &ldquo;stepped away&rdquo; breaks (~up to
            2 h — yoga, an errand), but <b>never an 8-hour night gap</b>. Use this instead of
            the sleep window when you don&apos;t want the account going dark overnight.
          </span>
        </span>
      </label>

      {!canEnable && (
        <div className="text-xs text-amber-400 pl-6">
          Set this creator&apos;s timezone first (or turn on <b>No sleep</b> above) —
          otherwise the account would sleep through its best hours.
        </div>
      )}

      {!!cfg.rhythm_enabled && noSleep && (
        <div className="text-xs text-fg-dim pl-6">
          It never sleeps — when it&apos;s quiet the account takes a short break (up to ~2 h) and
          comes back. No overnight gap, no timezone required.
        </div>
      )}

      {!noSleep && (
        <label className="flex items-center gap-2 text-xs text-fg-dim pl-6 flex-wrap">
          <span>Creator&apos;s timezone</span>
          <select className={`${INPUT} w-56`} value={tz}
            onChange={(e) => setTz(e.target.value)}>
            <option value="">— not set —</option>
            {zones.map((z) => (
              <option key={z} value={z}>{zoneLabel(z)}</option>
            ))}
          </select>
          {!tz && utcOffset !== 0 && (
            <span className="text-fg-dim/70">
              using the saved UTC{utcOffset > 0 ? "+" : ""}{utcOffset} offset
            </span>
          )}
        </label>
      )}

      {!!cfg.rhythm_enabled && !noSleep && (
        <div className="text-xs text-fg-dim pl-6">
          Asleep <span className="text-fg font-medium">
            {fmtClock(effective[0])}–{fmtClock(effective[1])}
          </span>{" "}
          {override ? "(you set this)."
            : sleepSource === "derived" ? "(this account's own quiet hours)."
            : "(the house default)."}{" "}
          <button type="button" className="text-accent hover:underline"
            onClick={() => setAdvanced((v) => !v)}>
            {advanced ? "close" : "change"}
          </button>
        </div>
      )}

      {advanced && !noSleep && (
        <details open className="text-xs text-fg-dim pl-6">
          <summary className="cursor-pointer select-none font-medium">Advanced</summary>
          <div className="mt-2 space-y-2">
            {/* Three sources, one radio group. It was a single "set it myself"
                checkbox, with derive-from-history as the silent otherwise — which
                made the house default unreachable on any account that had enough
                sends to derive from, i.e. all of them. */}
            <label className="flex items-start gap-2 cursor-pointer">
              <input type="radio" name="rhythm-sleep-source" className="mt-0.5"
                checked={!override && sleepSource !== "derived"}
                onChange={() => set({ rhythm_sleep_source: "default", sleep_window: null })} />
              <span>
                House default — <span className="text-fg">
                  {fmtClock(houseDefault[0])}–{fmtClock(houseDefault[1])}
                </span> on the creator&apos;s own clock
              </span>
            </label>
            <label className="flex items-start gap-2 cursor-pointer">
              <input type="radio" name="rhythm-sleep-source" className="mt-0.5"
                checked={!override && sleepSource === "derived"}
                onChange={() => set({ rhythm_sleep_source: "derived", sleep_window: null })} />
              <span>
                This account&apos;s own quiet hours, read off its send history —{" "}
                <span className="text-fg">{fmtClock(derived[0])}–{fmtClock(derived[1])}</span>
              </span>
            </label>
            <label className="flex items-start gap-2 cursor-pointer">
              <input type="radio" name="rhythm-sleep-source" className="mt-0.5"
                checked={!!override}
                onChange={() => set({ sleep_window: [houseDefault[0], houseDefault[1]] })} />
              <span>Set the sleep window myself</span>
            </label>
            <div className={override ? "flex items-center gap-2 pl-6" : "flex items-center gap-2 pl-6 opacity-50 pointer-events-none"}>
              <span>Asleep from</span>
              <input type="time" className={`${INPUT} w-28`}
                value={(override ?? houseDefault)[0]}
                onChange={(e) => setWindow(0, e.target.value)} />
              <span>until</span>
              <input type="time" className={`${INPUT} w-28`}
                value={(override ?? houseDefault)[1]}
                onChange={(e) => setWindow(1, e.target.value)} />
            </div>
            <div className="text-fg-dim/70">
              While asleep, an incoming message is answered at wake-up rather than
              instantly — and the reply says so.
            </div>
          </div>
        </details>
      )}

      {/* ── the reply-time curve ── */}
      <PaceCurveEditor cfg={cfg} set={set} />

      {/* ── how she TYPES, once she has started. Deliberately NOT gated on
           rhythm_enabled: rhythm decides when the first text lands, pacing decides
           the gaps between the rest, and an account may want either alone. ── */}
      <TypingPaceEditor cfg={cfg} set={set} />

      <GhostCycleEditor cfg={cfg} set={set} />
    </div>
  );
}

/* ── The script pack — the words are already written ───────────────── */

/** The pack editor. Takes NO lane: the copy names roles, and the
 *  lines themselves arrive in `pack` already resolved for the account's voice by
 *  `script_packs.shipped_pack` on the server — which is the half that matters,
 *  because editing one box persists the whole box onto the account. */
export function ScriptPackCard({ pack, help, text, setText, onSave, saving, saved, error,
  canSave = true }: {
  pack: Record<string, string[]>;
  /** {slot: "when it fires"} — from the server, which owns the slot schema. */
  help: Record<string, string>;
  text: Record<string, string>;
  setText: (slot: string, v: string) => void;
  onSave: () => void;
  saving: boolean;
  saved: boolean;
  /** Why the last save was rejected. Without it a failed save looks like nothing
   *  happened, and the operator walks away believing the lines are stored. */
  error?: string | null;
  /** False while the config hasn't loaded — saving then REPLACES it with placeholders. */
  canSave?: boolean;
}) {
  const slots = Object.keys(pack);
  return (
    <Card className="p-4 space-y-3">
      <div className="flex items-center gap-2">
        <BookOpen size={16} />
        <h3 className="text-sm font-medium">
          The lines — already written, edit if you want
        </h3>
      </div>
      <p className="text-xs text-fg-dim leading-relaxed">
        These are the words sent at each moment of a sale, in this account&apos;s own
        voice. They ship filled in, so you can leave every box alone and just attach
        your content above. One line per row — one is picked at random. Empty a box
        and the lines that ship with the app come back.{" "}
        <span className="font-mono text-accent">{"{name}"}</span> becomes
        the fan&apos;s name, <span className="font-mono text-accent">{"{price}"}</span> the price.
      </p>

      <div className="space-y-3">
        {slots.map((slot) => {
          const shipped = pack[slot] ?? [];
          const cur = text[slot] ?? "";
          const isDefault = sameLines(linesOf(cur), shipped);
          return (
            <div key={slot} className="rounded-lg border border-border bg-bg-elev-1 p-3 space-y-1.5">
              <div className="flex items-baseline gap-2 flex-wrap">
                <span className="text-xs font-mono text-fg">{slot}</span>
                <span className="text-[11px] text-fg-dim">· {help[slot] ?? ""}</span>
                <div className="flex-1" />
                <span className="text-[11px] text-fg-dim/70">
                  {linesOf(cur).length || shipped.length} line(s)
                </span>
                {!isDefault && (
                  <button type="button" className="text-[11px] text-accent hover:underline"
                    onClick={() => setText(slot, shipped.join("\n"))}>
                    Reset to default
                  </button>
                )}
              </div>
              <Textarea rows={Math.min(8, Math.max(3, linesOf(cur).length + 1))}
                className="w-full text-sm"
                value={cur}
                placeholder={shipped.join("\n")}
                onChange={(e) => setText(slot, e.target.value)} />
              {linesOf(cur).length === 0 && (
                <div className="text-[11px] text-fg-dim/70">
                  Empty — the {shipped.length} line(s) that ship with the app are used.
                </div>
              )}
            </div>
          );
        })}
      </div>

      <div className="flex items-center gap-2">
        <Button size="sm" disabled={saving || !canSave} onClick={onSave}>
          <Save size={14} className="mr-1" /> Save lines
        </Button>
        {saved && <span className="text-xs text-green-400">saved ✓</span>}
        {error && <span className="text-xs text-red-500">{error}</span>}
      </div>
    </Card>
  );
}

/* ── a load that FAILED, said so out loud ──────────────────────────── */

/** What a seller tab renders INSTEAD of its editors when the config never
 *  arrived. Every input on those tabs falls back to a hardcoded literal
 *  (`cfg.sla_minutes ?? 10`), so rendering anyway shows invented numbers that
 *  read as this account's saved settings — "it reset to defaults" — and one
 *  Save would write those placeholders over the real stored config. */
export function ConfigLoadError({ what, error, onRetry, retrying }: {
  /** What failed to load, in the operator's words ("this account's settings"). */
  what: string;
  error?: Error | null;
  onRetry: () => void;
  retrying?: boolean;
}) {
  return (
    <Card className="p-4 space-y-2">
      <h3 className="text-sm font-medium text-red-500">Couldn&apos;t load {what}.</h3>
      <p className="text-xs text-fg-dim leading-relaxed">
        {error?.message || "The relay didn't answer."} Nothing is shown rather than
        default values that were never yours — what you&apos;d be editing here are
        placeholders, and saving them would overwrite the settings this account
        actually has.
      </p>
      <Button size="sm" variant="secondary" disabled={retrying} onClick={onRetry}>
        {retrying ? "Retrying…" : "Retry"}
      </Button>
    </Card>
  );
}

/* ── how-to guide (read-only, for the whole team) ──────────────────── */

export function GuideCard() {
  const [open, setOpen] = useState(false);
  const H = "text-sm font-medium text-fg";
  const P = "text-sm text-fg-dim leading-relaxed";
  const LI = "text-sm text-fg-dim leading-relaxed";
  const B = ({ children }: { children: React.ReactNode }) =>
    <span className="text-fg font-medium">{children}</span>;
  return (
    <Card className="p-4">
      <button type="button" onClick={() => setOpen((v) => !v)}
        className="flex items-center gap-2 w-full text-left">
        <BookOpen size={16} className="text-accent" />
        <h3 className="text-sm font-medium flex-1">How the AI Seller works — read me</h3>
        <ChevronDown size={16}
          className={cn("text-fg-dim transition-transform", open && "rotate-180")} />
      </button>

      {open && (
        <div className="mt-4 space-y-5 border-t border-border pt-4">
          <p className={P}>
            The AI Seller chats <B>as the model</B> and sells your already-filmed
            content. It may <B>only ever claim what you type in “What the fan sees”</B> —
            it never invents content, lengths, prices, or customs. Everything below is
            just data you give it; the bot does the talking.
          </p>

          <div className="space-y-1.5">
            <h4 className={H}>The columns on every row</h4>
            <ul className="list-disc pl-5 space-y-1">
              <li className={LI}><B>Kind</B> — video / image / image set. Just a label for how the
                AI describes the piece. <B>It does not change the send</B> — “image set” of 5 photos
                = one message with 5 attachments, the fan pays once to unlock all.</li>
              <li className={LI}><B>Label</B> — your internal name (e.g. “ASS TEASE”). The fan never sees it.</li>
              <li className={LI}><B>What the fan sees</B> — the pitch contract. Present tense, only what’s
                truly in the media. This is also the <B>matching key</B>: the AI picks the piece whose
                description fits what the fan asked for — so write sharp, distinct descriptions.</li>
              <li className={LI}><B>Media</B> — the vault content delivered. Click the <ImageIcon size={12} className="inline -mt-0.5" /> button
                to <B>pick from the vault</B>. <span className="text-amber-400">No media = not offerable.</span></li>
              <li className={LI}><B>Previews</B> — tap thumbnails to mark which media go out <B>unlocked</B>
                as the free teaser on a PPV.</li>
              <li className={LI}><B>Tip $ / PPV $</B> — the two ways to sell it.</li>
              <li className={LI}><B>Free</B> — the whole piece goes out free (no paywall).</li>
            </ul>
          </div>

          <div className="space-y-1.5">
            <h4 className={H}>Scripts vs Singles — how the bot chooses what to offer</h4>
            <ul className="list-disc pl-5 space-y-1">
              <li className={LI}><B>Singles</B> = a flat pool. Every unseen single is available at once, and the AI
                picks the <B>best-fitting one by its description</B>. Flexible, no fixed order.</li>
              <li className={LI}><B>Scripts</B> = an <B>ordered ladder</B>. The bot offers items in position order
                (#1 → #2 → #3) and each unlock advances to the next. One script per fan at a time. Use it for a
                planned escalation — the price climbs by unlocking pricier items, not by begging on one.</li>
            </ul>
            <p className={P}>Already-sent pieces drop out automatically — the bot never re-offers the same media.</p>
          </div>

          <div className="space-y-1.5">
            <h4 className={H}>Always test in Simulate first</h4>
            <p className={P}>
              The <B>Simulate</B> box runs the real prompt + AI against a fake fan line and shows the exact bubbles
              and the offer it would make — <B>nothing is sent</B>. Catch a bad script or a missing price here, never
              on a paying fan.
            </p>
          </div>
        </div>
      )}
    </Card>
  );
}

/* ── shared config state + full sparse save (single source of truth) ─── */

/** Both seller tabs use this so their SAVE behavior is byte-identical: each save
 *  posts the FULL sparse config, seeding the two authoritative keys (sleep_window,
 *  script_pack_overrides) from the loaded config even when a tab doesn't render
 *  their editors — so no tab clobbers the other's keys. */
export function useSellerConfig(accountId: string | null) {
  const cfgQ = useAiChatterConfig(accountId);
  const saveCfgM = useSaveAiChatterConfig(accountId);

  /** The stored config actually came back. Until it does there is nothing to be
   *  sparse AGAINST: `buildSparse({})` emits only the two authoritative keys
   *  ({sleep_window: null, script_pack_overrides: {}}), and posting that REPLACES
   *  the whole blob — a save on a failed load silently wipes the account's real
   *  settings. So every save path is gated on this, and the tabs disable their
   *  Save buttons with it. */
  const configLoaded = cfgQ.isSuccess && !!cfgQ.data;

  const eff: AiChatterConfig = useMemo(() => {
    const d = cfgQ.data?.defaults ?? {};
    const c = cfgQ.data?.config ?? {};
    return { ...d, ...c };
  }, [cfgQ.data]);

  const [cfg, setCfg] = useState<AiChatterConfig>({});
  useEffect(() => { setCfg(eff); }, [eff]);

  const [tz, setTz] = useState("");
  useEffect(() => { setTz(cfgQ.data?.timezone ?? ""); }, [cfgQ.data?.timezone]);

  const shippedPack = useMemo(() => cfgQ.data?.script_pack ?? {}, [cfgQ.data?.script_pack]);
  const [packText, setPackText] = useState<Record<string, string>>({});
  useEffect(() => {
    const ov = (cfgQ.data?.config?.script_pack_overrides ?? {}) as Record<string, string[]>;
    const next: Record<string, string> = {};
    for (const [slot, lines] of Object.entries(shippedPack)) {
      next[slot] = (ov[slot]?.length ? ov[slot] : lines).join("\n");
    }
    setPackText(next);
  }, [shippedPack, cfgQ.data?.config?.script_pack_overrides]);

  const packOverrides = useMemo<Record<string, string[]>>(() => {
    const out: Record<string, string[]> = {};
    for (const [slot, shipped] of Object.entries(shippedPack)) {
      const lines = linesOf(packText[slot] ?? "");
      if (lines.length && !sameLines(lines, shipped)) out[slot] = lines;
    }
    return out;
  }, [packText, shippedPack]);

  /** The full sparse config over `base` (dropping keys still at the default), plus
   *  the two authoritative keys. `base` is `cfg` merged with any just-applied patch,
   *  so a patch can be saved WITHOUT waiting for a state round-trip.
   *
   *  Only meaningful once `configLoaded` — with no loaded config every key looks
   *  like a default and the result is the two authoritative keys alone. Callers
   *  that SEND it must check that flag first. */
  const buildSparse = (base: AiChatterConfig): AiChatterConfig => {
    const d = (cfgQ.data?.defaults ?? {}) as Record<string, unknown>;
    const out: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(base)) {
      if (v !== d[k]) out[k] = v;
    }
    // Authoritative, never sparse: these equal the default when cleared, so the pass
    // above would drop them — and dropping would leave a stale server override alive.
    // `rhythm_sleep_source` is the subtle one: it's a plain string, so switching an
    // account BACK from "derived" to the house default matches d[k] and vanishes
    // from the payload, leaving "derived" stored. The setting would look like it
    // moved and wouldn't have.
    out.sleep_window = base.sleep_window ?? null;
    out.rhythm_pace_curve = base.rhythm_pace_curve ?? null;
    // Same nullable-list shape as the pace curve, and the same reason: null is
    // both "reset me to the shipped cycle" and the default, so the sparse pass
    // above cannot tell a reset from a no-op. Getting it wrong here leaves the
    // OLD cycle stored — i.e. an account that keeps going dark on days the
    // operator thought they had just cleared.
    out.rhythm_ghost_cycle = base.rhythm_ghost_cycle ?? null;
    out.rhythm_sleep_source = base.rhythm_sleep_source ?? "default";
    out.script_pack_overrides = packOverrides;
    return out as AiChatterConfig;
  };
  const sparseCfg = useMemo<AiChatterConfig>(() => buildSparse(cfg),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [cfg, cfgQ.data, packOverrides]);

  const set = (p: Partial<AiChatterConfig>) => setCfg((c) => ({ ...c, ...p }));
  /** Save. An optional `patch` is applied to state AND folded into the saved config
   *  synchronously (for one-click presets like "Enable upseller"). */
  const saveCfg = (patch?: Partial<AiChatterConfig>) => {
    // A save is a REPLACE. Never write on top of a config we never read — that
    // posts placeholders as if they were the operator's settings. The tabs also
    // disable Save on `configLoaded`; this is the belt to that pair of braces.
    if (!configLoaded) return;
    const base = patch ? { ...cfg, ...patch } : cfg;
    if (patch) setCfg(base);
    saveCfgM.mutate({ config: buildSparse(base), timezone: tz.trim() || null });
  };

  /** The starter templates, resolved for the account's lane by the server. `[]`
   *  until the response lands, which the button already handles (nothing to add). */
  const starterSingles = useMemo(
    () => cfgQ.data?.starter_singles ?? [], [cfgQ.data?.starter_singles]);

  /** {slot: "when it fires"}, from the server beside the slots it names. */
  const slotHelp = useMemo(
    () => cfgQ.data?.slot_help ?? {}, [cfgQ.data?.slot_help]);

  return {
    cfgQ, saveCfgM, configLoaded, eff, cfg, setCfg, set, tz, setTz, starterSingles, slotHelp,
    shippedPack, packText, setPackText, packOverrides, sparseCfg, saveCfg,
  };
}

/** Texting-style opt-ins (human style / typos / non-native) — the style_config_json
 *  store the Auto Convo tab + rule editor use. Chatter-tab only, but shared here so
 *  the load/save shape is written once. */
export function useSellerStyle(accountId: string | null) {
  const styleQ = useStyleConfig(accountId);
  const saveStyleM = useSaveStyleConfig(accountId);
  const [humanStyle, setHumanStyle] = useState(false);
  const [typosOn, setTyposOn] = useState(false);
  const [nonnativeOn, setNonnativeOn] = useState(false);
  const [catStickers, setCatStickers] = useState(true);
  const [consistencyOn, setConsistencyOn] = useState(false);
  const [spacingOn, setSpacingOn] = useState(true);
  useEffect(() => {
    const c = { ...(styleQ.data?.defaults ?? {}), ...(styleQ.data?.config ?? {}) } as Record<string, unknown>;
    setHumanStyle(Boolean(c["ai_chatter"]));
    setTyposOn(Boolean(c["typos_ai_chatter"]));
    setNonnativeOn(Boolean(c["nonnative_ai_chatter"]));
    // account-wide, default ON — absent (older relay) still renders checked
    setCatStickers(c["cat_stickers"] !== false);
    // OFF unless explicitly on: this one bills a second AI call per reply, so an
    // absent key must never render checked (matches load_consistency_flags).
    setConsistencyOn(Boolean(c["consistency_ai_chatter"]));
    // Tri-state like its parent: ON for ai_chatter unless explicitly turned off,
    // so an older relay that never stored the key still renders the live behaviour.
    setSpacingOn(c["nonnative_spacing_ai_chatter"] !== false);
  }, [styleQ.data]);
  const saveStyle = () => saveStyleM.mutate({
    ai_chatter: humanStyle, typos_ai_chatter: typosOn, nonnative_ai_chatter: nonnativeOn,
    cat_stickers: catStickers, consistency_ai_chatter: consistencyOn,
    nonnative_spacing_ai_chatter: spacingOn,
  });
  return { saveStyleM, humanStyle, setHumanStyle, typosOn, setTyposOn, nonnativeOn, setNonnativeOn,
           catStickers, setCatStickers, consistencyOn, setConsistencyOn,
           spacingOn, setSpacingOn, saveStyle };
}
