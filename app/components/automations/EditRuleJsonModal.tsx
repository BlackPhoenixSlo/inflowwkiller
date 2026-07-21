"use client";

/**
 * EditRuleJsonButton + EditRuleJsonModal — a raw-JSON editor for ONE automation
 * rule's payload (`steps_json`), for the bespoke single-rule tabs (Mass Nudge,
 * Online Blast, …) that build their payload with a custom form instead of the
 * generic RuleEditor. It loads the account's rule of the given `kind`, lets an
 * owner hand-edit the payload object, validates it (parse + must be an object),
 * and PATCHes via the same /admin/automation-rules/{id} path the typed form uses
 * — server-side unknown keys pass through untouched, so hand edits are safe.
 *
 * Mirrors JsonConfigModal (the per-account config-blob editor) but targets a
 * rule row rather than a config surface. Single-rule assumption: these tabs keep
 * exactly one rule per (account, kind); if several exist we edit the first and
 * say so, and if none exists the button is disabled until the form saves one.
 */

import { useEffect, useMemo, useState } from "react";

import {
  useAutomationRules,
  useUpdateRule,
  type AutomationRule,
} from "@/hooks/useAutomations";

export function EditRuleJsonButton({
  accountId,
  kind,
  label = "{ } Edit raw JSON",
  className,
}: {
  accountId: string | null;
  kind: string;
  label?: string;
  className?: string;
}) {
  const [open, setOpen] = useState(false);
  const rulesQ = useAutomationRules(accountId);
  const rules = useMemo(
    () => (rulesQ.data ?? []).filter((r) => r.kind === kind),
    [rulesQ.data, kind],
  );
  const rule = rules[0] ?? null;
  const disabled = !accountId || !rule;
  return (
    <>
      <button
        type="button"
        disabled={disabled}
        onClick={() => setOpen(true)}
        className={
          className ??
          "text-[11px] text-fg-dim hover:text-fg underline underline-offset-2 disabled:opacity-40 disabled:no-underline"
        }
        title={
          !accountId
            ? "Pick an account first"
            : !rule
              ? "Save this automation once, then edit its raw JSON"
              : "Edit the raw JSON payload for this rule"
        }
      >
        {label}
      </button>
      {open && rule && accountId && (
        <EditRuleJsonModal
          rule={rule}
          accountId={accountId}
          extraCount={rules.length - 1}
          onClose={() => setOpen(false)}
        />
      )}
    </>
  );
}

function EditRuleJsonModal({
  rule,
  accountId,
  extraCount,
  onClose,
}: {
  rule: AutomationRule;
  accountId: string;
  extraCount: number;
  onClose: () => void;
}) {
  const updateM = useUpdateRule(accountId);
  const [text, setText] = useState<string>(() =>
    JSON.stringify(rule.payload ?? {}, null, 2),
  );
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  function parseOrError(): Record<string, unknown> | null {
    const t = text.trim();
    let parsed: unknown;
    try {
      parsed = JSON.parse(t || "{}");
    } catch (e) {
      setErr(`Invalid JSON: ${(e as Error).message}`);
      return null;
    }
    if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
      setErr('Payload must be a JSON object, e.g. { "enabled": true }');
      return null;
    }
    return parsed as Record<string, unknown>;
  }

  function onSave() {
    setErr(null);
    const parsed = parseOrError();
    if (!parsed) return;
    updateM
      .mutateAsync({ id: rule.id, payload: parsed })
      .then(() => onClose())
      .catch((e: Error) => setErr(e.message || "Save failed"));
  }

  function reformat() {
    setErr(null);
    const parsed = parseOrError();
    if (parsed) setText(JSON.stringify(parsed, null, 2));
  }

  return (
    <div
      className="fixed inset-0 z-50 bg-black/50 grid place-items-center p-4"
      onClick={onClose}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        className="w-full max-w-[820px] max-h-[90vh] flex flex-col bg-panel border border-border rounded-xl shadow-2xl"
      >
        <header className="px-4 py-3 border-b border-border flex items-start justify-between gap-3">
          <div className="min-w-0">
            <h2 className="text-sm font-semibold">
              Edit raw JSON — {rule.name || rule.kind}
            </h2>
            <p className="text-[11px] text-fg-dim truncate">
              acct {accountId} · rule #{rule.id} ({rule.kind}) · PATCH
              /admin/automation-rules/{rule.id}
              {extraCount > 0 && ` · +${extraCount} more rule(s) of this kind not shown`}
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="text-fg-dim hover:text-fg text-lg leading-none shrink-0 w-11 h-11 -mr-2 -my-2 grid place-items-center md:w-auto md:h-auto md:mr-0 md:my-0 md:inline"
            title="Close"
          >
            ×
          </button>
        </header>

        <div className="flex-1 overflow-y-auto p-4 min-h-0">
          <textarea
            value={text}
            onChange={(e) => {
              setText(e.target.value);
              if (err) setErr(null);
            }}
            spellCheck={false}
            className="w-full h-[52vh] rounded-lg bg-bg-elev-1 border border-border text-xs font-mono px-3 py-2 text-fg focus:outline-none focus:ring-2 focus:ring-accent/40 resize-none"
            placeholder="{ }"
          />
          {err && (
            <div className="mt-2 text-xs text-red-400 whitespace-pre-wrap">{err}</div>
          )}
        </div>

        <footer className="px-4 py-3 border-t border-border flex items-center justify-between gap-2">
          <button
            type="button"
            onClick={reformat}
            className="text-[11px] text-fg-dim hover:text-fg underline underline-offset-2"
          >
            Reformat
          </button>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={onClose}
              className="text-xs px-3 py-1.5 rounded-lg border border-border text-fg-dim hover:text-fg"
            >
              Cancel
            </button>
            <button
              type="button"
              onClick={onSave}
              disabled={updateM.isPending}
              className="text-xs px-3 py-1.5 rounded-lg bg-accent text-white disabled:opacity-50"
            >
              {updateM.isPending ? "Saving…" : "Save"}
            </button>
          </div>
        </footer>
      </div>
    </div>
  );
}
