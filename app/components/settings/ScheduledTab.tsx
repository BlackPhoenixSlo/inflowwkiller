"use client";

/**
 * ScheduledTab — read + cancel queued sends across all accounts.
 *
 * Surfaces what's about to fire (or already overdue) so the operator can
 * cancel before it goes out. Direct + mass sends share the queue; we
 * label each row by recipient count so a 1-fan send vs a 200-fan blast
 * is unambiguous.
 */

import { useMemo } from "react";
import { Lock, Paperclip } from "lucide-react";

import { Button, Card } from "@/components/ui/primitives";
import { useActiveAccounts } from "@/hooks/useAccounts";
import { useCancelScheduled, useScheduledMessages } from "@/hooks/useScheduledMessages";
import type { OFScheduledMessage } from "@/lib/relay";
import { cn } from "@/lib/utils";

export default function ScheduledTab() {
  const accounts = useActiveAccounts();
  const accountIds = useMemo(() => accounts.map((a) => a.id), [accounts]);
  const { rows, isLoading, isFetching, error, queries } = useScheduledMessages(accountIds);
  const cancelM = useCancelScheduled();

  const refresh = () => queries.forEach((q) => q.refetch());

  // Account id → nickname for the row label. Tiny lookup so the body
  // doesn't need to thread `accounts` through every row.
  const accountName = (id?: string) =>
    accounts.find((a) => a.id === id)?.nickname || id || "?";

  return (
    <div className="space-y-4">
      <Card>
        <div className="flex items-center justify-between gap-3 mb-3 flex-wrap">
          <div>
            <h2 className="text-base font-semibold">Scheduled messages</h2>
            <p className="text-xs text-fg-dim mt-0.5">
              All queued sends across the agency. Cancel before they fire.
            </p>
          </div>
          <Button size="sm" variant="secondary" onClick={refresh} disabled={isFetching}>
            <span className={cn("inline-block mr-1", isFetching && "animate-spin")}>↻</span>
            {isFetching ? "Refreshing…" : "Refresh"}
          </Button>
        </div>

        {error && (
          <div className="text-sm text-err mb-3">
            Failed to load: {error.message}
          </div>
        )}

        {isLoading && rows.length === 0 && (
          <div className="text-sm text-fg-dim">Loading…</div>
        )}

        {!isLoading && rows.length === 0 && (
          <div className="text-xs text-fg-dim italic">Nothing queued.</div>
        )}

        <div className="space-y-2">
          {rows.map((m) => (
            <ScheduledRow
              key={`${m.__accountId}:${m.id}`}
              m={m}
              accountLabel={accountName(m.__accountId)}
              busy={cancelM.isPending && cancelM.variables?.id === m.id}
              onCancel={() => {
                if (!m.__accountId) return;
                if (!confirm("Cancel this scheduled send?")) return;
                cancelM.mutate({ id: m.id, accountId: m.__accountId });
              }}
            />
          ))}
        </div>
      </Card>
    </div>
  );
}

function ScheduledRow({
  m, accountLabel, busy, onCancel,
}: {
  m: OFScheduledMessage;
  accountLabel: string;
  busy: boolean;
  onCancel: () => void;
}) {
  const when = m.scheduledDate ? new Date(m.scheduledDate) : null;
  const overdue = !!when && when.getTime() < Date.now();
  const recipientCount =
    (m.userIds?.length ?? 0) +
    (m.userLists?.length ? 1 : 0) +
    (m.groups?.length ? 1 : 0);
  const isMass = (m.userLists?.length ?? 0) > 0 || (m.groups?.length ?? 0) > 0;

  return (
    <div className={cn(
      "border border-border rounded-md px-3 py-2.5 flex items-start gap-3",
      busy && "opacity-60",
    )}>
      <div className="flex-1 min-w-0 space-y-1">
        <div className="flex items-center gap-2 text-[11px] flex-wrap">
          <span className={cn(
            "px-1.5 py-0.5 rounded",
            overdue ? "bg-warn/15 text-warn" : "bg-accent/15 text-accent",
          )}>
            {when ? when.toLocaleString() : "no time"}
            {overdue && " (overdue)"}
          </span>
          <span className="text-fg-dim">acct {accountLabel}</span>
          {isMass ? (
            <span className="text-fg-dim">📢 mass ({recipientCount || "?"} target{recipientCount === 1 ? "" : "s"})</span>
          ) : (
            <span className="text-fg-dim">→ {m.userIds?.length ?? 0} fan{(m.userIds?.length ?? 0) === 1 ? "" : "s"}</span>
          )}
          {(m.mediaCount ?? 0) > 0 && (
            <span className="text-fg-dim inline-flex items-center gap-1">
              <Paperclip size={12} aria-hidden /> {m.mediaCount}
            </span>
          )}
          {m.price && m.price > 0 && (
            <span className="text-warn inline-flex items-center gap-1">
              <Lock size={12} aria-hidden /> ${m.price.toFixed(2)}
            </span>
          )}
        </div>
        <div className="text-sm whitespace-pre-wrap break-words line-clamp-3">
          {stripHtml(m.text || "")}
        </div>
      </div>
      <Button size="sm" variant="danger" onClick={onCancel} disabled={busy}>
        Cancel
      </Button>
    </div>
  );
}

function stripHtml(s: string): string {
  return s
    .replace(/<br\s*\/?>/gi, "\n")
    .replace(/<\/p\s*>/gi, "\n")
    .replace(/<[^>]+>/g, "")
    .replace(/&nbsp;/g, " ")
    .replace(/&amp;/g, "&")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, "\"")
    .replace(/&#39;/g, "'")
    .trim();
}
