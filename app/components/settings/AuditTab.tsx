"use client";

/**
 * AuditTab — paginated view of the `actions` table.
 *
 * Each row: who (employee chip) · when · what (method + path) · target
 * + an expandable payload row. Filters: employee, account.
 *
 * Read-only. The middleware appends; this tab just renders. No infinite
 * scroll (yet) — Load more button keeps the surface area small.
 */

import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";

import { Card, Button } from "@/components/ui/primitives";
import { relay, type AuditAction, type Employee } from "@/lib/relay";
import { useRoster } from "@/hooks/useRoster";

interface AuditResp { actions: AuditAction[]; limit: number; offset: number }

const PAGE = 50;

export default function AuditTab() {
  const [offset, setOffset] = useState(0);
  const [filterEmployee, setFilterEmployee] = useState<number | null>(null);
  const [filterAccount, setFilterAccount] = useState<string>("");

  // Keep chatter mirrors so audit rows attributed to one still resolve a name.
  const { rows: roster } = useRoster({ includeChatterMirrors: true });
  const byId = useMemo(
    () => new Map<number, Employee>(roster.map((e) => [e.id, e])),
    [roster],
  );

  const qs = new URLSearchParams();
  qs.set("limit", String(PAGE));
  qs.set("offset", String(offset));
  if (filterEmployee != null) qs.set("employee_id", String(filterEmployee));
  if (filterAccount.trim()) qs.set("account_id", filterAccount.trim());

  const auditQ = useQuery<AuditResp>({
    queryKey: ["audit", offset, filterEmployee, filterAccount],
    queryFn: () => relay.get<AuditResp>(`/admin/audit?${qs.toString()}`),
    staleTime: 5_000,
  });

  const rows = auditQ.data?.actions ?? [];
  const canPaginate = rows.length === PAGE; // assume more if we got a full page

  return (
    <div className="space-y-4">
      <Card>
        <div className="flex items-center justify-between gap-3 mb-3 flex-wrap">
          <h2 className="text-base font-semibold">Audit log</h2>
          <div className="flex items-center gap-2 text-xs">
            <select
              value={filterEmployee ?? ""}
              onChange={(e) => {
                setOffset(0);
                setFilterEmployee(e.target.value ? Number(e.target.value) : null);
              }}
              className="bg-bg border border-border rounded-md px-2 py-1 text-xs focus:outline-none"
            >
              <option value="">All employees</option>
              {roster.map((e) => (
                <option key={e.id} value={e.id}>{e.display_name}</option>
              ))}
            </select>
            <input
              type="text"
              placeholder="account_id"
              value={filterAccount}
              onChange={(e) => { setOffset(0); setFilterAccount(e.target.value); }}
              className="w-32 bg-bg border border-border rounded-md px-2 py-1 text-xs focus:outline-none"
            />
          </div>
        </div>

        {auditQ.isLoading && <div className="text-fg-dim text-sm">Loading…</div>}
        {auditQ.error && (
          <div className="text-err text-sm">{(auditQ.error as Error).message || "failed"}</div>
        )}
        {!auditQ.isLoading && rows.length === 0 && (
          <div className="text-fg-dim text-sm py-8 text-center">
            No audit entries{filterEmployee || filterAccount ? " for this filter" : ""}.
          </div>
        )}

        {rows.length > 0 && (
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-fg-dim text-xs border-b border-border">
                <th className="px-3 py-2 font-medium w-44">When</th>
                <th className="px-3 py-2 font-medium w-32">Who</th>
                <th className="px-3 py-2 font-medium">Action</th>
                <th className="px-3 py-2 font-medium w-32">Account</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((a) => {
                const emp = a.employee_id != null ? byId.get(a.employee_id) : null;
                return <AuditRow key={a.id} a={a} emp={emp ?? null} />;
              })}
            </tbody>
          </table>
        )}

        <div className="flex items-center justify-between mt-3 text-xs text-fg-dim">
          <span>
            Showing {rows.length === 0 ? 0 : offset + 1}–{offset + rows.length}
          </span>
          <div className="flex items-center gap-2">
            <Button
              size="sm"
              variant="secondary"
              disabled={offset === 0}
              onClick={() => setOffset((o) => Math.max(0, o - PAGE))}
            >
              Newer
            </Button>
            <Button
              size="sm"
              variant="secondary"
              disabled={!canPaginate}
              onClick={() => setOffset((o) => o + PAGE)}
            >
              Older
            </Button>
          </div>
        </div>
      </Card>
    </div>
  );
}

function AuditRow({ a, emp }: { a: AuditAction; emp: Employee | null }) {
  const [open, setOpen] = useState(false);
  const hasPayload =
    a.payload != null &&
    !(typeof a.payload === "object" && a.payload && Object.keys(a.payload as object).length === 0);

  return (
    <>
      <tr
        className="border-b border-border/40 last:border-0 hover:bg-bg-elev-1/40 cursor-pointer"
        onClick={() => hasPayload && setOpen((v) => !v)}
      >
        <td className="px-3 py-2 text-xs text-fg-dim whitespace-nowrap">{fmtDateTime(a.at)}</td>
        <td className="px-3 py-2">
          {emp ? (
            <span className="inline-flex items-center gap-1.5 text-xs">
              <span
                className="w-2 h-2 rounded-full"
                style={{ background: emp.color || "#666" }}
              />
              {emp.display_name}
            </span>
          ) : (
            <span className="text-xs text-fg-dim italic">system</span>
          )}
        </td>
        <td className="px-3 py-2">
          <code className="text-xs">{a.action}</code>
          {hasPayload && (
            <span className="ml-2 text-[10px] text-fg-dim">{open ? "▾" : "▸"}</span>
          )}
        </td>
        <td className="px-3 py-2 text-xs text-fg-dim">{a.account_id ?? "—"}</td>
      </tr>
      {open && hasPayload && (
        <tr className="border-b border-border/40 last:border-0">
          <td colSpan={4} className="px-3 pb-3 pt-0">
            <pre className="bg-bg/70 border border-border rounded-md p-3 text-[10px] font-mono whitespace-pre-wrap break-all max-h-64 overflow-auto">
              {JSON.stringify(a.payload, null, 2)}
            </pre>
          </td>
        </tr>
      )}
    </>
  );
}

function fmtDateTime(iso?: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleString([], {
    month: "short", day: "numeric",
    hour: "2-digit", minute: "2-digit", second: "2-digit",
  });
}
