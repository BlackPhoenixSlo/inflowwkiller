"use client";

/**
 * UpsellerTab — Automations → "💰 AI Upseller".
 *
 * The "sell harder" tuning layer over the SAME seller the 🤖 AI Chatter tab runs
 * (one engine, one catalog). This tab does NOT hold the content — the live library
 * (Scripts + Singles + Simulate + Monitor) lives on the AI Chatter tab, because the
 * base chatter sells from it too. Here:
 *   • Enable Upseller — one click writes the recommended "sell hard + take over" set.
 *   • Takeover, the gate, smart pricing, the price ladder knobs, after-a-buy behaviors.
 *   • Prewritten scaffolding: the pitch pack + Load-starter-pack templates. Both
 *     are LANED SERVER-SIDE and arrive resolved — this tab holds no lane.
 *
 * Shares ai_chatter_config_json with the AI Chatter tab via `useSellerConfig` — both
 * post the FULL sparse config, so neither clobbers the other.
 */

import { useState } from "react";
import { Save, Sparkles } from "lucide-react";

import { Button, Card } from "@/components/ui/primitives";
import { EditRawJsonButton } from "@/components/settings/JsonConfigModal";
import { cn } from "@/lib/utils";
import {
  useAiChatterConfig, useCatalogScripts, useSaveSingles,
  type AiChatterConfig,
} from "@/hooks/useCatalog";
import {
  ConfigLoadError, GuideCard, INPUT, ScriptPackCard, dollars, useSellerConfig,
} from "@/components/settings/sellerShared";

/** The recommended "sell hard + take over" preset — written on "Enable Upseller". */
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

export default function UpsellerTab({ accountId }: { accountId: string | null }) {
  const cfgQ = useAiChatterConfig(accountId);
  const { cfg, set, saveCfg, saveCfgM, configLoaded, shippedPack, packText, setPackText,
    starterSingles, slotHelp } = useSellerConfig(accountId);
  const scriptsQ = useCatalogScripts(accountId);
  const saveSinglesM = useSaveSingles(accountId);

  if (!accountId) return <div className="text-sm text-fg-dim">Pick an account above.</div>;
  if (cfgQ.isLoading) return <div className="text-sm text-fg-dim">Loading…</div>;
  // Same rule as the AI Chatter tab: every knob below falls back to a hardcoded
  // literal, so a config that never loaded would render as a tab full of saved-
  // looking settings — and all three Save buttons write the whole blob.
  if (cfgQ.isError) {
    return (
      <ConfigLoadError what="this account's AI Upseller settings"
        error={cfgQ.error} retrying={cfgQ.isFetching}
        onRetry={() => void cfgQ.refetch()} />
    );
  }

  const gateOn = !!cfg.qualification_gate_enabled;
  const isRecommended =
    !!cfg.qualification_gate_enabled && !!cfg.smart_pricing_enabled &&
    !!cfg.upsell_takes_over && !!cfg.post_buy_rung_enabled && !!cfg.gift_enabled &&
    (cfg.stop_after_unpaid_rungs ?? 1) === 2;

  /** Append starter templates (skipping labels already present) to Singles and SAVE —
   *  the Singles editor lives on the AI Chatter tab, so this writes straight through. */
  const loadStarter = () => {
    const cur = scriptsQ.data?.singles ?? [];
    const have = new Set(cur.map((s) => (s.label ?? "").trim().toLowerCase()));
    const add = starterSingles.filter((s) => !have.has((s.label ?? "").trim().toLowerCase()));
    if (add.length) saveSinglesM.mutate([...cur, ...add.map((s) => ({ ...s }))]);
  };

  return (
    <div className="space-y-4">
      {/* ── enable + takeover ── */}
      <Card className="p-4 space-y-3">
        <div className="flex items-center gap-2">
          <Sparkles size={16} className="text-accent" />
          <h3 className="text-sm font-medium">AI Upseller — sells in the chat, then hands back</h3>
          <div className="flex-1" />
          <EditRawJsonButton surface="ai-chatter-config" accountId={accountId} />
        </div>

        <p className="text-xs text-fg-dim">
          Same seller as <b>🤖 AI Chatter</b>, turned up. Your content library (Scripts,
          Singles, Simulate, Monitor) lives on that tab — this one is the sell-harder
          tuning + the prewritten pitch lines.
        </p>

        <div className="rounded-md border border-accent/40 bg-accent/5 px-3 py-3 space-y-2">
          <div className="flex items-center gap-3 flex-wrap">
            <Button size="sm" variant={gateOn ? "secondary" : "primary"}
              disabled={saveCfgM.isPending || !configLoaded}
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
            {saveCfgM.isError && (
              <span className="text-xs text-red-500">
                {saveCfgM.error?.message || "Save failed"} — nothing was stored.
              </span>
            )}
          </div>
          <p className="max-md:hidden text-xs text-fg-dim leading-relaxed">
            One click turns on: <b>sell in the chat</b>, <b>smart pricing</b>, <b>full
            takeover</b>, a <b>follow-up after a buy</b> (engages a silent buyer), a
            <b> free thank-you</b> after a couple of buys, and <b>one win-back discount</b>
            before it stops. The &quot;I&apos;m filming it now&quot; fiction stays off
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
            <span className="block max-md:hidden text-fg-dim text-xs">
              Once a fan is actively buying (an open offer, a fresh purchase, a live
              ladder), the upseller owns that thread <b>even if AI Chatter is in backup or closer
              mode</b> — a sale is never left waiting on the SLA or skipped as
              &quot;no intent.&quot; When the sale winds down the fan is handed back to
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
            <span className="block max-md:hidden text-fg-dim text-xs">
              When a fan is actually in the chat, the upseller can offer him content directly —
              instead of waiting for the scheduled PPV blast. A price never goes in
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
            <span className="block max-md:hidden text-fg-dim text-xs">
              After a fan buys, another piece is offered inside the hot window, and the
              price climbs. If he doesn&apos;t buy, the same clip comes back once more, cheaper,
              then stops. Prices stay between the Min and Max in <b>💸 PPV Library</b>.
            </span>
          </span>
        </label>

        {/* ── When the seller actually pulls the trigger ──────────────────────────────
            The offer is written by the MODEL (an >>OFFER marker it may or may not
            emit). Live on one account that was 184 replies against 4 offers — the model,
            not the gate, is what stops the selling. These two take the trigger back. */}
        <div className={cn("rounded-md border border-border bg-bg-elev-1 px-3 py-2.5 space-y-3",
          gateOn ? "" : "opacity-50 pointer-events-none")}>
          <div className="text-fg-dim text-xs">When the ask actually goes out</div>

          <label className="flex items-start gap-2 cursor-pointer">
            <input type="checkbox" className="mt-0.5" checked={!!cfg.force_ask}
              disabled={!gateOn}
              onChange={(e) => set({ force_ask: e.target.checked })} />
            <span className="text-sm">
              <span className="font-medium">Always sell into a hot chat</span>
              <span className="block max-md:hidden text-fg-dim text-xs">
                When he&apos;s mid-scene — actually sexting, actually replying — the PPV is
                attached instead of hoping the AI volunteers one. That moment
                is <b>24× more likely to end in a sale</b> than an ordinary message, and
                the AI was mid-conversation right through it. A man who said he&apos;s broke, turned an
                offer down, or asked for it to stop is still never priced.
              </span>
            </span>
          </label>

          <label className="space-y-1 block">
            <div className="text-fg-dim text-xs">
              …and if he never gets there: ask after this many of his messages
              <span className="ml-1 opacity-70">(0 = never)</span>
            </div>
            <input type="number" min={0} max={100} step={1}
              className={`${INPUT} w-full md:w-40`}
              disabled={!gateOn}
              value={cfg.ask_after_fan_msgs ?? 0}
              onChange={(e) => set({
                ask_after_fan_msgs: Math.max(0, Math.min(100, parseInt(e.target.value || "0", 10) || 0)),
              })} />
            <div className="max-md:hidden text-fg-dim text-xs">
              Some men never turn the chat sexual — they&apos;re friendly, they&apos;re
              chatting, and nobody ever asks them for a penny. After this many of{" "}
              <b>his</b> messages with no offer in front of him — the seller&apos;s{" "}
              <i>or</i> a chatter&apos;s — one goes there anyway. 15 is a reasonable floor. The
              brakes still apply — a fan who said he&apos;s broke is never asked, however
              long he talks.
            </div>
          </label>
        </div>

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
          <p className="max-md:hidden text-[11px] text-fg-dim leading-relaxed">
            Most of the climb comes from your <b>content order</b> (a $8 piece, then $24,
            then $50…) — these two just bound how fast a single item&apos;s price can rise.
            The ceiling is the <b>&quot;to the moon&quot; lever</b>: raise it and a whale
            can run, but{" "}
            <span className="text-amber-400">conversion drops past 3× his history (about
            52% → 31%)</span>, so push it only for a fan who keeps buying.
          </p>
        </div>
      </Card>

      {/* ── answer a content-ask with the content ──────────────────────────
          The trigger is INERT without the master switch (guarded server-side in
          plan_pack/plan_ask), so the checkbox writes both. Two independent boxes
          would let an operator tick the visible one, see it save, and get
          nothing — the exact shape the config validator's own comments warn
          about twice. */}
      <Card className="p-4 space-y-3">
        <h3 className="text-sm font-medium">When he asks to see something</h3>
        <label className="flex items-start gap-2 cursor-pointer">
          <input type="checkbox" className="mt-0.5" checked={!!cfg.pack_on_ask_enabled}
            onChange={(e) => set({
              pack_on_ask_enabled: e.target.checked,
              ...(e.target.checked ? { pack_send_enabled: true } : {}),
            })} />
          <span className="text-sm">
            <span className="font-medium">Answer with the content, not a sentence</span>
            <span className="block max-md:hidden text-fg-dim text-xs">
              When he asks for something — <i>&quot;send me feet pics&quot;</i>,
              <i> &quot;show me u in leather&quot;</i>, or just <i>&quot;show me&quot;</i> —
              the reply is a <b>priced set from the vault that matches what he
              asked for</b>, instead of talking about it. It reads the ask from the
              thread, searches the whole vault plus your curated folders, and refuses
              rather than sending something that doesn&apos;t fit. Any refusal falls
              through to the normal reply, so this can only ever <b>replace</b> a
              generic offer, never silence one.
            </span>
          </span>
        </label>

        {/* The rules that are NOT knobs. An operator turning this on is entitled
            to know what it will do without reading the source. */}
        <div className={cn("rounded-md border border-border bg-bg-elev-1 px-3 py-2.5 space-y-1.5",
          cfg.pack_on_ask_enabled ? "" : "opacity-50")}>
          <div className="text-fg-dim text-xs">What it does on its own</div>
          <ul className="text-xs text-fg-dim leading-relaxed space-y-1 list-disc pl-4">
            <li>
              <b className="text-fg">The creator is alone in it</b> unless he asked for
              company. Anything the vault <i>knows</i> has someone else in frame is
              dropped before it can be picked — that comes from the AI description
              pass, so an item never described is not known either way and can still
              be sent. Run <b>Describe</b> in 🖼️ Vault AI first for this to be airtight.
            </li>
            <li>
              <b className="text-fg">The price follows what he has paid.</b> A fan who has
              never bought is capped at <b>$100</b>; one who has paid <b>$50+</b> for a
              single PPV unlocks <b>$200</b>. His first ask is never more than ~$60.
            </li>
            <li>
              <b className="text-fg">A cold fan gets the tamer end first</b> — an
              ordering, not a wall. If the tame end runs out before the price is
              covered it keeps going, because a fan who asked and got silence is the
              worse outcome.
            </li>
            <li>
              The caption states exactly what is attached, and the send is{" "}
              <b className="text-fg">refused rather than softened</b> if the media
              stops matching that claim.
            </li>
          </ul>
        </div>

        <label className={cn("flex items-start gap-2",
          cfg.pack_on_ask_enabled ? "cursor-pointer" : "cursor-not-allowed opacity-50")}>
          <input type="checkbox" className="mt-0.5" checked={!!cfg.value_caps_price}
            disabled={!cfg.pack_on_ask_enabled}
            onChange={(e) => set({ value_caps_price: e.target.checked })} />
          <span className="text-sm">
            <span className="font-medium">Never charge above the content&apos;s worth</span>
            <span className="block max-md:hidden text-fg-dim text-xs">
              Prices are built from a rate card — a video is worth $5–10 per 10 seconds
              depending on how explicit it is, an explicit photo about $10, teases are
              filler. By default that is a <b>baseline</b>, so a fan whose history
              supports it can be quoted <i>above</i> what the card says the set is
              worth. Tick this to make the card a hard ceiling instead.
            </span>
          </span>
        </label>
      </Card>

      {/* ── offer pacing ── */}
      <Card className="p-4 space-y-3">
        <h3 className="text-sm font-medium">Offer pacing</h3>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3 text-sm">
          <label className="space-y-1">
            <div className="text-fg-dim text-xs">Offer mode</div>
            {/* Default flipped both→ppv (2026-07-23): a "both" message (priced
                + tip-ask) let a fan pay twice for one promise. */}
            <select className={`${INPUT} w-full`} value={cfg.offer_mode ?? "ppv"}
              onChange={(e) => set({ offer_mode: e.target.value as AiChatterConfig["offer_mode"] })}>
              <option value="ppv">PPV only (default)</option>
              <option value="tip">tip only</option>
              <option value="both">tip or PPV</option>
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
                Even if he unlocks without a word, one more offer goes out while he&apos;s
                hot. Off, nothing moves until he talks.
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
              <div className="text-fg-dim text-xs">7-day spend brake ($ — then the selling eases off)</div>
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
                  Pretends the clip is being recorded live, right before a PPV. Off by default —
                  it&apos;s a chargeback surface and adds no measured revenue.
                </span>
              </span>
            </label>
          </div>
        </div>
        {/* Desktop keeps the in-flow Save row exactly where it was; on a phone
         *  it is ~2,000px down the scroll, so a sticky twin is appended as the
         *  last child of the outer container below. */}
        <div className="hidden md:flex items-center gap-2 flex-wrap border-t border-border pt-2">
          <Button size="sm" disabled={saveCfgM.isPending || !configLoaded}
            onClick={() => saveCfg()}>
            <Save size={14} className="mr-1" /> Save Upseller settings
          </Button>
          {saveCfgM.isSuccess && <span className="text-xs text-green-400">saved ✓</span>}
          {saveCfgM.isError && (
            <span className="text-xs text-red-500">
              {saveCfgM.error?.message || "Save failed"} — nothing was stored.
            </span>
          )}
        </div>
      </Card>

      {/* ── prewritten scaffolding: the creator's lines + starter pack ── */}
      <ScriptPackCard
        pack={shippedPack}
        help={slotHelp}
        text={packText}
        setText={(slot, v) => setPackText((t) => ({ ...t, [slot]: v }))}
        onSave={() => saveCfg()}
        saving={saveCfgM.isPending}
        canSave={configLoaded}
        saved={saveCfgM.isSuccess}
        error={saveCfgM.isError
          ? `${saveCfgM.error?.message || "Save failed"} — nothing was stored.`
          : null}
      />

      <Card className="p-4 space-y-2">
        <div className="flex items-center gap-2 flex-wrap">
          <h3 className="text-sm font-medium">Starter pack — prewritten pieces</h3>
          <div className="flex-1" />
          <Button size="sm" variant="secondary" disabled={saveSinglesM.isPending} onClick={loadStarter}>
            <Sparkles size={14} className="mr-1" /> Load starter pack
          </Button>
          {saveSinglesM.isSuccess && <span className="text-xs text-green-400">added ✓</span>}
        </div>
        <p className="text-xs text-fg-dim leading-relaxed">
          Drops {starterSingles.length} prewritten pieces (pitches + suggested prices) into
          your <b>Singles</b> — then attach media and edit them on the <b>🤖 AI Chatter</b>{" "}
          tab. Skips any already there; a piece with no media can&apos;t be offered, so nothing
          sells by accident. The price authority (Min/Max) is the <b>💸 PPV Library</b> tab.
        </p>
      </Card>

      <div className="md:mb-0"><GuideCard /></div>

      {/* Phone-only sticky Save bar (`hidden` at >=768px, so desktop renders
       *  nothing here — the in-flow row inside the Card above still owns it).
       *  Sticky only pins while its own container is on screen, hence "last
       *  child of the outer container". `-mx-4 px-4` bleeds to the host Card's
       *  p-4 edges. */}
      <div className="hidden max-md:flex sticky bottom-0 z-20 -mx-4 px-4 py-3 items-center gap-2 flex-wrap bg-panel/95 backdrop-blur border-t border-border pb-[calc(env(safe-area-inset-bottom)+0.75rem)]">
        <Button size="sm" disabled={saveCfgM.isPending || !configLoaded}
          onClick={() => saveCfg()}>
          <Save size={14} className="mr-1" /> Save Upseller settings
        </Button>
        {saveCfgM.isSuccess && <span className="text-xs text-green-400">saved ✓</span>}
        {saveCfgM.isError && (
          <span className="text-xs text-red-500">
            {saveCfgM.error?.message || "Save failed"} — nothing was stored.
          </span>
        )}
      </div>
    </div>
  );
}
