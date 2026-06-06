"use client";

/**
 * UserDataTab — the per-fan profile data we export to Google Sheets, read live
 * from our DB so the team can check it in-app (no sheet round-trip). Pick an
 * account, search, and scan the same columns push_to_sheets writes:
 * name / nickname / spend / msgs / short bio / Q1-3 / Tease1-3 / note-on-OF.
 *
 * Source: GET /admin/stats/per-fan-data (usePerFanData) → automations.push_to_sheets
 * build_fan_data, so the columns stay in lockstep with the export.
 */

import { useMemo, useState } from "react";

import { Card, Input } from "@/components/ui/primitives";
import { useActiveAccounts } from "@/hooks/useAccounts";
import { usePerFanData, type FanDataRow } from "@/hooks/useStats";

function fmtSpend(n: number): string {
  return n > 0 ? `$${n.toFixed(2)}` : "—";
}
function fmtDate(iso: string): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso.slice(0, 10) : d.toISOString().slice(0, 10);
}

export default function UserDataTab() {
  const accounts = useActiveAccounts();
  const [accountId, setAccountId] = useState<string | null>(null);
  const [q, setQ] = useState("");

  // Default to the first account once they load.
  const effectiveAccount = accountId ?? accounts[0]?.id ?? null;

  const dataQ = usePerFanData(effectiveAccount);
  const rows = dataQ.data?.rows ?? [];

  const filtered = useMemo(() => {
    const needle = q.trim().toLowerCase();
    if (!needle) return rows;
    return rows.filter((r) =>
      [r.fan_name, r.nickname, r.short_bio, r.q1, r.q2, r.q3, String(r.fan_id)]
        .some((v) => (v || "").toLowerCase().includes(needle)),
    );
  }, [rows, q]);

  return (
    <Card className="p-4 space-y-3">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <div className="min-w-0">
          <h2 className="text-sm font-medium text-fg">User data</h2>
          <p className="text-xs text-fg-dim">
            Per-fan profiles built by gen_info — the exact data{" "}
            <span className="font-mono">push_to_sheets</span> exports to the Google
            Sheet, read live from our DB. Sorted by spend.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <select
            value={effectiveAccount ?? ""}
            onChange={(e) => setAccountId(e.target.value || null)}
            className="rounded-lg bg-bg-elev-1 border border-border text-sm px-2 py-1.5 text-fg focus:outline-none focus:ring-2 focus:ring-accent/40"
          >
            {accounts.length === 0 && <option value="">No accounts</option>}
            {accounts.map((a) => (
              <option key={a.id} value={a.id}>
                {a.nickname || a.id}
              </option>
            ))}
          </select>
          <Input
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="Search name / nickname / bio…"
            className="w-56"
          />
        </div>
      </header>

      <div className="text-xs text-fg-dim">
        {dataQ.isLoading
          ? "Loading…"
          : dataQ.isError
            ? "Failed to load."
            : `${filtered.length}${filtered.length !== rows.length ? ` of ${rows.length}` : ""} fans`}
      </div>

      <div className="overflow-x-auto border border-border rounded-lg">
        <table className="w-full text-xs">
          <thead className="bg-bg-elev-1 text-fg-dim">
            <tr className="text-left">
              <Th>Fan</Th>
              <Th>Nickname</Th>
              <Th className="text-right">Spend</Th>
              <Th className="text-right">Msgs</Th>
              <Th>Short bio</Th>
              <Th>Q1 / Q2 / Q3</Th>
              <Th>Tease 1 / 2 / 3</Th>
              <Th>Note on OF</Th>
              <Th>Updated</Th>
            </tr>
          </thead>
          <tbody>
            {filtered.map((r) => (
              <Row key={r.fan_id} r={r} />
            ))}
            {!dataQ.isLoading && filtered.length === 0 && (
              <tr>
                <td colSpan={9} className="px-3 py-6 text-center text-fg-dim">
                  No profiled fans yet for this account.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </Card>
  );
}

function Th({ children, className = "" }: { children: React.ReactNode; className?: string }) {
  return <th className={`px-3 py-2 font-medium whitespace-nowrap ${className}`}>{children}</th>;
}

function Stack({ items }: { items: string[] }) {
  const real = items.filter(Boolean);
  if (real.length === 0) return <span className="text-fg-dim">—</span>;
  return (
    <ul className="space-y-0.5">
      {real.map((t, i) => (
        <li key={i} className="text-fg-dim">• {t}</li>
      ))}
    </ul>
  );
}

function Row({ r }: { r: FanDataRow }) {
  return (
    <tr className="border-t border-border align-top hover:bg-bg-elev-1/40">
      <td className="px-3 py-2 whitespace-nowrap">
        <div className="text-fg">{r.fan_name || r.fan_id}</div>
        <div className="text-[10px] text-fg-dim font-mono">{r.fan_id}</div>
      </td>
      <td className="px-3 py-2 whitespace-nowrap">{r.nickname || <span className="text-fg-dim">—</span>}</td>
      <td className="px-3 py-2 text-right whitespace-nowrap font-medium">{fmtSpend(r.total_spend)}</td>
      <td className="px-3 py-2 text-right whitespace-nowrap">{r.message_count || "—"}</td>
      <td className="px-3 py-2 max-w-[260px]"><span className="text-fg-dim">{r.short_bio || "—"}</span></td>
      <td className="px-3 py-2 max-w-[260px]"><Stack items={[r.q1, r.q2, r.q3]} /></td>
      <td className="px-3 py-2 max-w-[260px]"><Stack items={[r.tease1, r.tease2, r.tease3]} /></td>
      <td className="px-3 py-2 whitespace-nowrap">
        <span className={r.note_on_of === "added" ? "text-ok" : "text-fg-dim"}>{r.note_on_of}</span>
      </td>
      <td className="px-3 py-2 whitespace-nowrap text-fg-dim">{fmtDate(r.last_updated)}</td>
    </tr>
  );
}
