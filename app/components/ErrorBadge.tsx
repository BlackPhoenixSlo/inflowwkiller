"use client";

/**
 * ErrorBadge — small 🐞 indicator in TopNav showing how many errors
 * have landed in /admin/errors in the last 24h. Click opens a popover
 * with the recent rows so we can triage without leaving the inbox.
 *
 * Hidden entirely when count is 0 so it doesn't clutter the nav in the
 * happy path. Polls every 60s (cheap — one indexed query against a
 * small table).
 */

import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { relay } from "@/lib/relay";
import { fmtRelTime, cn } from "@/lib/utils";
import { useDeferredMount } from "@/hooks/useDeferredMount";

interface ErrorRow {
  id: number;
  occurred_at: string;
  source: string;
  kind: string;
  message: string;
  stack: string | null;
  url: string | null;
}

interface ErrorListResp {
  count: number;
  since_hours: number;
  list: ErrorRow[];
}

export function ErrorBadge() {
  const [open, setOpen] = useState(false);
  const popRef = useRef<HTMLDivElement | null>(null);
  const qc = useQueryClient();
  const clear = useMutation({
    mutationFn: () => relay.delete<{ ok: boolean; deleted: number }>("/admin/errors?since_hours=24"),
    onSuccess: () => {
      qc.setQueryData<ErrorListResp>(["app-errors", "recent"], (prev) =>
        prev ? { ...prev, count: 0, list: [] } : prev,
      );
      qc.invalidateQueries({ queryKey: ["app-errors", "recent"] });
      setOpen(false);
    },
  });

  // Defer the first /admin/errors fetch by ~1s so it doesn't compete
  // with the chat-list fetch for relay threads on cold load. The badge
  // only matters as a "did something break in the last 24h" indicator;
  // a one-second delay is invisible to anyone not staring at the icon.
  const ready = useDeferredMount(1000);
  const q = useQuery<ErrorListResp>({
    queryKey: ["app-errors", "recent"],
    queryFn: () => relay.get<ErrorListResp>("/admin/errors?since_hours=24&limit=50"),
    enabled: ready,
    refetchInterval: 5 * 60_000,
    refetchOnWindowFocus: false,
    staleTime: 30_000,
  });

  useEffect(() => {
    if (!open) return;
    const onClick = (e: MouseEvent) => {
      if (!popRef.current?.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", onClick);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onClick);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const count = q.data?.count ?? 0;
  if (count === 0 && !open) return null;

  return (
    <div className="relative" ref={popRef}>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className={cn(
          "h-8 px-2 grid place-items-center rounded-lg text-sm border",
          count > 0
            ? "bg-err/15 border-err/40 text-err hover:bg-err/25"
            : "bg-bg-elev-1 border-border text-fg-dim hover:bg-bg-elev-2",
        )}
        title={`${count} error${count === 1 ? "" : "s"} in last 24h`}
        aria-label="Recent errors"
      >
        <span>🐞</span>
        {count > 0 && <span className="ml-1 text-[11px] font-semibold">{count}</span>}
      </button>
      {open && (
        <div className="absolute right-0 mt-1 w-[420px] max-h-[60vh] bg-panel border border-border rounded-lg shadow-xl z-40 flex flex-col">
          <header className="px-3 py-2 border-b border-border flex items-center justify-between gap-2">
            <div className="text-sm font-semibold">Recent errors</div>
            <div className="flex items-center gap-2">
              <span className="text-[11px] text-fg-dim">
                {count} in {q.data?.since_hours ?? 24}h
              </span>
              {count > 0 && (
                <button
                  type="button"
                  onClick={() => clear.mutate()}
                  disabled={clear.isPending}
                  className="text-[11px] px-2 py-0.5 rounded border border-border bg-bg-elev-1 hover:bg-bg-elev-2 text-fg-dim hover:text-fg disabled:opacity-50"
                  title="Dismiss all errors from the last 24h"
                >
                  {clear.isPending ? "Clearing…" : "Clear"}
                </button>
              )}
            </div>
          </header>
          <div className="flex-1 overflow-y-auto">
            {q.isLoading && (
              <div className="p-3 text-xs text-fg-dim">Loading…</div>
            )}
            {!q.isLoading && count === 0 && (
              <div className="p-3 text-xs text-fg-dim">No errors. 🎉</div>
            )}
            {q.data?.list.map((row) => (
              <ErrorEntry key={row.id} row={row} />
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function ErrorEntry({ row }: { row: ErrorRow }) {
  const [expanded, setExpanded] = useState(false);
  return (
    <div className="border-b border-border/60 px-3 py-2 text-xs">
      <div className="flex items-baseline gap-2">
        <span
          className={cn(
            "text-[10px] uppercase font-semibold tracking-wide px-1.5 py-0.5 rounded",
            row.source === "server" ? "bg-warn/15 text-warn" : "bg-err/15 text-err",
          )}
        >
          {row.source}
        </span>
        <span className="text-fg-dim text-[10px]">{row.kind}</span>
        <span className="text-fg-dim text-[10px] ml-auto">
          {fmtRelTime(row.occurred_at)}
        </span>
      </div>
      <div className="mt-1 break-words text-fg">{row.message}</div>
      {row.url && (
        <div className="mt-0.5 text-[10px] text-fg-dim break-all">{row.url}</div>
      )}
      {row.stack && (
        <button
          type="button"
          onClick={() => setExpanded((v) => !v)}
          className="mt-1 text-[10px] text-fg-dim hover:text-fg underline underline-offset-2"
        >
          {expanded ? "Hide stack" : "Show stack"}
        </button>
      )}
      {expanded && row.stack && (
        <pre className="mt-1 text-[10px] text-fg-dim bg-bg-elev-1 border border-border rounded p-1.5 overflow-auto max-h-40">
          {row.stack}
        </pre>
      )}
    </div>
  );
}
