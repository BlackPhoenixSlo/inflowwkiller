"use client";

/**
 * RuleEditor — inline create/edit form for one automation rule.
 *
 * Renders at the top of the panel when you click "New rule" or "Edit". Picks a
 * kind (create only — kind is immutable once set), a cadence (value + unit →
 * every_seconds), optional quiet hours, an enabled flag, and the per-run
 * payload — rendered as TYPED fields driven by the kind's knob catalog
 * (number / switch / text / id-list / JSON sub-editor) instead of one raw JSON
 * box. An "Edit raw JSON" escape hatch is still there for power users and for
 * any kind the catalog hasn't described. Values are validated locally before
 * the mutation, mirroring the server's `_validate_payload_for_kind`.
 */

import { useEffect, useMemo, useRef, useState } from "react";

import { Button, Card, Input } from "@/components/ui/primitives";
import { cn } from "@/lib/utils";
import {
  useCreateRule,
  useUpdateRule,
  type AutomationKind,
  type AutomationRule,
  type KnobHint,
} from "@/hooks/useAutomations";
import { MassMessageComposer } from "@/components/compose/MassMessageComposer";
import { PremadeForm } from "@/components/compose/PremadeComposer";
import { relay } from "@/lib/relay";
import { useEmployee } from "@/contexts/EmployeeContext";
import {
  useStyleConfig,
  useSaveStyleConfig,
  type StyleConfig,
} from "@/hooks/useStyleConfig";

type Unit = "seconds" | "minutes" | "hours";
const UNIT_S: Record<Unit, number> = { seconds: 1, minutes: 60, hours: 3600 };

/** The chat automations that support the per-account "human style" + "typos"
 *  opt-ins (mirrors STYLE_AUTOMATIONS on the backend). The toggles read/write the
 *  account's style_config_json, keyed by kind — the SAME source the Auto Convo
 *  tab edits, so the two surfaces stay in sync. */
const STYLE_KINDS = ["of_ai_chat", "autoreply", "deep_convo"] as const;
const isStyleKind = (k: string): k is (typeof STYLE_KINDS)[number] =>
  (STYLE_KINDS as readonly string[]).includes(k);

/** Largest whole unit that divides `secs` cleanly, for a tidy default display. */
function splitInterval(secs: number): { value: number; unit: Unit } {
  if (secs > 0 && secs % 3600 === 0) return { value: secs / 3600, unit: "hours" };
  if (secs > 0 && secs % 60 === 0) return { value: secs / 60, unit: "minutes" };
  return { value: secs, unit: "seconds" };
}

/** Per-knob editor state: native for bool, raw string for everything else
 *  (so a half-typed id list / JSON never corrupts the payload mid-keystroke). */
type Fields = Record<string, unknown>;

/** Seed the typed-field state + any non-knob "leftover" keys from a payload. */
function seedFields(
  knobs: KnobHint[],
  payload: Record<string, unknown>,
): { fields: Fields; leftover: Record<string, unknown> } {
  const fields: Fields = {};
  const leftover = { ...payload };
  for (const kn of knobs) {
    delete leftover[kn.key];
    const v = payload[kn.key];
    if (kn.type === "bool") {
      fields[kn.key] = v === undefined ? Boolean(kn.default ?? false) : Boolean(v);
    } else if (kn.type === "ids") {
      fields[kn.key] = Array.isArray(v) ? v.join(", ") : "";
    } else if (kn.type === "json") {
      fields[kn.key] = v === undefined ? "" : JSON.stringify(v, null, 2);
    } else {
      fields[kn.key] = v === undefined || v === null ? "" : String(v);
    }
  }
  return { fields, leftover };
}

export default function RuleEditor({
  accountId,
  kinds,
  editing,
  onClose,
}: {
  accountId: string;
  kinds: AutomationKind[];
  editing: AutomationRule | null; // null = create
  onClose: () => void;
}) {
  const isEdit = editing !== null;
  const createM = useCreateRule(accountId);
  const updateM = useUpdateRule(accountId);

  // Only "rules"-surface kinds are creatable from this editor; everything else
  // (ready-made sends, brain, settings, internal) lives on its own surface.
  const ruleKinds = useMemo(
    () => kinds.filter((k) => k.surface === "rules"),
    [kinds],
  );
  // Split the rules dropdown into "Core" (everyday senders) and "Advanced"
  // (utility kinds: backfill, sheets export, old-fan onboarding).
  const coreKinds = useMemo(
    () => ruleKinds.filter((k) => k.group !== "advanced"),
    [ruleKinds],
  );
  const advancedKinds = useMemo(
    () => ruleKinds.filter((k) => k.group === "advanced"),
    [ruleKinds],
  );
  const [kind, setKind] = useState<string>(editing?.kind ?? ruleKinds[0]?.kind ?? "");
  const [name, setName] = useState<string>(editing?.name ?? "");
  const initInterval = splitInterval(editing?.every_seconds ?? 300);
  const [intervalValue, setIntervalValue] = useState<number>(initInterval.value);
  const [intervalUnit, setIntervalUnit] = useState<Unit>(initInterval.unit);
  const [enabled, setEnabled] = useState<boolean>(editing?.is_enabled ?? true);
  const [quietStart, setQuietStart] = useState<string>(
    editing?.quiet_hours ? String(editing.quiet_hours[0]) : "",
  );
  const [quietEnd, setQuietEnd] = useState<string>(
    editing?.quiet_hours ? String(editing.quiet_hours[1]) : "",
  );
  const [err, setErr] = useState<string | null>(null);

  // Per-account "human style" + "typos" opt-ins (only for the chat kinds). Read
  // from / written to style_config_json — the same store the Auto Convo tab uses.
  const styleCfgQ = useStyleConfig(accountId);
  const saveStyleM = useSaveStyleConfig(accountId);
  const [humanStyle, setHumanStyle] = useState(false);
  const [typos, setTypos] = useState(false);
  const [nonnative, setNonnative] = useState(false);
  useEffect(() => {
    const c: StyleConfig = {
      ...(styleCfgQ.data?.defaults ?? {}),
      ...(styleCfgQ.data?.config ?? {}),
    };
    setHumanStyle(Boolean(c[kind as keyof StyleConfig]));
    setTypos(Boolean(c[`typos_${kind}` as keyof StyleConfig]));
    setNonnative(Boolean(c[`nonnative_${kind}` as keyof StyleConfig]));
  }, [styleCfgQ.data, kind]);

  const meta = useMemo(() => kinds.find((k) => k.kind === kind), [kinds, kind]);
  const knobs = meta?.knobs ?? [];
  // Composer kinds build their whole payload with a rich composer (launched in a
  // modal) instead of the per-knob fields. The knobs stay as the raw-JSON hatch.
  const composer = meta?.composer ?? null;

  // Typed-field state, seeded from the rule being edited (or empty on create).
  const seed = useMemo(
    () => seedFields(knobs, (editing?.payload as Record<string, unknown>) ?? {}),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [],
  );
  const [fields, setFields] = useState<Fields>(seed.fields);
  const [leftover, setLeftover] = useState<Record<string, unknown>>(seed.leftover);
  // Raw-JSON escape hatch: forced on for kinds the catalog doesn't describe.
  const [rawMode, setRawMode] = useState<boolean>(knobs.length === 0);
  const [rawText, setRawText] = useState<string>(() =>
    JSON.stringify((editing?.payload as Record<string, unknown>) ?? {}, null, 2),
  );
  // Composer-built payload (composer kinds only). Seeded from the rule being
  // edited so saving WITHOUT re-composing preserves it; the composer opens empty
  // and only overwrites this once the user clicks "Use in rule".
  const [composed, setComposed] = useState<Record<string, unknown>>(
    () => (editing?.payload as Record<string, unknown>) ?? {},
  );
  const [composerOpen, setComposerOpen] = useState(false);

  // Scroll the editor into view when it opens — it mounts at the top of a long
  // rules list, so clicking "Edit" on a row far down otherwise leaves it
  // off-screen (the user had to scroll up to find it). Remounts per rule (key),
  // so this fires on every Edit.
  const headerRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    headerRef.current?.scrollIntoView({ behavior: "smooth", block: "center" });
  }, []);

  // Create mode: switching kind swaps the knob set, so reset the payload state.
  useEffect(() => {
    if (isEdit) return;
    const fresh = seedFields(meta?.knobs ?? [], {});
    setFields(fresh.fields);
    setLeftover(fresh.leftover);
    setRawText("{}");
    setComposed({});
    setComposerOpen(false);
    setRawMode((meta?.knobs ?? []).length === 0);
    setErr(null);
    // Seed the cadence from the kind's suggested default (fall back to 5 min).
    const seeded = splitInterval(meta?.cadence_default_s ?? 300);
    setIntervalValue(seeded.value);
    setIntervalUnit(seeded.unit);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [kind]);

  const everySeconds = Math.round(intervalValue * UNIT_S[intervalUnit]);
  const busy = createM.isPending || updateM.isPending;

  function setField(key: string, val: unknown) {
    setFields((f) => ({ ...f, [key]: val }));
  }

  /** Build the payload object from the typed fields (throws on a bad value). */
  function buildFromFields(): Record<string, unknown> {
    const out: Record<string, unknown> = { ...leftover };
    for (const kn of knobs) {
      const raw = fields[kn.key];
      if (kn.type === "bool") {
        out[kn.key] = Boolean(raw);
        continue;
      }
      // Empty = "unset" → omit so the executor's own default applies.
      if (raw === undefined || raw === null || (typeof raw === "string" && raw.trim() === "")) {
        continue;
      }
      if (kn.type === "int") {
        const n = Number(raw);
        if (!Number.isFinite(n) || !Number.isInteger(n)) {
          throw new Error(`${kn.key} must be a whole number`);
        }
        if (kn.min !== undefined && n < kn.min) throw new Error(`${kn.key} must be ≥ ${kn.min}`);
        if (kn.max !== undefined && n > kn.max) throw new Error(`${kn.key} must be ≤ ${kn.max}`);
        out[kn.key] = n;
      } else if (kn.type === "ids") {
        const parts = String(raw).split(/[\s,]+/).filter(Boolean);
        const ids = parts.map((p) => Number(p));
        if (ids.some((n) => !Number.isInteger(n))) {
          throw new Error(`${kn.key} must be a list of whole-number ids`);
        }
        out[kn.key] = ids;
      } else if (kn.type === "json") {
        let parsed: unknown;
        try {
          parsed = JSON.parse(String(raw));
        } catch (e) {
          throw new Error(`${kn.key} is not valid JSON: ${(e as Error).message}`);
        }
        out[kn.key] = parsed;
      } else {
        // str
        const s = String(raw);
        if (kn.enum && !kn.enum.map(String).includes(s)) {
          throw new Error(`${kn.key} must be one of ${kn.enum.join(", ")}`);
        }
        out[kn.key] = s;
      }
    }
    return out;
  }

  /** The current payload regardless of mode (throws with a clear message). */
  function currentPayload(): Record<string, unknown> {
    if (rawMode) {
      const t = rawText.trim();
      if (!t) return {};
      let parsed: unknown;
      try {
        parsed = JSON.parse(t);
      } catch (e) {
        throw new Error(`Payload is not valid JSON: ${(e as Error).message}`);
      }
      if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
        throw new Error('Payload must be a JSON object, e.g. { "limit": 40 }');
      }
      return parsed as Record<string, unknown>;
    }
    if (composer) return composed;
    return buildFromFields();
  }

  function enterRaw() {
    try {
      setRawText(JSON.stringify(composer ? composed : buildFromFields(), null, 2));
      setErr(null);
    } catch (e) {
      setErr((e as Error).message);
      return;
    }
    setRawMode(true);
  }

  function exitRaw() {
    let parsed: Record<string, unknown>;
    try {
      parsed = currentPayload(); // validates object shape
    } catch (e) {
      setErr((e as Error).message);
      return;
    }
    if (composer) {
      setComposed(parsed);
      setRawMode(false);
      setErr(null);
      return;
    }
    const fresh = seedFields(knobs, parsed);
    setFields(fresh.fields);
    setLeftover(fresh.leftover);
    setRawMode(false);
    setErr(null);
  }

  /** Quiet hours → [start, end] (or null = off). Both blank = off; one blank = error. */
  function quietHours(): number[] | null {
    const a = quietStart.trim();
    const b = quietEnd.trim();
    if (!a && !b) return null;
    if (!a || !b) throw new Error("Quiet hours need BOTH a start and an end (or leave both blank).");
    const s = Number(a);
    const e = Number(b);
    for (const h of [s, e]) {
      if (!Number.isInteger(h) || h < 0 || h > 23) {
        throw new Error("Quiet hours must be whole hours 0–23.");
      }
    }
    return [s, e];
  }

  async function save() {
    setErr(null);
    if (everySeconds < 30) {
      setErr("Interval must be at least 30 seconds (the executor ticks every 30s).");
      return;
    }
    let payload: Record<string, unknown>;
    let quiet: number[] | null;
    try {
      payload = currentPayload();
      quiet = quietHours();
    } catch (e) {
      setErr((e as Error).message);
      return;
    }

    try {
      if (isEdit && editing) {
        await updateM.mutateAsync({
          id: editing.id,
          name: name.trim() || editing.kind,
          every_seconds: everySeconds,
          payload,
          quiet_hours: quiet ?? [0, 0], // [0,0] clears server-side
          is_enabled: enabled,
        });
      } else {
        await createM.mutateAsync({
          account_id: accountId,
          kind,
          name: name.trim() || undefined,
          every_seconds: everySeconds,
          payload,
          quiet_hours: quiet ?? undefined,
          is_enabled: enabled,
        });
      }
      // Persist the style/typos/non-native opt-ins (chat kinds only). The backend
      // MERGES, so sending just this automation's keys won't touch the others'.
      if (isStyleKind(kind)) {
        try {
          await saveStyleM.mutateAsync({
            [kind]: humanStyle,
            [`typos_${kind}`]: typos,
            [`nonnative_${kind}`]: nonnative,
          } as StyleConfig);
        } catch (e) {
          // The rule already saved — surface the style failure but don't block.
          setErr(`Rule saved, but style toggles failed: ${(e as Error)?.message}`);
          return;
        }
      }
      onClose();
    } catch (e) {
      setErr((e as Error)?.message || "Save failed");
    }
  }

  return (
    <Card className="p-4 space-y-3 border-accent/40">
      <div ref={headerRef} className="flex items-center justify-between gap-3 scroll-mt-20">
        <h3 className="text-sm font-semibold text-fg">
          {isEdit ? `Edit rule · ${editing?.kind}` : "New automation rule"}
        </h3>
        <button
          type="button"
          onClick={onClose}
          className="text-fg-dim hover:text-fg text-sm"
          aria-label="Close editor"
        >
          ✕
        </button>
      </div>

      <div className="grid gap-3 sm:grid-cols-2">
        {/* Kind — immutable on edit (changing it would orphan run history). */}
        <label className="block space-y-1">
          <span className="text-[11px] uppercase tracking-wide text-fg-dim">Automation</span>
          {isEdit ? (
            <div className="text-sm text-fg font-mono py-1.5">{editing?.kind}</div>
          ) : (
            <select
              value={kind}
              onChange={(e) => setKind(e.target.value)}
              className="w-full rounded-lg bg-bg-elev-1 border border-border text-sm px-2 py-1.5 text-fg focus:outline-none focus:ring-2 focus:ring-accent/40"
            >
              <optgroup label="Core">
                {coreKinds.map((k) => (
                  <option key={k.kind} value={k.kind}>
                    {k.label} ({k.kind})
                  </option>
                ))}
              </optgroup>
              {advancedKinds.length > 0 && (
                <optgroup label="Advanced">
                  {advancedKinds.map((k) => (
                    <option key={k.kind} value={k.kind}>
                      {k.label} ({k.kind})
                    </option>
                  ))}
                </optgroup>
              )}
            </select>
          )}
        </label>

        {/* Name */}
        <label className="block space-y-1">
          <span className="text-[11px] uppercase tracking-wide text-fg-dim">Name (optional)</span>
          <Input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder={meta?.label ?? kind}
          />
        </label>

        {/* Cadence */}
        <label className="block space-y-1">
          <span className="text-[11px] uppercase tracking-wide text-fg-dim">Run every</span>
          <div className="flex items-center gap-2">
            <Input
              type="number"
              min={1}
              value={intervalValue}
              onChange={(e) => setIntervalValue(Math.max(1, Number(e.target.value) || 0))}
              className="w-24"
            />
            <select
              value={intervalUnit}
              onChange={(e) => setIntervalUnit(e.target.value as Unit)}
              className="rounded-lg bg-bg-elev-1 border border-border text-sm px-2 py-1.5 text-fg focus:outline-none focus:ring-2 focus:ring-accent/40"
            >
              <option value="seconds">seconds</option>
              <option value="minutes">minutes</option>
              <option value="hours">hours</option>
            </select>
          </div>
        </label>

        {/* Enabled */}
        <label className="flex items-center gap-2 pt-5">
          <input
            type="checkbox"
            checked={enabled}
            onChange={(e) => setEnabled(e.target.checked)}
            className="w-4 h-4 rounded accent-accent cursor-pointer"
          />
          <span className="text-sm text-fg">Enabled</span>
        </label>

        {/* Quiet hours (opt-in) */}
        <label className="block space-y-1 sm:col-span-2">
          <span className="text-[11px] uppercase tracking-wide text-fg-dim">
            Quiet hours (optional, creator-local)
          </span>
          <div className="flex items-center gap-2 text-sm text-fg-dim">
            <Input
              type="number"
              min={0}
              max={23}
              value={quietStart}
              onChange={(e) => setQuietStart(e.target.value)}
              placeholder="—"
              className="w-20"
            />
            <span>to</span>
            <Input
              type="number"
              min={0}
              max={23}
              value={quietEnd}
              onChange={(e) => setQuietEnd(e.target.value)}
              placeholder="—"
              className="w-20"
            />
            <span className="text-[11px]">leave blank to run 24/7 (wraps midnight if start &gt; end)</span>
          </div>
        </label>
      </div>

      {/* What this automation does + a "good settings" example. */}
      {meta && (meta.summary || meta.example) && (
        <div className="rounded-lg border border-border bg-bg-elev-1 px-3 py-2 text-[11px] text-fg-dim space-y-1">
          {meta.summary && <div>{meta.summary}</div>}
          {meta.example && (
            <div>
              <span className="text-accent">Example:</span> {meta.example}
            </div>
          )}
        </div>
      )}

      {/* Payload — typed fields (or the raw-JSON escape hatch). */}
      <div className="space-y-2">
        <div className="flex items-center justify-between">
          <span className="text-[11px] uppercase tracking-wide text-fg-dim">Settings</span>
          {knobs.length > 0 && (
            <button
              type="button"
              onClick={() => (rawMode ? exitRaw() : enterRaw())}
              className="text-[11px] text-accent hover:underline"
            >
              {rawMode ? "← Typed fields" : "Edit raw JSON →"}
            </button>
          )}
        </div>

        {rawMode ? (
          <textarea
            value={rawText}
            onChange={(e) => setRawText(e.target.value)}
            rows={6}
            spellCheck={false}
            className="w-full rounded-lg bg-bg-elev-1 border border-border text-xs font-mono px-2 py-1.5 text-fg focus:outline-none focus:ring-2 focus:ring-accent/40"
            placeholder="{ }"
          />
        ) : composer ? (
          <ComposerLauncher
            composer={composer}
            payload={composed}
            onCompose={() => setComposerOpen(true)}
          />
        ) : knobs.length === 0 ? (
          <div className="text-[11px] text-fg-dim">No settings for this automation.</div>
        ) : (
          <div className="grid gap-3 sm:grid-cols-2">
            {knobs.map((kn) =>
              kind === "push_to_sheets" && kn.key === "spreadsheet_id" ? (
                <SheetLinkField key={kn.key} knob={kn} value={fields[kn.key]} onChange={setField} />
              ) : (
                <KnobField key={kn.key} knob={kn} value={fields[kn.key]} onChange={setField} />
              ),
            )}
          </div>
        )}

        {meta && !meta.recurring && (
          <div className="text-[11px] text-warn pt-1">
            ⚠ {meta.kind} is an action-style automation — on a bare timer it needs
            settings to do anything useful. (Usually launched from the composer instead.)
          </div>
        )}
      </div>

      {/* process_old_fans has no audience until you give it one — let the operator
       *  onboard the N most-recent fans as a one-shot, no id list needed (W9-D). */}
      {kind === "process_old_fans" && (
        <div className="space-y-2">
          <span className="text-[11px] uppercase tracking-wide text-fg-dim">Run now</span>
          <OnboardRecentCard accountId={accountId} />
        </div>
      )}

      {/* Texting style — chat kinds only. Account-wide for this automation; saved
       *  together with the rule. Mirrors the Auto Convo tab's Style/Typos card. */}
      {isStyleKind(kind) && (
        <div className="rounded-lg border border-border bg-bg-elev-1 px-3 py-2.5 space-y-2">
          <div className="text-[11px] uppercase tracking-wide text-fg-dim">
            Texting style <span className="lowercase tracking-normal">(applies to this automation, account-wide)</span>
          </div>
          <div className="flex flex-wrap gap-x-6 gap-y-2">
            <label className="flex items-center gap-2 cursor-pointer">
              <input
                type="checkbox"
                checked={humanStyle}
                onChange={(e) => setHumanStyle(e.target.checked)}
                className="w-4 h-4 rounded accent-accent cursor-pointer"
              />
              <span className="text-sm text-fg">Human style</span>
              <span className="text-[11px] text-fg-dim/70">girly, short lowercase bursts, no em-dash</span>
            </label>
            <label className="flex items-center gap-2 cursor-pointer">
              <input
                type="checkbox"
                checked={typos}
                onChange={(e) => setTypos(e.target.checked)}
                className="w-4 h-4 rounded accent-accent cursor-pointer"
              />
              <span className="text-sm text-fg">Typos</span>
              <span className="text-[11px] text-fg-dim/70">occasional thumb-slip (+ sometimes a “*fix”)</span>
            </label>
            <label className="flex items-center gap-2 cursor-pointer">
              <input
                type="checkbox"
                checked={nonnative}
                onChange={(e) => setNonnative(e.target.checked)}
                className="w-4 h-4 rounded accent-accent cursor-pointer"
              />
              <span className="text-sm text-fg">Non-native</span>
              <span className="text-[11px] text-fg-dim/70">consistent non-native misspellings + broken grammar</span>
            </label>
          </div>
        </div>
      )}

      {err && <div className="text-xs text-err whitespace-pre-wrap">{err}</div>}

      <div className="flex items-center gap-2">
        <Button size="sm" variant="primary" onClick={save} disabled={busy || !kind}>
          {busy ? "Saving…" : isEdit ? "Save changes" : "Create rule"}
        </Button>
        <Button size="sm" variant="ghost" onClick={onClose} disabled={busy}>
          Cancel
        </Button>
        <span className={cn("text-[11px] text-fg-dim ml-auto tabular-nums")}>
          = every {everySeconds}s
        </span>
      </div>

      {/* Rich composer, launched for composer kinds. On "Use in rule" it hands
       *  back the payload, which replaces `composed` (and so the saved payload).
       *  Until then the existing payload is preserved. */}
      {composerOpen && composer === "mass_message" && (
        <MassMessageComposer
          open
          onClose={() => setComposerOpen(false)}
          forcedAccountId={accountId}
          initialPayload={composed}
          onComposePayload={(p) => {
            setComposed(p);
            setComposerOpen(false);
            setErr(null);
          }}
        />
      )}
      {composerOpen && composer === "premade" && (
        <PremadeComposerModal
          kind={kind as "auto_posts" | "mass_premade"}
          accountId={accountId}
          initialPayload={composed}
          onClose={() => setComposerOpen(false)}
          onComposePayload={(p) => {
            setComposed(p);
            setComposerOpen(false);
            setErr(null);
          }}
        />
      )}
    </Card>
  );
}

/** Compact read-out of the composer-built payload + the "Compose" launcher. */
function ComposerLauncher({
  composer,
  payload,
  onCompose,
}: {
  composer: "mass_message" | "premade";
  payload: Record<string, unknown>;
  onCompose: () => void;
}) {
  const summary = summarizePayload(composer, payload);
  return (
    <div className="rounded-lg border border-border bg-bg-elev-1 p-3 space-y-2">
      <div className="text-[11px] text-fg-dim">
        {summary ?? "No content yet — build this rule's message with the composer."}
      </div>
      <Button size="sm" variant={summary ? "secondary" : "primary"} onClick={onCompose}>
        {summary ? "Re-compose" : "Compose…"}
      </Button>
    </div>
  );
}

/** One-line summary of a composer payload for the launcher read-out. */
function summarizePayload(
  composer: "mass_message" | "premade",
  payload: Record<string, unknown>,
): string | null {
  if (composer === "mass_message") {
    const text = typeof payload.text === "string" ? payload.text : "";
    const lists = Array.isArray(payload.user_lists) ? payload.user_lists : [];
    const media = Array.isArray(payload.media_files) ? payload.media_files.length : 0;
    const price = typeof payload.price === "number" ? payload.price : 0;
    if (!text && !media && lists.length === 0) return null;
    const bits: string[] = [];
    if (text) bits.push(`“${text.slice(0, 40)}${text.length > 40 ? "…" : ""}”`);
    if (media) bits.push(`${media} media`);
    if (price > 0) bits.push(`$${price} PPV`);
    if (lists.length) bits.push(`→ ${lists.join(", ")}`);
    return bits.join(" · ");
  }
  // premade: {posts:[…]} or {messages:[…]}
  const posts = Array.isArray(payload.posts) ? payload.posts : null;
  const messages = Array.isArray(payload.messages) ? payload.messages : null;
  const arr = posts ?? messages;
  if (!arr || arr.length === 0) return null;
  const noun = posts ? "post" : "message";
  return `${arr.length} ${noun}${arr.length === 1 ? "" : "s"} configured`;
}

/** Thin modal wrapper around PremadeForm in return-payload mode (the form isn't
 *  a modal on its own — PremadeComposer is, but it enqueues). */
function PremadeComposerModal({
  kind,
  accountId,
  initialPayload,
  onClose,
  onComposePayload,
}: {
  kind: "auto_posts" | "mass_premade";
  accountId: string;
  initialPayload?: Record<string, unknown>;
  onClose: () => void;
  onComposePayload: (payload: Record<string, unknown>) => void;
}) {
  const title =
    kind === "auto_posts" ? "Compose auto posts for rule" : "Compose premade mass for rule";
  return (
    <div className="fixed inset-0 z-50 bg-black/50 grid place-items-center p-4" onClick={onClose}>
      <div
        onClick={(e) => e.stopPropagation()}
        className="w-full max-w-[640px] max-h-[90vh] flex flex-col bg-panel border border-border rounded-xl shadow-2xl"
      >
        <header className="px-4 py-3 border-b border-border flex items-center justify-between">
          <h2 className="text-sm font-semibold">{title}</h2>
          <button
            type="button"
            onClick={onClose}
            className="text-fg-dim hover:text-fg text-lg leading-none"
            title="Close"
          >
            ×
          </button>
        </header>
        <div className="flex-1 overflow-y-auto p-4">
          <PremadeForm
            kind={kind}
            forcedAccountId={accountId}
            initialPayload={initialPayload}
            onComposePayload={onComposePayload}
          />
        </div>
      </div>
    </div>
  );
}

/** "Onboard most recent N" — a one-shot run button for process_old_fans that
 *  needs no id list: enqueues {kind:"process_old_fans", payload:{recent_limit,
 *  recent_by}} (same /admin/automation/enqueue + employee-id pattern as UnsendCard
 *  / PremadeForm). The backend builds the batch from the N most-recently
 *  subscribed/messaged fans. Confirms first — it spends LLM credits on gen_info. */
function OnboardRecentCard({ accountId }: { accountId: string }) {
  const { current: currentEmployee } = useEmployee();
  const [n, setN] = useState("100");
  const [by, setBy] = useState<"subscribed" | "messaged">("subscribed");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [okMsg, setOkMsg] = useState<string | null>(null);

  async function enqueue() {
    setError(null);
    setOkMsg(null);
    const recent_limit = Math.floor(Number(n));
    if (!Number.isFinite(recent_limit) || recent_limit < 1) {
      setError("Enter how many recent fans to onboard (≥ 1).");
      return;
    }
    if (
      !window.confirm(
        `Onboard the ${recent_limit} most-recent (${by}) fans on this account?\n\n` +
          "This flags them out of the AI chatter and spends LLM credits profiling " +
          "each. It can't be undone.",
      )
    )
      return;

    setBusy(true);
    try {
      const resp = await relay.post<{ enqueued_job_id: number }>(
        "/admin/automation/enqueue",
        {
          account_id: accountId,
          kind: "process_old_fans",
          payload: { recent_limit, recent_by: by },
        },
        { accountId, employeeId: currentEmployee?.id },
      );
      setOkMsg(
        `Onboarding enqueued (job #${resp.enqueued_job_id}) — ${recent_limit} most-recent ` +
          `(${by}) fans. Runs within ~30s.`,
      );
    } catch (e) {
      setError((e as Error)?.message || "Enqueue failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="rounded-lg border border-border bg-bg-elev-1 p-3 space-y-2">
      <div className="text-[11px] text-fg-dim">
        Onboard the most-recent fans now — no id list needed. Flags them out of the
        AI chatter, then profiles + applies each (spends LLM credits).
      </div>
      <div className="flex flex-wrap items-end gap-2">
        <label className="block space-y-1">
          <span className="text-[11px] text-fg-dim">How many</span>
          <Input
            type="number"
            min={1}
            value={n}
            onChange={(e) => setN(e.target.value)}
            className="w-24"
          />
        </label>
        <label className="block space-y-1">
          <span className="text-[11px] text-fg-dim">Most recent by</span>
          <select
            value={by}
            onChange={(e) => setBy(e.target.value as "subscribed" | "messaged")}
            className="rounded-lg bg-bg-elev-1 border border-border text-sm px-2 py-1.5 text-fg focus:outline-none focus:ring-2 focus:ring-accent/40"
          >
            <option value="subscribed">subscribed</option>
            <option value="messaged">messaged</option>
          </select>
        </label>
        <Button size="sm" variant="primary" onClick={enqueue} disabled={busy || !accountId}>
          {busy ? "Enqueuing…" : "Onboard most recent N"}
        </Button>
      </div>
      {okMsg && (
        <div className="text-[11px] text-ok border border-ok/30 bg-ok/5 rounded-md px-2 py-1.5">
          {okMsg}
        </div>
      )}
      {error && <span className="block text-err text-[11px]">{error}</span>}
    </div>
  );
}

/** Extract a Google Sheet id from a pasted URL, else return the input as-is
 *  (so a bare id still works). */
const SHEET_ID_RE = /spreadsheets\/d\/([A-Za-z0-9_-]+)/;
function extractSheetId(input: string): string {
  const m = input.match(SHEET_ID_RE);
  return m ? m[1] : input.trim();
}

/** push_to_sheets.spreadsheet_id — a friendly "Google Sheet link" field: accepts
 *  a pasted URL and stores just the id; falls back to a raw id otherwise. */
function SheetLinkField({
  knob,
  value,
  onChange,
}: {
  knob: KnobHint;
  value: unknown;
  onChange: (key: string, val: unknown) => void;
}) {
  return (
    <label className="block space-y-1 sm:col-span-2">
      <span className="text-[11px] text-fg-dim">
        <code className="text-accent">{knob.key}</code> — Google Sheet link (or id)
      </span>
      <Input
        type="text"
        value={String(value ?? "")}
        onChange={(e) => onChange(knob.key, extractSheetId(e.target.value))}
        placeholder="https://docs.google.com/spreadsheets/d/…  (or a raw id)"
      />
      <span className="block text-[11px] text-warn">
        ⚠ this sheet is shared edit-all — the export clears and rewrites its tab.
      </span>
    </label>
  );
}

/** One typed input for a single knob, picked by its widget/type. */
function KnobField({
  knob,
  value,
  onChange,
}: {
  knob: KnobHint;
  value: unknown;
  onChange: (key: string, val: unknown) => void;
}) {
  const widget = knob.widget ?? knob.type;
  const label = (
    <span className="text-[11px] text-fg-dim">
      <code className="text-accent">{knob.key}</code> — {knob.hint}
    </span>
  );

  if (knob.type === "bool" || widget === "switch") {
    return (
      <label className="flex items-start gap-2 py-1">
        <input
          type="checkbox"
          checked={Boolean(value)}
          onChange={(e) => onChange(knob.key, e.target.checked)}
          className="w-4 h-4 mt-0.5 rounded accent-accent cursor-pointer shrink-0"
        />
        {label}
      </label>
    );
  }

  if (knob.type === "json") {
    return (
      <label className="block space-y-1 sm:col-span-2">
        {label}
        <textarea
          value={String(value ?? "")}
          onChange={(e) => onChange(knob.key, e.target.value)}
          rows={3}
          spellCheck={false}
          className="w-full rounded-lg bg-bg-elev-1 border border-border text-xs font-mono px-2 py-1.5 text-fg focus:outline-none focus:ring-2 focus:ring-accent/40"
          placeholder="[ ] or { }"
        />
      </label>
    );
  }

  // number / text / ids → a single-line input
  const isNumber = knob.type === "int";
  return (
    <label className="block space-y-1">
      {label}
      <Input
        type={isNumber ? "number" : "text"}
        min={isNumber ? knob.min : undefined}
        max={isNumber ? knob.max : undefined}
        value={String(value ?? "")}
        onChange={(e) => onChange(knob.key, e.target.value)}
        placeholder={
          knob.type === "ids"
            ? "123, 456"
            : knob.default !== undefined
              ? `default ${knob.default}`
              : ""
        }
      />
    </label>
  );
}
