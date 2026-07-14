"use client";

/**
 * PerAutomationTable — "which automations earn and which cost" rollup.
 *
 * Columns: Automation | Messages | Earnings | LLM calls | Tokens | Spend
 * Default sort: earnings DESC (matches the backend's own ordering).
 *
 * One row per automation_kind. An automation that only sends (mass
 * broadcasters) shows $0 spend; an LLM-only profiler (gen_info) shows spend
 * with 0 messages. Human / relay sends aren't here — that revenue lives in
 * the per-employee table.
 */

import { useMemo, useState } from "react";

import { Card } from "@/components/ui/primitives";
import { cn } from "@/lib/utils";
import { fmtCentsBlankZero } from "@/lib/format";
import { usePerAutomation, type PerAutomationRow } from "@/hooks/useStats";

interface Props {
  from: string | null;
  to: string | null;
}

// Friendly labels for the automation_kind tokens (= grok_calls.purpose).
const LABELS: Record<string, string> = {
  of_ai_chat: "Info-gather",
  welcome: "Welcome",
  deep_convo: "Deep Convo",
  followup: "Follow-up",
  autoreply: "Auto-reply",
  reply_mass_funnel: "Funnel reply",
  send_mass_message: "Mass message",
  mass_nudge: "Mass nudge",
  online_blast: "Online blast",
  nudge_online: "Online nudge",
  gen_info: "Profiler",
  // ai_chatter and ai_upseller are ONE engine — the split is by what the reply
  // carried (a priced offer or not), so the revenue column finally attributes the
  // sell turns separately from the chat turns.
  ai_chatter: "AI Chatter",
  ai_upseller: "AI Upseller",
};
const label = (k: string): string => LABELS[k] ?? k;

type SortKey = "automation" | "messages_sent" | "revenue_cents" | "llm_calls" | "cost_millicents";
type SortDir = "asc" | "desc";

function fmtTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return String(n);
}

export default function PerAutomationTable({ from, to }: Props) {
  const [sortKey, setSortKey] = useState<SortKey>("revenue_cents");
  const [sortDir, setSortDir] = useState<SortDir>("desc");

  const q = usePerAutomation(from, to);

  const sorted = useMemo<PerAutomationRow[]>(() => {
    const rows = q.data?.rows ? [...q.data.rows] : [];
    rows.sort((a, b) => {
      const av = (a[sortKey] ?? (sortKey === "automation" ? "" : 0)) as string | number;
      const bv = (b[sortKey] ?? (sortKey === "automation" ? "" : 0)) as string | number;
      const cmp =
        typeof av === "string" || typeof bv === "string"
          ? String(av).localeCompare(String(bv))
          : (av as number) - (bv as number);
      return sortDir === "asc" ? cmp : -cmp;
    });
    return rows;
  }, [q.data, sortKey, sortDir]);

  const totals = q.data?.totals;

  const toggleSort = (key: SortKey) => {
    if (key === sortKey) {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    } else {
      setSortKey(key);
      setSortDir(key === "automation" ? "asc" : "desc");
    }
  };

  return (
    <Card className="p-0 overflow-hidden">
      <header className="p-4 border-b border-border">
        <h2 className="text-sm font-medium text-fg">Per-automation</h2>
        <p className="text-xs text-fg-dim mt-0.5">
          Messages sent, earnings attributed, and LLM spend — per automation.
          Mass broadcasters show $0 spend; the profiler shows spend with no sends.
        </p>
      </header>

      {q.isError ? (
        <div className="p-4 text-sm text-err">
          {(q.error as Error)?.message || "Failed to load per-automation stats"}
        </div>
      ) : !q.isPending && sorted.length === 0 ? (
        <div className="p-6 text-sm text-fg-dim text-center">
          No automation activity in this window.
        </div>
      ) : (
        <table
          className={cn(
            "w-full text-sm transition-opacity",
            q.isFetching && q.isPlaceholderData && "opacity-60",
          )}
        >
          <thead>
            <tr className="text-[11px] uppercase tracking-wide text-fg-dim border-b border-border">
              <HeaderCell label="Automation" k="automation" curr={sortKey} dir={sortDir} onClick={toggleSort} align="left" />
              <HeaderCell label="Messages" k="messages_sent" curr={sortKey} dir={sortDir} onClick={toggleSort} align="right" />
              <HeaderCell label="Earnings" k="revenue_cents" curr={sortKey} dir={sortDir} onClick={toggleSort} align="right" />
              <HeaderCell label="LLM calls" k="llm_calls" curr={sortKey} dir={sortDir} onClick={toggleSort} align="right" />
              <th className="px-4 py-2 font-medium text-right select-none">Tokens (in/out)</th>
              <HeaderCell label="Spend" k="cost_millicents" curr={sortKey} dir={sortDir} onClick={toggleSort} align="right" />
            </tr>
          </thead>
          <tbody>
            {q.isPending && Array.from({ length: 5 }).map((_, i) => (
              <tr key={`skeleton:${i}`} className="border-b border-border last:border-0">
                {Array.from({ length: 6 }).map((__, j) => (
                  <td key={j} className="px-4 py-2.5 text-fg-dim tabular-nums">—</td>
                ))}
              </tr>
            ))}
            {sorted.map((r) => (
              <tr key={r.automation} className="border-b border-border last:border-0 hover:bg-bg-elev-1/40">
                <td className="px-4 py-2.5 font-medium text-fg">{label(r.automation)}</td>
                <td className="px-4 py-2.5 text-right tabular-nums">{r.messages_sent.toLocaleString()}</td>
                <td className="px-4 py-2.5 text-right tabular-nums font-semibold">
                  {fmtCentsBlankZero(r.revenue_cents)}
                </td>
                <td className="px-4 py-2.5 text-right tabular-nums text-fg-dim">{r.llm_calls.toLocaleString()}</td>
                <td className="px-4 py-2.5 text-right tabular-nums text-fg-dim">
                  {r.llm_calls > 0 ? `${fmtTokens(r.tokens_in)} / ${fmtTokens(r.tokens_out)}` : "—"}
                </td>
                <td className="px-4 py-2.5 text-right tabular-nums">{fmtCentsBlankZero(r.cost_cents)}</td>
              </tr>
            ))}
          </tbody>
          {totals && (
            <tfoot>
              <tr className="border-t border-border bg-bg-elev-1/30 text-fg font-medium">
                <td className="px-4 py-2.5">Total</td>
                <td className="px-4 py-2.5 text-right tabular-nums">{totals.messages_sent.toLocaleString()}</td>
                <td className="px-4 py-2.5 text-right tabular-nums">{fmtCentsBlankZero(totals.revenue_cents)}</td>
                <td className="px-4 py-2.5 text-right tabular-nums">{totals.llm_calls.toLocaleString()}</td>
                <td className="px-4 py-2.5 text-right tabular-nums text-fg-dim">—</td>
                <td className="px-4 py-2.5 text-right tabular-nums">{fmtCentsBlankZero(totals.cost_cents)}</td>
              </tr>
            </tfoot>
          )}
        </table>
      )}
    </Card>
  );
}

function HeaderCell({
  label,
  k,
  curr,
  dir,
  onClick,
  align,
}: {
  label: string;
  k: SortKey;
  curr: SortKey;
  dir: SortDir;
  onClick: (k: SortKey) => void;
  align: "left" | "right";
}) {
  const active = curr === k;
  return (
    <th
      className={cn(
        "px-4 py-2 font-medium select-none cursor-pointer hover:text-fg",
        align === "right" ? "text-right" : "text-left",
      )}
      onClick={() => onClick(k)}
    >
      {label}
      {active && <span className="ml-1">{dir === "asc" ? "▲" : "▼"}</span>}
    </th>
  );
}
