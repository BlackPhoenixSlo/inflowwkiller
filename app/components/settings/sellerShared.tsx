"use client";

/**
 * sellerShared — pieces shared by the two seller tabs:
 *   • 🤖 AI Chatter  (ScriptsTab)  — how she talks / who she talks to / her pacing
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
import { proxyImage, type VaultMedia } from "@/lib/relay";
import { cn } from "@/lib/utils";
import { useSaveStyleConfig, useStyleConfig } from "@/hooks/useStyleConfig";
import {
  useAiChatterConfig, useDeleteScript, useImportFolder, usePasteImport,
  useSaveAiChatterConfig, useSaveScriptItems, useUpsertScript,
  type AiChatterConfig, type CatalogItemT, type CatalogScriptT,
} from "@/hooks/useCatalog";

export const INPUT =
  "bg-bg border border-border rounded-lg px-2 py-1.5 text-sm focus:outline-none focus:border-accent";
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

/** WHEN each shipped slot fires, in plain English. */
export const SLOT_HELP: Record<string, string> = {
  question_hook: "she opens a scene — free, never has a price on it",
  rung_open: "the first thing he ever sees with a price on it",
  rung_escalate: "after he buys, she asks for more",
  post_buy_bridge: "right after he unlocks — free, keeps the conversation alive",
  edge_hold: "a short beat to keep him talking mid-scene",
  pre_ppv_stall: "while she's “filming it right now”, just before the paid message",
  objection_price: "he says it's too expensive — she answers about the content, not his wallet",
  haggle_counter: "she drops the price once, and only once",
  discount_resend: "he didn't buy — she takes it down and offers it cheaper",
  soft_broke_ack: "he says he's broke — she stops selling and keeps talking",
  aftercare: "the end of the ladder, then she leaves him alone",
};

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

export function MediaThumb({ id, accountId, size = 36, dim, selected, onClick }: {
  id: number;
  accountId: string | null;
  size?: number;
  dim?: boolean;
  selected?: boolean;
  onClick?: (m: VaultMedia | undefined) => void;
}) {
  const cache = useMediaCache();
  const media = cache.get(id);
  const url = thumbUrl(media, accountId);
  const isVideo = media?.type === "video";
  const style = { width: size, height: size };
  const ring = selected ? "ring-2 ring-accent border-accent" : "border-border";
  if (!url) {
    return (
      <button type="button" style={style} title={`vault ${id}`}
        onClick={() => onClick?.(media)}
        className={cn("grid place-items-center rounded border bg-bg-elev-1 text-[8px] text-fg-dim overflow-hidden",
          ring, dim && "opacity-40")}>
        {String(id).slice(-4)}
      </button>
    );
  }
  return (
    <button type="button" style={style} title={`vault ${id}${isVideo ? " · video" : ""}`}
      onClick={() => onClick?.(media)}
      className={cn("relative rounded overflow-hidden border hover:border-accent",
        ring, dim && "opacity-40")}>
      <img src={url} alt="" className="w-full h-full object-cover" />
      {isVideo && (
        <span className="absolute inset-0 grid place-items-center bg-black/25 text-white text-[10px]">▶</span>
      )}
    </button>
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
                  onClick={(m) => m && onPreview(m)} />
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
      <table className="w-full text-sm">
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

/* ── Human Rhythm — how long she waits before answering ────────────── */

export function RhythmSection({ cfg, set, tz, setTz, utcOffset, derived, effective }: {
  cfg: AiChatterConfig;
  set: (p: Partial<AiChatterConfig>) => void;
  tz: string;
  setTz: (v: string) => void;
  utcOffset: number;
  derived: [string, string];
  effective: [string, string];
}) {
  const [advanced, setAdvanced] = useState(false);
  const tzKnown = !!tz.trim() || utcOffset !== 0;
  const noSleep = !!cfg.rhythm_no_sleep;
  // No-sleep mode needs no timezone, so it can be enabled with none set.
  const canEnable = tzKnown || noSleep;
  const override = cfg.sleep_window ?? null;
  const zones = tz && !TIMEZONES.includes(tz) ? [tz, ...TIMEZONES] : TIMEZONES;

  const setWindow = (i: 0 | 1, v: string) => {
    const base: [string, string] = override ?? [derived[0], derived[1]];
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
          <span className="font-medium">Human Rhythm — she replies like a person, not a bot</span>
          <span className="block text-fg-dim text-xs">
            Fast when you&apos;re mid-conversation, slower when it&apos;s quiet, and she
            steps away sometimes.
          </span>
        </span>
      </label>

      {/* No-sleep mode: hot/cold/busy pacing + short breaks, no overnight sleep, no tz. */}
      <label className="flex items-start gap-2 cursor-pointer pl-6">
        <input type="checkbox" className="mt-0.5" checked={noSleep}
          onChange={(e) => set({
            rhythm_no_sleep: e.target.checked,
            // Turning on no-sleep is also turning Rhythm on (it's a Rhythm sub-mode).
            ...(e.target.checked ? { rhythm_enabled: true } : {}),
          })} />
        <span className="text-xs">
          <span className="font-medium text-fg">No sleep — she just gets busy (no timezone needed)</span>
          <span className="block text-fg-dim">
            Keeps the hot/cold pacing and short &ldquo;stepped away&rdquo; breaks (~up to
            2 h — yoga, an errand), but <b>never an 8-hour night gap</b>. Use this instead of
            the sleep window when you don&apos;t want her going dark overnight.
          </span>
        </span>
      </label>

      {!canEnable && (
        <div className="text-xs text-amber-400 pl-6">
          Set this creator&apos;s timezone first (or turn on <b>No sleep</b> above) —
          otherwise she&apos;d sleep through her best hours.
        </div>
      )}

      {!!cfg.rhythm_enabled && noSleep && (
        <div className="text-xs text-fg-dim pl-6">
          She never sleeps — when it&apos;s quiet she takes a short break (up to ~2 h) and
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
              <option key={z} value={z}>{z.replace(/_/g, " ")}</option>
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
          She sleeps <span className="text-fg font-medium">
            {fmtClock(effective[0])}–{fmtClock(effective[1])}
          </span>{" "}
          {override ? "(you set this)." : "(your team's quiet hours)."}{" "}
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
            <label className="flex items-center gap-2">
              <input type="checkbox" checked={!!override}
                onChange={(e) => set({
                  sleep_window: e.target.checked ? [derived[0], derived[1]] : null,
                })} />
              Set her sleep window myself (otherwise it&apos;s read off her own send history)
            </label>
            <div className={override ? "flex items-center gap-2" : "flex items-center gap-2 opacity-50 pointer-events-none"}>
              <span>Asleep from</span>
              <input type="time" className={`${INPUT} w-28`}
                value={(override ?? derived)[0]}
                onChange={(e) => setWindow(0, e.target.value)} />
              <span>until</span>
              <input type="time" className={`${INPUT} w-28`}
                value={(override ?? derived)[1]}
                onChange={(e) => setWindow(1, e.target.value)} />
            </div>
            <div className="text-fg-dim/70">
              From her own sends, her quiet hours look like{" "}
              <span className="text-fg">{fmtClock(derived[0])}–{fmtClock(derived[1])}</span>.
              While she&apos;s asleep an incoming message is answered when she wakes up, not
              instantly — and she says so.
            </div>
          </div>
        </details>
      )}
    </div>
  );
}

/* ── The script pack — the words are already written ───────────────── */

export function ScriptPackCard({ pack, text, setText, onSave, saving, saved }: {
  pack: Record<string, string[]>;
  text: Record<string, string>;
  setText: (slot: string, v: string) => void;
  onSave: () => void;
  saving: boolean;
  saved: boolean;
}) {
  const slots = Object.keys(pack);
  return (
    <Card className="p-4 space-y-3">
      <div className="flex items-center gap-2">
        <BookOpen size={16} />
        <h3 className="text-sm font-medium">Her lines — already written, edit if you want</h3>
      </div>
      <p className="text-xs text-fg-dim leading-relaxed">
        These are the words she uses at each moment of a sale. They ship filled in, so
        you can leave every box alone and just attach your content above. One line per
        row — she picks one at random. Empty a box and she goes back to the lines that
        ship with the app. <span className="font-mono text-accent">{"{name}"}</span> becomes
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
                <span className="text-[11px] text-fg-dim">· {SLOT_HELP[slot] ?? ""}</span>
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
                  Empty — she&apos;ll use the {shipped.length} line(s) that ship with the app.
                </div>
              )}
            </div>
          );
        })}
      </div>

      <div className="flex items-center gap-2">
        <Button size="sm" disabled={saving} onClick={onSave}>
          <Save size={14} className="mr-1" /> Save lines
        </Button>
        {saved && <span className="text-xs text-green-400">saved ✓</span>}
      </div>
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
   *  so a patch can be saved WITHOUT waiting for a state round-trip. */
  const buildSparse = (base: AiChatterConfig): AiChatterConfig => {
    const d = (cfgQ.data?.defaults ?? {}) as Record<string, unknown>;
    const out: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(base)) {
      if (v !== d[k]) out[k] = v;
    }
    // Authoritative, never sparse: null equals the default so the pass above would
    // drop them, but dropping would leave a stale server override alive.
    out.sleep_window = base.sleep_window ?? null;
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
    const base = patch ? { ...cfg, ...patch } : cfg;
    if (patch) setCfg(base);
    saveCfgM.mutate({ config: buildSparse(base), timezone: tz.trim() || null });
  };

  return {
    cfgQ, saveCfgM, eff, cfg, setCfg, set, tz, setTz,
    shippedPack, packText, setPackText, packOverrides, sparseCfg, saveCfg,
  };
}

/** Texting-style opt-ins (girl voice / typos / non-native) — the style_config_json
 *  store the Auto Convo tab + rule editor use. Chatter-tab only, but shared here so
 *  the load/save shape is written once. */
export function useSellerStyle(accountId: string | null) {
  const styleQ = useStyleConfig(accountId);
  const saveStyleM = useSaveStyleConfig(accountId);
  const [girlStyle, setGirlStyle] = useState(false);
  const [typosOn, setTyposOn] = useState(false);
  const [nonnativeOn, setNonnativeOn] = useState(false);
  useEffect(() => {
    const c = { ...(styleQ.data?.defaults ?? {}), ...(styleQ.data?.config ?? {}) } as Record<string, unknown>;
    setGirlStyle(Boolean(c["ai_chatter"]));
    setTyposOn(Boolean(c["typos_ai_chatter"]));
    setNonnativeOn(Boolean(c["nonnative_ai_chatter"]));
  }, [styleQ.data]);
  const saveStyle = () => saveStyleM.mutate({
    ai_chatter: girlStyle, typos_ai_chatter: typosOn, nonnative_ai_chatter: nonnativeOn,
  });
  return { saveStyleM, girlStyle, setGirlStyle, typosOn, setTyposOn, nonnativeOn, setNonnativeOn, saveStyle };
}
