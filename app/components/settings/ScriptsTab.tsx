"use client";

/**
 * ScriptsTab — Automations → "🤖 AI Chatter".
 *
 * The base conversation surface for the `ai_chatter` automation: WHO she talks to
 * and HOW she paces herself. The SELLING brain (the gate, smart pricing, the price
 * ladder, the content, simulate/monitor) lives in its own "💰 AI Upseller" tab
 * (UpsellerTab) — both edit the same ai_chatter_config_json via `useSellerConfig`,
 * which posts the FULL sparse config so neither tab clobbers the other's keys.
 *
 * Here: enable, Full/Closer engagement, old-fan engagement, backup-SLA vs always,
 * the whale gate, the Cadence controller (reply caps / back-off), Human Rhythm
 * (reply timing + sleep), and the texting-style opt-ins.
 */

import { Bot, Save } from "lucide-react";

import { Button, Card } from "@/components/ui/primitives";
import { EditRawJsonButton } from "@/components/settings/JsonConfigModal";
import { useAiChatterConfig } from "@/hooks/useCatalog";
import {
  INPUT, RhythmSection, dollars, useSellerConfig, useSellerStyle,
} from "@/components/settings/sellerShared";
import type { AiChatterConfig } from "@/hooks/useCatalog";

export default function ScriptsTab({ accountId }: { accountId: string | null }) {
  const cfgQ = useAiChatterConfig(accountId);
  const { cfg, set, tz, setTz, saveCfg, saveCfgM } = useSellerConfig(accountId);
  const style = useSellerStyle(accountId);

  if (!accountId) return <div className="text-sm text-fg-dim">Pick an account above.</div>;
  if (cfgQ.isLoading) return <div className="text-sm text-fg-dim">Loading…</div>;

  // Nested tier cap: always write a COMPLETE object so the server's shallow merge
  // never drops a tier.
  const setLimit = (
    tier: "baseline" | "buying_signal" | "no_signal" | "pic_sent", n: number,
  ) => set({ msg_limits_by_signal: { ...(cfg.msg_limits_by_signal ?? {}), [tier]: n } });

  return (
    <div className="space-y-4">
      <Card className="p-4 space-y-3">
        <div className="flex items-center gap-2">
          <Bot size={16} />
          <h3 className="text-sm font-medium">AI Chatter — how she talks &amp; who she talks to</h3>
          <div className="flex-1" />
          <EditRawJsonButton surface="ai-chatter-config" accountId={accountId} />
          <label className="flex items-center gap-2 text-sm">
            <input type="checkbox" checked={!!cfg.enabled}
              onChange={(e) => set({ enabled: e.target.checked })} />
            Enabled
          </label>
        </div>

        <p className="text-xs text-fg-dim">
          The selling controls (offering content in the chat, pricing, the ladder,
          your content library) live in the <b>💰 AI Upseller</b> tab. This tab is
          just her conversation behavior.
        </p>

        {/* ── engagement ── */}
        <div className="rounded-md border border-border bg-bg-elev-1 px-3 py-2.5 space-y-2 text-sm">
          <div className="text-fg-dim text-xs">Engagement</div>
          <label className="flex items-start gap-2 cursor-pointer">
            <input type="radio" name="ai-chatter-engagement" className="mt-0.5"
              checked={!cfg.intent_only}
              onChange={() => set({ intent_only: false })} />
            <span>
              <span className="font-medium">Full chatter</span>
              <span className="block text-fg-dim text-xs">
                Reply to everyone. (The Upseller only pitches when a fan shows buying intent.)
              </span>
            </span>
          </label>
          <label className="flex items-start gap-2 cursor-pointer">
            <input type="radio" name="ai-chatter-engagement" className="mt-0.5"
              checked={!!cfg.intent_only}
              onChange={() => set({ intent_only: true })} />
            <span>
              <span className="font-medium">Closer only</span>
              <span className="block text-fg-dim text-xs">
                Stay silent unless the fan shows buying intent (or already has an open
                offer). Pure chit-chat goes to Auto Convo + your team. Skipped fans cost
                no AI calls. <b>The Upseller still takes over any fan mid-sale</b> even in
                this mode.
              </span>
            </span>
          </label>
        </div>

        {/* ── old fans ── */}
        <div className="rounded-md border border-border bg-bg-elev-1 px-3 py-2.5 space-y-2 text-sm">
          <label className="flex items-start gap-2 cursor-pointer">
            <input type="checkbox" className="mt-0.5" checked={!!cfg.engage_old_fans}
              onChange={(e) => set({ engage_old_fans: e.target.checked })} />
            <span>
              <span className="font-medium">Also chat with old (pre-AI) fans</span>
              <span className="block text-fg-dim text-xs">
                Fans flagged during old-fan onboarding (old_fan_pre_ai) are normally
                left to your team. On, the AI replies to them too — mostly just keeping
                the convo going, weaving in one get-to-know question about every N replies.
              </span>
            </span>
          </label>
          {!!cfg.engage_old_fans && (
            <label className="flex items-center gap-2 text-xs text-fg-dim pl-6">
              Info question about every
              <input type="number" className={`${INPUT} w-16`} min={1} max={100}
                value={cfg.old_fan_question_every ?? 10}
                onChange={(e) => set({
                  old_fan_question_every:
                    Math.max(1, parseInt(e.target.value || "10", 10) || 10),
                })} />
              replies
            </label>
          )}
        </div>

        {/* ── mode / SLA / whale ── */}
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
            <div className="text-fg-dim text-xs">Hands-off after human send (h)</div>
            <input type="number" className={`${INPUT} w-full`} min={0}
              value={cfg.resume_after_manual_hours ?? 6}
              onChange={(e) => set({ resume_after_manual_hours: parseInt(e.target.value || "0", 10) })} />
          </label>
        </div>

        {/* ── cadence: stop / re-engage ── */}
        <div className="rounded-md border border-border bg-bg-elev-1 px-3 py-2.5 space-y-3 text-sm">
          <label className="flex items-center gap-2">
            <input type="checkbox" checked={!!cfg.cadence_enabled}
              onChange={(e) => set({ cadence_enabled: e.target.checked })} />
            <span className="font-medium">Cadence — stop &amp; re-engage</span>
            <span className="text-fg-dim text-xs">
              back off after a burst instead of chatting/selling forever
            </span>
          </label>
          <div className={cfg.cadence_enabled ? "space-y-3" : "space-y-3 opacity-50 pointer-events-none"}>
            <div>
              <div className="text-fg-dim text-xs mb-1">
                Reply caps per conversation burst, by signal (0 = no limit)
              </div>
              <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
                <label className="space-y-1">
                  <div className="text-fg-dim text-xs">Baseline (just chatting)</div>
                  <input type="number" className={`${INPUT} w-full`} min={0}
                    value={cfg.msg_limits_by_signal?.baseline ?? 10}
                    onChange={(e) => setLimit("baseline", parseInt(e.target.value || "0", 10))} />
                </label>
                <label className="space-y-1">
                  <div className="text-fg-dim text-xs">Buying signal</div>
                  <input type="number" className={`${INPUT} w-full`} min={0}
                    value={cfg.msg_limits_by_signal?.buying_signal ?? 30}
                    onChange={(e) => setLimit("buying_signal", parseInt(e.target.value || "0", 10))} />
                </label>
                <label className="space-y-1">
                  <div className="text-fg-dim text-xs">Offered, no buy</div>
                  <input type="number" className={`${INPUT} w-full`} min={0}
                    value={cfg.msg_limits_by_signal?.no_signal ?? 5}
                    onChange={(e) => setLimit("no_signal", parseInt(e.target.value || "0", 10))} />
                </label>
                <label className="space-y-1">
                  <div className="text-fg-dim text-xs">He sent a pic</div>
                  <input type="number" className={`${INPUT} w-full`} min={0}
                    value={cfg.msg_limits_by_signal?.pic_sent ?? 40}
                    onChange={(e) => setLimit("pic_sent", parseInt(e.target.value || "0", 10))} />
                </label>
              </div>
            </div>
            <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
              <label className="space-y-1">
                <div className="text-fg-dim text-xs">New-burst gap (min)</div>
                <input type="number" className={`${INPUT} w-full`} min={5}
                  value={cfg.session_gap_minutes ?? 60}
                  onChange={(e) => set({ session_gap_minutes: parseInt(e.target.value || "5", 10) })} />
              </label>
              <label className="space-y-1">
                <div className="text-fg-dim text-xs">Post-purchase window (min)</div>
                <input type="number" className={`${INPUT} w-full`} min={0}
                  value={cfg.post_purchase_minutes ?? 25}
                  onChange={(e) => set({ post_purchase_minutes: parseInt(e.target.value || "0", 10) })} />
              </label>
              <label className="space-y-1">
                <div className="text-fg-dim text-xs">Offer goes stale after (min)</div>
                <input type="number" className={`${INPUT} w-full`} min={0}
                  value={cfg.offer_expiry_minutes ?? 120}
                  onChange={(e) => set({ offer_expiry_minutes: parseInt(e.target.value || "0", 10) })} />
              </label>
            </div>
            <label className="flex items-center gap-1.5">
              <input type="checkbox" checked={!!cfg.nudge_enabled}
                onChange={(e) => set({ nudge_enabled: e.target.checked })} />
              Re-engage nudge — one message if an offered fan goes quiet without buying
            </label>
            {cfg.nudge_enabled && (
              <label className="space-y-1 block max-w-[12rem]">
                <div className="text-fg-dim text-xs">Nudge after (min)</div>
                <input type="number" className={`${INPUT} w-full`} min={1}
                  value={cfg.nudge_after_minutes ?? 15}
                  onChange={(e) => set({ nudge_after_minutes: parseInt(e.target.value || "1", 10) })} />
              </label>
            )}
          </div>
          <details className="text-xs text-fg-dim">
            <summary className="cursor-pointer select-none font-medium">
              ⓘ How cadence &amp; Human Rhythm work together
            </summary>
            <div className="mt-2 space-y-2 leading-relaxed">
              <p>
                <span className="font-medium text-fg">Two separate systems that compose.</span>{" "}
                <b>Cadence</b> is a COUNT — how many replies she sends in one conversation
                burst before backing off (a &quot;stop&quot; is a silent skip that reopens
                on a real buying signal or after a new-burst gap of silence).{" "}
                <b>Human Rhythm</b> (below) is TIMING — WHEN each reply lands, and whether
                she takes a break at all.
              </p>
              <p>
                <span className="font-medium text-fg">The &quot;goes for a walk&quot; break</span>{" "}
                is Rhythm, and it is <em>per-fan</em> and <em>heat-driven</em>: a hot chat
                (he&apos;s replying fast, an open offer, a fresh buy) → she answers fast; a
                cold / boring / no-sales chat → her replies stretch out and she may take a
                real multi-hour break. She is NOT cold right after a sale — a purchase makes
                her hotter (strike while hot); the break comes once the hot window winds down.
              </p>
            </div>
          </details>
        </div>

        {/* ── Human Rhythm ── */}
        <RhythmSection
          cfg={cfg}
          set={set}
          tz={tz}
          setTz={setTz}
          utcOffset={cfgQ.data?.utc_offset ?? 0}
          derived={cfgQ.data?.derived_sleep_window ?? ["03:00", "10:00"]}
          effective={cfgQ.data?.effective_sleep_window ?? ["03:00", "10:00"]}
        />

        {/* ── texting style ── */}
        <div className="flex flex-wrap items-center gap-x-4 gap-y-2 text-sm border-t border-border pt-2">
          <span className="text-xs text-fg-dim">Texting style:</span>
          <label className="flex items-center gap-1.5">
            <input type="checkbox" checked={style.girlStyle}
              onChange={(e) => style.setGirlStyle(e.target.checked)} />
            Girl style (bubbles + humanizer)
          </label>
          <label className="flex items-center gap-1.5">
            <input type="checkbox" checked={style.typosOn}
              onChange={(e) => style.setTyposOn(e.target.checked)} />
            Typos
          </label>
          <label className="flex items-center gap-1.5">
            <input type="checkbox" checked={style.nonnativeOn}
              onChange={(e) => style.setNonnativeOn(e.target.checked)} />
            Non-native English
          </label>
          <Button size="sm" variant="ghost" disabled={style.saveStyleM.isPending}
            onClick={style.saveStyle}>
            Save style
          </Button>
          {style.saveStyleM.isSuccess && <span className="text-xs text-green-400">saved ✓</span>}
        </div>

        <div className="flex items-center gap-2">
          <Button size="sm" disabled={saveCfgM.isPending} onClick={() => saveCfg()}>
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
    </div>
  );
}
