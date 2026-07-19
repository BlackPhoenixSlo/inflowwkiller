"use client";

/**
 * AutoreplyTab — Automations → "Auto-reply" (keep-warm re-engagement).
 *
 * When ON, a quiet, low-spend KNOWN fan who hasn't replied for a few minutes
 * (after WE spoke last) gets ONE casual, never-PPV line to keep the convo alive —
 * tone-matched to the recent messages. It never sells, never re-asks known info,
 * and stops after `max_nudges` until the fan replies. Past the silence window it
 * falls through to the slower send_followup drip.
 *
 * Persists account_ai_config.autoreply_config_json via /admin/autoreply-config.
 * Default OFF. ⚠️ Only enable on accounts with real fans (never promo-spam ones).
 */

import { useEffect, useMemo, useState } from "react";
import { MessageCircle, Sparkles } from "lucide-react";

import { Button, Card } from "@/components/ui/primitives";
import { EditRawJsonButton } from "./JsonConfigModal";
import {
  useAutoreplyConfig,
  useSaveAutoreplyConfig,
  type AutoreplyConfig,
} from "@/hooks/useAutoreplyConfig";
import {
  useStyleConfig,
  useSaveStyleConfig,
  type StyleConfig,
} from "@/hooks/useStyleConfig";

/** The "human texting style" opt-in — one checkbox per automation. */
const STYLE_AUTOMATIONS: { key: keyof StyleConfig; label: string; hint: string }[] = [
  { key: "of_ai_chat", label: "Info-gather", hint: "the get-to-know-you reply loop" },
  { key: "autoreply", label: "Auto Convo", hint: "the keep-warm re-engagement above" },
  { key: "deep_convo", label: "Deep Convo", hint: "the deepening drill (also casualizes the Q & tease)" },
];

function StyleSection({ accountId }: { accountId: string | null }) {
  const cfgQ = useStyleConfig(accountId);
  const saveM = useSaveStyleConfig(accountId);

  const eff: StyleConfig = useMemo(() => {
    const d = cfgQ.data?.defaults ?? {};
    const c = cfgQ.data?.config ?? {};
    return { ...d, ...c };
  }, [cfgQ.data]);

  const [form, setForm] = useState<StyleConfig>({});
  useEffect(() => setForm(eff), [eff]);

  if (cfgQ.isLoading) return null;
  const set = (patch: Partial<StyleConfig>) => setForm((f) => ({ ...f, ...patch }));

  return (
    <Card className="p-4 space-y-4 max-w-2xl">
      <header className="flex items-center gap-2">
        <Sparkles size={16} className="text-accent" />
        <h3 className="text-sm font-medium">Human texting style</h3>
      </header>
      <p className="text-xs text-fg-dim leading-relaxed">
        Makes the AI text like a real person — short lowercase bursts (up to 3
        quick bubbles), no robotic em-dashes or echoing, a little bratty. Off by
        default; flip it on per automation. Turning it off restores the current
        behavior exactly. <strong>Realistic typos</strong> is an independent toggle:
        it slips in the occasional human thumb-typo (and sometimes a “*fix” bubble),
        protecting names, prices and links. <strong>Non-native</strong> makes her
        text like a non-native speaker — consistent signature misspellings (deterministic,
        always-on when ticked) plus slightly broken grammar; names, prices and links
        are never touched.
      </p>
      <label className="flex items-start gap-2.5 cursor-pointer rounded-lg border border-border bg-bg-elev-1 px-3 py-2.5">
        <input type="checkbox" className="h-4 w-4 mt-0.5 accent-[var(--accent)] cursor-pointer"
          checked={!!form.strip_emojis}
          onChange={(e) => set({ strip_emojis: e.target.checked })} />
        <span className="space-y-0.5">
          <span className="block text-sm">Strip all emojis</span>
          <span className="block text-[11px] text-fg-dim/70">
            Removes every emoji at send time — emoji placement is a dead LLM tell.
            Applies to all automated sends (chat, auto convo, follow-ups, welcome,
            mass funnels). Off by default.
          </span>
        </span>
      </label>
      <label className="flex items-start gap-2.5 cursor-pointer rounded-lg border border-border bg-bg-elev-1 px-3 py-2.5">
        <input type="checkbox" className="h-4 w-4 mt-0.5 accent-[var(--accent)] cursor-pointer"
          checked={form.factground_of_ai_chat ?? true}
          onChange={(e) => set({ factground_of_ai_chat: e.target.checked })} />
        <span className="space-y-0.5">
          <span className="block text-sm">Fact-grounding (Auto Convo)</span>
          <span className="block text-[11px] text-fg-dim/70">
            Feeds the info-gather reply his full gen_info profile — bio, notes and the
            team-written teases — plus a nudge to work in one specific detail, so a reply
            lands as “she remembers me” instead of generic. On by default; a fan with no
            profile yet is unaffected.
          </span>
        </span>
      </label>
      <div className="space-y-2">
        <div className="flex items-center gap-3 text-[11px] text-fg-dim/70 pl-0">
          <span className="w-28">Automation</span>
          <span className="w-20 text-center">Human style</span>
          <span className="w-20 text-center">Typos</span>
          <span className="w-20 text-center">Non-native</span>
        </div>
        {STYLE_AUTOMATIONS.map(({ key, label, hint }) => {
          const typoKey = `typos_${key}` as keyof StyleConfig;
          const nonnativeKey = `nonnative_${key}` as keyof StyleConfig;
          return (
            <div key={key} className="flex items-center gap-3">
              <span className="w-28 text-sm" title={hint}>{label}</span>
              <span className="w-20 flex justify-center">
                <input type="checkbox" className="h-4 w-4 accent-[var(--accent)] cursor-pointer"
                  checked={!!form[key]} onChange={(e) => set({ [key]: e.target.checked })} />
              </span>
              <span className="w-20 flex justify-center">
                <input type="checkbox" className="h-4 w-4 accent-[var(--accent)] cursor-pointer"
                  checked={!!form[typoKey]} onChange={(e) => set({ [typoKey]: e.target.checked })} />
              </span>
              <span className="w-20 flex justify-center">
                <input type="checkbox" className="h-4 w-4 accent-[var(--accent)] cursor-pointer"
                  checked={!!form[nonnativeKey]} onChange={(e) => set({ [nonnativeKey]: e.target.checked })} />
              </span>
              <span className="text-[11px] text-fg-dim/70 hidden sm:inline">{hint}</span>
            </div>
          );
        })}
      </div>
      <div className="flex items-center gap-3 pt-1">
        <Button onClick={() => saveM.mutate(form)} disabled={saveM.isPending}>
          {saveM.isPending ? "Saving…" : "Save"}
        </Button>
        {saveM.isSuccess && !saveM.isPending && (
          <span className="text-xs text-emerald-500">Saved ✓</span>
        )}
        {saveM.isError && (
          <span className="text-xs text-red-500">{saveM.error?.message || "Save failed"}</span>
        )}
        <div className="ml-auto">
          <EditRawJsonButton surface="style-config" accountId={accountId} />
        </div>
      </div>
    </Card>
  );
}

const INPUT = "w-24 bg-bg border border-border rounded-lg px-3 py-2 text-sm focus:outline-none focus:border-accent";

function NumField({
  label, hint, value, onChange, min = 0, max = 100000, step = 1, disabled, suffix,
}: {
  label: string; hint?: string; value: number; min?: number; max?: number;
  step?: number; disabled?: boolean; suffix?: string;
  onChange: (n: number) => void;
}) {
  return (
    <label className="space-y-1 block">
      <div className="text-xs text-fg-dim">{label}</div>
      <div className="flex items-center gap-2">
        <input
          type="number" min={min} max={max} step={step} className={INPUT}
          value={value} disabled={disabled}
          onChange={(e) => onChange(Math.max(min, Math.min(Number(e.target.value) || 0, max)))}
        />
        {suffix && <span className="text-xs text-fg-dim">{suffix}</span>}
      </div>
      {hint && <div className="text-[11px] text-fg-dim/70">{hint}</div>}
    </label>
  );
}

export default function AutoreplyTab({ accountId }: { accountId: string | null }) {
  const cfgQ = useAutoreplyConfig(accountId);
  const saveM = useSaveAutoreplyConfig(accountId);

  const eff: AutoreplyConfig = useMemo(() => {
    const d = cfgQ.data?.defaults ?? {};
    const c = cfgQ.data?.config ?? {};
    return { ...d, ...c };
  }, [cfgQ.data]);

  const [form, setForm] = useState<AutoreplyConfig>({});
  useEffect(() => setForm(eff), [eff]);

  if (!accountId) return <div className="text-sm text-fg-dim">Pick an account above.</div>;
  if (cfgQ.isLoading) return <div className="text-sm text-fg-dim">Loading…</div>;

  const enabled = !!form.enabled;
  const set = (patch: Partial<AutoreplyConfig>) => setForm((f) => ({ ...f, ...patch }));
  const cents = (dollars: number) => Math.round(dollars * 100);
  const dollars = (c?: number) => Math.round(((c ?? 0) / 100) * 100) / 100;

  return (
    <div className="space-y-5">
    <Card className="p-4 space-y-5 max-w-2xl">
      <header className="flex items-center gap-2">
        <MessageCircle size={16} className="text-accent" />
        <h3 className="text-sm font-medium">Auto Convo (keep the chat going)</h3>
      </header>

      <p className="text-xs text-fg-dim leading-relaxed">
        When a known, low-spend fan <em>messages and your team hasn't replied</em>
        within the window, the AI continues the chat — one casual, tone-matched
        reply, never a PPV, never re-asking what it knows. A fast human reply (or
        the AI chat) beats it to the punch, so it only covers a slow inbox.
      </p>

      <label className="flex items-center gap-3 cursor-pointer">
        <input type="checkbox" className="h-4 w-4 accent-[var(--accent)]"
          checked={enabled} onChange={(e) => set({ enabled: e.target.checked })} />
        <span className="text-sm">
          {enabled ? "Enabled" : "Disabled"}
        </span>
      </label>

      <fieldset disabled={!enabled} className="space-y-5" style={{ opacity: enabled ? 1 : 0.5 }}>
        <div>
          <div className="text-xs font-medium text-fg mb-2">Reply window (after he messages)</div>
          <div className="flex flex-wrap gap-4">
            <NumField label="Step in after (min)" hint="give the team this long first"
              value={form.silence_min_minutes ?? 30} min={1} max={1440}
              onChange={(n) => set({ silence_min_minutes: n })} suffix="min" />
            <NumField label="…but not after (min)" hint="past this it's too stale"
              value={form.silence_max_minutes ?? 180} min={2} max={10080}
              onChange={(n) => set({ silence_max_minutes: n })} suffix="min" />
          </div>
        </div>

        <div>
          <div className="text-xs font-medium text-fg mb-2">How persistent</div>
          <div className="flex flex-wrap gap-4">
            <NumField label="Max replies per message" hint="usually 1"
              value={form.max_nudges ?? 1} min={0} max={10}
              onChange={(n) => set({ max_nudges: n })} />
            <NumField label="Min gap between replies (min)"
              value={form.min_gap_minutes ?? 20} min={1} max={1440}
              onChange={(n) => set({ min_gap_minutes: n })} suffix="min" />
          </div>
        </div>

        <div>
          <div className="text-xs font-medium text-fg mb-2">Only low spenders</div>
          <div className="flex flex-wrap gap-4">
            <NumField label="Lifetime spend under ($)"
              value={form.max_lifetime_spend_cents != null ? dollars(form.max_lifetime_spend_cents) : 20} min={0} max={100000} step={0.01}
              onChange={(n) => set({ max_lifetime_spend_cents: cents(n) })} suffix="$" />
            <NumField label="Recent spend under ($)"
              value={form.max_recent_spend_cents != null ? dollars(form.max_recent_spend_cents) : 5} min={0} max={100000} step={0.01}
              onChange={(n) => set({ max_recent_spend_cents: cents(n) })} suffix="$" />
            <NumField label="…in the last (days)"
              value={form.recent_spend_days ?? 30} min={1} max={365}
              onChange={(n) => set({ recent_spend_days: n })} suffix="days" />
          </div>
        </div>

        <div>
          <div className="text-xs font-medium text-fg mb-2">Timing filters</div>
          <div className="flex flex-wrap gap-4">
            <NumField label="Days since last purchase ≥" hint="re-warm cooled fans (never bought = ok)"
              value={form.min_days_since_purchase ?? 7} min={0} max={3650}
              onChange={(n) => set({ min_days_since_purchase: n })} suffix="days" />
            <NumField label="Days since first chat ≥" hint="established fans only"
              value={form.min_days_since_first_chat ?? 2} min={0} max={3650}
              onChange={(n) => set({ min_days_since_first_chat: n })} suffix="days" />
            <NumField label="Read last N messages" hint="context for tone"
              value={form.last_n_messages ?? 16} min={2} max={60}
              onChange={(n) => set({ last_n_messages: n })} />
          </div>
        </div>

        <div>
          <div className="text-xs font-medium text-fg mb-2">Coverage</div>
          <label className="flex items-start gap-3 cursor-pointer">
            <input type="checkbox" className="h-4 w-4 mt-0.5 accent-[var(--accent)] cursor-pointer"
              checked={!!form.info_not_required}
              onChange={(e) => set({ info_not_required: e.target.checked })} />
            <span className="text-sm">
              Info not needed
              <span className="block text-[11px] text-fg-dim/70">
                Don’t wait for a complete profile — just respond from the last few
                messages and whatever’s already been gathered. Covers fans we don’t
                fully know yet (off = known fans only).
              </span>
            </span>
          </label>
        </div>
      </fieldset>

      <div className="flex items-center gap-3 pt-1">
        <Button onClick={() => saveM.mutate(form)} disabled={saveM.isPending}>
          {saveM.isPending ? "Saving…" : "Save"}
        </Button>
        {saveM.isSuccess && !saveM.isPending && (
          <span className="text-xs text-emerald-500">Saved ✓</span>
        )}
        {saveM.isError && (
          <span className="text-xs text-red-500">{saveM.error?.message || "Save failed"}</span>
        )}
        <div className="ml-auto">
          <EditRawJsonButton surface="autoreply-config" accountId={accountId} />
        </div>
      </div>
    </Card>
    <StyleSection accountId={accountId} />
    </div>
  );
}
