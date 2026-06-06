"use client";

/**
 * IngestHealthBanner — traffic-light header for transaction-ingest health.
 *
 * Shape mirrors CoverageHeader: a single click-to-expand strip with a
 * traffic-light dot, a label, and a chevron. Expanding reveals per-account
 * rows with last-scan recency, row counters, and per-account "re-sync" /
 * "pause 1h" actions.
 *
 * Backfill is deliberately *not* exposed here — it's an admin-recovery
 * surface (synthesis §8). When we ship /admin/ingest, the empty-state /
 * red-state hint will link to it.
 */

import { useEffect, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";

import { Card } from "@/components/ui/primitives";
import { cn } from "@/lib/utils";
import {
  useIngestHealth,
  useIngestPause,
  useIngestResume,
  useIngestRun,
  type IngestAccountRow,
  type IngestTier,
} from "@/hooks/useStats";

function tier(t: IngestTier): { text: string; dot: string; label: string } {
  if (t === "green") return { text: "text-ok",   dot: "bg-ok",   label: "Healthy" };
  if (t === "yellow") return { text: "text-warn", dot: "bg-warn", label: "Watching" };
  return                       { text: "text-err",  dot: "bg-err",  label: "Action needed" };
}

function overallLabel(t: IngestTier, count: number): string {
  if (count === 0) return "No accounts bound";
  if (t === "green") return "All accounts up to date.";
  if (t === "yellow") return "One or more accounts behind.";
  return "Ingest paused or erroring.";
}

function relTime(mins: number | null, lastScanAt: string | null): string {
  if (lastScanAt == null) return "Never scanned";
  if (mins == null) return "—";
  if (mins < 1) return "just now";
  if (mins < 60) return `${Math.floor(mins)}m ago`;
  const h = Math.floor(mins / 60);
  if (h < 24) return `${h}h ago`;
  const d = Math.floor(h / 24);
  return `${d}d ago`;
}

function truncate(s: string, n: number = 120): string {
  return s.length > n ? s.slice(0, n - 1) + "…" : s;
}

export default function IngestHealthBanner() {
  const [expanded, setExpanded] = useState(false);
  const q = useIngestHealth();

  const overall: IngestTier = q.data?.overall_status ?? "green";
  const accounts = q.data?.accounts ?? [];
  const t = tier(overall);

  return (
    <Card className="p-0 overflow-hidden">
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        className="w-full text-left p-5 flex items-center gap-4 hover:bg-bg-elev-1/40 transition-colors"
        aria-expanded={expanded}
      >
        <span className={cn("w-3 h-3 rounded-full shrink-0", t.dot)} aria-hidden />
        <div className="flex-1 min-w-0">
          <div className="text-sm font-medium text-fg">
            Ingest health
            <span className={cn("ml-2 text-[11px] uppercase tracking-wide", t.text)}>
              {t.label}
            </span>
          </div>
          <div className="text-xs text-fg-dim mt-0.5">
            {q.isLoading
              ? "Loading…"
              : q.isError
                ? <span className="text-err">{(q.error as Error)?.message || "Failed to load"}</span>
                : (
                  <>
                    {overallLabel(overall, accounts.length)}
                    {accounts.length > 0 && (
                      <>
                        {" · "}
                        <span className="text-fg-dim">click for per-account detail</span>
                      </>
                    )}
                  </>
                )}
          </div>
        </div>
        <div className="text-fg-dim text-sm select-none">{expanded ? "▴" : "▾"}</div>
      </button>

      {expanded && (
        <div className="border-t border-border">
          {q.isLoading ? (
            <div className="p-4 text-sm text-fg-dim">Loading per-account health…</div>
          ) : accounts.length === 0 ? (
            <div className="p-4 text-sm text-fg-dim">
              No accounts yet — bind a session to start ingest.
            </div>
          ) : (
            <ul
              className={cn(
                "divide-y divide-border transition-opacity",
                q.isFetching && q.isPlaceholderData && "opacity-60",
              )}
            >
              {accounts.map((row) => <AccountRow key={row.account_id} row={row} />)}
            </ul>
          )}
        </div>
      )}
    </Card>
  );
}

function AccountRow({ row }: { row: IngestAccountRow }) {
  const t = tier(row.tier);
  const qc = useQueryClient();
  const runMut = useIngestRun();
  const pauseMut = useIngestPause();
  const resumeMut = useIngestResume();

  // Paused-until honoured by the supervisor — if it's in the future, no
  // tick will fire for this account, and the user needs to click resume
  // (or wait it out) for re-syncs to take effect.
  const pausedFor = row.paused_until ? new Date(row.paused_until).getTime() - Date.now() : 0;
  const isPaused = pausedFor > 0;
  const pausedMinsLeft = Math.ceil(pausedFor / 60_000);

  // Data-driven scanning state: snapshot the row's last_scan_at when the
  // user clicks, then watch it advance — that's the only honest signal
  // a tick actually ran. Wall-clock 12s cap covers the unhappy path
  // where the supervisor never gets to this account (paused, error).
  // 3s + 10s one-shot invalidations replace the old 2s polling loop —
  // same UX, ~6x fewer reads on the health endpoint.
  const [scanUntil, setScanUntil] = useState<number>(0);
  const [scanFrom, setScanFrom] = useState<string | null>(null);
  const [collisionMsg, setCollisionMsg] = useState<string | null>(null);
  const isScanning = scanUntil > 0 && Date.now() < scanUntil;

  // Bail out of scanning as soon as last_scan_at advances past the
  // click snapshot. Trusts the server-side stamp instead of guessing.
  useEffect(() => {
    if (!isScanning || scanFrom == null) return;
    if (row.last_scan_at && row.last_scan_at > scanFrom) {
      setScanUntil(0);
      setScanFrom(null);
    }
  }, [row.last_scan_at, scanFrom, isScanning]);

  useEffect(() => {
    if (!isScanning) return;
    const t1 = setTimeout(
      () => qc.invalidateQueries({ queryKey: ["stats", "ingest-health"] }),
      3000,
    );
    const t2 = setTimeout(
      () => qc.invalidateQueries({ queryKey: ["stats", "ingest-health"] }),
      10000,
    );
    const stop = setTimeout(() => {
      setScanUntil(0);
      setScanFrom(null);
    }, 12000);
    return () => {
      clearTimeout(t1);
      clearTimeout(t2);
      clearTimeout(stop);
    };
    // scanUntil is in deps so a rapid double-click restarts the
    // 3s/10s/12s timer set — without it React skips the effect re-run
    // (isScanning stayed true), the second click's stop timer never
    // fires, and the user's second window is silently truncated by
    // the first click's stop. See library/db_data/17_banner_ui_audit.md.
  }, [isScanning, scanUntil, qc]);

  // Auto-clear the inline collision banner after 4s — no extra UI to dismiss.
  useEffect(() => {
    if (!collisionMsg) return;
    const t = setTimeout(() => setCollisionMsg(null), 4000);
    return () => clearTimeout(t);
  }, [collisionMsg]);

  const onResync = () => {
    setScanFrom(row.last_scan_at ?? "");
    setScanUntil(Date.now() + 12_000);
    setCollisionMsg(null);
    runMut.mutate(row.account_id, {
      onError: (err: unknown) => {
        setScanUntil(0);
        setScanFrom(null);
        const msg = (err as { status?: number; message?: string })?.message ?? "";
        const status = (err as { status?: number })?.status;
        // 409 = supervisor tick already running this account. Be
        // explicit so the user knows the click registered but a
        // concurrent scan is in progress.
        if (status === 409 || msg.includes("409") || msg.toLowerCase().includes("already")) {
          setCollisionMsg("already scanning — try again in a moment");
        } else if (msg) {
          setCollisionMsg(msg);
        }
      },
    });
  };

  const busy = runMut.isPending || pauseMut.isPending || resumeMut.isPending || isScanning;

  return (
    <li className="px-4 py-3 flex flex-wrap items-center gap-x-4 gap-y-1">
      <span
        className={cn(
          "w-3 h-3 rounded-full shrink-0 transition-shadow",
          t.dot,
          isScanning && "ring-2 ring-accent/50 ring-offset-2 ring-offset-bg animate-pulse",
        )}
        aria-hidden
      />
      <span className="text-sm text-fg font-mono">
        {row.display_name ? `@${row.display_name}` : row.account_id}
      </span>
      <span className="text-xs text-fg-dim">
        last scan {relTime(row.minutes_since_scan, row.last_scan_at)}
      </span>
      <span className="text-xs text-fg-dim tabular-nums">
        {row.rows_inserted_total.toLocaleString()} rows
      </span>
      {isScanning ? (
        <span className="text-[11px] uppercase tracking-wide text-accent animate-pulse">
          scanning…
        </span>
      ) : isPaused ? (
        <span
          className="text-[11px] uppercase tracking-wide text-warn"
          title={`Supervisor will skip this account until ${new Date(row.paused_until!).toLocaleTimeString()}`}
        >
          paused · {pausedMinsLeft}m left
        </span>
      ) : row.current_status && row.current_status !== "ok" && (
        <span className={cn("text-[11px] uppercase tracking-wide", t.text)}>
          {row.current_status}
        </span>
      )}
      <div className="ml-auto flex items-center gap-2">
        {isPaused ? (
          <button
            type="button"
            disabled={busy}
            onClick={() => resumeMut.mutate(row.account_id)}
            className="text-[11px] px-2 py-1 rounded border border-border text-ok hover:bg-bg-elev-1 disabled:opacity-50"
          >
            resume
          </button>
        ) : (
          <button
            type="button"
            disabled={busy}
            onClick={() => pauseMut.mutate({ accountId: row.account_id, hours: 1 })}
            className="text-[11px] px-2 py-1 rounded border border-border text-fg-dim hover:bg-bg-elev-1 disabled:opacity-50"
          >
            pause 1h
          </button>
        )}
        <button
          type="button"
          disabled={busy || isPaused}
          onClick={onResync}
          title={isPaused ? "Resume this account first" : undefined}
          className="text-[11px] px-2 py-1 rounded border border-border text-fg-dim hover:bg-bg-elev-1 disabled:opacity-50"
        >
          {isScanning ? "scanning…" : "re-sync"}
        </button>
      </div>
      {collisionMsg && (
        <div className="basis-full pl-5 text-[11px] text-warn">
          {collisionMsg}
        </div>
      )}
      {row.tier === "red" && row.last_error && (
        <div className="basis-full pl-5 text-[11px] text-err font-mono">
          {truncate(row.last_error)}
        </div>
      )}
    </li>
  );
}
