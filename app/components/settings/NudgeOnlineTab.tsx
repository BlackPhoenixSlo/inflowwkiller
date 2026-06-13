"use client";

/**
 * NudgeOnlineTab — Settings → "Nudge online" (also opened from +New).
 *
 * Configures the nudge_online automation ("message a fan when they come
 * online"). Two stores, one screen:
 *   • the 60s detector cadence is an `automation_rules` row (kind nudge_online).
 *   • the rich behaviour (content_mode, timing, caps, quiet hours, tease pools)
 *     is account_ai_config.nudge_config_json — both the detector and the delayed
 *     fire-job read it, so it's persisted server-side via useNudgeConfig.
 *
 * UX: every field has inline help; the tease pools have a friendly per-line
 * editor AND a raw-JSON advanced view (copy in/out); a live Preview composes a
 * sample message for a chosen fan + time; and a "Roll out to models" checklist
 * applies one shared text-only config across many models at once.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { Radio, Play, Rocket, Eye, ChevronDown, Plus, X, Image as ImageIcon } from "lucide-react";

import { Button, Card, Input } from "@/components/ui/primitives";
import { VaultPicker } from "@/components/chat/VaultPicker";
import { proxyImage, type VaultMedia } from "@/lib/relay";
import { useActiveAccounts } from "@/hooks/useAccounts";
import {
  useAutomationRules,
  useCreateRule,
  useUpdateRule,
  useRunRuleNow,
} from "@/hooks/useAutomations";
import {
  useNudgeConfig,
  useSaveNudgeConfig,
  useBulkApplyNudgeConfig,
  useNudgePreview,
  type NudgeConfig,
  type NudgeSlots,
  type NudgeSlotEntry,
} from "@/hooks/useNudgeConfig";

const SELECT_CLS =
  "w-full bg-bg border border-border rounded-lg px-3 py-2 text-sm focus:outline-none focus:border-accent";

const DETECT_EVERY_S = 60;

// The 6 time-of-day slots (same buckets send_welcome uses), with friendly labels.
const SLOT_DEFS: [string, string][] = [
  ["morning_1", "Morning · early (5–9)"],
  ["morning_2", "Morning · late (9–12)"],
  ["afternoon_1", "Afternoon · early (12–15)"],
  ["afternoon_2", "Afternoon · late (15–18)"],
  ["evening", "Evening (18–21)"],
  ["night", "Night (21–5)"],
];

// Quick time presets for the preview (representative hour per slot).
const TIME_PRESETS: [string, number][] = [
  ["Morning", 8], ["Midday", 13], ["Afternoon", 16], ["Evening", 19], ["Night", 23],
];

export default function NudgeOnlineTab() {
  const accounts = useActiveAccounts();
  const [accountId, setAccountId] = useState<string | null>(null);
  useEffect(() => {
    if (!accountId && accounts.length > 0) setAccountId(accounts[0].id);
  }, [accounts, accountId]);

  const rulesQ = useAutomationRules(accountId);
  const rule = useMemo(
    () => (rulesQ.data ?? []).find((r) => r.kind === "nudge_online") ?? null,
    [rulesQ.data],
  );
  const cfgQ = useNudgeConfig(accountId);

  const createM = useCreateRule(accountId);
  const updateM = useUpdateRule(accountId);
  const runM = useRunRuleNow(accountId);
  const saveCfgM = useSaveNudgeConfig(accountId);
  const bulkM = useBulkApplyNudgeConfig();
  const previewM = useNudgePreview();
  const busy = createM.isPending || updateM.isPending || saveCfgM.isPending;

  const eff: NudgeConfig = useMemo(() => {
    const d = cfgQ.data?.defaults ?? {};
    const c = cfgQ.data?.config ?? {};
    return { ...d, ...c };
  }, [cfgQ.data]);

  const [form, setForm] = useState<NudgeConfig>({});
  const [slots, setSlots] = useState<NudgeSlots>({});
  const [jsonDraft, setJsonDraft] = useState("");
  const [jsonErr, setJsonErr] = useState<string | null>(null);
  const [advOpen, setAdvOpen] = useState(false);
  const [helpOpen, setHelpOpen] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);

  // Roll-out checklist (every model checked by default, jaka included).
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [bulkMsg, setBulkMsg] = useState<string | null>(null);
  const initRef = useRef(false);
  useEffect(() => {
    if (!initRef.current && accounts.length > 0) {
      setSelected(new Set(accounts.map((a) => a.id)));
      initRef.current = true;
    }
  }, [accounts]);
  const toggleModel = (id: string) =>
    setSelected((prev) => {
      const n = new Set(prev);
      n.has(id) ? n.delete(id) : n.add(id);
      return n;
    });

  // Preview controls.
  const [previewFan, setPreviewFan] = useState("");
  const [previewHour, setPreviewHour] = useState<number | "">("");

  // Image picking: which slot's picker is open + a cache of picked media so we
  // can render thumbnails by id (in the slot editor AND the preview).
  const [pickerSlot, setPickerSlot] = useState<string | null>(null);
  const [mediaCache, setMediaCache] = useState<Record<number, VaultMedia>>({});

  // Load effective config into the form/slots when it arrives.
  useEffect(() => {
    if (!cfgQ.data) return;
    const { slots: s, ...rest } = eff;
    setForm(rest);
    const so = (s ?? {}) as NudgeSlots;
    setSlots(so);
    setJsonDraft(JSON.stringify(so, null, 2));
  }, [cfgQ.data, accountId]); // eslint-disable-line react-hooks/exhaustive-deps

  const set = <K extends keyof NudgeConfig>(k: K, v: NudgeConfig[K]) =>
    setForm((f) => ({ ...f, [k]: v }));
  const num = (k: keyof NudgeConfig, fallback = 0): number => {
    const v = form[k];
    return typeof v === "number" ? v : fallback;
  };

  // Friendly slot edits keep the JSON view in sync (single source = `slots`).
  const applySlots = (next: NudgeSlots) => {
    setSlots(next);
    setJsonDraft(JSON.stringify(next, null, 2));
    setJsonErr(null);
  };
  const setDefaultSlot = (key: string, entry: NudgeSlotEntry) => {
    const def = { ...(slots.default ?? {}) };
    def[key] = entry;
    applySlots({ ...slots, default: def });
  };
  const applyJson = () => {
    try {
      const parsed = JSON.parse(jsonDraft || "{}");
      if (typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("must be an object");
      applySlots(parsed as NudgeSlots);
    } catch (e) {
      setJsonErr(`Invalid JSON: ${(e as Error).message}`);
    }
  };

  function buildConfig(): NudgeConfig {
    return { ...form, slots };
  }

  // Bulk "Roll out to models" applies the in-memory config to OTHER accounts, so
  // require the active account's edits to be saved (and valid) first — otherwise
  // we'd push unsaved/partial changes the operator hasn't committed here.
  const dirty = useMemo(() => {
    if (!cfgQ.data) return false;
    return JSON.stringify(buildConfig()) !== JSON.stringify(eff);
  }, [form, slots, eff, cfgQ.data]); // eslint-disable-line react-hooks/exhaustive-deps

  async function save() {
    setErr(null); setMsg(null);
    if (!accountId) return;
    try {
      const config = buildConfig();
      await saveCfgM.mutateAsync(config);
      const enabled = config.enabled !== false;
      // The rich config (incl. image toggles) lives in nudge_config_json; the rule
      // only carries the 60s cadence, so its payload stays empty.
      if (rule) {
        await updateM.mutateAsync({
          id: rule.id, every_seconds: DETECT_EVERY_S, payload: {}, is_enabled: enabled,
        });
      } else {
        await createM.mutateAsync({
          account_id: accountId, kind: "nudge_online", name: "Nudge online",
          every_seconds: DETECT_EVERY_S, payload: {}, is_enabled: enabled,
        });
      }
      setMsg("✓ Saved.");
    } catch (e) {
      setErr((e as Error)?.message || "Save failed");
    }
  }

  async function runPreview() {
    if (!accountId) return;
    setErr(null);
    try {
      await previewM.mutateAsync({
        account_id: accountId, config: buildConfig(),
        fan_id: previewFan.trim() ? Number(previewFan.trim()) : null,
        hour: previewHour === "" ? null : Number(previewHour),
      });
    } catch (e) {
      setErr(`Preview failed: ${(e as Error)?.message || "unknown"}`);
    }
  }

  async function applyToModels() {
    setBulkMsg(null);
    if (dirty) { setBulkMsg("Save your changes first, then roll out."); return; }
    const ids = [...selected];
    if (ids.length === 0) { setBulkMsg("Pick at least one model."); return; }
    try {
      const r = await bulkM.mutateAsync({ account_ids: ids, config: buildConfig(), enable: true });
      setBulkMsg(`✓ Enabled nudge online (text-only) on ${r.count} model${r.count === 1 ? "" : "s"}.`);
    } catch (e) {
      setBulkMsg(`Failed: ${(e as Error)?.message || "unknown error"}`);
    }
  }

  async function runNow() {
    if (!rule) return;
    setMsg(null);
    try {
      await runM.mutateAsync(rule.id);
      setMsg("✓ Ran one detector pass — nudges fire after their delay.");
    } catch (e) {
      setMsg(`Run failed: ${(e as Error)?.message || "unknown error"}`);
    }
  }

  const last = rule?.last_run;

  return (
    <div className="space-y-5 max-w-2xl">
      <header className="flex items-center gap-2">
        <Radio className="size-5 text-accent" />
        <div>
          <h2 className="text-lg font-semibold">Nudge online</h2>
          <p className="text-sm text-fg-dim">
            Message a fan a few minutes after they come online — a personalized,
            time-of-day tease (or a question from their profile). Runs in the
            background; it never pauses AI chat.
          </p>
        </div>
      </header>

      {accounts.length > 1 && (
        <Field label="Account">
          <select className={SELECT_CLS} value={accountId ?? ""}
            onChange={(e) => setAccountId(e.target.value)}>
            {accounts.map((a) => (
              <option key={a.id} value={a.id}>{a.nickname || a.id}</option>
            ))}
          </select>
        </Field>
      )}

      {cfgQ.isLoading ? (
        <div className="text-sm text-fg-dim">Loading…</div>
      ) : (
        <Card className="p-4 space-y-5">
          {/* Enable */}
          <div>
            <label className="flex items-center gap-2 text-sm cursor-pointer">
              <input type="checkbox" checked={form.enabled !== false}
                onChange={(e) => set("enabled", e.target.checked)} />
              Enabled
            </label>
          </div>

          {/* What gets sent — up to TWO messages, each independently toggled. */}
          <div className="space-y-3">
            <div className="text-sm font-semibold">What gets sent</div>
            <Hint>
              A nudge can send up to two messages in order. Pick which to send and where the
              image goes. Default: a greeting <b>with image</b>, then a profile line (Q&amp;A or
              tease) as text.
            </Hint>

            {/* Part 1 — nudge line */}
            <div className="border border-border rounded-lg p-3 space-y-2">
              <label className="flex items-center gap-2 text-sm font-medium cursor-pointer">
                <input type="checkbox" checked={form.send_nudge_line !== false}
                  onChange={(e) => set("send_nudge_line", e.target.checked)} />
                1 · Nudge line (time-of-day greeting)
              </label>
              <div className="flex flex-wrap gap-4 pl-6">
                <label className="flex items-center gap-2 text-sm cursor-pointer">
                  <input type="checkbox" checked={form.nudge_line_text !== false}
                    disabled={form.send_nudge_line === false}
                    onChange={(e) => set("nudge_line_text", e.target.checked)} />
                  Include text (off = image only)
                </label>
                <label className="flex items-center gap-2 text-sm cursor-pointer">
                  <input type="checkbox" checked={form.nudge_line_image !== false}
                    disabled={form.send_nudge_line === false}
                    onChange={(e) => set("nudge_line_image", e.target.checked)} />
                  Attach image
                </label>
              </div>
            </div>

            {/* Part 2 — info line */}
            <div className="border border-border rounded-lg p-3 space-y-2">
              <label className="flex items-center gap-2 text-sm font-medium cursor-pointer">
                <input type="checkbox" checked={form.send_info_line !== false}
                  onChange={(e) => set("send_info_line", e.target.checked)} />
                2 · Info line (from the fan’s profile)
              </label>
              <div className="flex flex-wrap items-center gap-4 pl-6">
                <label className="flex items-center gap-2 text-sm">
                  Type:
                  <select className="bg-bg border border-border rounded px-2 py-1 text-sm"
                    value={form.info_line_mode ?? "mix"}
                    disabled={form.send_info_line === false}
                    onChange={(e) => set("info_line_mode", e.target.value as "qa" | "tease" | "mix")}>
                    <option value="mix">Q&amp;A or tease (mix)</option>
                    <option value="qa">Q&amp;A only</option>
                    <option value="tease">Tease only</option>
                  </select>
                </label>
                <label className="flex items-center gap-2 text-sm cursor-pointer">
                  <input type="checkbox" checked={form.info_line_image === true}
                    disabled={form.send_info_line === false}
                    onChange={(e) => set("info_line_image", e.target.checked)} />
                  Attach image
                </label>
              </div>
              <Hint>
                <b>Q&amp;A</b> asks about a profile fact (pet, hobby, city…), never repeating one.
                <b> Tease</b> is a second flirty line. <b>Mix</b> uses Q&amp;A when there’s a fact,
                else a tease.
              </Hint>
            </div>

            <div className="max-w-xs">
              <Field label="On re-engagement">
                <select className={SELECT_CLS} value={form.repeat_mode ?? "new"}
                  onChange={(e) => set("repeat_mode", e.target.value as "new" | "same")}>
                  <option value="new">New line each time</option>
                  <option value="same">Same line again</option>
                </select>
              </Field>
              <Hint>What a returning fan gets the next time they re-qualify.</Hint>
            </div>
          </div>

          {/* Timing */}
          <div>
            <div className="grid grid-cols-3 gap-4">
              <NumField label="Delay (min)" value={num("delay_minutes", 4)}
                onChange={(v) => set("delay_minutes", v)} min={0} />
              <NumField label="Jitter (min)" value={num("jitter_minutes", 3)}
                onChange={(v) => set("jitter_minutes", v)} min={0} />
              <NumField label="Online gap (min)" value={num("gap_minutes", 5)}
                onChange={(v) => set("gap_minutes", v)} min={1} />
            </div>
            <Hint>
              <b>Delay</b>: wait this long after a fan comes online before messaging (so it
              doesn’t look instant/bot-like). <b>Jitter</b>: add a random 0–N min on top so
              sends aren’t all at the exact same offset. <b>Online gap</b>: how long a fan
              must have been <i>offline</i> before coming back counts as “newly online” again
              (stops flapping from re-triggering).
            </Hint>
          </div>

          {/* Caps */}
          <div>
            <div className="grid grid-cols-3 gap-4">
              <NumField label="Min hours between" value={num("min_hours_between_nudges", 12)}
                onChange={(v) => set("min_hours_between_nudges", v)} min={0} />
              <NumField label="Max per tick" value={num("max_per_tick", 25)}
                onChange={(v) => set("max_per_tick", v)} min={1} />
              <NumField label="Online recent (min)" value={num("online_recent_minutes", 5)}
                onChange={(v) => set("online_recent_minutes", v)} min={1} />
            </div>
            <Hint>
              <b>Min hours between</b>: never nudge the same fan more than once per this many
              hours (re-engagement cap). <b>Max per tick</b>: most fans to start per 60s scan —
              extras roll to the next tick (never dropped). <b>Online recent</b>: at send time
              the fan must have been seen online within this many minutes, else the nudge is
              skipped (they left during the delay).
            </Hint>
          </div>

          {/* Spend targeting (optional) */}
          <div>
            <div className="grid grid-cols-2 gap-4">
              <DollarField label="Min lifetime spend ($)" cents={num("min_lifetime_spend_cents", 0)}
                onChange={(c) => set("min_lifetime_spend_cents", c)} />
              <DollarField label="Max lifetime spend ($)" cents={num("max_lifetime_spend_cents", 0)}
                onChange={(c) => set("max_lifetime_spend_cents", c)} />
            </div>
            <div className="grid grid-cols-3 gap-4 mt-3">
              <NumField label="Recent window (days)" value={num("recent_spend_days", 30)}
                onChange={(v) => set("recent_spend_days", v)} min={1} />
              <DollarField label="Min recent spend ($)" cents={num("min_recent_spend_cents", 0)}
                onChange={(c) => set("min_recent_spend_cents", c)} />
              <DollarField label="Max recent spend ($)" cents={num("max_recent_spend_cents", 0)}
                onChange={(c) => set("max_recent_spend_cents", c)} />
            </div>
            <Hint>
              Only nudge fans in a spend tier (<b>0 = off</b>). <b>Lifetime</b> uses total spend;{" "}
              <b>recent</b> sums the last <i>N days</i> of transactions. Set a <b>max</b> to target
              low / keep-warm fans, a <b>min</b> to target spenders, or both for a band.
            </Hint>
          </div>

          {/* Quiet hours */}
          <div>
            <Field label="Quiet hours (creator-local) — start / end">
              <div className="flex items-center gap-2 text-sm">
                <Input type="number" min={0} max={23} className="w-20"
                  value={form.quiet_hours?.[0] ?? 0}
                  onChange={(e) => set("quiet_hours", [Number(e.target.value), form.quiet_hours?.[1] ?? 0])} />
                <span>to</span>
                <Input type="number" min={0} max={23} className="w-20"
                  value={form.quiet_hours?.[1] ?? 0}
                  onChange={(e) => set("quiet_hours", [form.quiet_hours?.[0] ?? 0, Number(e.target.value)])} />
                <span className="text-muted">(equal = off)</span>
              </div>
            </Field>
            <Hint>No nudges during this window, in the creator’s local time. Set both equal to disable.</Hint>
          </div>

          {/* ── Preview ─────────────────────────────────────────────── */}
          <div className="border border-border rounded-lg p-3 space-y-3 bg-bg/40">
            <div className="flex items-center gap-1.5 text-sm font-semibold">
              <Eye className="size-4 text-accent" /> Preview
            </div>
            <div className="flex flex-wrap items-end gap-2">
              <label className="block">
                <span className="text-xs text-fg-dim block mb-1">Fan id (blank = sample)</span>
                <Input className="w-40" placeholder="e.g. 123456789"
                  value={previewFan} onChange={(e) => setPreviewFan(e.target.value)} />
              </label>
              <label className="block">
                <span className="text-xs text-fg-dim block mb-1">Hour (blank = now)</span>
                <Input type="number" min={0} max={23} className="w-24"
                  value={previewHour} onChange={(e) =>
                    setPreviewHour(e.target.value === "" ? "" : Number(e.target.value))} />
              </label>
              <div className="flex flex-wrap gap-1">
                {TIME_PRESETS.map(([lbl, h]) => (
                  <button key={lbl} type="button"
                    onClick={() => setPreviewHour(h)}
                    className="text-xs px-2 py-1 rounded border border-border hover:border-accent">
                    {lbl}
                  </button>
                ))}
              </div>
              <Button onClick={runPreview} disabled={previewM.isPending || !accountId}>
                {previewM.isPending ? "…" : "Preview"}
              </Button>
            </div>
            {previewM.data && (
              <div className="space-y-2">
                {previewM.data.messages.length === 0 ? (
                  <div className="text-sm text-fg-dim italic">
                    Nothing would be sent — enable a part above (and give it text or an image).
                  </div>
                ) : (
                  previewM.data.messages.map((m, i) => (
                    <PreviewBox
                      key={i}
                      label={m.kind === "nudge" ? `Message ${i + 1} · Nudge line` : `Message ${i + 1} · Info line`}
                      one={{ text: m.text, slot: previewM.data!.slot, media: m.media }}
                      accountId={accountId}
                      mediaCache={mediaCache}
                    />
                  ))
                )}
                <p className="text-[11px] text-muted">
                  Resolved name: <b>{previewM.data.name}</b> — this is what{" "}
                  <code>{"{name}"}</code> becomes (their first name / nickname, not the full
                  “name/age/city” string).
                </p>
              </div>
            )}
          </div>

          {/* ── Tease pools: friendly editor ────────────────────────── */}
          <div className="space-y-3">
            <div>
              <span className="text-sm font-semibold">Message lines (default times)</span>
              <Hint>
                Lines for each time of day. <code>{"{name}"}</code>, <code>{"{city}"}</code>,{" "}
                <code>{"{age}"}</code>, <code>{"{job}"}</code>, <code>{"{hobby}"}</code>,{" "}
                <code>{"{pet}"}</code> are filled from the fan’s profile (empty ones vanish).
                Weekend/Friday overrides live in the Advanced JSON below.
              </Hint>
            </div>
            {SLOT_DEFS.map(([key, label]) => (
              <SlotEditor
                key={key}
                label={label}
                entry={(slots.default?.[key] as NudgeSlotEntry) ?? { text: [], image: [] }}
                onChange={(entry) => setDefaultSlot(key, entry)}
                accountId={accountId}
                mediaCache={mediaCache}
                onPickImages={() => setPickerSlot(key)}
              />
            ))}
          </div>

          {/* Advanced JSON (copy in/out, weekend/friday, full control) */}
          <Collapsible open={advOpen} onToggle={() => setAdvOpen((v) => !v)}
            title="Advanced — edit pools as JSON (copy in / out)">
            <div className="space-y-2">
              <textarea
                className="w-full bg-bg border border-border rounded-lg px-3 py-2 text-xs font-mono h-56 focus:outline-none focus:border-accent"
                value={jsonDraft} onChange={(e) => setJsonDraft(e.target.value)} spellCheck={false} />
              <div className="flex items-center gap-2">
                <Button variant="ghost" onClick={applyJson}>Apply JSON</Button>
                <Button variant="ghost"
                  onClick={() => navigator.clipboard?.writeText(jsonDraft)}>Copy</Button>
                {jsonErr && <span className="text-sm text-err">{jsonErr}</span>}
              </div>
              <Hint>
                Shape: day-bucket → slot → <code>{`{ text: [...], image: [...] }`}</code>.
                Buckets: <code>default</code>, <code>weekend</code>, <code>weekday</code>, or a
                weekday name like <code>friday</code> (a per-day bucket wins over weekend/weekday,
                which win over default). “Apply JSON” pulls your edits back into the friendly
                editor above.
              </Hint>
            </div>
          </Collapsible>

          {err && <div className="text-sm text-err">{err}</div>}

          <div className="flex items-center gap-2 pt-1">
            <Button onClick={save} disabled={busy || !accountId}>
              {rule ? "Save changes" : "Create automation"}
            </Button>
            {rule && (
              <Button variant="ghost" onClick={runNow} disabled={runM.isPending}
                title="Run one detector pass now (bypasses the 60s timer)">
                <Play className="size-4" /> {runM.isPending ? "Running…" : "Run now"}
              </Button>
            )}
          </div>

          {msg && <div className="text-sm text-accent border-t border-border pt-3">{msg}</div>}

          {rule && last && (
            <div className="text-xs text-fg-dim border-t border-border pt-3">
              Last run: <span className="text-fg">{last.status}</span>
              {typeof last.stats?.enqueued === "number" && (
                <span className="text-fg"> · {last.stats.enqueued as number} nudges queued</span>
              )}
              {typeof last.stats?.online === "number" && (
                <span> · {last.stats.online as number} online</span>
              )}
              {last.stats?.warmup === true && <span> · warm-up (no sends)</span>}
              {last.started_at && ` · ${new Date(last.started_at).toLocaleString()}`}
              {rule.has_pending_job && " · job queued"}
              {last.error_text && <span className="text-err"> · {last.error_text}</span>}
            </div>
          )}
        </Card>
      )}

      {/* Roll out to all models at once (text-only — no vault images needed) */}
      {accounts.length > 1 && (
        <Card className="p-4 space-y-3">
          <div className="flex items-start justify-between gap-2">
            <div>
              <h3 className="text-sm font-semibold flex items-center gap-1.5">
                <Rocket className="size-4 text-accent" /> Roll out to models
              </h3>
              <p className="text-xs text-fg-dim mt-0.5">
                Apply the config above to every checked model and turn it on live.
                Text-only (no images), so it behaves identically across accounts.
              </p>
            </div>
            <div className="flex gap-2 text-xs shrink-0">
              <button className="text-accent hover:underline"
                onClick={() => setSelected(new Set(accounts.map((a) => a.id)))}>All</button>
              <button className="text-fg-dim hover:underline"
                onClick={() => setSelected(new Set())}>None</button>
            </div>
          </div>
          <div className="grid grid-cols-2 gap-x-4 gap-y-0.5 max-h-56 overflow-auto border border-border rounded-lg p-2">
            {accounts.map((a) => (
              <label key={a.id} className="flex items-center gap-2 text-sm py-0.5 cursor-pointer">
                <input type="checkbox" checked={selected.has(a.id)} onChange={() => toggleModel(a.id)} />
                <span className="truncate">{a.nickname || a.id}</span>
              </label>
            ))}
          </div>
          <div className="flex items-center gap-2">
            <Button onClick={applyToModels} disabled={bulkM.isPending || selected.size === 0 || dirty}
              title={dirty ? "Save your changes first" : undefined}>
              <Rocket className="size-4" />
              {bulkM.isPending ? "Applying…" : `Apply + enable on ${selected.size} model${selected.size === 1 ? "" : "s"}`}
            </Button>
            <span className="text-xs text-muted">
              {dirty ? "save your changes first to roll out" : "enables the 60s rule live on each"}
            </span>
          </div>
          {bulkMsg && <div className="text-sm text-accent border-t border-border pt-3">{bulkMsg}</div>}
        </Card>
      )}

      {/* How it works — long explanation */}
      <Collapsible open={helpOpen} onToggle={() => setHelpOpen((v) => !v)}
        title="How nudge online works (full explanation)">
        <div className="text-sm text-fg-dim space-y-2 leading-relaxed">
          <p>
            Every ~60 seconds the automation asks OnlyFans who is online right now. A fan who
            just came online (after being offline at least the <b>online gap</b>) is queued for
            a nudge <b>delay + jitter</b> minutes later. Just before sending it re-checks: still
            online recently (<b>online recent</b>), no live conversation, not nudged within the
            last <b>min hours between</b>, and outside <b>quiet hours</b>. If all good, it sends.
          </p>
          <p>
            <b>What gets sent.</b> Up to two messages, in order. <i>1 · Nudge line</i> — a
            time-of-day greeting from the slot pools (turn its text off for an image-only opener).
            <i> 2 · Info line</i> — a second line from the fan’s profile: <i>Q&amp;A</i> (asks
            about a pet/hobby/city/job/age, never repeating one), <i>Tease</i> (another flirty
            line), or <i>Mix</i> (Q&amp;A when there’s a fact, else tease). Turn either part off.
            Placeholders like <code>{"{name}"}</code> fill from the fan — <code>{"{name}"}</code>{" "}
            is their first name or nickname only (never the full “name/age/city” label).
          </p>
          <p>
            <b>Images.</b> Each part has its own “Attach image” toggle (default: image on the
            nudge line, none on the info line). If a slot lists several images, <b>one</b> is sent
            (rotated), not all. The bulk “Roll out to models” is always text-only so it works
            identically on every account without per-account vault ids.
          </p>
          <p>
            <b>Fully async.</b> Nudges never pause AI chat — if a chat automation already holds a
            fan, the nudge yields. It only sends to fans you’ve welcomed and who aren’t mid-thread,
            so it reads as a natural “hey, saw you’re around” rather than a bot blast.
          </p>
        </div>
      </Collapsible>

      {/* Vault image picker for a slot (opened from a slot's "Add image"). */}
      {pickerSlot && accountId && (
        <VaultPicker
          open
          onClose={() => setPickerSlot(null)}
          accountId={accountId}
          fanId={null}
          initialSelectedIds={(slots.default?.[pickerSlot]?.image as number[]) ?? []}
          onConfirm={(picked) => {
            setMediaCache((prev) => {
              const next = { ...prev };
              picked.forEach((m) => { next[m.id] = m; });
              return next;
            });
            const cur = slots.default?.[pickerSlot] ?? { text: [], image: [] };
            setDefaultSlot(pickerSlot, { ...cur, image: picked.map((m) => m.id) });
            setPickerSlot(null);
          }}
        />
      )}
    </div>
  );
}

// ── small bits ────────────────────────────────────────────────────────

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block">
      <span className="text-xs font-medium text-fg-dim block mb-1">{label}</span>
      {children}
    </label>
  );
}

function NumField({
  label, value, onChange, min,
}: { label: string; value: number; onChange: (v: number) => void; min?: number }) {
  return (
    <Field label={label}>
      <Input type="number" min={min} value={value}
        onChange={(e) => onChange(Number(e.target.value))} />
    </Field>
  );
}

/** Dollar input that stores CENTS; 0/empty saves `null` (= off), so a bound of
 *  0 never accidentally means "skip every paying fan". */
function DollarField({
  label, cents, onChange,
}: { label: string; cents: number; onChange: (cents: number | null) => void }) {
  const dollars = cents > 0 ? cents / 100 : 0;
  return (
    <Field label={label}>
      <Input type="number" min={0} step="0.01" placeholder="off"
        value={dollars || ""}
        onChange={(e) => {
          const d = Number(e.target.value);
          onChange(d > 0 ? Math.round(d * 100) : null);
        }} />
    </Field>
  );
}

function Hint({ children }: { children: React.ReactNode }) {
  return <p className="text-[11px] text-muted leading-snug mt-1">{children}</p>;
}

function thumbSrc(media: VaultMedia | undefined, accountId: string | null): string {
  const raw = media?.files?.thumb?.url || media?.files?.squarePreview?.url
    || media?.files?.preview?.url || "";
  return raw ? proxyImage(raw, accountId) : "";
}

function MediaThumb({
  id, media, accountId, onRemove,
}: { id: number; media?: VaultMedia; accountId: string | null; onRemove?: () => void }) {
  const url = thumbSrc(media, accountId);
  return (
    <div className="relative w-12 h-12 rounded overflow-hidden border border-border bg-bg shrink-0">
      {url
        ? <img src={url} alt="" loading="lazy" className="w-full h-full object-cover" />
        : <span className="w-full h-full grid place-items-center text-[9px] text-fg-dim">#{id}</span>}
      {onRemove && (
        <button type="button" onClick={onRemove} title="Remove"
          className="absolute -top-1 -right-1 bg-panel border border-border rounded-full size-4 grid place-items-center text-[10px] leading-none hover:text-err">×</button>
      )}
    </div>
  );
}

function PreviewBox({
  label, one, accountId, mediaCache,
}: {
  label: string;
  one: { text: string; slot: string; media: number[] };
  accountId: string | null;
  mediaCache: Record<number, VaultMedia>;
}) {
  return (
    <div className="rounded-lg border border-border p-2 text-sm bg-panel">
      <div className="flex items-center justify-between text-[11px] text-fg-dim mb-1">
        <span className="font-medium text-fg">{label}</span>
        <span>slot: {one.slot}</span>
      </div>
      {one.text
        ? <div>{one.text}</div>
        : <div className="text-fg-dim italic">(image only — no text)</div>}
      {one.media.length > 0 && (
        <div className="flex flex-wrap gap-1.5 mt-1.5">
          {one.media.map((id) => (
            <MediaThumb key={id} id={id} media={mediaCache[id]} accountId={accountId} />
          ))}
        </div>
      )}
    </div>
  );
}

/** One time-slot's editable list of message lines + a vault image picker. */
function SlotEditor({
  label, entry, onChange, accountId, mediaCache, onPickImages,
}: {
  label: string;
  entry: NudgeSlotEntry;
  onChange: (e: NudgeSlotEntry) => void;
  accountId: string | null;
  mediaCache: Record<number, VaultMedia>;
  onPickImages: () => void;
}) {
  const texts = entry.text ?? [];
  const images = entry.image ?? [];
  const setLine = (i: number, v: string) => {
    const next = [...texts]; next[i] = v;
    onChange({ ...entry, text: next });
  };
  const removeLine = (i: number) => onChange({ ...entry, text: texts.filter((_, j) => j !== i) });
  const addLine = () => onChange({ ...entry, text: [...texts, ""] });
  const removeImage = (id: number) => onChange({ ...entry, image: images.filter((x) => x !== id) });

  return (
    <div className="border border-border rounded-lg p-2.5 space-y-1.5">
      <div className="text-xs font-medium text-fg-dim">{label}</div>
      {texts.length === 0 && <div className="text-[11px] text-muted">No lines yet.</div>}
      {texts.map((t, i) => (
        <div key={i} className="flex items-center gap-1.5">
          <Input className="flex-1 text-sm" value={t} onChange={(e) => setLine(i, e.target.value)}
            placeholder="message line, e.g. hey {name} 👀" />
          <button type="button" className="text-fg-dim hover:text-err" title="Remove"
            onClick={() => removeLine(i)}><X className="size-4" /></button>
        </div>
      ))}
      <Button variant="ghost" type="button" onClick={addLine}>
        <Plus className="size-3.5" /> Add line
      </Button>
      {/* Images: thumbnails + an add-from-vault tile. Sends ONE (rotated) if several. */}
      <div className="flex flex-wrap items-center gap-2 pt-1">
        {images.map((id) => (
          <MediaThumb key={id} id={id} media={mediaCache[id]} accountId={accountId}
            onRemove={() => removeImage(id)} />
        ))}
        <button type="button" onClick={onPickImages} title="Add image from vault"
          className="w-12 h-12 rounded border border-dashed border-border grid place-items-center text-fg-dim hover:border-accent hover:text-accent">
          <ImageIcon className="size-4" />
        </button>
      </div>
      <p className="text-[11px] text-muted">Only <b>one</b> image is sent per nudge — add more and they rotate between sends.</p>
    </div>
  );
}

function Collapsible({
  open, onToggle, title, children,
}: { open: boolean; onToggle: () => void; title: string; children: React.ReactNode }) {
  return (
    <div className="border border-border rounded-lg">
      <button type="button" onClick={onToggle}
        className="w-full flex items-center justify-between px-3 py-2 text-sm font-medium hover:bg-bg-elev-1">
        <span>{title}</span>
        <ChevronDown className={`size-4 transition-transform ${open ? "rotate-180" : ""}`} />
      </button>
      {open && <div className="px-3 pb-3 pt-1">{children}</div>}
    </div>
  );
}
