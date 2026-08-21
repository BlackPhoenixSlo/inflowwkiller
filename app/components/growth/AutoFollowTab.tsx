"use client";

/**
 * AutoFollowTab — Growth → "Auto-follow / Auto-like".
 *
 * Trigger OnlyFans re-engagement by LIKING fans' recent messages or by
 * FOLLOWING fans back (both are notification-only nudges; never a DM). Saved
 * through the generic rules API with kind "auto_follow". Defaults to dry-run
 * so nothing acts until the operator confirms + enables.
 *
 *   action     like_messages · like_posts · follow · ping (all real; follow/ping money-gated)
 *   targets    recently-expired (follow only) · recently-active · a Smart List
 *              (ping derives its own pool: fans quiet ≥ quiet_days)
 *   daily_cap  max actions per run
 *
 * Follow only ever targets FREE fan profiles — the engine price-checks every
 * fan and skips paid pages, so it can never spend money. "Specific fan ids"
 * was dropped from the UI (operators pasted ids that went stale); the engine
 * still honors targets.source="fan_ids" for API callers.
 */

import { useEffect, useMemo, useState } from "react";

import { Button, Card, Input } from "@/components/ui/primitives";
import { RunStats, errMsg, isDryRun } from "@/components/growth/_bits";
import {
  useAutomationRules, useCreateRule, useUpdateRule, useRunRuleNow,
  type AutomationRule,
} from "@/hooks/useAutomations";
import { useSmartLists } from "@/hooks/useSmartLists";

const SELECT_CLS =
  "w-full bg-bg border border-border rounded-lg px-3 py-2 text-sm focus:outline-none focus:border-accent";

type Action = "like_messages" | "like_posts" | "follow" | "ping";
type Source = "expired" | "recent_active" | "smart_list";

// The one table that owns the action↔source pairing: which pools an action may
// target (first entry = its default). ruleToForm, the action-switch snap, and
// the Target dropdown all derive from it, so a new action/source lands in one
// place. "expired" is follow-only (OF's lapsed-subscriber list — nothing to
// like there); ping/like_posts derive their own pools and show no Target.
const SOURCES_BY_ACTION: Record<Action, Source[]> = {
  follow: ["expired", "recent_active", "smart_list"],
  like_messages: ["recent_active", "smart_list"],
  like_posts: [],
  ping: [],
};
const SOURCE_LABELS: Record<Source, string> = {
  expired: "Recently-expired fans (win-back)",
  recent_active: "Recently-active fans",
  smart_list: "A Smart List",
};

/** Valid source for `action`, keeping `source` when allowed. Legacy values
 *  ("fan_ids", dropped from the UI) snap to the action's default pool. */
function sourceFor(action: Action, source?: string): Source {
  const valid = SOURCES_BY_ACTION[action];
  return valid.includes(source as Source) ? (source as Source) : (valid[0] ?? "recent_active");
}

interface Form {
  enabled: boolean;
  action: Action;
  source: Source;
  days: number;
  smartListId: number | null;
  postIds: string;      // comma-separated (like_posts)
  quietDays: number;    // ping: fan counts as quiet after N days silent
  pingGapDays: number;  // ping: per-fan cooldown between pings
  dailyCap: number;
  dryRun: boolean;
  everyMinutes: number;
}

function ruleToForm(rule: AutomationRule | null): Form {
  const p = rule?.payload ?? {};
  const t = (p.targets ?? {}) as Record<string, unknown>;
  const action = (p.action as Action) || "like_messages";
  const every = rule?.every_seconds ? Math.max(1, Math.round(rule.every_seconds / 60)) : 240;
  return {
    enabled: rule?.is_enabled ?? false,
    action,
    source: sourceFor(action, t.source as string | undefined),
    days: typeof t.days === "number" ? t.days : 7,
    smartListId: typeof t.smart_list_id === "number" ? t.smart_list_id : null,
    postIds: Array.isArray(t.post_ids) ? (t.post_ids as number[]).join(", ") : "",
    quietDays: typeof p.quiet_days === "number" ? p.quiet_days : 7,
    pingGapDays: typeof p.min_days_between_pings === "number" ? p.min_days_between_pings : 14,
    dailyCap: typeof p.daily_cap === "number" ? p.daily_cap : 50,
    dryRun: p.dry_run !== false,          // default TRUE
    everyMinutes: every,
  };
}

function parseIds(s: string): number[] {
  return s.split(/[,\s]+/).map((x) => Number(x.trim())).filter((n) => Number.isFinite(n) && n > 0);
}

export default function AutoFollowTab({ accountId }: { accountId: string | null }) {
  const rulesQ = useAutomationRules(accountId);
  const rule = (rulesQ.data ?? []).find((r) => r.kind === "auto_follow") ?? null;
  const smartListsQ = useSmartLists(accountId);

  const createM = useCreateRule(accountId);
  const updateM = useUpdateRule(accountId);
  const runM = useRunRuleNow(accountId);
  const busy = createM.isPending || updateM.isPending;

  const [form, setForm] = useState<Form>(ruleToForm(null));
  const [err, setErr] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);

  useEffect(() => { setForm(ruleToForm(rule)); }, [rule?.id]); // eslint-disable-line react-hooks/exhaustive-deps
  const set = <K extends keyof Form>(k: K, v: Form[K]) => setForm((f) => ({ ...f, [k]: v }));

  const payload = useMemo(() => {
    const p: Record<string, unknown> = {
      action: form.action, daily_cap: form.dailyCap, dry_run: form.dryRun,
    };
    if (form.action === "ping") {
      p.quiet_days = form.quietDays;
      p.min_days_between_pings = form.pingGapDays;
    } else if (form.action === "like_posts") {
      p.targets = { post_ids: parseIds(form.postIds) };
    } else if (form.source === "smart_list") {
      p.targets = { source: "smart_list", smart_list_id: form.smartListId };
    } else if (form.source === "expired") {
      p.targets = { source: "expired" };
    } else {
      p.targets = { source: "recent_active", days: form.days };
    }
    return p;
  }, [form]);

  async function save() {
    setErr(null); setMsg(null);
    if (!accountId) return;
    const every_seconds = Math.max(60, Math.round(form.everyMinutes) * 60);
    try {
      if (rule) {
        await updateM.mutateAsync({ id: rule.id, every_seconds, payload, is_enabled: form.enabled });
      } else {
        await createM.mutateAsync({
          account_id: accountId, kind: "auto_follow", name: "Auto-follow / Auto-like",
          every_seconds, payload, is_enabled: form.enabled,
        });
      }
      setMsg("✓ Saved.");
    } catch (e) { setErr(errMsg(e, "Save failed")); }
  }

  async function runNow() {
    if (!rule) { setErr("Save the automation first."); return; }
    setErr(null); setMsg(null);
    // "Run now" executes the SAVED rule (rule.id), so the confirm + toast must
    // key off the SAVED payload's dry_run — NOT the (possibly unsaved) form,
    // which would otherwise say "nothing sent" while real likes fire.
    const savedDryRun = isDryRun(rule);
    const savedAction = rule.payload.action;
    const verb =
      savedAction === "ping" ? "UNFOLLOW + RE-FOLLOW real fans"
      : savedAction === "follow" ? "FOLLOW real fans"
      : "LIKE real messages";
    if (!savedDryRun && !window.confirm(
      `Run now uses the LAST SAVED config, which has dry-run OFF — this will ${verb} on OnlyFans. Continue?`,
    )) return;
    try {
      await runM.mutateAsync(rule.id);
      setMsg(savedDryRun
        ? "✓ Dry run queued (last saved config) — plans only, nothing sent."
        : "✓ Running now (last saved config) — actions land within ~30s.");
    } catch (e) { setErr(errMsg(e, "Run failed")); }
  }

  const last = rule?.last_run;

  return (
    <div className="space-y-5 max-w-2xl">
      <header>
        <h2 className="text-lg font-semibold">Auto-follow / Auto-like</h2>
        <p className="text-sm text-fg-dim">
          Trigger OnlyFans re-engagement by <b>liking fans’ recent messages</b>,{" "}
          <b>following fans back</b>, or <b>re-follow pinging</b> fans who went
          quiet — notification-only nudges, never a DM. Follow/ping{" "}
          <b>only ever touch free profiles</b> (every fan is price-checked; paid
          pages are skipped, so it can’t spend). Runs on a timer; <b>dry-run is on
          by default</b> so nothing acts until you confirm.
        </p>
      </header>

      <Card className="p-4 space-y-4">
        <div className="flex flex-wrap gap-4">
          <label className="flex items-center gap-2 text-sm cursor-pointer">
            <input type="checkbox" checked={form.enabled} onChange={(e) => set("enabled", e.target.checked)} />
            Enabled
          </label>
          <label className="flex items-center gap-2 text-sm cursor-pointer">
            <input type="checkbox" checked={form.dryRun} onChange={(e) => set("dryRun", e.target.checked)} />
            Dry run <span className="text-[11px] text-fg-dim">(plan only — never acts; may read OnlyFans to list the pool)</span>
          </label>
        </div>

        <div className="grid gap-3 sm:grid-cols-2">
          <label className="block space-y-1">
            <span className="text-[11px] uppercase tracking-wide text-fg-dim">Action</span>
            {/* "Like posts by id" is gone from the UI: pasting post ids is not
              *  something an operator can meaningfully do, and the only posts
              *  she could like are her own, which notifies nobody. The engine
              *  still supports the action for API callers. */}
            <select
              className={SELECT_CLS} value={form.action}
              onChange={(e) => {
                const action = e.target.value as Action;
                setForm((f) => ({ ...f, action, source: sourceFor(action, f.source) }));
              }}
            >
              <option value="like_messages">Like latest message (re-engage)</option>
              <option value="follow">Follow fans back (win-back)</option>
              <option value="ping">Re-follow ping (quiet fans)</option>
            </select>
          </label>
          <label className="block space-y-1">
            <span className="text-[11px] uppercase tracking-wide text-fg-dim">Max actions / run</span>
            <Input type="number" min={0} value={form.dailyCap}
              onChange={(e) => set("dailyCap", Number(e.target.value))} />
          </label>
        </div>

        {form.action === "ping" ? (
          <div className="space-y-2">
            <div className="grid gap-3 sm:grid-cols-2">
              <label className="block space-y-1">
                <span className="text-[11px] uppercase tracking-wide text-fg-dim">Quiet after (days)</span>
                <Input type="number" min={1} value={form.quietDays}
                  onChange={(e) => set("quietDays", Number(e.target.value))} />
              </label>
              <label className="block space-y-1">
                <span className="text-[11px] uppercase tracking-wide text-fg-dim">Min days between pings (per fan)</span>
                <Input type="number" min={1} value={form.pingGapDays}
                  onChange={(e) => set("pingGapDays", Number(e.target.value))} />
              </label>
            </div>
            <p className="text-xs text-fg-dim">
              A fan who chatted before but has been silent this long gets an
              unfollow + instant re-follow, so OnlyFans pings them with
              “started following you”. Each fan is pinged at most once per
              cooldown window; free profiles only.
            </p>
          </div>
        ) : form.action === "like_posts" ? (
          <label className="block space-y-1">
            <span className="text-[11px] uppercase tracking-wide text-fg-dim">Post ids (comma-separated)</span>
            <Input value={form.postIds} onChange={(e) => set("postIds", e.target.value)} placeholder="123456, 234567" />
          </label>
        ) : (
          <div className="grid gap-3 sm:grid-cols-2">
            <label className="block space-y-1">
              <span className="text-[11px] uppercase tracking-wide text-fg-dim">Target</span>
              <select className={SELECT_CLS} value={form.source} onChange={(e) => set("source", e.target.value as Source)}>
                {SOURCES_BY_ACTION[form.action].map((s) => (
                  <option key={s} value={s}>{SOURCE_LABELS[s]}</option>
                ))}
              </select>
            </label>
            {form.source === "expired" && (
              <div className="text-xs text-fg-dim self-end pb-2.5">
                OnlyFans’ own lapsed-subscriber list — the fans worth winning back.
              </div>
            )}
            {form.source === "recent_active" && (
              <label className="block space-y-1">
                <span className="text-[11px] uppercase tracking-wide text-fg-dim">Active within (days)</span>
                <Input type="number" min={1} value={form.days} onChange={(e) => set("days", Number(e.target.value))} />
              </label>
            )}
            {form.source === "smart_list" && (
              <label className="block space-y-1">
                <span className="text-[11px] uppercase tracking-wide text-fg-dim">Smart List</span>
                <select className={SELECT_CLS} value={form.smartListId ?? ""}
                  onChange={(e) => set("smartListId", e.target.value ? Number(e.target.value) : null)}>
                  <option value="">— pick a segment —</option>
                  {(smartListsQ.data ?? []).map((l) => <option key={l.id} value={l.id}>{l.name}</option>)}
                </select>
              </label>
            )}
          </div>
        )}

        <label className="block space-y-1 max-w-xs">
          <span className="text-[11px] uppercase tracking-wide text-fg-dim">Run every (minutes)</span>
          <Input type="number" min={1} value={form.everyMinutes}
            onChange={(e) => set("everyMinutes", Number(e.target.value))} />
        </label>

        {err && <div className="text-sm text-err">{err}</div>}

        <div className="flex items-center gap-2 pt-1">
          <Button size="sm" onClick={save} disabled={busy || !accountId}>
            {rule ? "Save changes" : "Create automation"}
          </Button>
          {rule && (
            <Button size="sm" variant="ghost" onClick={runNow} disabled={runM.isPending}>
              {runM.isPending ? "Running…" : "Run now"}
            </Button>
          )}
        </div>

        {msg && <div className="text-sm text-accent border-t border-border pt-3">{msg}</div>}

        {rule && last && (
          <div className="text-xs text-fg-dim border-t border-border pt-3">
            Last run: <span className="text-fg">{last.status}</span>
            <RunStats stats={last.stats} />
            {last.started_at && ` · ${new Date(last.started_at).toLocaleString()}`}
            {last.error_text && <span className="text-err"> · {last.error_text}</span>}
          </div>
        )}
      </Card>
    </div>
  );
}
