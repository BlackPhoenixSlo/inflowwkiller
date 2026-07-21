"use client";

/**
 * UnattributedDrawer — lazy-loaded debug drawer for the most-recent
 * outbound messages we couldn't tie to a chatter.
 *
 * Collapsed by default; the GET only fires once the user expands the
 * drawer (`useUnattributedRecent(limit, enabled=expanded)`). Closing it
 * doesn't blow away the data — staleTime/gcTime still apply — so a
 * quick re-open is free.
 */

import { useState } from "react";

import { Card } from "@/components/ui/primitives";
import { cn } from "@/lib/utils";
import { fmtDateTime } from "@/lib/format";
import { useUnattributedRecent } from "@/hooks/useStats";

export default function UnattributedDrawer() {
  const [open, setOpen] = useState(false);
  const q = useUnattributedRecent(20, open);

  const rows = q.data?.messages ?? [];

  return (
    <Card className="p-0 overflow-hidden">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="w-full text-left p-4 flex items-center justify-between hover:bg-bg-elev-1/40 transition-colors"
        aria-expanded={open}
      >
        <div>
          <div className="text-sm font-medium text-fg">Recent unattributed sends</div>
          <div className="text-xs text-fg-dim mt-0.5">
            Outbound messages with no <code className="font-mono text-[11px]">sent_by_employee_id</code> — typically
            automation gaps or pre-attribution backfill.
          </div>
        </div>
        <span className="text-fg-dim text-sm select-none">{open ? "▴" : "▾"}</span>
      </button>

      {open && (
        <div className="border-t border-border">
          {q.isLoading ? (
            <div className="p-4 text-sm text-fg-dim">Loading recent unattributed…</div>
          ) : q.isError ? (
            <div className="p-4 text-sm text-err">
              {(q.error as Error)?.message || "Failed to load"}
            </div>
          ) : rows.length === 0 ? (
            <div className="p-4 text-sm text-fg-dim">
              No unattributed outbound messages — everything is signed off.
            </div>
          ) : (
            <div className="overflow-x-auto">
            <table
              className={cn(
                "w-full min-w-[520px] text-sm transition-opacity",
                q.isFetching && q.isPlaceholderData && "opacity-60",
              )}
            >
              <thead>
                <tr className="text-[11px] uppercase tracking-wide text-fg-dim border-b border-border">
                  <th className="text-left px-4 py-2 font-medium">When</th>
                  <th className="text-left px-4 py-2 font-medium">Account</th>
                  <th className="text-left px-4 py-2 font-medium">Fan</th>
                  <th className="text-left px-4 py-2 font-medium">Body</th>
                  <th className="text-right px-4 py-2 font-medium">Media</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r, i) => (
                  <tr key={`${r.account_id}:${r.message_id ?? i}`} className="border-b border-border last:border-0">
                    <td className="px-4 py-2 text-xs text-fg-dim whitespace-nowrap">
                      {fmtDateTime(r.created_at)}
                    </td>
                    <td className="px-4 py-2 text-xs font-mono text-fg-dim">{r.account_id}</td>
                    <td className="px-4 py-2 text-xs tabular-nums">
                      {r.fan_id ?? "—"}
                    </td>
                    <td className={cn(
                      "px-4 py-2 text-xs max-w-md truncate",
                      r.body ? "text-fg" : "text-fg-dim italic",
                    )}>
                      {r.body || "(no text)"}
                    </td>
                    <td className="px-4 py-2 text-right text-xs tabular-nums">
                      {r.media_count || ""}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            </div>
          )}
        </div>
      )}
    </Card>
  );
}
