"use client";

/**
 * ScriptsTab — Automations → "🤖💸 AI Seller".
 *
 * Everything for the `ai_chatter` automation (the freestyle chatter+seller
 * that replaces Auto-AI-chat for fans under the spend gate):
 *
 *   • Config — enable, backup-SLA vs always mode, the $ whale gate, offer
 *     pacing. Writes account_ai_config.ai_chatter_config_json.
 *   • Scripts — ordered sexting sequences. Each item carries the pitch
 *     contract (`what the fan sees`), media ids, tip/PPV prices, free-teaser
 *     flag. Items save WHOLESALE per script (the editor owns the list).
 *     Import items from a vault folder (media-first) or paste the agency
 *     Notion table (text-first).
 *   • Singles — standalone sellable pieces (bed dance, photo sets).
 *   • Simulate — dry prompt+LLM against a fake fan line; shows the exact
 *     bubbles + the offer the bot would make. Catch a bad script here, not
 *     on a paying fan.
 *   • Monitor — script pins + offers, with a kill switch on open offers so
 *     human chatters can take over cleanly.
 */

import { useEffect, useMemo, useState } from "react";
import { BookOpen, Bot, ChevronDown, FolderOpen, Image as ImageIcon, Play, Plus, Save, Trash2, X } from "lucide-react";

import { Badge, Button, Card, Textarea } from "@/components/ui/primitives";
import { VaultFolderPicker } from "@/components/settings/VaultFolderPicker";
import { VaultPicker } from "@/components/chat/VaultPicker";
import { MediaPreviewModal } from "@/components/settings/MediaPreviewModal";
import { MediaCacheProvider, useMediaCache } from "@/hooks/useMediaCache";
import { proxyImage, type VaultMedia } from "@/lib/relay";
import { cn } from "@/lib/utils";
import { useSaveStyleConfig, useStyleConfig } from "@/hooks/useStyleConfig";
import {
  useAiChatterConfig, useAiChatterSessions, useCancelOffer, useCatalogScripts,
  useDeleteScript, useImportFolder, usePasteImport, useSaveAiChatterConfig,
  useSaveScriptItems, useSaveSingles, useSimulate, useUpsertScript,
  type AiChatterConfig, type CatalogItemT, type CatalogScriptT,
} from "@/hooks/useCatalog";

const INPUT =
  "bg-bg border border-border rounded-lg px-2 py-1.5 text-sm focus:outline-none focus:border-accent";
const TH = "text-left text-[11px] uppercase tracking-wide text-fg-dim px-2 py-1";
const TD = "px-2 py-1 align-top";

const dollars = (cents: number) => Math.round((cents || 0) / 100);

function thumbUrl(m: VaultMedia | undefined, accountId: string | null): string | null {
  const raw = m?.files?.thumb?.url || m?.files?.squarePreview?.url || m?.files?.preview?.url || null;
  return raw ? proxyImage(raw, accountId) : null;
}

/** One vault-id square. Resolves to a thumbnail once the shared cache has the
 *  media (picked-from-vault or paged in); until then it's a compact id chip.
 *  `selectable` turns it into a preview toggle (PREVIEW cell). */
function MediaThumb({ id, accountId, size = 36, dim, selected, onClick }: {
  id: number;
  accountId: string | null;
  size?: number;
  /** Render at reduced opacity (preview toggle, unselected). */
  dim?: boolean;
  /** Accent ring (preview toggle, selected). */
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

  // Setting media also prunes any preview that is no longer in the set, so
  // preview_media_ids stays a valid subset by construction.
  const setMedia = (ids: number[]) =>
    onChange({ media_ids: ids, preview_media_ids: it.preview_media_ids.filter((p) => ids.includes(p)) });
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

function ItemsTable({ items, setItems, accountId }: {
  items: CatalogItemT[];
  setItems: (items: CatalogItemT[]) => void;
  accountId: string | null;
}) {
  const { prime } = useMediaCache();
  // Which row is picking media (single shared VaultPicker) + the lightbox.
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
              preview_media_ids: (items[pickRow]?.preview_media_ids ?? []).filter((p) => ids.includes(p)),
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

const NEW_ITEM: CatalogItemT = {
  kind: "video", label: "", description_for_ai: "", media_ids: [],
  preview_media_ids: [], duration_sec: null, price_cents: 0,
  tip_unlock_cents: 0, is_free_teaser: false, tags: [], enabled: true,
};

/* ── one script editor card ────────────────────────────────────────── */

function ScriptCard({ accountId, sc }: { accountId: string; sc: CatalogScriptT }) {
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

  useEffect(() => { setItems(sc.items); }, [sc.items]);
  useEffect(() => { setName(sc.name); setTheme(sc.theme ?? ""); setStatus(sc.status); }, [sc]);

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

/* ── how-to guide (read-only, for the whole team) ──────────────────── */

function GuideCard() {
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
          <div className="space-y-1.5">
            <p className={P}>
              The AI Seller chats <B>as the model</B> and sells your already-filmed
              content. It may <B>only ever claim what you type in “What the fan sees”</B> —
              it never invents content, lengths, prices, or customs. Everything below is
              just data you give it; the bot does the talking.
            </p>
          </div>

          {/* columns */}
          <div className="space-y-1.5">
            <h4 className={H}>The columns on every row</h4>
            <ul className="list-disc pl-5 space-y-1">
              <li className={LI}><B>Kind</B> — video / image / image set. Just a label for how the
                AI describes the piece. <B>It does not change the send</B> — “image set” of 5 photos
                = one message with 5 attachments, the fan pays once to unlock all. Nothing is stitched
                or merged.</li>
              <li className={LI}><B>Label</B> — your internal name (e.g. “ASS TEASE”). The fan never sees it.</li>
              <li className={LI}><B>What the fan sees</B> — the pitch contract. Present tense, only what’s
                truly in the media. This is also the <B>matching key</B>: when a fan asks for something,
                the AI picks the piece whose description fits best — so write sharp, distinct descriptions.</li>
              <li className={LI}><B>Media</B> — the vault content delivered. Click the <ImageIcon size={12} className="inline -mt-0.5" /> button
                to <B>pick from the vault</B> (or paste vault ids). Thumbnails show what you attached —
                click one to preview it full-screen. <span className="text-amber-400">No media = not offerable.</span></li>
              <li className={LI}><B>Previews</B> — tap the thumbnails to mark which media go out <B>unlocked</B>
                as the free teaser on a PPV (see below).</li>
              <li className={LI}><B>Tip $ / PPV $</B> — the two ways to sell it (see below).</li>
              <li className={LI}><B>Free</B> — the whole piece goes out free (no paywall), a treat the bot
                sends when the fan is sweet.</li>
              <li className={LI}><B>Sold</B> — delivered / offered counts.</li>
            </ul>
          </div>

          {/* tip vs ppv */}
          <div className="space-y-1.5">
            <h4 className={H}>Tip $ vs PPV $ — two ways to sell the same piece</h4>
            <ul className="list-disc pl-5 space-y-1">
              <li className={LI}><B>PPV $</B> — a locked paid message. The media is paywalled in the DM; the
                fan pays exactly that to unlock. The standard “pay to view”.</li>
              <li className={LI}><B>Tip $</B> — tip-to-unlock. The bot says “tip me $X and I’ll send it 😏”;
                once the fan tips that amount, it delivers. Softer, flirtier.</li>
            </ul>
            <p className={P}>
              The <B>Offer mode</B> dropdown up top (tip / PPV / both) decides which rail is used. Set both
              prices for “tip or pay”, or leave one at 0 to use only the other. A piece with <B>no price
              and not Free</B> can’t be offered.
            </p>
          </div>

          {/* previews */}
          <div className="space-y-1.5">
            <h4 className={H}>Previews (free frames) — the free peek on a PPV</h4>
            <p className={P}>
              A preview is a media item sent <B>unlocked</B> next to a locked PPV — the free taste. It must
              be one of the row’s own media (that’s why you tap to toggle them).
            </p>
            <ul className="list-disc pl-5 space-y-1">
              <li className={LI}><B>Set of several</B> → mark 1 as the preview: the fan sees that one free,
                pays to unlock the rest. ✅</li>
              <li className={LI}><B>One image, with a teaser copy</B> → attach the real photo + a blurred/cropped
                teaser copy as two media, mark the teaser copy as the preview. ✅</li>
              <li className={LI}><span className="text-amber-400">One image, marked as its own preview</span> → the
                whole thing goes out free and there’s nothing left locked. The row warns “all media unlocked →
                fan pays for nothing.” For a single image with no teaser copy, leave previews empty. ❌</li>
            </ul>
          </div>

          {/* scripts vs singles */}
          <div className="space-y-1.5">
            <h4 className={H}>Scripts vs Singles — how the bot chooses what to offer</h4>
            <ul className="list-disc pl-5 space-y-1">
              <li className={LI}><B>Singles</B> = a flat pool. Every unseen single is available at once, and the AI
                picks the <B>best-fitting one by its description</B>. This is what lets a fan “ask for the red set”
                and get it. Flexible, no fixed order.</li>
              <li className={LI}><B>Scripts</B> = an <B>ordered ladder</B>. The bot offers the items in position order
                (#1 → #2 → #3) and <B>ignores what the fan asks</B>; each unlock advances to the next. One script runs
                per fan at a time. Use it for a planned escalation, not for “pick one of these”.</li>
            </ul>
            <p className={P}>
              Already-sent pieces drop out automatically — the bot never re-offers the same media to the same fan.
            </p>
          </div>

          {/* color branch recipe */}
          <div className="space-y-1.5">
            <h4 className={H}>Recipe: “I have 3 colors — pick one, then I sell more of that color”</h4>
            <p className={P}>
              A <B>single script can’t branch</B> on the fan’s choice (it just walks in order). To get
              “pick a color → reveal it → upsell that color”, build it like this:
            </p>
            <ul className="list-disc pl-5 space-y-1">
              <li className={LI}>The “which color do you want?” tease = a <B>Free single</B> (or just let the
                persona say it). <B>Don’t</B> make it a script step — that would lock in a color too early.</li>
              <li className={LI}>One <B>script per color</B> (Red / Black / White). Each script’s <B>first item</B>
                is the free dressing reveal of that color; the rest are the paid variations, in sell order.</li>
            </ul>
            <p className={P}>
              At the start, all three reveals are on offer; the fan says “red”, the AI picks the red reveal by its
              description and sends it — which <B>pins the fan to the Red script</B>, so from then on it sells the red
              variations in order and the other colors stay hidden. Works only if each color’s description is clearly
              distinct (“red lace” vs “black mesh” vs “white sheer”), and it’s the AI matching your words, not a hard
              rule — so write the descriptions carefully.
            </p>
          </div>

          {/* import vs pick */}
          <div className="space-y-1.5">
            <h4 className={H}>Import folder vs Pick from vault</h4>
            <ul className="list-disc pl-5 space-y-1">
              <li className={LI}><B>Import folder</B> (on a script) — bulk-creates one row per file in a vault folder,
                in folder order, with the media auto-attached. It leaves the <B>description blank and prices $0</B>,
                so you still fill those in. Matches the folder by exact name.</li>
              <li className={LI}><B>Pick from vault</B> (the <ImageIcon size={12} className="inline -mt-0.5" /> button on a
                row) — attach the exact media you want to a single row. Use this to fix or build one item precisely.</li>
            </ul>
          </div>

          {/* simulate */}
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

/* ── the tab ───────────────────────────────────────────────────────── */

export default function ScriptsTab({ accountId }: { accountId: string | null }) {
  const cfgQ = useAiChatterConfig(accountId);
  const saveCfgM = useSaveAiChatterConfig(accountId);
  const scriptsQ = useCatalogScripts(accountId);
  const upsertM = useUpsertScript(accountId);
  const saveSinglesM = useSaveSingles(accountId);
  const simulateM = useSimulate(accountId);
  const sessionsQ = useAiChatterSessions(accountId);
  const cancelM = useCancelOffer(accountId);

  const eff: AiChatterConfig = useMemo(() => {
    const d = cfgQ.data?.defaults ?? {};
    const c = cfgQ.data?.config ?? {};
    return { ...d, ...c };
  }, [cfgQ.data]);

  const [cfg, setCfg] = useState<AiChatterConfig>({});
  useEffect(() => { setCfg(eff); }, [eff]);

  // Texting-style opt-ins (girl voice / typos / non-native) — same
  // style_config_json store the Auto Convo tab + rule editor use.
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

  const [singles, setSingles] = useState<CatalogItemT[]>([]);
  useEffect(() => { setSingles(scriptsQ.data?.singles ?? []); }, [scriptsQ.data?.singles]);

  const [fanSays, setFanSays] = useState("ngl im kinda in the mood.. u got anything for me? 🥵");

  if (!accountId) return <div className="text-sm text-fg-dim">Pick an account above.</div>;
  if (cfgQ.isLoading || scriptsQ.isLoading)
    return <div className="text-sm text-fg-dim">Loading…</div>;

  const set = (p: Partial<AiChatterConfig>) => setCfg((c) => ({ ...c, ...p }));
  const openOffers = (sessionsQ.data?.offers ?? []).filter((o) => o.status === "open");

  return (
    <MediaCacheProvider accountId={accountId}>
    <div className="space-y-4">
      {/* ── config ── */}
      <Card className="p-4 space-y-3">
        <div className="flex items-center gap-2">
          <Bot size={16} />
          <h3 className="text-sm font-medium">AI Seller (replaces Auto-AI-chat for fans under the gate)</h3>
          <div className="flex-1" />
          <label className="flex items-center gap-2 text-sm">
            <input type="checkbox" checked={!!cfg.enabled}
              onChange={(e) => set({ enabled: e.target.checked })} />
            Enabled
          </label>
        </div>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3 text-sm">
          <label className="space-y-1">
            <div className="text-fg-dim text-xs">Mode</div>
            <select className={`${INPUT} w-full`} value={cfg.mode ?? "backup"}
              onChange={(e) => set({ mode: e.target.value as AiChatterConfig["mode"] })}>
              <option value="backup">backup (when chatters are slow)</option>
              <option value="always">always on</option>
            </select>
          </label>
          <label className="space-y-1">
            <div className="text-fg-dim text-xs">SLA minutes (backup)</div>
            <input type="number" className={`${INPUT} w-full`} min={0}
              value={cfg.sla_minutes ?? 10}
              onChange={(e) => set({ sla_minutes: parseInt(e.target.value || "0", 10) })} />
          </label>
          <label className="space-y-1">
            <div className="text-fg-dim text-xs">Whale gate ($ lifetime — at/over = humans only)</div>
            <input type="number" className={`${INPUT} w-full`} min={0}
              value={dollars(cfg.max_lifetime_spend_cents ?? 100000)}
              onChange={(e) => set({ max_lifetime_spend_cents: (parseInt(e.target.value || "0", 10) || 0) * 100 })} />
          </label>
          <label className="space-y-1">
            <div className="text-fg-dim text-xs">Offer mode</div>
            <select className={`${INPUT} w-full`} value={cfg.offer_mode ?? "both"}
              onChange={(e) => set({ offer_mode: e.target.value as AiChatterConfig["offer_mode"] })}>
              <option value="both">tip or PPV</option>
              <option value="tip">tip only</option>
              <option value="ppv">PPV only</option>
            </select>
          </label>
          <label className="space-y-1">
            <div className="text-fg-dim text-xs">Max offers / fan / day</div>
            <input type="number" className={`${INPUT} w-full`} min={0}
              value={cfg.max_offers_per_fan_per_day ?? 2}
              onChange={(e) => set({ max_offers_per_fan_per_day: parseInt(e.target.value || "0", 10) })} />
          </label>
          <label className="space-y-1">
            <div className="text-fg-dim text-xs">Min fan msgs between offers</div>
            <input type="number" className={`${INPUT} w-full`} min={0}
              value={cfg.min_fan_msgs_between_offers ?? 4}
              onChange={(e) => set({ min_fan_msgs_between_offers: parseInt(e.target.value || "0", 10) })} />
          </label>
          <label className="space-y-1">
            <div className="text-fg-dim text-xs">Hands-off after human send (h)</div>
            <input type="number" className={`${INPUT} w-full`} min={0}
              value={cfg.resume_after_manual_hours ?? 6}
              onChange={(e) => set({ resume_after_manual_hours: parseInt(e.target.value || "0", 10) })} />
          </label>
          <label className="space-y-1">
            <div className="text-fg-dim text-xs">Offer expires after (h)</div>
            <input type="number" className={`${INPUT} w-full`} min={1}
              value={cfg.stall_ttl_hours ?? 48}
              onChange={(e) => set({ stall_ttl_hours: parseInt(e.target.value || "1", 10) })} />
          </label>
        </div>
        <div className="flex flex-wrap items-center gap-x-4 gap-y-2 text-sm border-t border-border pt-2">
          <span className="text-xs text-fg-dim">Texting style:</span>
          <label className="flex items-center gap-1.5">
            <input type="checkbox" checked={girlStyle}
              onChange={(e) => setGirlStyle(e.target.checked)} />
            Girl style (bubbles + humanizer)
          </label>
          <label className="flex items-center gap-1.5">
            <input type="checkbox" checked={typosOn}
              onChange={(e) => setTyposOn(e.target.checked)} />
            Typos
          </label>
          <label className="flex items-center gap-1.5">
            <input type="checkbox" checked={nonnativeOn}
              onChange={(e) => setNonnativeOn(e.target.checked)} />
            Non-native English
          </label>
          <Button size="sm" variant="ghost" disabled={saveStyleM.isPending}
            onClick={() => saveStyleM.mutate({
              ai_chatter: girlStyle,
              typos_ai_chatter: typosOn,
              nonnative_ai_chatter: nonnativeOn,
            })}>
            Save style
          </Button>
          {saveStyleM.isSuccess && <span className="text-xs text-green-400">saved ✓</span>}
        </div>
        <div className="flex items-center gap-2">
          <Button size="sm" disabled={saveCfgM.isPending}
            onClick={() => saveCfgM.mutate(cfg)}>
            <Save size={14} className="mr-1" /> Save config
          </Button>
          {saveCfgM.isSuccess && <span className="text-xs text-green-400">saved ✓</span>}
          {!!cfg.enabled && (
            <span className="text-xs text-amber-400">
              live — replaces Auto-AI-chat for fans under the gate
            </span>
          )}
        </div>
      </Card>

      {/* ── scripts ── */}
      <div className="flex items-center gap-2">
        <h3 className="text-sm font-medium">Scripts (sexting sequences)</h3>
        <div className="flex-1" />
        <Button size="sm" variant="ghost"
          onClick={() => upsertM.mutate({ name: `script_${(scriptsQ.data?.scripts.length ?? 0) + 1}` })}>
          <Plus size={14} className="mr-1" /> New script
        </Button>
      </div>
      {(scriptsQ.data?.scripts ?? []).map((sc) => (
        <ScriptCard key={sc.id} accountId={accountId} sc={sc} />
      ))}

      {/* ── singles ── */}
      <Card className="p-4 space-y-3">
        <h3 className="text-sm font-medium">Singles (standalone pieces — bed dance, photo sets…)</h3>
        <ItemsTable items={singles} setItems={setSingles} accountId={accountId} />
        <div className="flex items-center gap-2">
          <Button size="sm" variant="ghost"
            onClick={() => setSingles([...singles, { ...NEW_ITEM }])}>
            <Plus size={14} className="mr-1" /> Add single
          </Button>
          <Button size="sm" disabled={saveSinglesM.isPending}
            onClick={() => saveSinglesM.mutate(singles)}>
            <Save size={14} className="mr-1" /> Save singles ({singles.length})
          </Button>
          {saveSinglesM.isSuccess && <span className="text-xs text-green-400">saved ✓</span>}
        </div>
      </Card>

      {/* ── simulate ── */}
      <Card className="p-4 space-y-3">
        <h3 className="text-sm font-medium">Simulate (dry run — nothing is sent)</h3>
        <div className="flex gap-2">
          <input className={`${INPUT} flex-1`} value={fanSays}
            onChange={(e) => setFanSays(e.target.value)} />
          <Button size="sm" disabled={simulateM.isPending}
            onClick={() => simulateM.mutate(fanSays)}>
            <Play size={14} className="mr-1" /> Run
          </Button>
        </div>
        {simulateM.data && (
          <div className="space-y-2">
            {!simulateM.data.manifest_present && (
              <div className="text-xs text-amber-400">
                ⚠ no sellable items reached the prompt (script enabled? items have media + prices?)
              </div>
            )}
            <div className="space-y-1">
              {simulateM.data.bubbles.map((b, i) => (
                <div key={i}
                  className="inline-block bg-accent/10 border border-accent/30 rounded-2xl px-3 py-1.5 text-sm mr-2">
                  {b}
                </div>
              ))}
            </div>
            {simulateM.data.offer && (
              <Badge className="bg-green-500/15 text-green-400">
                would offer: {simulateM.data.offer.label ?? simulateM.data.offer.item_id}
                {simulateM.data.offer.is_free_teaser
                  ? " (free teaser)"
                  : ` — tip $${dollars(simulateM.data.offer.tip_unlock_cents)} / ppv $${dollars(simulateM.data.offer.price_cents)}`}
              </Badge>
            )}
          </div>
        )}
      </Card>

      {/* ── monitor ── */}
      <Card className="p-4 space-y-3">
        <h3 className="text-sm font-medium">
          Live monitor {openOffers.length > 0 && `— ${openOffers.length} open offer(s)`}
        </h3>
        <div className="grid md:grid-cols-2 gap-4">
          <div>
            <div className="text-xs text-fg-dim mb-1">Script pins</div>
            {(sessionsQ.data?.progress ?? []).length === 0 && (
              <div className="text-xs text-fg-dim">none yet</div>
            )}
            {(sessionsQ.data?.progress ?? []).map((p) => (
              <div key={`${p.fan_id}-${p.script}`} className="text-sm py-0.5">
                <span className="text-fg-dim">{p.fan_name || p.fan_id}</span>{" "}
                🎬 {p.script} · item {p.position} ·{" "}
                <Badge className={p.status === "active"
                  ? "bg-green-500/15 text-green-400"
                  : "bg-fg-dim/10 text-fg-dim"}>{p.status}</Badge>
              </div>
            ))}
          </div>
          <div>
            <div className="text-xs text-fg-dim mb-1">Offers</div>
            {(sessionsQ.data?.offers ?? []).slice(0, 20).map((o) => (
              <div key={o.id} className="text-sm py-0.5 flex items-center gap-2">
                <span className="text-fg-dim">{o.fan_name || o.fan_id}</span>
                <span>{o.item_label}</span>
                <Badge className={o.status === "open"
                  ? "bg-amber-500/15 text-amber-400"
                  : o.status === "delivered"
                    ? "bg-green-500/15 text-green-400"
                    : "bg-fg-dim/10 text-fg-dim"}>
                  {o.status}{o.resolved_by ? ` (${o.resolved_by})` : ""}
                </Badge>
                {o.status === "open" && (
                  <>
                    <span className="text-xs text-fg-dim">
                      ${dollars(o.tips_accum_cents)}/${dollars(o.tip_unlock_cents)} tipped
                    </span>
                    <button className="text-fg-dim hover:text-red-400"
                      title="cancel offer (hand to a human)"
                      onClick={() => cancelM.mutate(o.id)}>
                      <X size={13} />
                    </button>
                  </>
                )}
              </div>
            ))}
          </div>
        </div>
      </Card>

      {/* ── how-to guide ── */}
      <GuideCard />
    </div>
    </MediaCacheProvider>
  );
}
