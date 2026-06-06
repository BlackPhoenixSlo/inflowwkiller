"use client";

/**
 * AutoStoriesTab — Settings → "Auto stories".
 *
 * One `auto_stories` automation_rule per account. The owner picks a vault
 * folder, a schedule ("cron time"), how many stories to post per trigger, how
 * long each lives before auto-delete, and an optional total run-limit. On its
 * cadence the executor posts `per_run` random photos from the folder as stories
 * (vault/CDN → convert → POST /stories) and enqueues each delete at now+hours.
 *
 * Schedule modes (all three the spec asked for):
 *   • "At set times"  → daily_at: ["09:00","20:00", …]  (one fire per slot/day)
 *   • "Every N hours" → every_seconds interval
 * Run-limit ("how many times to run") → trigger.max_runs; blank = run forever.
 * Per-trigger count ("how many to post") → payload.per_run; default 1.
 */

import { useEffect, useMemo, useState } from "react";
import { Images, Play, Trash2 } from "lucide-react";

import { Button, Card, Input } from "@/components/ui/primitives";
import { useActiveAccounts } from "@/hooks/useAccounts";
import { useVaultLists } from "@/hooks/useVaultMedia";
import {
  useAutomationRules,
  useCreateRule,
  useUpdateRule,
  useDeleteRule,
  useRunRuleNow,
  type AutomationRule,
} from "@/hooks/useAutomations";
import { cn } from "@/lib/utils";

type Mode = "clock" | "interval";

/** Match the Input primitive so the native <select>s look consistent. */
const SELECT_CLS =
  "w-full bg-bg border border-border rounded-lg px-3 py-2 text-sm focus:outline-none focus:border-accent";

interface FormState {
  folderId: number | null;
  mode: Mode;
  times: string[];          // ["09:00", …] for clock mode
  everyHours: number;       // interval mode
  perRun: number;
  hoursToLive: number;      // 0 = keep
  maxRuns: number | null;   // null = run forever
  enabled: boolean;
}

const DEFAULT_FORM: FormState = {
  folderId: null,
  mode: "clock",
  times: ["09:00"],
  everyHours: 6,
  perRun: 1,
  hoursToLive: 6,
  maxRuns: null,
  enabled: true,
};

/** Minutes to add to UTC to get the user's local time (e.g. +120 for UTC+2). */
function tzOffsetMinutes(): number {
  return -new Date().getTimezoneOffset();
}

function ruleToForm(rule: AutomationRule): FormState {
  const t = rule.trigger ?? {};
  const p = rule.payload ?? {};
  const clock = Array.isArray(t.daily_at) && t.daily_at.length > 0;
  return {
    folderId: typeof p.folder_id === "number" ? p.folder_id : null,
    mode: clock ? "clock" : "interval",
    times: clock ? (t.daily_at as string[]) : ["09:00"],
    everyHours: t.every_seconds ? Math.max(1, Math.round(t.every_seconds / 3600)) : 6,
    perRun: typeof p.per_run === "number" ? p.per_run : 1,
    hoursToLive: typeof p.hours_to_live === "number" ? p.hours_to_live : 0,
    maxRuns: typeof t.max_runs === "number" ? t.max_runs : null,
    enabled: rule.is_enabled,
  };
}

export default function AutoStoriesTab() {
  const accounts = useActiveAccounts();
  const [accountId, setAccountId] = useState<string | null>(null);
  // Default to the first account once they load.
  useEffect(() => {
    if (!accountId && accounts.length > 0) setAccountId(accounts[0].id);
  }, [accounts, accountId]);

  const rulesQ = useAutomationRules(accountId);
  const rule = useMemo(
    () => (rulesQ.data ?? []).find((r) => r.kind === "auto_stories") ?? null,
    [rulesQ.data],
  );

  const foldersQ = useVaultLists(accountId);
  // Only folders that actually hold photos are postable as stories.
  const folders = useMemo(
    () => (foldersQ.data?.list ?? []).filter((f) => (f.photosCount ?? 0) > 0),
    [foldersQ.data],
  );

  const [form, setForm] = useState<FormState>(DEFAULT_FORM);
  const set = <K extends keyof FormState>(k: K, v: FormState[K]) =>
    setForm((f) => ({ ...f, [k]: v }));

  // Load the existing rule into the form whenever it (re)loads.
  useEffect(() => {
    if (rule) setForm(ruleToForm(rule));
    else setForm(DEFAULT_FORM);
  }, [rule?.id]); // eslint-disable-line react-hooks/exhaustive-deps

  const createM = useCreateRule(accountId);
  const updateM = useUpdateRule(accountId);
  const deleteM = useDeleteRule(accountId);
  const runM = useRunRuleNow(accountId);
  const busy = createM.isPending || updateM.isPending || deleteM.isPending;

  const [err, setErr] = useState<string | null>(null);
  const [runMsg, setRunMsg] = useState<string | null>(null);

  async function runNow() {
    if (!rule) return;
    setRunMsg(null);
    const n = Math.max(1, Math.round(form.perRun));
    try {
      await runM.mutateAsync(rule.id);
      setRunMsg(
        `✓ Posting ${n} ${n === 1 ? "story" : "stories"} from “${
          folders.find((f) => f.id === form.folderId)?.name ?? "folder"
        }” now — it’ll appear on the account within ~30s.`,
      );
    } catch (e) {
      setRunMsg(`Run failed: ${(e as Error)?.message || "unknown error"}`);
    }
  }

  function buildTrigger() {
    const max_runs = form.maxRuns && form.maxRuns > 0 ? form.maxRuns : undefined;
    if (form.mode === "clock") {
      return { daily_at: form.times, tz_offset_minutes: tzOffsetMinutes(), max_runs };
    }
    return { every_seconds: Math.max(1, Math.round(form.everyHours)) * 3600, max_runs };
  }

  function buildPayload() {
    return {
      folder_id: form.folderId,
      per_run: Math.max(1, Math.round(form.perRun)),
      hours_to_live: Math.max(0, Math.round(form.hoursToLive)),
    };
  }

  async function save() {
    setErr(null);
    if (!accountId) return;
    if (!form.folderId) { setErr("Pick a vault folder first."); return; }
    if (form.mode === "clock" && form.times.length === 0) {
      setErr("Add at least one time."); return;
    }
    try {
      if (rule) {
        await updateM.mutateAsync({
          id: rule.id, trigger: buildTrigger(),
          payload: buildPayload(), is_enabled: form.enabled,
        });
      } else {
        await createM.mutateAsync({
          account_id: accountId, kind: "auto_stories",
          name: "Auto stories", trigger: buildTrigger(),
          payload: buildPayload(), is_enabled: form.enabled,
        });
      }
    } catch (e) {
      setErr((e as Error)?.message || "Save failed");
    }
  }

  const last = rule?.last_run;

  return (
    <div className="space-y-5 max-w-2xl">
      <header className="flex items-center gap-2">
        <Images className="size-5 text-accent" />
        <div>
          <h2 className="text-lg font-semibold">Auto stories</h2>
          <p className="text-sm text-fg-dim">
            Post random photos from a vault folder as stories on a schedule, each
            auto-deleting after a set number of hours.
          </p>
        </div>
      </header>

      {/* Account selector (only when more than one) */}
      {accounts.length > 1 && (
        <Field label="Account">
          <select
            className={SELECT_CLS}
            value={accountId ?? ""}
            onChange={(e) => setAccountId(e.target.value)}
          >
            {accounts.map((a) => (
              <option key={a.id} value={a.id}>{a.nickname || a.id}</option>
            ))}
          </select>
        </Field>
      )}

      <Card className="p-4 space-y-4">
        {/* Folder */}
        <Field label="Vault folder">
          {foldersQ.isLoading ? (
            <div className="text-sm text-fg-dim">Loading folders…</div>
          ) : folders.length === 0 ? (
            <div className="text-sm text-fg-dim">
              No vault folders with photos found for this account.
            </div>
          ) : (
            <select
              className={SELECT_CLS}
              value={form.folderId ?? ""}
              onChange={(e) => set("folderId", e.target.value ? Number(e.target.value) : null)}
            >
              <option value="">— pick a folder —</option>
              {folders.map((f) => (
                <option key={f.id} value={f.id}>
                  {f.name} ({f.photosCount} photos)
                </option>
              ))}
            </select>
          )}
        </Field>

        {/* Schedule — pick ONE of the two modes */}
        <div>
          <span className="text-xs font-medium text-fg-dim block mb-2">
            When to post <span className="text-muted">(choose one)</span>
          </span>
          <div className="space-y-2">
            {/* Option A — clock times */}
            <ScheduleOption
              selected={form.mode === "clock"}
              onSelect={() => set("mode", "clock")}
              title="At set times each day"
              desc="Posts once at each time below, every day."
            >
              <TimesEditor times={form.times} onChange={(times) => set("times", times)} />
            </ScheduleOption>

            {/* Option B — interval */}
            <ScheduleOption
              selected={form.mode === "interval"}
              onSelect={() => set("mode", "interval")}
              title="Every few hours"
              desc="Posts on a repeating interval, starting when you save (not immediately — use Run now for that)."
            >
              <div className="flex items-center gap-2 text-sm">
                <span>Every</span>
                <Input
                  type="number" min={1} className="w-20"
                  value={form.everyHours}
                  onChange={(e) => set("everyHours", Number(e.target.value))}
                />
                <span>hours</span>
              </div>
            </ScheduleOption>
          </div>
        </div>

        {/* Counts */}
        <div className="grid grid-cols-2 gap-4">
          <Field label="Stories per run">
            <Input
              type="number" min={1}
              value={form.perRun}
              onChange={(e) => set("perRun", Number(e.target.value))}
            />
          </Field>
          <Field label="Delete after (hours, 0 = keep)">
            <Input
              type="number" min={0}
              value={form.hoursToLive}
              onChange={(e) => set("hoursToLive", Number(e.target.value))}
            />
          </Field>
        </div>

        <Field label="Total runs (blank = forever)">
          <Input
            type="number" min={1} className="w-40"
            value={form.maxRuns ?? ""}
            placeholder="∞"
            onChange={(e) =>
              set("maxRuns", e.target.value ? Number(e.target.value) : null)}
          />
        </Field>

        {/* Enable */}
        <label className="flex items-center gap-2 text-sm cursor-pointer">
          <input
            type="checkbox"
            checked={form.enabled}
            onChange={(e) => set("enabled", e.target.checked)}
          />
          Enabled
        </label>

        {err && <div className="text-sm text-err">{err}</div>}

        <div className="flex items-center gap-2 pt-1">
          <Button onClick={save} disabled={busy || !accountId}>
            {rule ? "Save changes" : "Create automation"}
          </Button>
          {rule && (
            <>
              <Button
                variant="ghost"
                onClick={runNow}
                disabled={runM.isPending}
                title="Post one batch now (bypasses the schedule)"
              >
                <Play className="size-4" /> {runM.isPending ? "Posting…" : "Run now"}
              </Button>
              <Button
                variant="ghost"
                onClick={() => { if (confirm("Delete this automation?")) deleteM.mutate(rule.id); }}
                disabled={deleteM.isPending}
                className="text-err ml-auto"
              >
                <Trash2 className="size-4" /> Delete
              </Button>
            </>
          )}
        </div>

        {runMsg && (
          <div className="text-sm text-accent border-t border-border pt-3">{runMsg}</div>
        )}

        {rule && last && (
          <div className="text-xs text-fg-dim border-t border-border pt-3">
            Last run: <span className="text-fg">{last.status}</span>
            {typeof last.stats?.posted === "number" && (
              <span className="text-fg"> · posted {last.stats.posted as number}</span>
            )}
            {typeof last.stats?.failed === "number" && (last.stats.failed as number) > 0 && (
              <span className="text-err"> · {last.stats.failed as number} failed</span>
            )}
            {last.started_at && ` · ${new Date(last.started_at).toLocaleString()}`}
            {rule.has_pending_job && " · job queued"}
            {last.error_text && (
              <span className="text-err"> · {last.error_text}</span>
            )}
          </div>
        )}
      </Card>
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

/** One selectable schedule mode: a radio + title + description, and — when
 *  selected — its own controls (time pickers / interval input) below. Clicking
 *  anywhere on the card selects it; only the selected card's controls show. */
function ScheduleOption({
  selected, onSelect, title, desc, children,
}: {
  selected: boolean;
  onSelect: () => void;
  title: string;
  desc: string;
  children: React.ReactNode;
}) {
  return (
    <div
      onClick={onSelect}
      className={cn(
        "rounded-lg border p-3 cursor-pointer transition-colors",
        selected ? "border-accent bg-accent/5" : "border-border hover:border-border-light",
      )}
    >
      <div className="flex items-start gap-2.5">
        <span
          className={cn(
            "mt-0.5 size-4 shrink-0 rounded-full border-2 grid place-items-center",
            selected ? "border-accent" : "border-border",
          )}
        >
          {selected && <span className="size-2 rounded-full bg-accent" />}
        </span>
        <div className="min-w-0 flex-1">
          <div className={cn("text-sm font-medium", selected ? "text-fg" : "text-fg-dim")}>
            {title}
          </div>
          <p className="text-xs text-muted mt-0.5">{desc}</p>
          {selected && (
            // Stop clicks on the controls from bubbling up (they'd re-select,
            // harmless, but keep focus/typing clean).
            <div className="mt-2.5" onClick={(e) => e.stopPropagation()}>
              {children}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function TimesEditor({
  times, onChange,
}: { times: string[]; onChange: (t: string[]) => void }) {
  return (
    <div className="flex flex-wrap items-center gap-2">
      {times.map((t, i) => (
        <span key={i} className="inline-flex items-center gap-1">
          <Input
            type="time"
            value={t}
            className="w-28"
            onChange={(e) => {
              const next = [...times];
              next[i] = e.target.value;
              onChange(next);
            }}
          />
          <button
            type="button"
            className="text-fg-dim hover:text-err"
            onClick={() => onChange(times.filter((_, j) => j !== i))}
            title="Remove time"
          >
            <Trash2 className="size-3.5" />
          </button>
        </span>
      ))}
      <Button
        variant="ghost"
        type="button"
        onClick={() => onChange([...times, "12:00"])}
      >
        + Add time
      </Button>
    </div>
  );
}
