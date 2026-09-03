"use client";

/**
 * MassNudgeTab — Settings → "Mass Nudge" (also opened from +New).
 *
 * The high-traffic sibling of Nudge online: instead of a personalized, delayed
 * per-fan DM, it broadcasts ONE time-of-day message + image to everyone online
 * right now (no {name}, no per-fan state). Config lives entirely in the
 * automation_rules payload (slots / exclude / unsend / with_image) + the rule's
 * cadence — read/written via the rules API, with a roll-out fan-out across models.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { Megaphone, Play, Rocket, Eye, ChevronDown, Plus, X, Image as ImageIcon } from "lucide-react";

import { Button, Card, Input } from "@/components/ui/primitives";
import { EditRuleJsonButton } from "@/components/automations/EditRuleJsonModal";
import { VaultPicker } from "@/components/chat/VaultPicker";
import { type VaultMedia } from "@/lib/relay";
import { proxyImage } from "@/lib/mediaUrl";
import { useActiveAccounts } from "@/hooks/useAccounts";
import {
  useAutomationRules,
  useCreateRule,
  useUpdateRule,
  useRunRuleNow,
  type AutomationRule,
} from "@/hooks/useAutomations";
import {
  useMassNudgeBulk,
  useMassNudgePreview,
  type MassNudgeConfig,
} from "@/hooks/useMassNudge";
import type { NudgeSlots, NudgeSlotEntry } from "@/hooks/useNudgeConfig";

const SELECT_CLS =
  "w-full bg-bg border border-border rounded-lg px-3 py-2 text-sm focus:outline-none focus:border-accent";

const SLOT_DEFS: [string, string][] = [
  ["morning_1", "Morning · early (5–9)"],
  ["morning_2", "Morning · late (9–12)"],
  ["afternoon_1", "Afternoon · early (12–15)"],
  ["afternoon_2", "Afternoon · late (15–18)"],
  ["evening", "Evening (18–21)"],
  ["night", "Night (21–5)"],
];
const TIME_PRESETS: [string, number][] = [
  ["Morning", 8], ["Midday", 13], ["Afternoon", 16], ["Evening", 19], ["Night", 23],
];

const DEFAULT_SLOTS: NudgeSlots = {
  default: {
    morning_1: { text: ["morning loves ☀️ who's up early? 👀", "good morning 💋 come start the day with me"], image: [] },
    morning_2: { text: ["heyy 🌸 online this morning? say hi 👀"], image: [] },
    afternoon_1: { text: ["afternoon 😘 who's online? keep me company"], image: [] },
    afternoon_2: { text: ["bored this afternoon? 😏 come chat"], image: [] },
    evening: { text: ["evening everyone 🍷 who's online? 👀", "online tonight? come unwind with me 😉"], image: [] },
    night: { text: ["up late? 🌙😏 i'm still here"], image: [] },
  },
};

interface Form {
  enabled: boolean;
  withImage: boolean;
  everyMinutes: number;
  excludeRepliedHours: number | "";
  excludeInboundHours: number | "";
  unsendAfterHours: number | "";
}

function ruleToForm(rule: AutomationRule | null): Form {
  const p = (rule?.payload ?? {}) as MassNudgeConfig;
  const every = rule?.every_seconds ? Math.max(1, Math.round(rule.every_seconds / 60)) : 60;
  return {
    enabled: rule?.is_enabled ?? true,
    withImage: p.with_image !== false,
    everyMinutes: every,
    excludeRepliedHours: typeof p.exclude_replied_hours === "number" ? p.exclude_replied_hours : 12,
    excludeInboundHours: typeof p.exclude_inbound_hours === "number" ? p.exclude_inbound_hours : 12,
    unsendAfterHours: typeof p.unsend_after_hours === "number" ? p.unsend_after_hours : "",
  };
}

export default function MassNudgeTab() {
  const accounts = useActiveAccounts();
  const [accountId, setAccountId] = useState<string | null>(null);
  useEffect(() => {
    if (!accountId && accounts.length > 0) setAccountId(accounts[0].id);
  }, [accounts, accountId]);

  const rulesQ = useAutomationRules(accountId);
  const rule = useMemo(
    () => (rulesQ.data ?? []).find((r) => r.kind === "mass_nudge") ?? null,
    [rulesQ.data],
  );

  const createM = useCreateRule(accountId);
  const updateM = useUpdateRule(accountId);
  const runM = useRunRuleNow(accountId);
  const bulkM = useMassNudgeBulk();
  const previewM = useMassNudgePreview();
  const busy = createM.isPending || updateM.isPending;

  const [form, setForm] = useState<Form>(ruleToForm(null));
  const [slots, setSlots] = useState<NudgeSlots>(DEFAULT_SLOTS);
  const [jsonDraft, setJsonDraft] = useState(JSON.stringify(DEFAULT_SLOTS, null, 2));
  const [jsonErr, setJsonErr] = useState<string | null>(null);
  const [advOpen, setAdvOpen] = useState(false);
  const [helpOpen, setHelpOpen] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);

  // Preview + image picking.
  const [previewHour, setPreviewHour] = useState<number | "">("");
  const [pickerSlot, setPickerSlot] = useState<string | null>(null);
  const [mediaCache, setMediaCache] = useState<Record<number, VaultMedia>>({});

  // Roll-out checklist.
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

  useEffect(() => {
    setForm(ruleToForm(rule));
    const s = ((rule?.payload as MassNudgeConfig | undefined)?.slots ?? DEFAULT_SLOTS) as NudgeSlots;
    setSlots(s);
    setJsonDraft(JSON.stringify(s, null, 2));
  }, [rule?.id]); // eslint-disable-line react-hooks/exhaustive-deps

  const set = <K extends keyof Form>(k: K, v: Form[K]) => setForm((f) => ({ ...f, [k]: v }));

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

  function buildPayload(): MassNudgeConfig {
    return {
      with_image: form.withImage,
      online_only: true,
      exclude_replied_hours: form.excludeRepliedHours === "" ? null : Number(form.excludeRepliedHours),
      exclude_inbound_hours: form.excludeInboundHours === "" ? null : Number(form.excludeInboundHours),
      unsend_after_hours: form.unsendAfterHours === "" ? null : Number(form.unsendAfterHours),
      slots,
    };
  }

  async function save() {
    setErr(null); setMsg(null);
    if (!accountId) return;
    const every_seconds = Math.max(60, Math.round(form.everyMinutes) * 60);
    const payload = buildPayload() as unknown as Record<string, unknown>;
    try {
      if (rule) {
        await updateM.mutateAsync({ id: rule.id, every_seconds, payload, is_enabled: form.enabled });
      } else {
        await createM.mutateAsync({
          account_id: accountId, kind: "mass_nudge", name: "Mass Nudge",
          every_seconds, payload, is_enabled: form.enabled,
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
        account_id: accountId, payload: buildPayload(),
        hour: previewHour === "" ? null : Number(previewHour),
      });
    } catch (e) {
      setErr(`Preview failed: ${(e as Error)?.message || "unknown"}`);
    }
  }

  async function runNow() {
    if (!rule) return;
    setErr(null); setMsg(null);
    if (!confirm("Send one Mass Nudge broadcast to everyone online now?")) return;
    try {
      await runM.mutateAsync(rule.id);
      setMsg("✓ Broadcasting to online fans now — appears in stats within ~30s.");
    } catch (e) {
      setErr(`Run failed: ${(e as Error)?.message || "unknown"}`);
    }
  }

  async function applyToModels() {
    setBulkMsg(null);
    const ids = [...selected];
    if (ids.length === 0) { setBulkMsg("Pick at least one model."); return; }
    const every_seconds = Math.max(60, Math.round(form.everyMinutes) * 60);
    try {
      const r = await bulkM.mutateAsync({ account_ids: ids, payload: buildPayload(), enable: form.enabled, every_seconds });
      setBulkMsg(`✓ Mass Nudge set on ${r.count} model${r.count === 1 ? "" : "s"} (every ${Math.round(r.every_seconds / 60)} min).`);
    } catch (e) {
      setBulkMsg(`Failed: ${(e as Error)?.message || "unknown"}`);
    }
  }

  const last = rule?.last_run;

  return (
    <div className="space-y-5 max-w-2xl">
      <header className="flex items-start gap-2">
        <Megaphone className="size-5 text-accent" />
        <div>
          <h2 className="text-lg font-semibold">Mass Nudge</h2>
          <p className="text-sm text-fg-dim">
            Broadcast ONE time-of-day message + image to everyone online right now —
            no names, no per-fan delay. For accounts with lots of fans coming online,
            where a personalized per-fan nudge would be too many sends.
          </p>
        </div>
        <div className="ml-auto shrink-0">
          <EditRuleJsonButton accountId={accountId} kind="mass_nudge" />
        </div>
      </header>

      {accounts.length > 1 && (
        <Field label="Account">
          <select className={SELECT_CLS} value={accountId ?? ""} onChange={(e) => setAccountId(e.target.value)}>
            {accounts.map((a) => <option key={a.id} value={a.id}>{a.nickname || a.id}</option>)}
          </select>
        </Field>
      )}

      <Card className="p-4 space-y-5">
        <div className="space-y-2">
          <div className="flex flex-wrap gap-4">
            <label className="flex items-center gap-2 text-sm cursor-pointer">
              <input type="checkbox" checked={form.enabled} onChange={(e) => set("enabled", e.target.checked)} />
              Enabled
            </label>
            <label className="flex items-center gap-2 text-sm cursor-pointer">
              <input type="checkbox" checked={form.withImage} onChange={(e) => set("withImage", e.target.checked)} />
              Attach per-slot image
            </label>
          </div>
          <Hint>Targets fans <b>online now</b> (OnlyFans’ native online filter). A slot with several images sends <b>one</b> (rotated), not all.</Hint>
        </div>

        <div className="grid grid-cols-2 gap-3 md:grid-cols-4 md:gap-4">
          <NumField label="Send every (min)" value={form.everyMinutes} onChange={(v) => set("everyMinutes", v)} min={5} />
          <NumField label="Re-nudge cooldown (hrs)" value={form.excludeRepliedHours === "" ? 0 : form.excludeRepliedHours}
            onChange={(v) => set("excludeRepliedHours", v > 0 ? v : "")} min={0} />
          <NumField label="Skip repliers within (hrs)" value={form.excludeInboundHours === "" ? 0 : form.excludeInboundHours}
            onChange={(v) => set("excludeInboundHours", v > 0 ? v : "")} min={0} />
          <NumField label="Auto-unsend after (hrs)" value={form.unsendAfterHours === "" ? 0 : form.unsendAfterHours}
            onChange={(v) => set("unsendAfterHours", v > 0 ? v : "")} min={0} />
        </div>
        <Hint>
          <b>Send every</b>: how often to broadcast. <b>Re-nudge cooldown</b>: don’t nudge (or blast) a fan
          again until N hours after the last nudge/DM (0 = off). <b>Skip repliers within</b>: skip fans who
          <i> messaged us</i> in the last N hours — leave active conversations alone (0 = off). <b>Auto-unsend</b>:
          remove the broadcast after N hours (0 = keep).
        </Hint>

        {/* ── Preview ─────────────────────────────────────────────── */}
        <div className="border border-border rounded-lg p-3 space-y-3 bg-bg/40">
          <div className="flex items-center gap-1.5 text-sm font-semibold">
            <Eye className="size-4 text-accent" /> Preview
          </div>
          <div className="flex flex-wrap items-end gap-2">
            <label className="block">
              <span className="text-xs text-fg-dim block mb-1">Hour (blank = now)</span>
              <Input type="number" min={0} max={23} className="w-24" value={previewHour}
                onChange={(e) => setPreviewHour(e.target.value === "" ? "" : Number(e.target.value))} />
            </label>
            <div className="flex flex-wrap gap-1">
              {TIME_PRESETS.map(([lbl, h]) => (
                <button key={lbl} type="button" onClick={() => setPreviewHour(h)}
                  className="text-xs px-2 py-1 rounded border border-border hover:border-accent">{lbl}</button>
              ))}
            </div>
            <Button onClick={runPreview} disabled={previewM.isPending || !accountId}>
              {previewM.isPending ? "…" : "Preview"}
            </Button>
          </div>
          {previewM.data && (
            <div className="rounded-lg border border-border p-2 text-sm bg-panel">
              <div className="flex items-center justify-between text-[11px] text-fg-dim mb-1">
                <span className="font-medium text-fg">Broadcast</span>
                <span>slot: {previewM.data.slot} · rotates {previewM.data.lines} line{previewM.data.lines === 1 ? "" : "s"}</span>
              </div>
              {previewM.data.text
                ? <div>{previewM.data.text}</div>
                : <div className="text-fg-dim italic">(no line for this slot — add one below)</div>}
              {previewM.data.media.length > 0 && (
                <div className="flex flex-wrap gap-1.5 mt-1.5">
                  {previewM.data.media.map((id) => (
                    <MediaThumb key={id} id={id} media={mediaCache[id]} accountId={accountId} />
                  ))}
                </div>
              )}
            </div>
          )}
        </div>

        {/* Message lines per slot */}
        <div className="space-y-3">
          <div>
            <span className="text-sm font-semibold">Message lines (default times)</span>
            <Hint>
              Generic lines per time of day — <b>no personalization</b>, so don’t use{" "}
              <code>{"{name}"}</code> here (it won’t be filled). Add images per slot with the tile.
              Weekend/Friday overrides live in the Advanced JSON below.
            </Hint>
          </div>
          {SLOT_DEFS.map(([key, label]) => (
            <SlotEditor key={key} label={label}
              entry={(slots.default?.[key] as NudgeSlotEntry) ?? { text: [], image: [] }}
              onChange={(entry) => setDefaultSlot(key, entry)}
              accountId={accountId} mediaCache={mediaCache}
              onPickImages={() => setPickerSlot(key)} />
          ))}
        </div>

        <Collapsible open={advOpen} onToggle={() => setAdvOpen((v) => !v)} title="Advanced — edit pools as JSON (copy in / out)">
          <div className="space-y-2">
            <textarea
              className="w-full bg-bg border border-border rounded-lg px-3 py-2 text-xs font-mono h-56 focus:outline-none focus:border-accent"
              value={jsonDraft} onChange={(e) => setJsonDraft(e.target.value)} spellCheck={false} />
            <div className="flex items-center gap-2">
              <Button variant="ghost" onClick={applyJson}>Apply JSON</Button>
              <Button variant="ghost" onClick={() => navigator.clipboard?.writeText(jsonDraft)}>Copy</Button>
              {jsonErr && <span className="text-sm text-err">{jsonErr}</span>}
            </div>
            <Hint>Shape: day-bucket → slot → <code>{`{ text: [...], image: [...] }`}</code>. Buckets: <code>default</code>, <code>weekend</code>, <code>weekday</code>, or a weekday like <code>friday</code>.</Hint>
          </div>
        </Collapsible>

        {err && <div className="text-sm text-err">{err}</div>}

        <div className="flex items-center gap-2 pt-1">
          <Button onClick={save} disabled={busy || !accountId}>{rule ? "Save changes" : "Create automation"}</Button>
          {rule && (
            <Button variant="ghost" onClick={runNow} disabled={runM.isPending} title="Broadcast one now (bypasses the timer)">
              <Play className="size-4" /> {runM.isPending ? "Sending…" : "Send now"}
            </Button>
          )}
        </div>

        {msg && <div className="text-sm text-accent border-t border-border pt-3">{msg}</div>}

        {rule && last && (
          <div className="text-xs text-fg-dim border-t border-border pt-3">
            Last run: <span className="text-fg">{last.status}</span>
            {!!last.stats?.text_preview && <span className="text-fg"> · “{String(last.stats.text_preview)}”</span>}
            {typeof last.stats?.excluded === "number" && (last.stats.excluded as number) > 0 && (
              <span> · excluded {last.stats.excluded as number}</span>
            )}
            {last.started_at && ` · ${new Date(last.started_at).toLocaleString()}`}
            {rule.has_pending_job && " · job queued"}
            {last.error_text && <span className="text-err"> · {last.error_text}</span>}
          </div>
        )}
      </Card>

      {/* Roll out to models */}
      {accounts.length > 1 && (
        <Card className="p-4 space-y-3">
          <div className="flex items-start justify-between gap-2">
            <div>
              <h3 className="text-sm font-semibold flex items-center gap-1.5">
                <Rocket className="size-4 text-accent" /> Roll out to models
              </h3>
              <p className="text-xs text-fg-dim mt-0.5">Apply this Mass Nudge config + cadence to every checked model.</p>
            </div>
            <div className="flex gap-2 text-xs shrink-0">
              <button className="text-accent hover:underline" onClick={() => setSelected(new Set(accounts.map((a) => a.id)))}>All</button>
              <button className="text-fg-dim hover:underline" onClick={() => setSelected(new Set())}>None</button>
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
          <Button onClick={applyToModels} disabled={bulkM.isPending || selected.size === 0}>
            <Rocket className="size-4" />
            {bulkM.isPending ? "Applying…" : `Apply to ${selected.size} model${selected.size === 1 ? "" : "s"}`}
          </Button>
          {bulkMsg && <div className="text-sm text-accent border-t border-border pt-3">{bulkMsg}</div>}
        </Card>
      )}

      <Collapsible open={helpOpen} onToggle={() => setHelpOpen((v) => !v)} title="How Mass Nudge works (full explanation)">
        <div className="text-sm text-fg-dim space-y-2 leading-relaxed">
          <p>
            On its cadence, Mass Nudge sends <b>one</b> broadcast to every fan online at that moment
            (OnlyFans resolves “online” server-side). It picks a line + image from the pool for the
            current time of day / day of week and rotates through the lines over time. No
            personalization — the same message to the whole online crowd — which is why it scales to
            high-traffic accounts where a per-fan nudge would mean hundreds of DMs.
          </p>
          <p>
            <b>Nudge online vs Mass Nudge.</b> Nudge online sends a personalized, delayed DM to each
            fan as they come online (best for normal accounts). Mass Nudge sends a single broadcast to
            everyone online on a timer (best for high traffic). You can run either or both.
          </p>
          <p>
            <b>Exclude / unsend.</b> “Exclude DMed within N hours” keeps the blast off fans you’re
            already chatting with. “Auto-unsend after N hours” cleans it up afterward. Images: one is
            sent per broadcast (rotated), not all in a slot.
          </p>
        </div>
      </Collapsible>

      {/* Vault image picker for a slot. */}
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
function NumField({ label, value, onChange, min }: { label: string; value: number; onChange: (v: number) => void; min?: number }) {
  return (
    <Field label={label}>
      <Input type="number" min={min} value={value} onChange={(e) => onChange(Number(e.target.value))} />
    </Field>
  );
}
function Hint({ children }: { children: React.ReactNode }) {
  return <p className="text-[11px] text-muted leading-snug mt-1">{children}</p>;
}

function thumbSrc(media: VaultMedia | undefined, accountId: string | null): string {
  const raw = media?.files?.thumb?.url || media?.files?.squarePreview?.url || media?.files?.preview?.url || "";
  return raw ? proxyImage(raw, accountId) : "";
}

function MediaThumb({ id, media, accountId, onRemove }: { id: number; media?: VaultMedia; accountId: string | null; onRemove?: () => void }) {
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
  const setLine = (i: number, v: string) => { const next = [...texts]; next[i] = v; onChange({ ...entry, text: next }); };
  const removeLine = (i: number) => onChange({ ...entry, text: texts.filter((_, j) => j !== i) });
  const addLine = () => onChange({ ...entry, text: [...texts, ""] });
  const removeImage = (id: number) => onChange({ ...entry, image: images.filter((x) => x !== id) });
  return (
    <div className="border border-border rounded-lg p-2.5 space-y-1.5">
      <div className="text-xs font-medium text-fg-dim">{label}</div>
      {texts.length === 0 && <div className="text-[11px] text-muted">No lines yet.</div>}
      {texts.map((t, i) => (
        <div key={i} className="flex items-center gap-1.5">
          <Input className="flex-1 text-base md:text-sm" value={t} onChange={(e) => setLine(i, e.target.value)} placeholder="message line, e.g. who's online tonight? 👀" />
          <button type="button" className="text-fg-dim hover:text-err" title="Remove" onClick={() => removeLine(i)}><X className="size-4" /></button>
        </div>
      ))}
      <Button variant="ghost" type="button" onClick={addLine}><Plus className="size-3.5" /> Add line</Button>
      <div className="flex flex-wrap items-center gap-2 pt-1">
        {images.map((id) => (
          <MediaThumb key={id} id={id} media={mediaCache[id]} accountId={accountId} onRemove={() => removeImage(id)} />
        ))}
        <button type="button" onClick={onPickImages} title="Add image from vault"
          className="w-12 h-12 rounded border border-dashed border-border grid place-items-center text-fg-dim hover:border-accent hover:text-accent">
          <ImageIcon className="size-4" />
        </button>
      </div>
      <p className="text-[11px] text-muted">Only <b>one</b> image is sent per broadcast — add more and they rotate between sends.</p>
    </div>
  );
}

function Collapsible({ open, onToggle, title, children }: { open: boolean; onToggle: () => void; title: string; children: React.ReactNode }) {
  return (
    <div className="border border-border rounded-lg">
      <button type="button" onClick={onToggle} className="w-full flex items-center justify-between px-3 py-2 text-sm font-medium hover:bg-bg-elev-1">
        <span>{title}</span>
        <ChevronDown className={`size-4 transition-transform ${open ? "rotate-180" : ""}`} />
      </button>
      {open && <div className="px-3 pb-3 pt-1">{children}</div>}
    </div>
  );
}
