"use client";

/**
 * PaidMessagesTab — owns the filter state and the infinite-scroll list.
 *
 * The date range comes in from the page; everything else (model,
 * employee, status, fan search) is local to this tab so switching to
 * the Tips tab in the future doesn't pollute its filter state.
 *
 * Pagination is keyset (message_id DESC) via usePaidMessages — the
 * sentinel below the last row triggers fetchNextPage() via
 * IntersectionObserver.
 */

import { useEffect, useMemo, useRef, useState } from "react";

import CsvExportButton from "@/components/messages/CsvExportButton";
import MessagesFilters from "@/components/messages/MessagesFilters";
import MessageRow from "@/components/messages/MessageRow";
import { Button } from "@/components/ui/primitives";
import { usePaidMessages, type PaidStatus } from "@/hooks/usePaidMessages";
import { cn } from "@/lib/utils";

interface Props {
  from: string | null;
  to: string | null;
}

export default function PaidMessagesTab({ from, to }: Props) {
  const [accountId, setAccountId] = useState<string | null>(null);
  const [employeeId, setEmployeeId] = useState<number | null>(null);
  const [status, setStatus] = useState<PaidStatus>("all");
  const [fanQuery, setFanQuery] = useState("");

  const params = useMemo(
    () => ({
      from,
      to,
      account_id: accountId,
      employee_id: employeeId,
      status,
      // PPV-tab semantics: outbound priced messages only. type=paid +
      // direction=out are backend defaults, but we set them explicitly
      // here so the CSV export reflects what the tab actually shows
      // (Status: All|Paid|Unpaid still applies — that's what the user
      // ticks to widen between paid-only and paid+unpaid).
      type: "paid" as const,
      direction: "out" as const,
      fan_query: fanQuery,
    }),
    [from, to, accountId, employeeId, status, fanQuery],
  );

  const q = usePaidMessages(params);

  const rows = useMemo(
    () => (q.data?.pages ?? []).flatMap((p) => p.rows),
    [q.data],
  );

  const sentinelRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    const el = sentinelRef.current;
    if (!el) return;
    const io = new IntersectionObserver((entries) => {
      if (entries[0]?.isIntersecting && q.hasNextPage && !q.isFetchingNextPage) {
        q.fetchNextPage();
      }
    }, { rootMargin: "200px" });
    io.observe(el);
    return () => io.disconnect();
    // We want the observer to re-attach whenever the page set or fetch
    // state changes so a freshly-arrived page reliably starts loading
    // the next one when the sentinel is already in view.
  }, [q.hasNextPage, q.isFetchingNextPage, q.fetchNextPage]);

  return (
    <div className="space-y-3">
      <div className="flex items-start justify-between gap-3 flex-wrap">
        <MessagesFilters
          accountId={accountId}
          onAccountChange={setAccountId}
          employeeId={employeeId}
          onEmployeeChange={setEmployeeId}
          status={status}
          onStatusChange={setStatus}
          fanQuery={fanQuery}
          onFanQueryChange={setFanQuery}
        />
        <CsvExportButton params={params} disabled={!from || !to} />
      </div>

      {q.isError && (
        <div className="p-4 text-sm text-err border border-err/30 bg-err/10 rounded-xl">
          Failed to load: {(q.error as Error)?.message || "unknown error"}
        </div>
      )}

      {q.isPending ? (
        <div className="p-8 text-center text-sm text-fg-dim">Loading…</div>
      ) : rows.length === 0 ? (
        <div className="p-8 text-center text-sm text-fg-dim border border-border rounded-xl bg-panel">
          No paid messages in this window.
        </div>
      ) : (
        <div
          className={cn(
            "space-y-2 transition-opacity",
            q.isFetching && q.isPlaceholderData && "opacity-60",
          )}
        >
          {rows.map((r) => (
            <MessageRow key={`${r.account_id}:${r.fan_id}:${r.message_id}`} row={r} />
          ))}

          <div ref={sentinelRef} className="h-4" />

          {q.hasNextPage && (
            <div className="flex justify-center pt-2">
              <Button
                type="button"
                variant="secondary"
                size="sm"
                onClick={() => q.fetchNextPage()}
                disabled={q.isFetchingNextPage}
              >
                {q.isFetchingNextPage ? "Loading…" : "Load more"}
              </Button>
            </div>
          )}

          {!q.hasNextPage && rows.length > 0 && (
            <div className="text-center text-[11px] text-fg-dim py-2">
              · end of results ·
            </div>
          )}
        </div>
      )}
    </div>
  );
}
