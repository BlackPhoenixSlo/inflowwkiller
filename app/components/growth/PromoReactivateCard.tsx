"use client";

/**
 * PromoReactivateCard — settings for the automation that re-creates finished
 * promos (kind: "promo_reactivate").
 *
 * Sits at the bottom of the Promotion tab, directly under the campaigns it acts
 * on: the per-campaign "Keep running" switches are the opt-in, this is the
 * engine config (schedule, dry-run, cycle cap).
 */

import { useEffect, useState } from "react";

import { Button, Card } from "@/components/ui/primitives";
import { Field, NumInput, RunStats, errMsg, isDryRun } from "@/components/growth/_bits";
import { usePromotions } from "@/hooks/useGrowth";
import {
  useAutomationRules, useCreateRule, useUpdateRule, useRunRuleNow,
  type AutomationRule,
} from "@/hooks/useAutomations";

interface RForm {
  enabled: boolean;
  dryRun: boolean;
  everyMinutes: number;
  maxCycles: number;   // 0 = unlimited
}

function ruleToForm(rule: AutomationRule | null): RForm {
  const p = rule?.payload ?? {};
  return {
    enabled: rule?.is_enabled ?? false,
    dryRun: isDryRun(rule),          // default TRUE
    everyMinutes: rule?.every_seconds ? Math.max(1, Math.round(rule.every_seconds / 60)) : 60,
    maxCycles: typeof p.max_reactivations === "number" ? p.max_reactivations : 0,
  };
}

export default function PromoReactivateCard({ accountId }: { accountId: string | null }) {
  const promosQ = usePromotions(accountId);
  const armed = (promosQ.data?.campaigns ?? []).filter((c) => c.auto_reactivate).length;

  const rulesQ = useAutomationRules(accountId);
  const rule = (rulesQ.data ?? []).find((r) => r.kind === "promo_reactivate") ?? null;
  const createM = useCreateRule(accountId);
  const updateM = useUpdateRule(accountId);
  const runM = useRunRuleNow(accountId);
  const busy = createM.isPending || updateM.isPending;

  const [form, setForm] = useState<RForm>(ruleToForm(null));
  const [err, setErr] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);

  useEffect(() => { setForm(ruleToForm(rule)); }, [rule?.id]); // eslint-disable-line react-hooks/exhaustive-deps
  const set = <K extends keyof RForm>(k: K, v: RForm[K]) => setForm((f) => ({ ...f, [k]: v }));

  async function save() {
    setErr(null); setMsg(null);
    if (!accountId) return;
    const every_seconds = Math.max(60, Math.round(form.everyMinutes) * 60);
    const payload = { max_reactivations: form.maxCycles, dry_run: form.dryRun };
    try {
      if (rule) {
        await updateM.mutateAsync({ id: rule.id, every_seconds, payload, is_enabled: form.enabled });
      } else {
        await createM.mutateAsync({
          account_id: accountId, kind: "promo_reactivate", name: "Keep promos running",
          every_seconds, payload, is_enabled: form.enabled,
        });
      }
      setMsg("✓ Saved.");
    } catch (e) { setErr(errMsg(e, "Save failed")); }
  }

  async function runNow() {
    if (!rule) { setErr("Save the automation first."); return; }
    setErr(null); setMsg(null);
    // Run now executes the SAVED rule — key the warning off the saved payload.
    const savedDryRun = isDryRun(rule);
    if (!savedDryRun && !window.confirm(
      "Check now uses the LAST SAVED config, which has dry-run OFF — any finished campaign marked “Keep running” will be re-created on OnlyFans. Continue?",
    )) return;
    try {
      await runM.mutateAsync(rule.id);
      setMsg(savedDryRun
        ? "✓ Dry run queued — it will report what it would re-create, and change nothing."
        : "✓ Checking now — finished campaigns get re-created within ~30s.");
    } catch (e) { setErr(errMsg(e, "Run failed")); }
  }

  const last = rule?.last_run;

  return (
    <Card className="p-4 space-y-3">
      <div>
        <h3 className="text-sm font-semibold text-fg">♻️ Keep promos running</h3>
        <p className="text-xs text-fg-dim">
          OnlyFans ends a promo when its clock runs out or the claim cap fills. This
          re-creates any campaign you ticked <b>Keep running</b> above — same
          discount, same cap — so the offer never silently lapses.{" "}
          {armed > 0
            ? <span className="text-fg">{armed} campaign{armed === 1 ? "" : "s"} set to keep running.</span>
            : <span>Nothing is set to keep running yet — tick a campaign above.</span>}
        </p>
      </div>

      <div className="flex flex-wrap gap-4">
        <label className="flex items-center gap-2 text-sm cursor-pointer">
          <input type="checkbox" checked={form.enabled}
            onChange={(e) => set("enabled", e.target.checked)}
            className="w-4 h-4 rounded accent-accent cursor-pointer" />
          Enabled
        </label>
        <label className="flex items-center gap-2 text-sm cursor-pointer">
          <input type="checkbox" checked={form.dryRun}
            onChange={(e) => set("dryRun", e.target.checked)}
            className="w-4 h-4 rounded accent-accent cursor-pointer" />
          Dry run <span className="text-[11px] text-fg-dim">(report only — creates nothing)</span>
        </label>
      </div>

      <div className="grid gap-3 sm:grid-cols-2 max-w-md">
        <Field label="Check every (minutes)">
          <NumInput value={form.everyMinutes} min={1} onChange={(v) => set("everyMinutes", v)} />
        </Field>
        <Field label="Max re-arms each (0 = no limit)">
          <NumInput value={form.maxCycles} min={0} onChange={(v) => set("maxCycles", v)} />
        </Field>
      </div>

      {err && <div className="text-sm text-err">{err}</div>}

      <div className="flex items-center gap-2">
        <Button size="sm" onClick={save} disabled={busy || !accountId}>
          {rule ? "Save changes" : "Create automation"}
        </Button>
        {rule && (
          <Button size="sm" variant="ghost" onClick={runNow} disabled={runM.isPending}>
            {runM.isPending ? "Checking…" : "Check now"}
          </Button>
        )}
      </div>

      {msg && <div className="text-sm text-accent border-t border-border pt-3">{msg}</div>}

      {rule && last && (
        <div className="text-xs text-fg-dim border-t border-border pt-3">
          Last check: <span className="text-fg">{last.status}</span>
          <RunStats stats={last.stats} />
          {last.started_at && ` · ${new Date(last.started_at).toLocaleString()}`}
          {last.error_text && <span className="text-err"> · {last.error_text}</span>}
        </div>
      )}
    </Card>
  );
}
