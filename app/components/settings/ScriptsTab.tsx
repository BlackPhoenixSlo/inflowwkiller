"use client";

/**
 * ScriptsTab — Automations → "🤖 AI Chatter".
 *
 * (The file name is historical — the ordered-script ladders it was named for
 * were removed 2026-08-19; singles are the whole catalog.)
 *
 * The base seller + conversation surface for the `ai_chatter` automation: WHO it
 * talks to, HOW it paces itself, AND the content library (Singles, Simulate,
 * Monitor) — because the base chatter sells from that catalog too. The
 * "sell harder" TUNING (the gate, smart pricing, price ladder, after-a-buy) lives in
 * the "💰 AI Upseller" tab (UpsellerTab). Both edit the same ai_chatter_config_json
 * via `useSellerConfig`, which posts the FULL sparse config so neither tab clobbers
 * the other's keys.
 */

import { Fragment, useEffect, useState } from "react";
import { Bot, Play, Plus, Save, X } from "lucide-react";

import { Badge, Button, Card } from "@/components/ui/primitives";
import { EditRawJsonButton } from "@/components/settings/JsonConfigModal";
import {
  AutomationRuleBadge, AutomationSwitchCard,
} from "@/components/settings/AutomationSwitch";
import { MediaCacheProvider } from "@/hooks/useMediaCache";
import {
  useAiChatterConfig, useAiChatterSessions, useCancelOffer, useCatalogSingles,
  useGenerateLines, useSaveSingles, useSimulate, useSuggestSingles, useSuggestTexts,
  type CatalogItemT, type TextSuggestionT,
} from "@/hooks/useCatalog";
import {
  ConfigLoadError, INPUT, ItemsTable, NEW_ITEM, RhythmSection, dollars,
  useSellerConfig, useSellerStyle,
} from "@/components/settings/sellerShared";
import ReengageBuyersTab from "@/components/settings/ReengageBuyersTab";
import MakeRightTab from "@/components/settings/MakeRightTab";
import type { AiChatterConfig } from "@/hooks/useCatalog";

const LT_NUM = "w-16 rounded border border-border bg-bg px-1.5 py-0.5 text-sm";

/** A [min, max] pair of small number boxes. `fallback` is what the engine uses
 *  when the key is absent; the boxes show it so an untouched account reads true.
 *  Order is fixed on blur (max pulled up to min), never while typing. */
function PairInput({ value, fallback, min, max, prefix, onChange }: {
  value: number[] | undefined; fallback: [number, number]; min: number; max: number;
  prefix?: string; onChange: (v: number[]) => void;
}) {
  const lo = value?.[0] ?? fallback[0];
  const hi = value?.[1] ?? fallback[1];
  const clamp = (n: number) => Math.max(min, Math.min(max, Math.round(n)));
  const fix = () => { if (lo > hi) onChange([lo, lo]); };
  return (
    <span className="inline-flex items-center gap-1">
      {prefix}
      <input type="number" min={min} max={max} className={LT_NUM} value={lo} onBlur={fix}
        onChange={(e) => onChange([clamp(Number(e.target.value) || min), hi])} />
      –{prefix}
      <input type="number" min={min} max={max} className={LT_NUM} value={hi} onBlur={fix}
        onChange={(e) => onChange([lo, clamp(Number(e.target.value) || min)])} />
    </span>
  );
}

/** One reason per line. Edited as free text and committed on blur, so a newline
 *  mid-typing is not eaten by the list round-trip. An empty box commits [] and
 *  the server drops the key — the shipped pool applies. */
function ReasonsBox({ label, value, onCommit }: {
  label: string; value: string[] | undefined; onCommit: (v: string[]) => void;
}) {
  const joined = (value ?? []).join("\n");
  const [draft, setDraft] = useState(joined);
  const [focused, setFocused] = useState(false);
  useEffect(() => { if (!focused) setDraft(joined); }, [joined, focused]);
  return (
    <label className="block">
      <span className="block mb-1">{label} — one per line</span>
      <textarea className={`${INPUT} w-full h-28 font-mono text-xs`} value={draft} spellCheck={false}
        onFocus={() => setFocused(true)}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={() => {
          setFocused(false);
          onCommit(draft.split(/[\n,]/).map((s) => s.trim()).filter(Boolean));
        }} />
    </label>
  );
}

// Proven-spender cap floor: the two windows the UI edits, with the display/seed
// defaults. THE in-file home for these numbers — the render maps over this table
// (the same defaults live server-side in ai_chatter's config table).
const SPEND_RULES = [
  { days: 30, dollars: 10, cap: 10 },
  { days: 7, dollars: 100, cap: 15 },
] as const;

export default function ScriptsTab({ accountId }: { accountId: string | null }) {
  const cfgQ = useAiChatterConfig(accountId);
  const { cfg, set, tz, saveCfg, saveCfgM, configLoaded } = useSellerConfig(accountId);
  const style = useSellerStyle(accountId);

  // Content library — the base chatter sells from this catalog (Upseller just tunes how).
  const scriptsQ = useCatalogSingles(accountId);
  const saveSinglesM = useSaveSingles(accountId);
  const suggestM = useSuggestSingles(accountId);
  const simulateM = useSimulate(accountId);
  const sessionsQ = useAiChatterSessions(accountId);
  const cancelM = useCancelOffer(accountId);

  const [singles, setSingles] = useState<CatalogItemT[]>([]);
  useEffect(() => { setSingles(scriptsQ.data?.singles ?? []); }, [scriptsQ.data?.singles]);
  // What the last fill could NOT place, so an empty row explains itself instead
  // of just staying blank.
  const [fillNote, setFillNote] = useState<string>("");

  // Fill content into singles that have TEXT but no CONTENT. Only ever patches
  // EMPTY rows — hand-picked media is never second-guessed — and nothing is
  // saved until the operator presses "Save singles".
  // Let a row take media another row already sells. Off is the safer default
  // (a fan climbing the ladder shouldn't be re-sold what he owns), but once the
  // catalog outgrows the vault exclusivity starves rows outright — 18 rows over
  // 101 items left a caption matching 69 photos with none free.
  const [allowReuse, setAllowReuse] = useState(false);

  const fillContent = () => {
    setFillNote("");
    // Rows with no id are not in the DB yet (they came from "Suggest text
    // sets"), so they have to ride along in the request or the fill silently
    // skips exactly the rows it was meant to serve.
    const drafts = singles.filter((it) => it.id == null);
    const draftKeyOf = new Map(drafts.map((it, i) => [it, `draft:${i}`]));
    suggestM.mutate({ allowReuse, drafts: drafts.map((it) => ({
      label: it.label ?? "", description_for_ai: it.description_for_ai ?? "",
      kind: it.kind, price_cents: it.price_cents,
    })) }, {
      onSuccess: (res) => {
        const byKey = new Map(res.proposals.map((p) => [p.key ?? `db:${p.id}`, p]));
        setSingles((prev) => prev.map((it) => {
          const key = it.id != null ? `db:${it.id}` : draftKeyOf.get(it);
          const p = key ? byKey.get(key) : undefined;
          if (!p || !p.media_ids.length || it.media_ids.length) return it;
          // The preview rides along with the media: OF unlocks a slice of the
          // attachment rather than a separate file, so dropping it here is what
          // left all 21 saved rows as locked PPVs with no free frame.
          return { ...it, media_ids: p.media_ids,
            preview_media_ids: p.preview_media_ids ?? [] };
        }));
        const filled = res.proposals.filter((p) => p.media_ids.length).length;
        const stuck = res.proposals.filter((p) => !p.media_ids.length);
        // Each stuck row carries its OWN reason, and the two reasons call for
        // opposite actions: "nothing matches" means shoot it, "already sold by
        // another row" means rebalance. Collapsing both into "no honest match"
        // sent the operator off to film content they already own.
        const withPrev = res.proposals.filter((p) => p.preview_media_ids?.length).length;
        const padded = res.proposals.reduce((n, p) => n + (p.padding_media_ids?.length ?? 0), 0);
        const duped = res.proposals.filter((p) => p.dup_media_ids?.length).length;
        // A shape shortfall never blocks the fill — the row ships and says so.
        // Runtime is the one bar a vault of short clips can't reach, and the
        // fix is a longer shoot or a lower price, neither of which is ours.
        const thin = res.proposals.filter((p) => p.shape_note);
        setFillNote(
          `Filled ${filled} of ${res.proposals.length}` +
          (withPrev ? `, ${withPrev} with a free preview frame` : "") +
          (padded ? `, ${padded} filler still(s) added` : "") +
          (duped ? `, ${duped} row(s) duplicated to reach their minimum` : "") + ". " +
          (stuck.length
            ? "Left empty on purpose (an item with no content is never offered) — " +
              stuck.map((p) => `${p.label || "(unnamed)"}: ${p.empty_reason}`).join("; ")
            : "Review the thumbnails, then Save singles.") +
          (thin.length
            ? ` ⚠ Under-sized for the price: ${thin.map(
              (p) => `${p.label || "(unnamed)"} — ${p.shape_note}`).join("; ")}`
            : ""),
        );
      },
    });
  };
  // Propose CAPTION rows to add — text/price/kind, no media. The other half of
  // "Fill content": that fills media into a row that already has text, and
  // until now nothing produced the text, so an empty catalog stayed empty.
  //
  // Suggestions are a PICK LIST, never auto-added: what the account sells is the
  // operator's call, and a button that silently appends 13 rows is one the
  // operator has to undo. Chosen rows land with ZERO content, which "Fill
  // content" then binds.
  const suggestTextsM = useSuggestTexts(accountId);
  const [suggested, setSuggested] = useState<TextSuggestionT[]>([]);
  const [shootList, setShootList] = useState<TextSuggestionT[]>([]);
  const [picked, setPicked] = useState<Set<string>>(new Set());

  const suggestTexts = () => {
    setFillNote("");
    suggestTextsM.mutate(undefined, {
      onSuccess: (res) => {
        setShootList(res.shoot);
        if (res.blocked) { setSuggested([]); setFillNote(res.blocked); return; }
        setSuggested(res.proposals);
        setPicked(new Set(res.proposals.map((p) => p.label)));   // all on by default
        if (!res.proposals.length) {
          setFillNote(
            res.summary.already_present
              ? `Nothing new to suggest — all ${res.summary.already_present} default ` +
                "captions are already in your catalog."
              : "Nothing to suggest from this vault.",
          );
        }
      },
    });
  };

  // Write NEW lines from the vault's own vision facts. Feeds the SAME pick
  // list: the 14 shipped captions are identical for every account, these are
  // about the media THIS creator has and nothing is selling yet.
  const genLinesM = useGenerateLines(accountId);
  const generateLines = () => {
    setFillNote("");
    genLinesM.mutate(undefined, {
      onSuccess: (res) => {
        setShootList([]);
        if (res.blocked) { setSuggested([]); setFillNote(res.blocked); return; }
        setSuggested(res.proposals);
        setPicked(new Set(res.proposals.map((p) => p.label)));
        setFillNote(
          `Wrote ${res.summary.generated} line(s) from ${res.summary.groups} media ` +
          `group(s)${res.errors?.length ? ` — ${res.errors.length} failed` : ""}. ` +
          "Tick the ones you want, then Add selected.",
        );
      },
    });
  };

  const addPicked = () => {
    const rows = suggested.filter((p) => picked.has(p.label));
    setSingles((prev) => [
      ...prev,
      ...rows.map((p) => ({
        ...NEW_ITEM, kind: p.kind, label: p.label,
        description_for_ai: p.description_for_ai,
        price_cents: p.price_cents,
      })),
    ]);
    setSuggested([]);
    setPicked(new Set());
    setFillNote(
      `Added ${rows.length} caption row(s) with no content. ` +
      'Press "✨ Fill content" to pick vault media for them, then Save singles.',
    );
  };

  const [fanSays, setFanSays] = useState("ngl im kinda in the mood.. u got anything for me? 🥵");

  if (!accountId) return <div className="text-sm text-fg-dim">Pick an account above.</div>;
  if (cfgQ.isLoading || scriptsQ.isLoading) return <div className="text-sm text-fg-dim">Loading…</div>;
  // A failed load is NOT an empty account. Rendering on would fall back to the
  // literals below (`cfg.sla_minutes ?? 10`, a $1,000 whale gate, 6h hands-off)
  // and they read as SAVED settings — the config looks like it "reset to
  // defaults" when it simply never arrived, and one Save makes that true.
  if (cfgQ.isError || scriptsQ.isError) {
    const what = [cfgQ.isError && "AI Chatter settings", scriptsQ.isError && "content library"]
      .filter(Boolean).join(" or ");
    return (
      <ConfigLoadError what={`this account's ${what}`}
        error={cfgQ.error ?? scriptsQ.error}
        retrying={cfgQ.isFetching || scriptsQ.isFetching}
        onRetry={() => {
          if (cfgQ.isError) void cfgQ.refetch();
          if (scriptsQ.isError) void scriptsQ.refetch();
        }} />
    );
  }

  // Nested tier cap: always write a COMPLETE object so the server's shallow merge
  // never drops a tier.
  const setLimit = (
    tier: "baseline" | "buying_signal" | "no_signal" | "pic_sent", n: number,
  ) => set({ msg_limits_by_signal: { ...(cfg.msg_limits_by_signal ?? {}), [tier]: n } });

  // Proven-spender cap floor (item 21b). Each row edits either the dollar
  // threshold or the cap for its window. Always write the WHOLE list (the server
  // shallow-merges the config, so a partial list would drop the other rule).
  // A row whose threshold or cap is cleared to 0 is inert server-side (never caps at 0).
  const spendRule = (days: number) =>
    (cfg.msg_limits_by_spend ?? []).find((r) => r.days === days);
  const setSpend = (days: number, patch: { min_cents?: number; cap?: number }) => {
    const rule = SPEND_RULES.find((r) => r.days === days)!;
    const cur = spendRule(days) ??
      { days, min_cents: rule.dollars * 100, cap: rule.cap };
    const next = { ...cur, ...patch };
    const rest = (cfg.msg_limits_by_spend ?? []).filter((r) => r.days !== days);
    set({ msg_limits_by_spend: [...rest, next].sort((a, b) => b.days - a.days) });
  };

  const openOffers = (sessionsQ.data?.offers ?? []).filter((o) => o.status === "open");

  return (
    <MediaCacheProvider accountId={accountId}>
    <div className="space-y-4">
      <Card className="p-4 space-y-3">
        <div className="flex items-center gap-2">
          <Bot size={16} />
          <h3 className="text-sm font-medium">AI Chatter — how it talks &amp; who it talks to</h3>
          <div className="flex-1" />
          <EditRawJsonButton surface="ai-chatter-config" accountId={accountId} />
          {/* "On" has two halves — this CONFIG flag, and the automation_rules row
              that actually ticks the engine — and for a while the tab showed a
              checkbox for each, side by side, which read as one control duplicated.
              Save now writes both (see put_ai_chatter_config), so this box is the
              whole of on and the badge is a status light: it says whether the row
              is ticking, and at what cadence. */}
          <AutomationRuleBadge accountId={accountId} kind="ai_chatter" />
          <label className="flex items-center gap-2 text-sm">
            <input type="checkbox" checked={!!cfg.enabled}
              onChange={(e) => set({ enabled: e.target.checked })} />
            Enabled
          </label>
        </div>

        <p className="text-xs text-fg-dim">
          The selling controls (offering content in the chat, pricing, the ladder,
          your content library) live in the <b>💰 AI Upseller</b> tab. This tab is
          just its conversation behavior.
        </p>

        {/* ── the chatter that had nowhere else to live ──
            The info-gather engine has no tab of its own: building a rule by hand in
            the Automation rules list was the ONLY way to start it, and it is what
            works everyone the payer floor below hands off. (This tab's OWN rule is
            the "Enabled" box in the header — one control, not two.) */}
        <AutomationSwitchCard accountId={accountId} kind="welcome_chatter_for_info"
          title="Get to know fans (AI info-gather) — the warm-up chatter">
          Opens and continues conversations to learn about a fan and build his
          profile, one gentle question at a time (it feeds Generate fan profiles).
          It never sells — it is what talks to everyone <b>below</b> the payer line
          set here, until they buy something and this tab&apos;s seller takes over.
        </AutomationSwitchCard>

        {/* ── payer floor ── */}
        <div className="rounded-md border border-border bg-bg-elev-1 px-3 py-2.5 space-y-2 text-sm">
          <label className="flex items-start gap-2 cursor-pointer">
            {/* Default ON, and an account that has never saved this key has no
                stored value — so `!== false` is what makes the box match what the
                engine actually does. `!!cfg.payers_only` would draw it unchecked
                on every untouched account while the floor was live. */}
            <input type="checkbox" className="mt-0.5" checked={cfg.payers_only !== false}
              onChange={(e) => set({ payers_only: e.target.checked })} />
            <span>
              <span className="font-medium">Only chat fans who have bought before</span>
              <span className="block text-fg-dim text-xs">
                The Upseller answers a fan once he has bought <b>content</b> — a tip or an
                unlocked PPV. <b>A subscription doesn&apos;t count</b>: paying to get in the
                door is what makes him worth working, not what ends it. Everyone below the
                line stays with Auto Convo, which keeps chatting and profiling him until he
                buys something. Fans mid-sale and engaged old fans are always answered.
              </span>
            </span>
          </label>
        </div>

        {/* ── life-expense tip ask — the switch and EVERY knob, one card ── */}
        <div className="rounded-md border border-border bg-bg-elev-1 px-3 py-2.5 space-y-2 text-sm">
          <label className="flex items-start gap-2 cursor-pointer">
            <input type="checkbox" className="mt-0.5" checked={!!cfg.life_tip_ask_enabled}
              onChange={(e) => set({ life_tip_ask_enabled: e.target.checked })} />
            <span>
              <span className="font-medium">Ask chatty fans for a little tip</span>
              <span className="block text-fg-dim text-xs">
                Every so many of his messages, and at least a few days apart, one ordinary reply
                mentions something real the creator is paying for today and asks him to cover
                it, right here in chat. Never on a selling turn, never with content attached.
                Needs <b>Only chat fans who have bought before</b> OFF to reach fans who never
                bought. Everything below is per account; clear a box to get the shipped value.
              </span>
            </span>
          </label>
          <div className="pl-6 grid grid-cols-1 md:grid-cols-2 gap-x-6 gap-y-1.5 text-xs text-fg-dim">
            <label className="flex items-center gap-2 flex-wrap">
              Suggested amount $
              <input type="number" min={1} max={500} className={LT_NUM}
                value={cfg.life_tip_ask_amount_dollars ?? ""}
                placeholder="creator picks"
                onChange={(e) => set({ life_tip_ask_amount_dollars: e.target.value === "" ? null : Math.max(1, Math.round(Number(e.target.value) || 0)) })} />
              <span>blank = the creator names one, from</span>
              <PairInput value={cfg.life_tip_ask_range_dollars} fallback={[10, 40]} min={1} max={500} prefix="$"
                onChange={(v) => set({ life_tip_ask_range_dollars: v })} />
            </label>
            <label className="flex items-center gap-2 flex-wrap">
              Show the creator
              <input type="number" min={1} max={12} className={LT_NUM}
                value={cfg.life_tip_ask_reasons_shown ?? 6}
                onChange={(e) => set({ life_tip_ask_reasons_shown: Math.max(1, Math.min(12, Math.round(Number(e.target.value) || 1))) })} />
              reasons per ask (one that fits the chat gets picked)
            </label>
            <label className="flex items-center gap-2 flex-wrap">
              Ask every
              <PairInput value={cfg.life_tip_ask_every} fallback={[15, 25]} min={1} max={500}
                onChange={(v) => set({ life_tip_ask_every: v })} />
              of his messages
            </label>
            <label className="flex items-center gap-2 flex-wrap">
              Once he has tipped since the last ask, every
              <PairInput value={cfg.life_tip_ask_every_tipped} fallback={[20, 30]} min={1} max={500}
                onChange={(v) => set({ life_tip_ask_every_tipped: v })} />
            </label>
            <label className="flex items-center gap-2 flex-wrap">
              At least
              <PairInput value={cfg.life_tip_ask_days} fallback={[3, 20]} min={1} max={365}
                onChange={(v) => set({ life_tip_ask_days: v })} />
              days apart (drawn so the middle is about a week)
            </label>
            <label className="flex items-center gap-2 flex-wrap">
              If the reply forgot to ask, retry after
              <PairInput value={cfg.life_tip_ask_retry} fallback={[3, 5]} min={1} max={500}
                onChange={(v) => set({ life_tip_ask_retry: v })} />
              of his messages
            </label>
          </div>
          <div className="pl-6 grid grid-cols-1 md:grid-cols-2 gap-3 text-xs text-fg-dim">
            <ReasonsBox label="Things the creator is paying for (female account)" value={cfg.life_tip_ask_reasons_her}
              onCommit={(v) => set({ life_tip_ask_reasons_her: v })} />
            <ReasonsBox label="Things he is paying for (male account)" value={cfg.life_tip_ask_reasons_him}
              onCommit={(v) => set({ life_tip_ask_reasons_him: v })} />
          </div>
          <label className="pl-6 flex items-center gap-2 text-xs text-fg-dim">
            <span className="shrink-0">Extra note to the creator</span>
            <input type="text" maxLength={400} className={`${INPUT} flex-1`}
              value={cfg.life_tip_ask_extra ?? ""}
              placeholder='e.g. "always blame the vet bill" — appended to the ask instructions on that turn'
              onChange={(e) => set({ life_tip_ask_extra: e.target.value })} />
          </label>

          {/* ── the BIG ask: the same turn, the rarer and larger version ── */}
          <div className="pl-6 pt-2 border-t border-border space-y-2">
            <label className="flex items-start gap-2 cursor-pointer">
              <input type="checkbox" className="mt-0.5" checked={!!cfg.life_tip_big_enabled}
                onChange={(e) => set({ life_tip_big_enabled: e.target.checked })} />
              <span>
                <span className="font-medium">Bigger ask — about once a month, only fans who have tipped</span>
                <span className="block text-fg-dim text-xs">
                  For a fan who has tipped at least once and is talking right now, one reply a
                  month is about something that <b>hit the creator today</b> — locked out and a new key,
                  a broken window, the car towed — for a real, bigger number the creator names.
                  Needs the switch above ON. Never within a few days of the little ask.
                </span>
              </span>
            </label>
            <div className="grid grid-cols-1 md:grid-cols-2 gap-x-6 gap-y-1.5 text-xs text-fg-dim">
              <label className="flex items-center gap-2 flex-wrap">
                The creator names one from
                <PairInput value={cfg.life_tip_big_range_dollars} fallback={[100, 300]} min={1} max={2000} prefix="$"
                  onChange={(v) => set({ life_tip_big_range_dollars: v })} />
              </label>
              <label className="flex items-center gap-2 flex-wrap">
                At least
                <PairInput value={cfg.life_tip_big_days} fallback={[25, 40]} min={1} max={365}
                  onChange={(v) => set({ life_tip_big_days: v })} />
                days apart (drawn so the middle is about a month)
              </label>
            </div>
            <div className="grid grid-cols-1 md:grid-cols-2 gap-3 text-xs text-fg-dim">
              <ReasonsBox label="What hit the creator today (female account)" value={cfg.life_tip_big_reasons_her}
                onCommit={(v) => set({ life_tip_big_reasons_her: v })} />
              <ReasonsBox label="What hit him today (male account)" value={cfg.life_tip_big_reasons_him}
                onCommit={(v) => set({ life_tip_big_reasons_him: v })} />
            </div>
            <label className="flex items-center gap-2 text-xs text-fg-dim">
              <span className="shrink-0">Extra note to the creator</span>
              <input type="text" maxLength={400} className={`${INPUT} flex-1`}
                value={cfg.life_tip_big_extra ?? ""}
                placeholder='e.g. "renting, so never a mortgage" — appended on the big-ask turn'
                onChange={(e) => set({ life_tip_big_extra: e.target.value })} />
            </label>
          </div>
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
            <select className={`${INPUT} w-full`} value={cfg.mode ?? "always"}
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
              value={cfg.resume_after_manual_hours ?? 1}
              onChange={(e) => set({ resume_after_manual_hours: parseInt(e.target.value || "0", 10) })} />
          </label>
        </div>

        {/* ── deepen phase: gen_info openers ── */}
        <div className="rounded-md border border-border bg-bg-elev-1 px-3 py-2.5 space-y-2 text-sm">
          <label className="flex items-center gap-2">
            <input type="checkbox" checked={cfg.profile_openers_enabled !== false}
              onChange={(e) => set({ profile_openers_enabled: e.target.checked })} />
            <span className="font-medium">Pick up what he told you</span>
            <span className="text-fg-dim text-xs">
              once there is nothing left to ask, work a real detail into the reply
            </span>
          </label>
          <div className={cfg.profile_openers_enabled !== false
            ? "space-y-1" : "space-y-1 opacity-50 pointer-events-none"}>
            <label className="space-y-1 block max-w-xs">
              <div className="text-fg-dim text-xs">
                How often a reply uses one (%)
              </div>
              <input type="number" className={`${INPUT} w-full`} min={0} max={100}
                value={Math.round((cfg.profile_openers_rate ?? 0.3) * 100)}
                onChange={(e) => set({
                  profile_openers_rate:
                    Math.min(100, Math.max(0, parseInt(e.target.value || "0", 10))) / 100,
                })} />
            </label>
            <div className="text-fg-dim text-xs">
              Not every turn wants a question. At 100% every reply to a fan the account has
              finished getting to know carries one — which reads as an interview,
              not a conversation. 30% is the house default.
            </div>
          </div>
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
                    value={cfg.msg_limits_by_signal?.buying_signal ?? 20}
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
                    value={cfg.msg_limits_by_signal?.pic_sent ?? 25}
                    onChange={(e) => setLimit("pic_sent", parseInt(e.target.value || "0", 10))} />
                </label>
              </div>
            </div>
            <div>
              <div className="text-fg-dim text-xs mb-1">
                Proven-spender floor — a fan who has PAID this much recently gets at
                least this cap (only ever raises it; 0 = off)
              </div>
              <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
                {SPEND_RULES.map(({ days, dollars: dflt, cap }) => (
                  <Fragment key={days}>
                    <label className="space-y-1">
                      <div className="text-fg-dim text-xs">Spent $ in last {days} days</div>
                      <input type="number" className={`${INPUT} w-full`} min={0}
                        value={Math.round((spendRule(days)?.min_cents ?? dflt * 100) / 100)}
                        onChange={(e) => setSpend(days, {
                          min_cents: parseInt(e.target.value || "0", 10) * 100 })} />
                    </label>
                    <label className="space-y-1">
                      <div className="text-fg-dim text-xs">→ cap</div>
                      <input type="number" className={`${INPUT} w-full`} min={0}
                        value={spendRule(days)?.cap ?? cap}
                        onChange={(e) => setSpend(days, {
                          cap: parseInt(e.target.value || "0", 10) })} />
                    </label>
                  </Fragment>
                ))}
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
                  value={cfg.post_purchase_minutes ?? 30}
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
                <b>Cadence</b> is a COUNT — how many replies go out in one conversation
                burst before backing off (a &quot;stop&quot; is a silent skip that reopens
                on a real buying signal or after a new-burst gap of silence).{" "}
                <b>Human Rhythm</b> (below) is TIMING — WHEN each reply lands, and whether
                a break happens at all.
              </p>
              <p>
                <span className="font-medium text-fg">The &quot;goes for a walk&quot; break</span>{" "}
                is Rhythm, and it is <em>per-fan</em> and <em>heat-driven</em>: a hot chat
                (he&apos;s replying fast, an open offer, a fresh buy) → replies come fast; a
                cold / boring / no-sales chat → replies stretch out and a real multi-hour
                break becomes possible. A thread is NOT cold right after a sale — a purchase
                makes it hotter (strike while hot); the break comes once the hot window
                winds down.
              </p>
            </div>
          </details>
        </div>

        {/* ── Human Rhythm ── */}
        <RhythmSection
          cfg={cfg}
          set={set}
          tz={tz}
          utcOffset={cfgQ.data?.utc_offset ?? 0}
          derived={cfgQ.data?.derived_sleep_window ?? ["02:00", "06:00"]}
          effective={cfgQ.data?.effective_sleep_window ?? ["02:00", "06:00"]}
          houseDefault={cfgQ.data?.default_sleep_window ?? ["02:00", "06:00"]}
        />

        {/* ── texting style ── */}
        <div className="flex flex-wrap items-center gap-x-4 gap-y-2 text-sm border-t border-border pt-2">
          <span className="text-xs text-fg-dim">Texting style:</span>
          <label className="flex items-center gap-1.5">
            <input type="checkbox" checked={style.humanStyle}
              onChange={(e) => style.setHumanStyle(e.target.checked)} />
            Human style (bubbles + humanizer)
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
          <label className="flex items-center gap-1.5"
            title="Sometimes detach the '?' from the word before it — “you like it ?”. Part of the non-native register, so it only applies while that is on; its own box because it is the one visible artifact you may want off while keeping the rest.">
            <input type="checkbox" checked={style.spacingOn} disabled={!style.nonnativeOn}
              onChange={(e) => style.setSpacingOn(e.target.checked)} />
            Space before ?
          </label>
          <label className="flex items-center gap-1.5"
            title="Some replies end with a cat reaction gif — occasionally the gif IS the reply. Hand-picked pack, capped in code to one per fan every few hours.">
            <input type="checkbox" checked={style.catStickers}
              onChange={(e) => style.setCatStickers(e.target.checked)} />
            Cat stickers 🐱
          </label>
          <label className="flex items-center gap-1.5"
            title="Before a reply sends, check it against what this account has already told THIS fan (the pinned facts + the drawer's 'What this fan was told') and fix a contradiction. Only fires on replies that actually say something about the creator — costs a second AI call on those.">
            <input type="checkbox" checked={style.consistencyOn}
              onChange={(e) => style.setConsistencyOn(e.target.checked)} />
            Never contradict what the creator already said
          </label>
          <Button size="sm" variant="ghost" disabled={style.saveStyleM.isPending}
            onClick={style.saveStyle}>
            Save style
          </Button>
          {style.saveStyleM.isSuccess && <span className="text-xs text-green-400">saved ✓</span>}
        </div>

        <div className="flex items-center gap-2 flex-wrap">
          <Button size="sm" disabled={saveCfgM.isPending || !configLoaded}
            onClick={() => saveCfg()}>
            <Save size={14} className="mr-1" /> Save config
          </Button>
          {saveCfgM.isSuccess && <span className="text-xs text-green-400">saved ✓</span>}
          {saveCfgM.isError && (
            <span className="text-xs text-red-500">
              {saveCfgM.error?.message || "Save failed"} — nothing was stored.
            </span>
          )}
          {!!cfg.enabled && (
            <span className="text-xs text-amber-400">
              live — replaces Auto-AI-chat for fans under the gate
            </span>
          )}
        </div>
      </Card>

      {/* ── content library — the seller works from this whether or not the Upseller
          is on. Tune HOW it sells (pricing, escalation) on the 💰 AI Upseller tab. ── */}
      <Card className="p-4 space-y-3">
        <div className="flex items-baseline gap-2">
          <h3 className="text-sm font-medium">Singles (standalone pieces)</h3>
          <span className="text-xs text-fg-dim">
            a flat pool — whatever fits what he asks for is picked. Grab prewritten ones from the 💰 AI Upseller tab.
          </span>
        </div>
        <ItemsTable items={singles} setItems={setSingles} accountId={accountId} />
        <div className="flex items-center gap-2 flex-wrap">
          <Button size="sm" variant="ghost"
            onClick={() => setSingles([...singles, { ...NEW_ITEM }])}>
            <Plus size={14} className="mr-1" /> Add single
          </Button>
          <Button size="sm" variant="secondary" disabled={suggestTextsM.isPending}
            title="Propose caption rows this vault can actually fill — text and price only, no media. Nothing is saved until you press Save singles."
            onClick={suggestTexts}>
            📝 {suggestTextsM.isPending ? "Thinking…" : "Suggest text sets"}
          </Button>
          <Button size="sm" variant="secondary" disabled={genLinesM.isPending}
            title="Write NEW caption lines from what the vision layer found in your vault — about the media you actually have and nothing is selling yet. Costs a small LLM call per media group."
            onClick={generateLines}>
            🪄 {genLinesM.isPending ? "Writing…" : "Write lines from vault"}
          </Button>
          <Button size="sm" variant="secondary" disabled={suggestM.isPending}
            title="Pick vault content for any single that has a caption but no media. Nothing is saved until you press Save singles."
            onClick={fillContent}>
            ✨ {suggestM.isPending ? "Finding…" : "Fill content"}
          </Button>
          <label className="flex items-center gap-1.5 text-xs text-fg-dim cursor-pointer"
            title="Off: no media is ever bound to two rows (a fan climbing the ladder never re-buys what he owns). On: rows may share media — use it when the catalog has outgrown the vault and rows are filling empty.">
            <input type="checkbox" checked={allowReuse}
              onChange={(e) => setAllowReuse(e.target.checked)} />
            ♻️ allow reuse
          </label>
          <Button size="sm" disabled={saveSinglesM.isPending}
            className="max-md:w-full max-md:order-first"
            onClick={() => saveSinglesM.mutate(singles)}>
            <Save size={14} className="mr-1" /> Save singles ({singles.length})
          </Button>
          {saveSinglesM.isSuccess && <span className="text-xs text-green-400">saved ✓</span>}
        </div>
        {suggestM.isError && (
          <div className="text-[11px] text-warn">Couldn&apos;t fill: {suggestM.error.message}</div>
        )}
        {genLinesM.isError && (
          <div className="text-[11px] text-warn">
            Couldn&apos;t write lines: {genLinesM.error.message}
          </div>
        )}
        {suggestTextsM.isError && (
          <div className="text-[11px] text-warn">
            Couldn&apos;t suggest: {suggestTextsM.error.message}
          </div>
        )}
        {fillNote && <div className="text-[11px] text-fg-dim">{fillNote}</div>}

        {/* ── suggested catalog items — the operator picks, nothing is auto-added ── */}
        {!!suggested.length && (
          <div className="rounded-md border border-border bg-bg-elev-1 p-3 space-y-2">
            <div className="flex items-baseline gap-2">
              <h4 className="text-sm font-medium">Suggested catalog items</h4>
              <span className="text-xs text-fg-dim">
                only captions this vault can actually fill. Ticked ones are added with
                <b> no content</b> — then press ✨ Fill content.
              </span>
              <div className="flex-1" />
              <button className="text-xs text-fg-dim hover:text-fg"
                onClick={() => setPicked(new Set(suggested.map((p) => p.label)))}>
                all
              </button>
              <button className="text-xs text-fg-dim hover:text-fg"
                onClick={() => setPicked(new Set())}>none</button>
            </div>

            <div className="space-y-1">
              {suggested.map((p) => (
                <label key={p.label}
                  className="flex items-start gap-2 text-sm cursor-pointer rounded px-1.5 py-1 hover:bg-bg-elev-2">
                  <input type="checkbox" className="mt-1" checked={picked.has(p.label)}
                    onChange={(e) => setPicked((prev) => {
                      const next = new Set(prev);
                      if (e.target.checked) next.add(p.label); else next.delete(p.label);
                      return next;
                    })} />
                  <span className="min-w-0 flex-1">
                    <span className="font-medium">{p.label}</span>
                    <span className="text-fg-dim"> · {p.kind}</span>
                    <span className="text-green-400"> {dollars(p.price_cents)}</span>
                    {p.floor_cents > 0 && (
                      <span className="text-fg-dim text-xs"> (floor {dollars(p.floor_cents)})</span>
                    )}
                    <span className="md:block text-xs text-fg-dim italic truncate max-md:whitespace-normal max-md:line-clamp-2">
                      “{p.description_for_ai}”
                    </span>
                    <span className="hidden max-md:flex flex-wrap items-center gap-1 pt-1">
                      {p.source && <Badge>{p.source}</Badge>}
                      <Badge>{p.fillable} to fill</Badge>
                    </span>
                  </span>
                  {p.source && <Badge className="max-md:hidden">{p.source}</Badge>}
                  <Badge className="max-md:hidden">{p.fillable} to fill</Badge>
                </label>
              ))}
            </div>

            <div className="flex items-center gap-2 pt-1">
              <Button size="sm" disabled={!picked.size} onClick={addPicked}>
                <Plus size={14} className="mr-1" /> Add selected ({picked.size})
              </Button>
              <Button size="sm" variant="ghost"
                onClick={() => { setSuggested([]); setPicked(new Set()); }}>
                Dismiss
              </Button>
            </div>
          </div>
        )}

        {/* Captions worth having that this vault has nothing behind — a shot
            list, deliberately NOT offered as rows that would sit empty. */}
        {!!shootList.length && (
          <div className="text-[11px] text-fg-dim">
            Nothing in the vault fits{" "}
            <b>{shootList.map((p) => p.label).join(", ")}</b> — worth shooting; left out
            rather than added as an empty promise.
          </div>
        )}
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
                ⚠ no sellable items reached the prompt (do the singles have media + a PPV price?)
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
                  : ` — ppv $${dollars(simulateM.data.offer.price_cents)}`}
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
        <div>
          <div className="text-xs text-fg-dim mb-1">Offers</div>
            {(sessionsQ.data?.offers ?? []).length === 0 && (
              <div className="text-xs text-fg-dim">none yet</div>
            )}
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
                  <button className="text-fg-dim hover:text-red-400"
                    title="cancel offer (hand to a human)"
                    onClick={() => cancelM.mutate(o.id)}>
                    <X size={13} />
                  </button>
                )}
              </div>
            ))}
        </div>
      </Card>

      {/* ── re-engage cold buyers — a personal 1:1 win-back the account runs, so it lives
          with the chatter (not the mass broadcasts). ── */}
      <div className="border-t border-border pt-4">
        <ReengageBuyersTab accountId={accountId} />
      </div>

      {/* ── Make It Right — the safety net that apologises + gifts free content
          when a fan got the wrong outcome (esp. a double-charge). ── */}
      <div className="border-t border-border pt-4">
        <MakeRightTab accountId={accountId} />
      </div>
    </div>
    </MediaCacheProvider>
  );
}
