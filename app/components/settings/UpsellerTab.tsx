"use client";

/**
 * UpsellerTab — Automations → "💰 AI Upseller".
 *
 * The selling brain. Everything about turning a live conversation into money:
 *   • Enable Upseller — one click writes the recommended "sell hard + take over" set.
 *   • Takeover — once a fan is in an active sale, she drives his thread regardless of
 *     the base AI Chatter mode (backup/closer), then hands back when it winds down.
 *   • Sell in the chat (the gate) + Smart pricing (the hot-window ladder).
 *   • Ladder aggressiveness — how hard the price climbs after a buy.
 *   • Offer pacing, and the "after a buy" behaviors (post-buy follow-up, gift, win-back).
 *   • Content — PPV Library (price authority) + Scripts (ladders) + Singles (pool) +
 *     the Starter pack (prewritten sellable pieces, just add media) + Her lines.
 *   • Simulate + Live monitor.
 *
 * Shares ai_chatter_config_json with the AI Chatter tab via `useSellerConfig` — both
 * post the FULL sparse config, so neither clobbers the other.
 */

import { useEffect, useState } from "react";
import { Play, Plus, Save, Sparkles, X } from "lucide-react";

import { Badge, Button, Card } from "@/components/ui/primitives";
import { EditRawJsonButton } from "@/components/settings/JsonConfigModal";
import { MediaCacheProvider } from "@/hooks/useMediaCache";
import { cn } from "@/lib/utils";
import {
  useAiChatterConfig, useAiChatterSessions, useCancelOffer, useCatalogScripts,
  useSaveSingles, useSimulate, useUpsertScript,
  type AiChatterConfig, type CatalogItemT,
} from "@/hooks/useCatalog";
import {
  GuideCard, INPUT, ItemsTable, NEW_ITEM, ScriptCard, ScriptPackCard, dollars,
  useSellerConfig,
} from "@/components/settings/sellerShared";

/** The recommended "sell hard + take over" preset — written on "Enable Upseller".
 *  Matches the confirmed defaults: gate + smart pricing + full takeover + engage a
 *  silent buyer + free thank-you gift + one win-back discount; filming fiction stays
 *  OFF (chargeback-safe). `enabled` is set so the engine actually runs. */
const RECOMMENDED: Partial<AiChatterConfig> = {
  enabled: true,
  qualification_gate_enabled: true,
  smart_pricing_enabled: true,
  upsell_takes_over: true,
  post_buy_rung_enabled: true,
  gift_enabled: true,
  stop_after_unpaid_rungs: 2,
  filming_stall_enabled: false,
  // Human pacing out of the box — hot/cold/busy with short breaks, no overnight sleep
  // and no timezone required (the operator can switch to the sleep window later).
  rhythm_enabled: true,
  rhythm_no_sleep: true,
};

/** Prewritten sellable pieces. The pitches + suggested prices are written; the
 *  operator only attaches media (media_ids empty → "no media = not offerable", so an
 *  un-attached starter can never be sold by accident). Laddered low→high so the
 *  ladder has room to climb by unlocking pricier items. */
const STARTER_SINGLES: CatalogItemT[] = ([
  { label: "Ass tease", kind: "video",
    description_for_ai: "a short teasing clip of me turning around and playing with my ass for you",
    price_cents: 800, tip_unlock_cents: 800 },
  { label: "Feet set", kind: "image_set",
    description_for_ai: "a set of photos of my bare feet and soles, close up",
    price_cents: 1000, tip_unlock_cents: 1000 },
  { label: "Lingerie set", kind: "image_set",
    description_for_ai: "photos of me posing in my lingerie before I take it off",
    price_cents: 1200, tip_unlock_cents: 1200 },
  { label: "Shower strip", kind: "video",
    description_for_ai: "a video of me slowly getting undressed and stepping into the shower",
    price_cents: 1500, tip_unlock_cents: 1500 },
  { label: "Full nude set", kind: "image_set",
    description_for_ai: "a set of fully nude photos of me on my bed",
    price_cents: 1800, tip_unlock_cents: 1800 },
  { label: "Bed dance", kind: "video",
    description_for_ai: "a video of me dancing and teasing on my bed for you",
    price_cents: 2000, tip_unlock_cents: 2000 },
  { label: "Dirty talk / JOI", kind: "video",
    description_for_ai: "a video of me talking dirty and telling you exactly what to do",
    price_cents: 2500, tip_unlock_cents: 2500 },
  { label: "Toy play", kind: "video",
    description_for_ai: "a video of me playing with my toy until I finish",
    price_cents: 3500, tip_unlock_cents: 3500 },
  { label: "Solo finish", kind: "video",
    description_for_ai: "a longer video of me touching myself all the way to the end",
    price_cents: 4500, tip_unlock_cents: 4500 },
  { label: "B/G collab", kind: "video",
    description_for_ai: "a video of me with a partner — the full scene",
    price_cents: 6000, tip_unlock_cents: 6000 },
] as Array<Partial<CatalogItemT>>).map((s) => ({ ...NEW_ITEM, ...s }));

export default function UpsellerTab({ accountId }: { accountId: string | null }) {
  const cfgQ = useAiChatterConfig(accountId);
  const { cfg, set, saveCfg, saveCfgM, shippedPack, packText, setPackText } = useSellerConfig(accountId);

  const scriptsQ = useCatalogScripts(accountId);
  const upsertM = useUpsertScript(accountId);
  const saveSinglesM = useSaveSingles(accountId);
  const simulateM = useSimulate(accountId);
  const sessionsQ = useAiChatterSessions(accountId);
  const cancelM = useCancelOffer(accountId);

  const [singles, setSingles] = useState<CatalogItemT[]>([]);
  useEffect(() => { setSingles(scriptsQ.data?.singles ?? []); }, [scriptsQ.data?.singles]);

  const [fanSays, setFanSays] = useState("ngl im kinda in the mood.. u got anything for me? 🥵");

  if (!accountId) return <div className="text-sm text-fg-dim">Pick an account above.</div>;
  if (cfgQ.isLoading || scriptsQ.isLoading)
    return <div className="text-sm text-fg-dim">Loading…</div>;

  const gateOn = !!cfg.qualification_gate_enabled;
  const isRecommended =
    !!cfg.qualification_gate_enabled && !!cfg.smart_pricing_enabled &&
    !!cfg.upsell_takes_over && !!cfg.post_buy_rung_enabled && !!cfg.gift_enabled &&
    (cfg.stop_after_unpaid_rungs ?? 1) === 2;
  const openOffers = (sessionsQ.data?.offers ?? []).filter((o) => o.status === "open");

  /** Append the starter singles that aren't already present (by label). */
  const loadStarter = () => {
    const have = new Set(singles.map((s) => (s.label ?? "").trim().toLowerCase()));
    const add = STARTER_SINGLES
      .filter((s) => !have.has((s.label ?? "").trim().toLowerCase()))
      .map((s) => ({ ...s }));
    setSingles([...singles, ...add]);
  };

  return (
    <MediaCacheProvider accountId={accountId}>
    <div className="space-y-4">
      {/* ── enable + takeover ── */}
      <Card className="p-4 space-y-3">
        <div className="flex items-center gap-2">
          <Sparkles size={16} className="text-accent" />
          <h3 className="text-sm font-medium">AI Upseller — she sells in the chat, then hands back</h3>
          <div className="flex-1" />
          <EditRawJsonButton surface="ai-chatter-config" accountId={accountId} />
        </div>

        <div className="rounded-md border border-accent/40 bg-accent/5 px-3 py-3 space-y-2">
          <div className="flex items-center gap-3 flex-wrap">
            <Button size="sm" variant={gateOn ? "secondary" : "primary"}
              disabled={saveCfgM.isPending}
              onClick={() => saveCfg(RECOMMENDED)}>
              <Sparkles size={14} className="mr-1" />
              {gateOn ? "Re-apply recommended" : "Enable Upseller (recommended)"}
            </Button>
            {gateOn && (
              isRecommended
                ? <span className="text-xs text-green-400">running the recommended setup ✓</span>
                : <span className="text-xs text-amber-400">custom settings — click to reset to recommended</span>
            )}
            {saveCfgM.isSuccess && <span className="text-xs text-green-400">saved ✓</span>}
          </div>
          <p className="text-xs text-fg-dim leading-relaxed">
            One click turns on: <b>sell in the chat</b>, <b>smart pricing</b>, <b>full
            takeover</b>, a <b>follow-up after a buy</b> (engages a silent buyer), a
            <b> free thank-you</b> after a couple of buys, and <b>one win-back discount</b>
            before she stops. The &quot;I&apos;m filming it now&quot; fiction stays off
            (chargeback-safe). Requires AI Chatter to be enabled — this turns it on too.
          </p>
        </div>

        {/* takeover */}
        <label className={cn("flex items-start gap-2 rounded-md border border-border bg-bg-elev-1 px-3 py-2.5",
          gateOn ? "cursor-pointer" : "cursor-not-allowed opacity-50")}>
          <input type="checkbox" className="mt-0.5" checked={!!cfg.upsell_takes_over}
            disabled={!gateOn}
            onChange={(e) => set({ upsell_takes_over: e.target.checked })} />
          <span className="text-sm">
            <span className="font-medium">Take over the chat during a sale</span>
            <span className="block text-fg-dim text-xs">
              Once a fan is actively buying (an open offer, a fresh purchase, a live
              ladder), she runs his thread <b>even if AI Chatter is in backup or closer
              mode</b> — a sale is never left waiting on the SLA or skipped as
              &quot;no intent.&quot; When the sale winds down she hands him back to
              normal chat automatically.
            </span>
          </span>
        </label>
      </Card>

      {/* ── the gate + smart pricing ── */}
      <Card className="p-4 space-y-3">
        <h3 className="text-sm font-medium">Selling in the chat</h3>
        <label className="flex items-start gap-2 cursor-pointer">
          <input type="checkbox" className="mt-0.5" checked={gateOn}
            onChange={(e) => set({
              qualification_gate_enabled: e.target.checked,
              ...(e.target.checked ? {} : { smart_pricing_enabled: false, upsell_takes_over: false }),
            })} />
          <span className="text-sm">
            <span className="font-medium">Sell in the chat (1:1)</span>
            <span className="block text-fg-dim text-xs">
              When a fan is actually talking to her, she can offer him content herself —
              instead of waiting for the scheduled PPV blast. She never puts a price in
              front of a fan who isn&apos;t replying. (This is the 1:1 seller only — the
              same gate on the mass blast would delete the blast.)
            </span>
          </span>
        </label>
        <label className={cn("flex items-start gap-2",
          gateOn ? "cursor-pointer" : "cursor-not-allowed opacity-50")}>
          <input type="checkbox" className="mt-0.5" checked={!!cfg.smart_pricing_enabled}
            disabled={!gateOn}
            onChange={(e) => set({ smart_pricing_enabled: e.target.checked })} />
          <span className="text-sm">
            <span className="font-medium">Smart pricing</span>
            <span className="block text-fg-dim text-xs">
              After a fan buys, she asks for more inside the hot window, and the price
              climbs. If he doesn&apos;t buy, she offers the same clip once more, cheaper,
              then stops. Prices stay between the Min and Max in <b>💸 PPV Library</b>.
            </span>
          </span>
        </label>

        {/* ladder aggressiveness */}
        <div className={cn("rounded-md border border-border bg-bg-elev-1 px-3 py-2.5 space-y-3",
          cfg.smart_pricing_enabled ? "" : "opacity-50 pointer-events-none")}>
          <div className="text-fg-dim text-xs">How hard the price climbs (advanced)</div>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            <label className="space-y-1">
              <div className="text-fg-dim text-xs">Step after each buy (× his last paid)</div>
              <input type="number" step="0.05" min={1} className={`${INPUT} w-full`}
                value={cfg.escalation_mult ?? 1.75}
                onChange={(e) => set({ escalation_mult: parseFloat(e.target.value || "1.75") || 1.75 })} />
            </label>
            <label className="space-y-1">
              <div className="text-fg-dim text-xs">Ceiling (× his biggest-ever buy)</div>
              <input type="number" step="0.5" min={1} className={`${INPUT} w-full`}
                value={cfg.max_ask_history_mult ?? 3.0}
                onChange={(e) => set({ max_ask_history_mult: parseFloat(e.target.value || "3") || 3.0 })} />
            </label>
          </div>
          <p className="text-[11px] text-fg-dim leading-relaxed">
            Most of the climb comes from your <b>script order</b> (a $3 piece, then a $15
            piece, then $80…) — these two just bound how fast a single item&apos;s price
            can rise. The ceiling is the <b>&quot;to the moon&quot; lever</b>: raise it and
            a whale can run, but{" "}
            <span className="text-amber-400">conversion drops past 3× his history (about
            52% → 31%)</span>, so push it only for a fan who keeps buying.
          </p>
        </div>
      </Card>

      {/* ── offer pacing ── */}
      <Card className="p-4 space-y-3">
        <h3 className="text-sm font-medium">Offer pacing</h3>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3 text-sm">
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
            <div className="text-fg-dim text-xs">Chat first: msgs before lean-in offer</div>
            <input type="number" className={`${INPUT} w-full`} min={0}
              value={cfg.min_fan_msgs_before_escalation_pitch ?? 2}
              onChange={(e) => set({ min_fan_msgs_before_escalation_pitch: parseInt(e.target.value || "0", 10) })} />
          </label>
          <label className="space-y-1">
            <div className="text-fg-dim text-xs">Offer expires after (h)</div>
            <input type="number" className={`${INPUT} w-full`} min={1}
              value={cfg.stall_ttl_hours ?? 6}
              onChange={(e) => set({ stall_ttl_hours: parseInt(e.target.value || "1", 10) })} />
          </label>
        </div>
        <label className="flex items-center gap-1.5 text-sm">
          <input type="checkbox" checked={cfg.pivot_on_escalation ?? true}
            onChange={(e) => set({ pivot_on_escalation: e.target.checked })} />
          Pivot to an offer when he&apos;s clearly into it (lean-in/flirty, not just &quot;show me&quot;)
        </label>
        <label className="flex items-center gap-1.5 text-sm">
          <input type="checkbox" checked={cfg.unsend_expired_offer ?? true}
            onChange={(e) => set({ unsend_expired_offer: e.target.checked })} />
          Unsend the in-chat offer when it expires (pull the unpurchased PPV)
        </label>
      </Card>

      {/* ── after a buy ── */}
      <Card className="p-4 space-y-3">
        <h3 className="text-sm font-medium">After a buy</h3>
        <div className={cn("space-y-2", gateOn ? "" : "opacity-50 pointer-events-none")}>
          <label className="flex items-start gap-2 cursor-pointer text-sm">
            <input type="checkbox" className="mt-0.5" checked={!!cfg.post_buy_rung_enabled}
              onChange={(e) => set({ post_buy_rung_enabled: e.target.checked })} />
            <span>
              <span className="font-medium">Follow up after a buy (engage a silent buyer)</span>
              <span className="block text-fg-dim text-xs">
                Even if he unlocks without a word, she sends one more offer while he&apos;s
                hot. Off, she only reacts when he talks.
              </span>
            </span>
          </label>
          <label className="flex items-start gap-2 cursor-pointer text-sm">
            <input type="checkbox" className="mt-0.5" checked={!!cfg.gift_enabled}
              onChange={(e) => set({ gift_enabled: e.target.checked })} />
            <span>
              <span className="font-medium">Free thank-you after a few buys</span>
              <span className="block text-fg-dim text-xs">
                A genuinely free (no paywall) unseen clip as a thank-you once he&apos;s
                bought a couple of times this session. Never priced, never after a
                &quot;I&apos;m out of money.&quot;
              </span>
            </span>
          </label>
          <div className="text-sm space-y-1 pt-1">
            <div className="text-fg-dim text-xs">If he doesn&apos;t buy an offer</div>
            <label className="flex items-center gap-2">
              <input type="radio" name="winback" checked={(cfg.stop_after_unpaid_rungs ?? 1) === 1}
                onChange={() => set({ stop_after_unpaid_rungs: 1 })} />
              Tap out after one unpaid offer (no discount)
            </label>
            <label className="flex items-center gap-2">
              <input type="radio" name="winback" checked={(cfg.stop_after_unpaid_rungs ?? 1) === 2}
                onChange={() => set({ stop_after_unpaid_rungs: 2 })} />
              One win-back discount, then stop <span className="text-fg-dim text-xs">(recommended — he has to work for it)</span>
            </label>
          </div>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-3 pt-1">
            <label className="space-y-1 text-sm">
              <div className="text-fg-dim text-xs">7-day spend brake ($ — then she eases off)</div>
              <input type="number" className={`${INPUT} w-full`} min={0}
                value={dollars(cfg.spend_velocity_cap_7d_cents ?? 30000)}
                onChange={(e) => set({ spend_velocity_cap_7d_cents: (parseInt(e.target.value || "0", 10) || 0) * 100 })} />
            </label>
            <label className="flex items-start gap-2 cursor-pointer text-sm pt-5">
              <input type="checkbox" className="mt-0.5" checked={!!cfg.filming_stall_enabled}
                onChange={(e) => set({ filming_stall_enabled: e.target.checked })} />
              <span>
                <span className="font-medium">&quot;I&apos;m filming it now&quot; fiction</span>
                <span className="block text-fg-dim text-xs">
                  She pretends she&apos;s recording it live before a PPV. Off by default —
                  it&apos;s a chargeback surface and adds no measured revenue.
                </span>
              </span>
            </label>
          </div>
        </div>
        <div className="flex items-center gap-2 border-t border-border pt-2">
          <Button size="sm" disabled={saveCfgM.isPending} onClick={() => saveCfg()}>
            <Save size={14} className="mr-1" /> Save Upseller settings
          </Button>
          {saveCfgM.isSuccess && <span className="text-xs text-green-400">saved ✓</span>}
        </div>
      </Card>

      {/* ── content: PPV Library callout ── */}
      <Card className="p-4 space-y-2">
        <h3 className="text-sm font-medium">Content she sells from</h3>
        <p className="text-xs text-fg-dim leading-relaxed">
          She draws on three pools, all shown below plus the <b>💸 PPV Library</b> tab
          (the price Min/Max there is the single price authority — smart pricing never
          asks below the Min or above the Max):
        </p>
        <ul className="text-xs text-fg-dim list-disc pl-5 space-y-0.5">
          <li><b>Scripts</b> — ordered ladders; the price climbs by unlocking pricier items in order.</li>
          <li><b>Singles</b> — a flexible pool; she picks the piece that fits what he asked for.</li>
          <li><b>Starter pack</b> — prewritten sellable pieces below; just attach your media.</li>
        </ul>
      </Card>

      {/* ── scripts ── */}
      <div className="flex items-center gap-2">
        <h3 className="text-sm font-medium">Scripts (ordered sexting ladders)</h3>
        <div className="flex-1" />
        <Button size="sm" variant="ghost"
          onClick={() => upsertM.mutate({ name: `script_${(scriptsQ.data?.scripts.length ?? 0) + 1}` })}>
          <Plus size={14} className="mr-1" /> New script
        </Button>
      </div>
      {(scriptsQ.data?.scripts ?? []).map((sc) => (
        <ScriptCard key={sc.id} accountId={accountId} sc={sc} />
      ))}

      {/* ── singles + starter pack ── */}
      <Card className="p-4 space-y-3">
        <div className="flex items-center gap-2 flex-wrap">
          <h3 className="text-sm font-medium">Singles &amp; starter pack (standalone pieces)</h3>
          <div className="flex-1" />
          <Button size="sm" variant="secondary" onClick={loadStarter}>
            <Sparkles size={14} className="mr-1" /> Load starter pack
          </Button>
        </div>
        <p className="text-xs text-fg-dim">
          <b>Load starter pack</b> drops in {STARTER_SINGLES.length} prewritten pieces with
          pitches and suggested prices — you just attach the media to each, then Save. A
          piece with no media can&apos;t be offered, so nothing sells by accident.
        </p>
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

      {/* ── her lines ── */}
      <ScriptPackCard
        pack={shippedPack}
        text={packText}
        setText={(slot, v) => setPackText((t) => ({ ...t, [slot]: v }))}
        onSave={() => saveCfg()}
        saving={saveCfgM.isPending}
        saved={saveCfgM.isSuccess}
      />

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

      <GuideCard />
    </div>
    </MediaCacheProvider>
  );
}
