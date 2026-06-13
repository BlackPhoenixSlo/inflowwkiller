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
import { Bot, FolderOpen, Play, Plus, Save, Trash2, X } from "lucide-react";

import { Badge, Button, Card, Textarea } from "@/components/ui/primitives";
import { VaultFolderPicker } from "@/components/settings/VaultFolderPicker";
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

/* ── per-item editable row ─────────────────────────────────────────── */

function ItemRow({ it, onChange, onRemove }: {
  it: CatalogItemT;
  onChange: (patch: Partial<CatalogItemT>) => void;
  onRemove: () => void;
}) {
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
        <input className={`${INPUT} w-28`}
          value={it.media_ids.join(",")}
          placeholder="vault ids"
          onChange={(e) => onChange({
            media_ids: e.target.value.split(",").map((x) => parseInt(x.trim(), 10))
              .filter((n) => Number.isFinite(n)),
          })} />
        {it.media_ids.length === 0 && (
          <div className="text-[10px] text-amber-400 mt-0.5">no media → not offerable</div>
        )}
      </td>
      <td className={TD}>
        <input className={`${INPUT} w-24`}
          value={it.preview_media_ids.join(",")}
          placeholder="free frames"
          title="Media ids (subset of media) sent UNLOCKED as the free teaser on a PPV send"
          onChange={(e) => onChange({
            preview_media_ids: e.target.value.split(",").map((x) => parseInt(x.trim(), 10))
              .filter((n) => Number.isFinite(n)),
          })} />
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

function ItemsTable({ items, setItems }: {
  items: CatalogItemT[];
  setItems: (items: CatalogItemT[]) => void;
}) {
  const patch = (i: number, p: Partial<CatalogItemT>) =>
    setItems(items.map((x, j) => (j === i ? { ...x, ...p } : x)));
  return (
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
          <ItemRow key={it.id ?? `new-${i}`} it={it}
            onChange={(p) => patch(i, p)}
            onRemove={() => setItems(items.filter((_, j) => j !== i))} />
        ))}
      </tbody>
    </table>
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

      <ItemsTable items={items} setItems={setItems} />

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
        <ItemsTable items={singles} setItems={setSingles} />
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
    </div>
  );
}
