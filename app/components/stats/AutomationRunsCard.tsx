"use client";

/**
 * AutomationRunsCard — read-only surface over the `automation_runs` audit
 * log the automation executor writes (one row per run). Lets an owner see,
 * at a glance, when each automation last ran, whether it succeeded, and the
 * error text if it didn't.
 *
 * The endpoint (/admin/stats/automation-runs) returns the most-recent runs
 * first across all kinds. We group client-side by `kind` so the headline is
 * one row per automation showing its latest run; each group expands to the
 * recent run history for that kind (status, duration, error, stats_json).
 *
 * Purely read-only — no mutations, no triggers. Polls every 60s (see the
 * hook) so an in-flight "running" row flips to ok/error without a manual
 * refresh.
 */

import { useMemo, useState } from "react";

import { Card } from "@/components/ui/primitives";
import { cn } from "@/lib/utils";
import { fmtDateTime } from "@/lib/format";
import { useAutomationRuns, type AutomationRunRow } from "@/hooks/useStats";

interface KindGroup {
  kind: string;
  latest: AutomationRunRow;
  runs: AutomationRunRow[];
}

/** "8 min ago" / "2 h ago" / "3 d ago" — matches the model's own
 *  "gen_info_sweep last ran 8 min ago" framing. Falls back to absolute via
 *  the caller when the timestamp is missing. */
function timeAgo(iso: string | null): string | null {
  if (!iso) return null;
  const then = new Date(iso).getTime();
  if (!Number.isFinite(then)) return null;
  const secs = Math.round((Date.now() - then) / 1000);
  if (secs < 0) return "just now";
  if (secs < 60) return `${secs}s ago`;
  const mins = Math.round(secs / 60);
  if (mins < 60) return `${mins} min ago`;
  const hrs = Math.round(mins / 60);
  if (hrs < 24) return `${hrs} h ago`;
  const days = Math.round(hrs / 24);
  return `${days} d ago`;
}

/** Wall-clock run duration as a short string, or null when still running /
 *  missing a boundary. */
function fmtDuration(start: string | null, end: string | null): string | null {
  if (!start || !end) return null;
  const ms = new Date(end).getTime() - new Date(start).getTime();
  if (!Number.isFinite(ms) || ms < 0) return null;
  if (ms < 1000) return `${ms}ms`;
  const secs = ms / 1000;
  if (secs < 60) return `${secs.toFixed(secs < 10 ? 1 : 0)}s`;
  const mins = Math.floor(secs / 60);
  const rem = Math.round(secs % 60);
  return `${mins}m ${rem}s`;
}

function StatusBadge({ status }: { status: string }) {
  const tone =
    status === "ok"
      ? "bg-ok/15 text-ok"
      : status === "error"
        ? "bg-err/15 text-err"
        : status === "running"
          ? "bg-accent/15 text-accent"
          : "bg-bg-elev-1 text-fg-dim";
  const label =
    status === "running" ? "running…" : status;
  return (
    <span
      className={cn(
        "inline-flex items-center rounded px-1.5 py-0.5 text-[11px] font-medium capitalize",
        tone,
      )}
    >
      {label}
    </span>
  );
}

export default function AutomationRunsCard() {
  const q = useAutomationRuns(200);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());

  // Group runs by kind, preserving the server's most-recent-first order so
  // the first run we see for a kind is its latest.
  const groups = useMemo<KindGroup[]>(() => {
    const byKind = new Map<string, AutomationRunRow[]>();
    for (const r of q.data?.runs ?? []) {
      const arr = byKind.get(r.kind);
      if (arr) arr.push(r);
      else byKind.set(r.kind, [r]);
    }
    return Array.from(byKind.entries())
      .map(([kind, runs]) => ({ kind, latest: runs[0], runs }))
      // Surface the most-recently-active automations first.
      .sort((a, b) =>
        (b.latest.started_at ?? "").localeCompare(a.latest.started_at ?? ""),
      );
  }, [q.data]);

  const toggle = (kind: string) =>
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(kind)) next.delete(kind);
      else next.add(kind);
      return next;
    });

  return (
    <Card className="p-4 space-y-3">
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h2 className="text-sm font-medium text-fg">Automation runs</h2>
          <p className="text-xs text-fg-dim">
            Last run, status, and error per automation. Read-only over the
            executor&apos;s run log; refreshes every 60s.
          </p>
        </div>
        {groups.length > 0 && (
          <span className="text-xs text-fg-dim tabular-nums">
            {groups.length} automation{groups.length === 1 ? "" : "s"}
          </span>
        )}
      </header>

      {q.isLoading && <div className="text-sm text-fg-dim">Loading…</div>}
      {q.isError && (
        <div className="text-sm text-err">
          {(q.error as Error)?.message || "Failed to load automation runs"}
        </div>
      )}

      {!q.isLoading && !q.isError && (
        <div
          className={cn(
            "transition-opacity",
            q.isFetching && q.isPlaceholderData && "opacity-60",
          )}
        >
          {groups.length === 0 ? (
            <div className="text-sm text-fg-dim py-1">
              No automation runs recorded yet.
            </div>
          ) : (
            <ul className="divide-y divide-border">
              {groups.map((g) => {
                const isOpen = expanded.has(g.kind);
                const ago = timeAgo(g.latest.started_at);
                return (
                  <li key={g.kind} className="py-2">
                    <button
                      type="button"
                      onClick={() => toggle(g.kind)}
                      className="w-full flex flex-wrap items-center gap-3 text-left"
                      title="Show recent run history"
                    >
                      <span className="text-fg-dim text-xs w-3 shrink-0">
                        {isOpen ? "▾" : "▸"}
                      </span>
                      <span className="text-sm font-medium text-fg flex-1 min-w-0 truncate font-mono">
                        {g.kind}
                      </span>
                      <StatusBadge status={g.latest.status} />
                      <span
                        className="text-xs text-fg-dim w-28 text-right tabular-nums"
                        title={g.latest.started_at ?? ""}
                      >
                        {ago ?? fmtDateTime(g.latest.started_at)}
                      </span>
                      {g.runs.length > 1 && (
                        <span className="text-[11px] text-fg-dim/70 w-14 text-right tabular-nums">
                          {g.runs.length} runs
                        </span>
                      )}
                    </button>

                    {/* Latest error always visible — that's the whole point. */}
                    {g.latest.error_text && (
                      <div
                        className="mt-1 ml-6 text-xs text-err whitespace-pre-wrap break-words"
                        title={g.latest.error_text}
                      >
                        {g.latest.error_text}
                      </div>
                    )}

                    {isOpen && (
                      <ul className="mt-2 ml-6 space-y-1.5 border-l border-border pl-3">
                        {g.runs.map((r) => (
                          <RunDetail key={r.id} run={r} />
                        ))}
                      </ul>
                    )}
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      )}
    </Card>
  );
}

function RunDetail({ run }: { run: AutomationRunRow }) {
  const dur = fmtDuration(run.started_at, run.completed_at);
  return (
    <li className="text-xs space-y-0.5">
      <div className="flex flex-wrap items-center gap-2">
        <StatusBadge status={run.status} />
        <span className="text-fg-dim tabular-nums" title={run.started_at ?? ""}>
          {fmtDateTime(run.started_at)}
        </span>
        {dur && <span className="text-fg-dim/70 tabular-nums">· {dur}</span>}
        {run.account_id && (
          <span className="text-fg-dim/70 font-mono truncate max-w-[10rem]">
            · {run.account_id}
          </span>
        )}
      </div>
      {run.error_text && (
        <div className="text-err whitespace-pre-wrap break-words">
          {run.error_text}
        </div>
      )}
      {run.stats_json && run.stats_json !== "{}" && (
        <div className="text-fg-dim/80 font-mono whitespace-pre-wrap break-words">
          {run.stats_json}
        </div>
      )}
    </li>
  );
}
