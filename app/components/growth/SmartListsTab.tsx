"use client";

/**
 * SmartListsTab — Growth → Smart Lists.
 *
 * Build a saved dynamic fan segment: rules over spend / recency / status / tag,
 * previewed live (count + sample) against the account's fans, then saved for
 * reuse as a mass-message audience (include/exclude in the composer).
 */

import { useMemo, useState } from "react";

import { Badge, Button, Card, Input } from "@/components/ui/primitives";
import { ConfirmDeleteButton, errMsg } from "@/components/growth/_bits";
import {
  useSmartLists, useCreateSmartList, useUpdateSmartList, useDeleteSmartList,
  usePreviewSmartList,
  type SmartField, type SmartOp, type SmartRule, type SmartRules, type SmartList,
} from "@/hooks/useSmartLists";

const SELECT_CLS =
  "bg-bg border border-border rounded-lg px-2 py-1.5 text-sm focus:outline-none focus:border-accent";

const FIELD_DEFS: Array<{ field: SmartField; label: string; kind: "money" | "days" | "status" | "tag" }> = [
  { field: "lifetime_spend",   label: "Lifetime spend ($)", kind: "money" },
  { field: "recent_spend",     label: "Recent spend ($)",   kind: "money" },
  { field: "last_active_days", label: "Days since active",  kind: "days" },
  { field: "status",           label: "Subscription status", kind: "status" },
  { field: "tag",              label: "Tag",                kind: "tag" },
];

const OPS_BY_KIND: Record<string, SmartOp[]> = {
  money: [">=", "<=", ">", "<", "==", "!="],
  days: [">=", "<=", ">", "<", "==", "!="],
  status: ["==", "!="],
  tag: ["has", "not_has"],
};

function kindOf(field: SmartField): "money" | "days" | "status" | "tag" {
  return FIELD_DEFS.find((f) => f.field === field)?.kind ?? "money";
}

/** UI rule row — value kept as a string; converted to the wire shape on save. */
interface RuleUI { field: SmartField; op: SmartOp; value: string; days: string }

function toUI(r: SmartRule): RuleUI {
  const k = kindOf(r.field);
  const value = k === "money" ? String(Number(r.value) / 100) : String(r.value ?? "");
  return { field: r.field, op: r.op, value, days: String(r.days ?? 30) };
}

function fromUI(r: RuleUI): SmartRule {
  const k = kindOf(r.field);
  if (k === "money") {
    const out: SmartRule = { field: r.field, op: r.op, value: Math.round(Number(r.value || 0) * 100) };
    if (r.field === "recent_spend") out.days = Math.max(1, Math.round(Number(r.days || 30)));
    return out;
  }
  if (k === "days") return { field: r.field, op: r.op, value: Number(r.value || 0) };
  return { field: r.field, op: r.op, value: r.value };
}

function summarize(rules: SmartRules): string {
  if (!rules.rules?.length) return "everyone (no filters)";
  const join = rules.match === "any" ? " OR " : " AND ";
  return rules.rules.map((r) => {
    const k = kindOf(r.field);
    if (k === "money") return `${r.field === "recent_spend" ? "recent" : "lifetime"} ${r.op} $${(Number(r.value) / 100).toFixed(0)}${r.field === "recent_spend" ? `/${r.days ?? 30}d` : ""}`;
    if (k === "days") return `active ${r.op} ${r.value}d`;
    if (k === "tag") return `${r.op === "not_has" ? "not tagged" : "tagged"} ${r.value}`;
    return `status ${r.op} ${r.value}`;
  }).join(join);
}

const EMPTY_RULE: RuleUI = { field: "lifetime_spend", op: ">=", value: "100", days: "30" };

export default function SmartListsTab({ accountId }: { accountId: string | null }) {
  const listsQ = useSmartLists(accountId);
  const createM = useCreateSmartList(accountId);
  const updateM = useUpdateSmartList(accountId);
  const deleteM = useDeleteSmartList(accountId);

  const [editing, setEditing] = useState<SmartList | null>(null);
  const [adding, setAdding] = useState(false);

  const lists = listsQ.data ?? [];

  function startNew() { setEditing(null); setAdding(true); }
  function close() { setEditing(null); setAdding(false); }

  return (
    <div className="space-y-4 max-w-3xl">
      <header className="flex items-start justify-between gap-3">
        <div>
          <h2 className="text-lg font-semibold">Smart Lists</h2>
          <p className="text-sm text-fg-dim">
            Saved dynamic segments — build a rule once (spend / recency / status /
            tag), reuse it as a mass-message audience. Preview the exact count
            before you save.
          </p>
        </div>
        <Button size="sm" onClick={startNew} disabled={!accountId}>+ New segment</Button>
      </header>

      {(adding || editing) && accountId && (
        <SegmentEditor
          key={editing?.id ?? "new"}
          accountId={accountId}
          editing={editing}
          onClose={close}
          onSave={async (name, rules) => {
            if (editing) await updateM.mutateAsync({ id: editing.id, name, rules });
            else await createM.mutateAsync({ name, rules });
            close();
          }}
        />
      )}

      {listsQ.isLoading && <div className="text-sm text-fg-dim">Loading…</div>}
      {listsQ.isError && (
        <div className="text-sm text-err">{errMsg(listsQ.error, "Failed to load")}</div>
      )}

      {!listsQ.isLoading && lists.length === 0 && !adding && (
        <div className="text-sm text-fg-dim py-2">
          No segments yet. Click <span className="text-fg">+ New segment</span> to build one.
        </div>
      )}

      <ul className="divide-y divide-border">
        {lists.map((l) => (
          <li key={l.id} className="py-3 flex flex-wrap items-center gap-3">
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-2">
                <span className="text-sm font-medium text-fg truncate">{l.name}</span>
                <Badge color="info">{l.rules.match === "any" ? "any" : "all"}</Badge>
              </div>
              <div className="text-[11px] text-fg-dim">{summarize(l.rules)}</div>
            </div>
            <div className="flex items-center gap-1.5 shrink-0">
              <Button size="sm" variant="ghost" onClick={() => { setAdding(false); setEditing(l); }}>
                Edit
              </Button>
              <ConfirmDeleteButton
                confirm={`Delete segment "${l.name}"?`}
                onConfirm={() => deleteM.mutate(l.id)}
                pending={deleteM.isPending}
              />
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}

function SegmentEditor({
  accountId, editing, onClose, onSave,
}: {
  accountId: string;
  editing: SmartList | null;
  onClose: () => void;
  onSave: (name: string, rules: SmartRules) => Promise<void>;
}) {
  const preview = usePreviewSmartList(accountId);
  const [name, setName] = useState(editing?.name ?? "");
  const [match, setMatch] = useState<"all" | "any">(editing?.rules.match ?? "all");
  const [rows, setRows] = useState<RuleUI[]>(
    editing?.rules.rules?.length ? editing.rules.rules.map(toUI) : [{ ...EMPTY_RULE }],
  );
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const rules: SmartRules = useMemo(
    () => ({ match, rules: rows.map(fromUI) }), [match, rows],
  );

  const setRow = (i: number, patch: Partial<RuleUI>) =>
    setRows((rs) => rs.map((r, j) => (j === i ? { ...r, ...patch } : r)));
  const addRow = () => setRows((rs) => [...rs, { ...EMPTY_RULE }]);
  const removeRow = (i: number) => setRows((rs) => rs.filter((_, j) => j !== i));

  async function doPreview() {
    setErr(null);
    try { await preview.mutateAsync(rules); }
    catch (e) { setErr(errMsg(e, "Preview failed")); }
  }

  async function save() {
    setErr(null);
    if (!name.trim()) { setErr("Name is required."); return; }
    setBusy(true);
    try { await onSave(name.trim(), rules); }
    catch (e) { setErr(errMsg(e, "Save failed")); }
    finally { setBusy(false); }
  }

  return (
    <Card className="p-4 space-y-3 border-accent/40">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-semibold">{editing ? "Edit segment" : "New segment"}</h3>
        <button type="button" onClick={onClose} className="text-fg-dim hover:text-fg" aria-label="Close">✕</button>
      </div>

      <label className="block space-y-1">
        <span className="text-[11px] uppercase tracking-wide text-fg-dim">Name</span>
        <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. Active whales" />
      </label>

      <div className="flex items-center gap-2 text-sm">
        <span className="text-fg-dim">Match</span>
        <select className={SELECT_CLS} value={match} onChange={(e) => setMatch(e.target.value as "all" | "any")}>
          <option value="all">ALL rules (AND)</option>
          <option value="any">ANY rule (OR)</option>
        </select>
      </div>

      <div className="space-y-2">
        {rows.map((r, i) => {
          const k = kindOf(r.field);
          return (
            <div key={i} className="flex flex-wrap items-center gap-2">
              <select
                className={SELECT_CLS}
                value={r.field}
                onChange={(e) => {
                  const field = e.target.value as SmartField;
                  const ops = OPS_BY_KIND[kindOf(field)];
                  setRow(i, { field, op: ops[0] });
                }}
              >
                {FIELD_DEFS.map((f) => <option key={f.field} value={f.field}>{f.label}</option>)}
              </select>
              <select className={SELECT_CLS} value={r.op} onChange={(e) => setRow(i, { op: e.target.value as SmartOp })}>
                {OPS_BY_KIND[k].map((op) => <option key={op} value={op}>{op}</option>)}
              </select>
              {k === "status" ? (
                <select className={SELECT_CLS} value={r.value} onChange={(e) => setRow(i, { value: e.target.value })}>
                  <option value="active">active</option>
                  <option value="expired">expired</option>
                </select>
              ) : (
                <Input
                  className="w-28"
                  value={r.value}
                  onChange={(e) => setRow(i, { value: e.target.value })}
                  placeholder={k === "money" ? "$" : k === "days" ? "days" : "tag"}
                />
              )}
              {r.field === "recent_spend" && (
                <span className="flex items-center gap-1 text-[11px] text-fg-dim">
                  in last
                  <Input className="w-16" value={r.days} onChange={(e) => setRow(i, { days: e.target.value })} />
                  days
                </span>
              )}
              {rows.length > 1 && (
                <button type="button" className="text-fg-dim hover:text-err text-sm" onClick={() => removeRow(i)} aria-label="Remove rule">✕</button>
              )}
            </div>
          );
        })}
        <Button size="sm" variant="ghost" onClick={addRow}>+ Add rule</Button>
      </div>

      <div className="flex flex-wrap items-center gap-2 border-t border-border pt-3">
        <Button size="sm" variant="secondary" onClick={doPreview} disabled={preview.isPending}>
          {preview.isPending ? "Counting…" : "Preview count"}
        </Button>
        {preview.data && (
          <span className="text-sm text-fg">
            <b>{preview.data.count}</b>
            <span className="text-fg-dim"> / {preview.data.total_fans} fans match</span>
          </span>
        )}
        <div className="ml-auto flex items-center gap-2">
          <Button size="sm" variant="primary" onClick={save} disabled={busy}>
            {busy ? "Saving…" : editing ? "Save changes" : "Create segment"}
          </Button>
          <Button size="sm" variant="ghost" onClick={onClose}>Cancel</Button>
        </div>
      </div>

      {preview.data && preview.data.sample.length > 0 && (
        <div className="text-[11px] text-fg-dim">
          Sample: {preview.data.sample.slice(0, 8).map((s) => s.name).join(", ")}
          {preview.data.count > 8 && " …"}
        </div>
      )}
      {err && <div className="text-xs text-err">{err}</div>}
    </Card>
  );
}
