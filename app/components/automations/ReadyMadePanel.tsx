"use client";

/**
 * ReadyMadePanel — the "ready-made posts & broadcasts" surface that rides under
 * the automation-rules list on /automations. Two tabs:
 *
 *   Auto Posts   (auto_posts)   — post a list one-by-one; each optionally
 *                                 auto-deletes after N hours (self-cleaning feed).
 *   Premade Mass (mass_premade) — ready broadcasts; resend on a timer and/or
 *                                 auto-unsend later.
 *
 * Unlike the old fire-once tabs, these are now SAVED, persisted definitions
 * stored as `automation_rules` rows (reusing that table — no new schema). Each
 * saved rule shows what's queued next (`next_due_at` / `has_pending_job`) and a
 * compact strip of previous runs (from the executor's run log). "New / Edit"
 * builds the payload with the existing PremadeForm in COMPOSE mode and persists
 * it via the same create/update path RuleEditor uses; "Run now" enqueues an
 * immediate job off the saved rule.
 *
 * The account picker is lifted into this panel (P1 / Nudge tabs reuse it).
 */

import { useEffect, useMemo, useRef, useState } from "react";

import { Badge, Button, Card, Input } from "@/components/ui/primitives";
import { cn } from "@/lib/utils";
import { fmtDateTime } from "@/lib/format";
import { relay } from "@/lib/relay";
import { useEmployee } from "@/contexts/EmployeeContext";
import { useActiveAccounts } from "@/hooks/useAccounts";
import {
  useAutomationRules,
  useCreateRule,
  useDeleteRule,
  useRunRuleNow,
  useUpdateRule,
  type AutomationRule,
} from "@/hooks/useAutomations";
import { useAutomationRuns, type AutomationRunRow } from "@/hooks/useStats";
import { PremadeForm } from "@/components/compose/PremadeComposer";
import { MassFunnelsTab } from "@/components/automations/FunnelEditor";
import NudgeOnlineTab from "@/components/settings/NudgeOnlineTab";
import MassNudgeTab from "@/components/settings/MassNudgeTab";
import OnlineBlastTab from "@/components/settings/OnlineBlastTab";
import WebhookDispatchTab from "@/components/settings/WebhookDispatchTab";
import AutoreplyTab from "@/components/settings/AutoreplyTab";
import TipRewardTab from "@/components/settings/TipRewardTab";
import PPVLibraryTab from "@/components/settings/PPVLibraryTab";
import VaultAiTab from "@/components/settings/VaultAiTab";
import ScriptsTab from "@/components/settings/ScriptsTab";
import UpsellerTab from "@/components/settings/UpsellerTab";

type Tab = "auto_posts" | "mass_premade" | "mass_funnels" | "nudge_online" | "mass_nudge" | "online_blast" | "instant_reply" | "autoreply" | "ai_seller" | "ai_upseller" | "tip_reward" | "ppv_library" | "vault_ai" | "unsend" | "onboard";
/** The two tabs that map to an automation_rules kind (the others have their
 *  own self-contained surfaces). */
type RuleKind = "auto_posts" | "mass_premade";

const TABS: Array<{ key: Tab; label: string }> = [
  { key: "auto_posts", label: "🗓️ Auto Posts" },
  { key: "mass_premade", label: "♻️ Mass Premade" },
  { key: "mass_funnels", label: "🎯 Mass funnels" },
  { key: "nudge_online", label: "👋 Nudge Online" },
  { key: "mass_nudge", label: "📣 Mass Nudge" },
  { key: "online_blast", label: "📡 Blast Online" },
  { key: "instant_reply", label: "⚡ Reply Instant" },
  { key: "autoreply", label: "💬 Auto Convo" },
  { key: "ai_seller", label: "🤖 AI Chatter" },
  { key: "ai_upseller", label: "💰 AI Upseller" },
  { key: "tip_reward", label: "🎁 Tip Reward" },
  { key: "ppv_library", label: "💸 PPV Library" },
  { key: "vault_ai", label: "🧠 Vault AI" },
  { key: "onboard", label: "🆕 Onboard old fans" },
];

type Unit = "minutes" | "hours";
const UNIT_S: Record<Unit, number> = { minutes: 60, hours: 3600 };

/** Largest tidy unit (≥ minutes) that divides `secs` cleanly. */
function splitInterval(secs: number): { value: number; unit: Unit } {
  if (secs > 0 && secs % 3600 === 0) return { value: secs / 3600, unit: "hours" };
  return { value: Math.max(1, Math.round(secs / 60)), unit: "minutes" };
}

function timeAgo(iso: string | null): string | null {
  if (!iso) return null;
  const then = new Date(iso).getTime();
  if (!Number.isFinite(then)) return null;
  const secs = Math.round((Date.now() - then) / 1000);
  if (secs < 0) return "soon";
  if (secs < 60) return `${secs}s ago`;
  const mins = Math.round(secs / 60);
  if (mins < 60) return `${mins} min ago`;
  const hrs = Math.round(mins / 60);
  if (hrs < 24) return `${hrs} h ago`;
  return `${Math.round(hrs / 24)} d ago`;
}

/** "in 2 h" / "in 30 min" for an upcoming due time (or null if past/missing). */
function timeUntil(iso: string | null): string | null {
  if (!iso) return null;
  const t = new Date(iso).getTime();
  if (!Number.isFinite(t)) return null;
  const secs = Math.round((t - Date.now()) / 1000);
  if (secs <= 0) return "now";
  if (secs < 60) return `in ${secs}s`;
  const mins = Math.round(secs / 60);
  if (mins < 60) return `in ${mins} min`;
  const hrs = Math.round(mins / 60);
  if (hrs < 24) return `in ${hrs} h`;
  return `in ${Math.round(hrs / 24)} d`;
}

function StatusBadge({ status }: { status: string }) {
  const color =
    status === "ok" ? "ok" : status === "error" ? "err" : status === "running" ? "warn" : "muted";
  return <Badge color={color}>{status === "running" ? "running…" : status}</Badge>;
}

/** Reusable account-chip picker (lifted into the panel; Nudge tabs reuse it). */
export function AccountChips({
  accountId,
  onChange,
}: {
  accountId: string | null;
  onChange: (id: string) => void;
}) {
  const accounts = useActiveAccounts();
  if (accounts.length <= 1) return null;
  return (
    <div className="flex items-center gap-1.5 flex-wrap">
      <span className="text-[10px] uppercase tracking-wide text-fg-dim mr-1">Account</span>
      {accounts.map((a) => {
        const active = accountId === a.id;
        return (
          <button
            key={a.id}
            type="button"
            onClick={() => onChange(a.id)}
            className={cn(
              "px-2.5 py-1 rounded-full text-xs border transition-colors",
              active
                ? "bg-accent text-white border-accent"
                : "bg-bg-elev-1 text-fg-dim border-border hover:text-fg hover:border-fg-dim",
            )}
          >
            {a.nickname || a.id}
          </button>
        );
      })}
    </div>
  );
}

export default function ReadyMadePanel() {
  const accounts = useActiveAccounts();
  const [accountId, setAccountId] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab>("auto_posts");

  // Default to the first session-backed account once they load.
  useEffect(() => {
    if (!accountId && accounts.length > 0) setAccountId(accounts[0].id);
  }, [accounts, accountId]);

  // Only the two ready-made rule kinds drive the saved-rule list below; the
  // funnels / nudge tabs render their own self-contained surfaces.
  const ruleKind: RuleKind | null =
    tab === "auto_posts" || tab === "mass_premade" ? tab : null;

  // Gate the polling rules query on a rule-backed tab — passing null when off-tab
  // makes useAutomationRules' `enabled: !!accountId` short-circuit, so its 30s
  // poll stops while the funnels / nudge tabs are showing.
  const rulesQ = useAutomationRules(ruleKind ? accountId : null);
  const runsQ = useAutomationRuns(200);
  const updateM = useUpdateRule(accountId);
  const deleteM = useDeleteRule(accountId);
  const runM = useRunRuleNow(accountId);

  const [editing, setEditing] = useState<AutomationRule | null>(null);
  const [adding, setAdding] = useState(false);
  const [rowErr, setRowErr] = useState<{ id: number; msg: string } | null>(null);

  const rules = useMemo(
    () => (ruleKind ? (rulesQ.data ?? []).filter((r) => r.kind === ruleKind) : []),
    [rulesQ.data, ruleKind],
  );

  function closeEditor() {
    setEditing(null);
    setAdding(false);
  }

  // Switching tabs / accounts closes a half-open editor (kind/account would mismatch).
  useEffect(() => {
    closeEditor();
  }, [tab, accountId]);

  async function toggle(rule: AutomationRule) {
    setRowErr(null);
    try {
      await updateM.mutateAsync({ id: rule.id, is_enabled: !rule.is_enabled });
    } catch (e) {
      setRowErr({ id: rule.id, msg: (e as Error)?.message || "Toggle failed" });
    }
  }

  async function runNow(rule: AutomationRule) {
    setRowErr(null);
    try {
      await runM.mutateAsync(rule.id);
    } catch (e) {
      setRowErr({ id: rule.id, msg: (e as Error)?.message || "Run failed" });
    }
  }

  async function remove(rule: AutomationRule) {
    setRowErr(null);
    if (!window.confirm(`Delete "${rule.name}"? This stops the schedule.`)) return;
    try {
      await deleteM.mutateAsync(rule.id);
      if (editing?.id === rule.id) closeEditor();
    } catch (e) {
      setRowErr({ id: rule.id, msg: (e as Error)?.message || "Delete failed" });
    }
  }

  const runs = runsQ.data?.runs ?? [];

  return (
    <Card className="p-4 space-y-3">
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h2 className="text-sm font-medium text-fg">Ready-made posts &amp; broadcasts</h2>
          <p className="text-xs text-fg-dim">
            Saved definitions for self-cleaning feeds and premade broadcasts. Each
            runs on its schedule via the executor — see what&apos;s queued next and
            the recent run log below. &ldquo;Run now&rdquo; fires one immediately.
          </p>
        </div>
        {ruleKind && (
          <Button
            size="sm"
            variant="primary"
            onClick={() => { setEditing(null); setAdding(true); }}
            disabled={!accountId}
          >
            + New {ruleKind === "auto_posts" ? "auto post" : "premade mass"}
          </Button>
        )}
      </header>

      <AccountChips accountId={accountId} onChange={setAccountId} />

      {/* Tab strip */}
      <div className="flex items-center gap-1 border-b border-border">
        {TABS.map((t) => {
          const active = tab === t.key;
          return (
            <button
              key={t.key}
              type="button"
              onClick={() => setTab(t.key)}
              className={cn(
                "px-3 py-1.5 text-sm rounded-t-md border-b-2 -mb-px transition-colors",
                active
                  ? "border-accent text-fg font-medium"
                  : "border-transparent text-fg-dim hover:text-fg",
              )}
            >
              {t.label}
            </button>
          );
        })}
      </div>

      {tab === "mass_funnels" && <MassFunnelsTab accountId={accountId} />}
      {/* Nudge tabs are fully self-contained (own account selector + roll-out). */}
      {tab === "nudge_online" && <NudgeOnlineTab />}
      {tab === "mass_nudge" && <MassNudgeTab />}
      {tab === "online_blast" && <OnlineBlastTab />}
      {tab === "instant_reply" && <WebhookDispatchTab accountId={accountId} />}
      {tab === "autoreply" && <AutoreplyTab accountId={accountId} />}
      {tab === "ai_seller" && <ScriptsTab accountId={accountId} />}
      {tab === "ai_upseller" && <UpsellerTab accountId={accountId} />}
      {tab === "tip_reward" && <TipRewardTab accountId={accountId} />}
      {tab === "ppv_library" && <PPVLibraryTab accountId={accountId} />}
      {tab === "vault_ai" && <VaultAiTab accountId={accountId} />}
      {tab === "unsend" && <UnsendCard accountId={accountId} />}
      {tab === "onboard" && <OnboardCard accountId={accountId} />}

      {ruleKind && (adding || editing) && accountId && (
        <ReadyMadeRuleEditor
          key={editing?.id ?? "new"}
          kind={ruleKind}
          accountId={accountId}
          editing={editing}
          onClose={closeEditor}
        />
      )}

      {ruleKind && rulesQ.isLoading && <div className="text-sm text-fg-dim">Loading…</div>}
      {ruleKind && rulesQ.isError && (
        <div className="text-sm text-err">
          {(rulesQ.error as Error)?.message || "Failed to load saved definitions"}
        </div>
      )}

      {ruleKind && !rulesQ.isLoading && !rulesQ.isError && (
        rules.length === 0 ? (
          <div className="text-sm text-fg-dim py-2">
            No saved {tab === "auto_posts" ? "auto posts" : "premade broadcasts"} yet.
            Click <span className="text-fg">+ New</span> to create one.
          </div>
        ) : (
          <ul className="divide-y divide-border">
            {rules.map((r) => {
              const ago = timeAgo(r.last_run?.started_at ?? null);
              const due = timeUntil(r.next_due_at);
              return (
                <li key={r.id} className="py-2.5 space-y-1.5">
                  <div className="flex flex-wrap items-center gap-3">
                    <input
                      type="checkbox"
                      checked={r.is_enabled}
                      onChange={() => toggle(r)}
                      disabled={updateM.isPending}
                      title={r.is_enabled ? "Disable" : "Enable"}
                      className="w-4 h-4 rounded accent-accent cursor-pointer shrink-0"
                    />
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-2 min-w-0">
                        <span className="text-sm font-medium text-fg truncate">{r.name}</span>
                        {r.has_pending_job && <Badge color="warn">queued</Badge>}
                        {!r.is_enabled && (
                          <span className="text-[11px] text-fg-dim/70">paused</span>
                        )}
                      </div>
                      <div className="text-[11px] text-fg-dim flex flex-wrap items-center gap-x-2 gap-y-0.5">
                        {/* Next-up line: queued > scheduled > paused/idle. */}
                        {r.has_pending_job ? (
                          <span className="text-warn">Next: queued (runs ~30s)</span>
                        ) : r.is_enabled && r.next_due_at ? (
                          <span title={r.next_due_at}>
                            Next: {due ?? fmtDateTime(r.next_due_at)}
                          </span>
                        ) : (
                          <span className="italic">Next: —</span>
                        )}
                        {r.last_run && (
                          <>
                            <span>·</span>
                            <StatusBadge status={r.last_run.status} />
                            {ago && <span title={r.last_run.started_at ?? ""}>{ago}</span>}
                          </>
                        )}
                      </div>
                    </div>

                    <div className="flex items-center gap-1.5 shrink-0">
                      <Button
                        size="sm"
                        variant="secondary"
                        onClick={() => runNow(r)}
                        disabled={runM.isPending}
                      >
                        Run now
                      </Button>
                      <Button
                        size="sm"
                        variant="ghost"
                        onClick={() => { setAdding(false); setEditing(r); }}
                      >
                        Edit
                      </Button>
                      <Button
                        size="sm"
                        variant="danger"
                        onClick={() => remove(r)}
                        disabled={deleteM.isPending}
                      >
                        Delete
                      </Button>
                    </div>
                  </div>

                  {/* Previous-runs strip (executor run log, filtered to this kind +
                   *  account). stats_json carries of_post_id + delete_at. */}
                  <PreviousRuns kind={r.kind} accountId={r.account_id} runs={runs} />

                  {r.last_run?.error_text && (
                    <div className="ml-7 text-[11px] text-err whitespace-pre-wrap break-words">
                      {r.last_run.error_text}
                    </div>
                  )}
                  {rowErr?.id === r.id && (
                    <div className="ml-7 text-[11px] text-err">{rowErr.msg}</div>
                  )}
                </li>
              );
            })}
          </ul>
        )
      )}
    </Card>
  );
}

// ── Previous-runs strip ────────────────────────────────────────────────

function parseStats(s: string | null): Record<string, unknown> | null {
  if (!s || s === "{}") return null;
  try {
    const o = JSON.parse(s);
    return o && typeof o === "object" && !Array.isArray(o) ? (o as Record<string, unknown>) : null;
  } catch {
    return null;
  }
}

/** Hours between two ISO timestamps, rounded — for "deleted after N hrs". */
function hoursBetween(a: string | null, b: string | null): number | null {
  if (!a || !b) return null;
  const ms = new Date(b).getTime() - new Date(a).getTime();
  if (!Number.isFinite(ms) || ms <= 0) return null;
  return Math.round((ms / 3_600_000) * 10) / 10;
}

/** One previous-run chip: status · when · post id / delete window. */
function runSummary(run: AutomationRunRow): string {
  const st = parseStats(run.stats_json);
  const bits: string[] = [];
  if (st) {
    if (st.dry_run) bits.push("dry run");
    const postId = st.of_post_id;
    if (typeof postId === "number") bits.push(`post #${postId}`);
    const delAt = typeof st.delete_at === "string" ? st.delete_at : null;
    const ttl = hoursBetween(run.started_at, delAt);
    if (ttl != null) bits.push(`auto-deletes after ${ttl} h`);
    if (typeof st.sent === "number" && st.sent > 0) bits.push(`sent ${st.sent}`);
    if (typeof st.remaining === "number" && st.remaining > 0) bits.push(`${st.remaining} left`);
    if (typeof st.reason === "string" && st.status === "skipped") bits.push(`skipped (${st.reason})`);
  }
  return bits.join(" · ");
}

function PreviousRuns({
  kind,
  accountId,
  runs,
}: {
  kind: string;
  accountId: string;
  runs: AutomationRunRow[];
}) {
  const mine = useMemo(() => {
    // Run rows carry no rule_id, only (kind, account_id) — so we can't pin a run
    // to ONE rule. Account-scoped rows of this kind clearly belong here; the
    // global (account_id == null) rows are ambiguous (any account, any same-kind
    // rule), so only fall back to them when this account has no scoped runs of
    // the kind. That stops a global run from being claimed under every account.
    const ofKind = runs.filter((r) => r.kind === kind);
    const scoped = ofKind.filter((r) => r.account_id === accountId);
    const pool = scoped.length > 0 ? scoped : ofKind.filter((r) => r.account_id == null);
    return pool.slice(0, 4);
  }, [runs, kind, accountId]);
  if (mine.length === 0) {
    return <div className="ml-7 text-[11px] text-fg-dim/70 italic">No previous runs.</div>;
  }
  return (
    <ul className="ml-7 space-y-0.5">
      {mine.map((run) => {
        const ago = timeAgo(run.started_at);
        const extra = runSummary(run);
        return (
          <li key={run.id} className="text-[11px] text-fg-dim flex flex-wrap items-center gap-1.5">
            <StatusBadge status={run.status} />
            <span title={run.started_at ?? ""}>{ago ?? fmtDateTime(run.started_at)}</span>
            {extra && <span className="text-fg-dim/80">· {extra}</span>}
          </li>
        );
      })}
    </ul>
  );
}

// ── Inline editor (create / edit a saved ready-made definition) ─────────

/** One-line summary of a premade payload for the compose read-out. */
function summarize(payload: Record<string, unknown>): string | null {
  const posts = Array.isArray(payload.posts) ? payload.posts : null;
  const messages = Array.isArray(payload.messages) ? payload.messages : null;
  const arr = posts ?? messages;
  if (!arr || arr.length === 0) return null;
  const noun = posts ? "post" : "message";
  const bits = [`${arr.length} ${noun}${arr.length === 1 ? "" : "s"}`];
  const rc = payload.resend_count;
  if (typeof rc === "number" && rc > 0) bits.push(`reposts ×${rc}`);
  return bits.join(" · ");
}

function ReadyMadeRuleEditor({
  kind,
  accountId,
  editing,
  onClose,
}: {
  kind: RuleKind;
  accountId: string;
  editing: AutomationRule | null;
  onClose: () => void;
}) {
  const isEdit = editing !== null;
  const createM = useCreateRule(accountId);
  const updateM = useUpdateRule(accountId);

  const [name, setName] = useState(editing?.name ?? "");
  const init = splitInterval(editing?.every_seconds ?? 24 * 3600);
  const [intervalValue, setIntervalValue] = useState<number>(init.value);
  const [intervalUnit, setIntervalUnit] = useState<Unit>(init.unit);
  const [enabled, setEnabled] = useState<boolean>(editing?.is_enabled ?? true);
  const [dryRun, setDryRun] = useState<boolean>(
    Boolean((editing?.payload as Record<string, unknown> | undefined)?.dry_run),
  );

  // The composed payload — seeded from the rule being edited so saving WITHOUT
  // re-composing preserves it. The composer only overwrites it on "Use in rule".
  const [composed, setComposed] = useState<Record<string, unknown>>(() => {
    const p = { ...((editing?.payload as Record<string, unknown>) ?? {}) };
    delete p.dry_run; // tracked by the toggle, re-merged on save
    return p;
  });
  const [composerOpen, setComposerOpen] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  // Raw-JSON escape hatch (mirrors RuleEditor): hand-edit / paste the whole
  // `{posts|messages:[…]}` payload directly — the composer forces you to PICK
  // every media id, so this is the only way to paste a big media_files pool or
  // tweak a field the form doesn't expose. `dry_run` stays owned by the toggle.
  const [rawMode, setRawMode] = useState(false);
  const [rawText, setRawText] = useState("");

  const everySeconds = Math.round(intervalValue * UNIT_S[intervalUnit]);
  const busy = createM.isPending || updateM.isPending;
  const summary = summarize(composed);

  // Scroll the editor into view on open — it mounts above a long saved-rule
  // list, so clicking "Edit" far down otherwise leaves it off-screen. Remounts
  // per rule (key), so this fires on every Edit.
  const headerRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    headerRef.current?.scrollIntoView({ behavior: "smooth", block: "center" });
  }, []);

  /** The composed payload from whichever mode is active — raw text is parsed and
   *  required to be an object; `dry_run` is stripped (the toggle re-merges it on
   *  save). Throws with a clear message on bad JSON. */
  function currentComposed(): Record<string, unknown> {
    if (!rawMode) return composed;
    const t = rawText.trim();
    if (!t) return {};
    let parsed: unknown;
    try {
      parsed = JSON.parse(t);
    } catch (e) {
      throw new Error(`Payload is not valid JSON: ${(e as Error).message}`);
    }
    if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
      throw new Error(
        `Payload must be a JSON object, e.g. { "${kind === "auto_posts" ? "posts" : "messages"}": [ … ] }`,
      );
    }
    const p = { ...(parsed as Record<string, unknown>) };
    delete p.dry_run; // owned by the Dry-run toggle; re-merged on save
    return p;
  }

  /** Enter raw mode: seed the textarea from the current composed payload. */
  function enterRaw() {
    setRawText(JSON.stringify(composed, null, 2));
    setErr(null);
    setRawMode(true);
  }
  /** Leave raw mode: parse the textarea back into `composed` (keeps the two
   *  views in sync so "Re-compose" opens pre-filled with the hand edits). */
  function exitRaw() {
    let parsed: Record<string, unknown>;
    try {
      parsed = currentComposed();
    } catch (e) {
      setErr((e as Error).message);
      return;
    }
    setComposed(parsed);
    setRawMode(false);
    setErr(null);
  }

  async function save() {
    setErr(null);
    if (everySeconds < 30) {
      setErr("Interval must be at least 30 seconds.");
      return;
    }
    let base: Record<string, unknown>;
    try {
      base = currentComposed();
    } catch (e) {
      setErr((e as Error).message);
      return;
    }
    const arr = kind === "auto_posts" ? base.posts : base.messages;
    if (!Array.isArray(arr) || arr.length === 0) {
      setErr(`Compose at least one ${kind === "auto_posts" ? "post" : "message"} first.`);
      return;
    }
    const payload: Record<string, unknown> = { ...base };
    if (dryRun) payload.dry_run = true;
    try {
      if (isEdit && editing) {
        await updateM.mutateAsync({
          id: editing.id,
          name: name.trim() || editing.name,
          every_seconds: everySeconds,
          payload,
          is_enabled: enabled,
        });
      } else {
        await createM.mutateAsync({
          account_id: accountId,
          kind,
          name: name.trim() || undefined,
          every_seconds: everySeconds,
          payload,
          is_enabled: enabled,
        });
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
          {isEdit ? `Edit · ${kind}` : `New ${kind === "auto_posts" ? "auto post" : "premade mass"}`}
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
        <label className="block space-y-1">
          <span className="text-[11px] uppercase tracking-wide text-fg-dim">Name (optional)</span>
          <Input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder={kind === "auto_posts" ? "Auto posts" : "Premade mass"}
          />
        </label>

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
              <option value="minutes">minutes</option>
              <option value="hours">hours</option>
            </select>
          </div>
        </label>

        <label className="flex items-center gap-2 pt-1">
          <input
            type="checkbox"
            checked={enabled}
            onChange={(e) => setEnabled(e.target.checked)}
            className="w-4 h-4 rounded accent-accent cursor-pointer"
          />
          <span className="text-sm text-fg">Enabled</span>
        </label>

        <label className="flex items-center gap-2 pt-1">
          <input
            type="checkbox"
            checked={dryRun}
            onChange={(e) => setDryRun(e.target.checked)}
            className="w-4 h-4 rounded accent-accent cursor-pointer"
          />
          <span className="text-sm text-fg">Dry run</span>
          <span className="text-[11px] text-fg-dim">(plan only)</span>
        </label>
      </div>

      {/* Compose the payload with the rich PremadeForm (return-payload mode), or
       *  hand-edit the raw JSON directly (paste a big media_files pool, tweak a
       *  field the form doesn't expose). Same escape hatch RuleEditor has. */}
      <div className="rounded-lg border border-border bg-bg-elev-1 p-3 space-y-2">
        <div className="flex items-center justify-between gap-2">
          <span className="text-[11px] uppercase tracking-wide text-fg-dim">
            {kind === "auto_posts" ? "Posts" : "Messages"}
          </span>
          <button
            type="button"
            onClick={() => (rawMode ? exitRaw() : enterRaw())}
            className="text-[11px] text-accent hover:underline"
          >
            {rawMode ? "← Composer" : "Edit raw JSON →"}
          </button>
        </div>

        {rawMode ? (
          <>
            <textarea
              value={rawText}
              onChange={(e) => {
                setRawText(e.target.value);
                if (err) setErr(null);
              }}
              rows={12}
              spellCheck={false}
              className="w-full rounded-lg bg-bg border border-border text-[11px] font-mono px-2 py-1.5 text-fg focus:outline-none focus:ring-2 focus:ring-accent/40"
              placeholder={`{ "${kind === "auto_posts" ? "posts" : "messages"}": [ … ] }`}
            />
            <div className="text-[10px] text-fg-dim leading-relaxed">
              Edit the whole <code className="text-accent">
                {kind === "auto_posts" ? "{ posts: [ … ] }" : "{ messages: [ … ] }"}
              </code>{" "}
              payload by hand — e.g. paste a <code className="text-accent">media_files</code> list
              and set <code className="text-accent">media_count</code>. The Dry-run toggle above
              owns <code className="text-accent">dry_run</code>, so leave it out here.
            </div>
          </>
        ) : (
          <>
            <div className="text-[11px] text-fg-dim">
              {summary ?? `No content yet — build this ${kind === "auto_posts" ? "feed" : "broadcast"} with the composer.`}
            </div>
            <Button
              size="sm"
              variant={summary ? "secondary" : "primary"}
              onClick={() => setComposerOpen(true)}
            >
              {summary ? "Re-compose" : "Compose…"}
            </Button>
            {summary && (
              <details className="pt-1">
                <summary className="text-[11px] text-accent cursor-pointer select-none">
                  View JSON
                </summary>
                <pre className="mt-1 text-[11px] font-mono text-fg-dim bg-bg border border-border rounded-md p-2 overflow-x-auto whitespace-pre-wrap break-words max-h-64 overflow-y-auto">
                  {JSON.stringify(composed, null, 2)}
                </pre>
              </details>
            )}
          </>
        )}
      </div>

      {err && <div className="text-xs text-err whitespace-pre-wrap">{err}</div>}

      <div className="flex items-center gap-2">
        <Button size="sm" variant="primary" onClick={save} disabled={busy}>
          {busy ? "Saving…" : isEdit ? "Save changes" : "Create"}
        </Button>
        <Button size="sm" variant="ghost" onClick={onClose} disabled={busy}>
          Cancel
        </Button>
        <span className="text-[11px] text-fg-dim ml-auto tabular-nums">= every {everySeconds}s</span>
      </div>

      {composerOpen && (
        <ComposeModal
          kind={kind}
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

/** Modal wrapper around PremadeForm in return-payload (compose) mode. */
function ComposeModal({
  kind,
  accountId,
  initialPayload,
  onClose,
  onComposePayload,
}: {
  kind: RuleKind;
  accountId: string;
  initialPayload?: Record<string, unknown>;
  onClose: () => void;
  onComposePayload: (payload: Record<string, unknown>) => void;
}) {
  const title =
    kind === "auto_posts" ? "Compose auto posts" : "Compose premade mass";
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

// ── Unsend action card ──────────────────────────────────────────────────

/** Blank/zero/garbage → undefined; a positive number → the number. Keeps an
 *  unset window out of the policy so the sweep ignores that message class. */
function posHours(s: string): number | undefined {
  const n = Number(s);
  return s.trim() !== "" && Number.isFinite(n) && n > 0 ? n : undefined;
}

/** One-shot unsend sweep. No saved rule, no raw JSON — two number fields →
 *  enqueue {kind:"unsend_messages", payload:{policy:{text_hours, media_hours}}}.
 *  The backend (service/automations/unsend_messages.py) sweeps our OWN outbound
 *  bubbles inside OF's 24h edit window: text-only older than text_hours (always)
 *  and media older than media_hours (opt-in — blank ⇒ media is left alone). */
function UnsendCard({ accountId }: { accountId: string | null }) {
  const { current: currentEmployee } = useEmployee();
  const [textHours, setTextHours] = useState("");
  const [mediaHours, setMediaHours] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [okMsg, setOkMsg] = useState<string | null>(null);

  async function enqueue() {
    setError(null);
    setOkMsg(null);
    if (!accountId) {
      setError("Pick an account first.");
      return;
    }
    const text_hours = posHours(textHours);
    const media_hours = posHours(mediaHours);
    if (text_hours === undefined && media_hours === undefined) {
      setError("Set at least one window (free text and/or media hours).");
      return;
    }
    const policy: Record<string, number> = {};
    if (text_hours !== undefined) policy.text_hours = text_hours;
    if (media_hours !== undefined) policy.media_hours = media_hours;

    if (!window.confirm(
      `Unsend your own outbound bubbles older than these windows on this account?` +
      `\n\nThis can't be undone.`,
    )) return;

    setBusy(true);
    try {
      const resp = await relay.post<{ enqueued_job_id: number }>(
        "/admin/automation/enqueue",
        { account_id: accountId, kind: "unsend_messages", payload: { policy } },
        { accountId, employeeId: currentEmployee?.id },
      );
      const bits: string[] = [];
      if (text_hours !== undefined) bits.push(`free text > ${text_hours} h`);
      if (media_hours !== undefined) bits.push(`media > ${media_hours} h`);
      setOkMsg(`Sweep enqueued (job #${resp.enqueued_job_id}) — ${bits.join(" + ")}. Runs within ~30s.`);
    } catch (e) {
      setError((e as Error)?.message || "Enqueue failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-3">
      <h3 className="text-sm font-medium text-fg">Unsend my own chat messages</h3>
      <p className="text-xs text-fg-dim">
        Removes YOUR outbound bubbles in each chat within OF&apos;s 24-hour edit
        window. Not for mass broadcasts (those auto-unsend via the broadcast&apos;s
        own timer) or posts (auto-delete via Auto Posts). Free text-only older than
        the first window, and media older than the second (leave a field blank to
        skip that class). Enqueues a one-shot job — nothing is saved or recurring.
      </p>

      <div className="grid grid-cols-2 gap-3 max-w-md">
        <UnsendNumField
          label="Unsend free text older than"
          suffix="hrs · blank = skip"
          value={textHours}
          onChange={setTextHours}
          placeholder="e.g. 9"
        />
        <UnsendNumField
          label="Unsend media older than"
          suffix="hrs · blank = skip"
          value={mediaHours}
          onChange={setMediaHours}
          placeholder="e.g. 12"
        />
      </div>

      {okMsg && (
        <div className="text-[11px] text-ok border border-ok/30 bg-ok/5 rounded-md px-3 py-2">
          {okMsg}
        </div>
      )}

      <div className="flex items-center gap-2">
        {error && <span className="text-err text-[11px] mr-auto">{error}</span>}
        <Button
          size="sm"
          variant="primary"
          onClick={enqueue}
          disabled={busy || !accountId}
          className={error ? "" : "ml-auto"}
        >
          {busy ? "Enqueuing…" : "Run unsend sweep"}
        </Button>
      </div>
    </div>
  );
}

/** Compact labelled numeric input — blank = the window isn't applied. */
/** Onboard pre-AI fans — fire-once process_old_fans over the N most-recent fans
 *  (W9-D). Composes gen_info + apply_profiles and flags them; ZERO sends. */
function OnboardCard({ accountId }: { accountId: string | null }) {
  const { current: currentEmployee } = useEmployee();
  const [n, setN] = useState("100");
  const [by, setBy] = useState<"subscribed" | "messaged">("subscribed");
  const [flagOnly, setFlagOnly] = useState(false);
  const [pushToOf, setPushToOf] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [okMsg, setOkMsg] = useState<string | null>(null);

  async function enqueue() {
    setError(null);
    setOkMsg(null);
    if (!accountId) { setError("Pick an account first."); return; }
    const recent_limit = Math.floor(Number(n));
    if (!Number.isFinite(recent_limit) || recent_limit < 1) {
      setError("Enter how many recent fans to onboard (≥ 1).");
      return;
    }
    if (!window.confirm(
      `Onboard the ${recent_limit} most-recent fans by ${by}` +
      `${flagOnly ? " (flag only — no profiling)"
                  : ` (profiles + applies nicknames/notes${pushToOf ? ", pushes to OnlyFans" : ""} — spends LLM)`}?`,
    )) return;

    setBusy(true);
    try {
      const payload: Record<string, unknown> = { recent_limit, recent_by: by };
      if (flagOnly) payload.flag_only = true;
      else if (pushToOf) payload.push_to_of = true;
      const resp = await relay.post<{ enqueued_job_id: number }>(
        "/admin/automation/enqueue",
        { account_id: accountId, kind: "process_old_fans", payload },
        { accountId, employeeId: currentEmployee?.id },
      );
      setOkMsg(`Onboard enqueued (job #${resp.enqueued_job_id}) — ${recent_limit} most-recent by ${by}. Runs within ~30s.`);
    } catch (e) {
      setError((e as Error)?.message || "Enqueue failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-3">
      <h3 className="text-sm font-medium text-fg">Onboard pre-AI fans</h3>
      <p className="text-xs text-fg-dim">
        One-shot pass over your most-recent fans: flags them out of the
        &ldquo;old&rdquo; bucket, builds their profile (gen_info) and applies the
        nickname/note (apply_profiles). <b>Zero messages are sent.</b> Use it once to
        catch fans who subscribed before the AI was running.
      </p>

      <div className="flex flex-wrap items-end gap-3 max-w-xl">
        <UnsendNumField
          label="How many recent fans"
          suffix="count"
          value={n}
          onChange={setN}
          placeholder="100"
        />
        <label className="flex flex-col gap-1 text-[11px] text-fg-dim">
          <span className="leading-tight">Order by</span>
          <select
            value={by}
            onChange={(e) => setBy(e.target.value as "subscribed" | "messaged")}
            className="rounded-lg bg-bg-elev-1 border border-border text-sm px-2 py-1.5 text-fg focus:outline-none focus:ring-2 focus:ring-accent/40"
          >
            <option value="subscribed">Newest subscribers</option>
            <option value="messaged">Most recently messaged</option>
          </select>
        </label>
        <label className="flex items-center gap-1.5 text-xs pb-2">
          <input type="checkbox" checked={flagOnly} onChange={(e) => setFlagOnly(e.target.checked)} />
          Flag only (no profiling)
        </label>
        <label className="flex items-center gap-1.5 text-xs pb-2">
          <input
            type="checkbox"
            checked={pushToOf}
            disabled={flagOnly}
            onChange={(e) => setPushToOf(e.target.checked)}
          />
          Push nicknames/notes to OnlyFans
        </label>
      </div>

      {okMsg && (
        <div className="text-[11px] text-ok border border-ok/30 bg-ok/5 rounded-md px-3 py-2">
          {okMsg}
        </div>
      )}

      <div className="flex items-center gap-2">
        {error && <span className="text-err text-[11px] mr-auto">{error}</span>}
        <Button
          size="sm"
          variant="primary"
          onClick={enqueue}
          disabled={busy || !accountId}
          className={error ? "" : "ml-auto"}
        >
          {busy ? "Enqueuing…" : "Onboard recent fans"}
        </Button>
      </div>
    </div>
  );
}

function UnsendNumField({
  label, suffix, value, onChange, placeholder,
}: {
  label: string;
  suffix: string;
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
}) {
  return (
    <label className="flex flex-col gap-1 text-[11px] text-fg-dim">
      <span className="leading-tight">{label} <span className="opacity-60">({suffix})</span></span>
      <Input
        type="text"
        inputMode="numeric"
        value={value}
        onChange={(e) => onChange(e.target.value.replace(/[^0-9.]/g, ""))}
        placeholder={placeholder}
      />
    </label>
  );
}
