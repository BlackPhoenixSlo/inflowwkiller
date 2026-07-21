"use client";

/**
 * ScheduledForChat — per-chat indicator + popover of queued sends.
 *
 * Reads from the same /messages/queue endpoint as the Settings tab,
 * filtered to this fan's pending sends. Click to open a popover; each
 * row has a Cancel button so the operator can ditch a queued send
 * without leaving the chat.
 *
 * We only show direct sends to this fan (not mass ones that happen to
 * include them) — picking apart mass-sends is the Settings tab's job.
 */

import { useEffect, useRef, useState } from "react";

import { useCancelScheduled, useScheduledMessages } from "@/hooks/useScheduledMessages";
import { stripOFHtml } from "@/lib/ofHtml";
import { proxyImage } from "@/lib/relay";

export function ScheduledForChat({
  accountId, fanId,
}: { accountId: string; fanId: number }) {
  const { rows, queries } = useScheduledMessages([accountId]);
  const cancelM = useCancelScheduled();
  const [open, setOpen] = useState(false);
  const popoverRef = useRef<HTMLDivElement | null>(null);

  // Only this chat's direct sends.
  const mine = rows.filter(
    (m) => (m.userIds?.length ?? 0) === 1 && m.userIds?.[0] === fanId,
  );

  // Close on outside click.
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (popoverRef.current && !popoverRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    window.addEventListener("pointerdown", onDown);
    return () => window.removeEventListener("pointerdown", onDown);
  }, [open]);

  if (mine.length === 0) return null;

  return (
    <div className="static md:relative">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="text-[11px] px-1.5 py-0.5 rounded border border-accent/40 bg-accent/10 text-accent hover:bg-accent/15"
        title={`${mine.length} scheduled for this fan — click to view/cancel`}
      >
        🕐 {mine.length}
      </button>
      {open && (
        <div
          ref={popoverRef}
          className="absolute top-full mt-1 left-0 right-0 w-auto md:left-auto md:right-0 md:w-80 bg-panel border border-border rounded-lg shadow-lg z-30 max-h-96 overflow-y-auto"
        >
          <div className="px-3 py-2 border-b border-border text-xs font-medium">
            Scheduled for this fan
          </div>
          <div className="p-2 space-y-2">
            {mine.map((m) => {
              const busy = cancelM.isPending && cancelM.variables?.id === m.id;
              const when = m.scheduledDate ? new Date(m.scheduledDate) : null;
              const firstThumb =
                m.media?.[0]?.files?.thumb?.url ||
                m.media?.[0]?.files?.preview?.url ||
                null;
              return (
                <div
                  key={m.id}
                  className={`border border-border rounded-md p-2 text-xs space-y-1 ${busy ? "opacity-60" : ""}`}
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-[10px] text-fg-dim">
                      {when ? when.toLocaleString() : "no time"}
                    </span>
                    <div className="flex items-center gap-1.5 text-[10px]">
                      {(m.mediaCount ?? 0) > 0 && (
                        <span className="text-fg-dim">📎 {m.mediaCount}</span>
                      )}
                      {m.price && m.price > 0 && (
                        <span className="text-warn">🔒 ${m.price.toFixed(2)}</span>
                      )}
                    </div>
                  </div>
                  <div className="flex items-start gap-2">
                    {firstThumb && (
                      <img
                        src={proxyImage(firstThumb, accountId)}
                        alt=""
                        loading="lazy"
                        decoding="async"
                        className="w-10 h-10 rounded object-cover border border-border shrink-0"
                      />
                    )}
                    <div className="flex-1 min-w-0 line-clamp-3 break-words">
                      {stripOFHtml(m.text || "")}
                    </div>
                  </div>
                  <div className="flex justify-end">
                    <button
                      type="button"
                      onClick={() => {
                        if (!confirm("Cancel this scheduled send?")) return;
                        cancelM.mutate(
                          { id: m.id, accountId },
                          { onSuccess: () => queries.forEach((q) => q.refetch()) },
                        );
                      }}
                      disabled={busy}
                      className="text-[10px] px-2 py-1.5 md:py-0.5 rounded border border-err/40 text-err hover:bg-err/10 disabled:opacity-40"
                    >
                      {busy ? "Cancelling…" : "Cancel"}
                    </button>
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}
