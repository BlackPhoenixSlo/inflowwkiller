"use client";

/**
 * TipsTab — owns the filter state + infinite-scroll list for the Tips
 * surface in /messages. Mirrors PaidMessagesTab conventions; the date
 * range still comes in from the page so both tabs share the window.
 *
 * `presetFanQuery` is set by the parent when the user clicks a row in
 * TopTippersCard — when it changes, we adopt it into local state so the
 * filter input reflects the click. The user can still edit it after.
 *
 * Pagination is keyset (transaction_id DESC) via useTipsList; the
 * sentinel triggers fetchNextPage() via IntersectionObserver and a
 * manual "Load more" button is rendered as the keyboard escape hatch.
 */

import { useEffect, useMemo, useRef, useState } from "react";

import MessagesFilters from "@/components/messages/MessagesFilters";
import TipRow from "@/components/messages/TipRow";
import { Button } from "@/components/ui/primitives";
import { useTipsList } from "@/hooks/useTipsList";
import { cn } from "@/lib/utils";

interface Props {
  from: string | null;
  to: string | null;
  /** Bumped by the page when the user clicks a Top Tippers row. Latest
   *  non-empty value is mirrored into the local fanQuery state. */
  presetFanQuery?: string | null;
}

export default function TipsTab({ from, to, presetFanQuery }: Props) {
  const [accountId, setAccountId] = useState<string | null>(null);
  const [employeeId, setEmployeeId] = useState<number | null>(null);
  const [fanQuery, setFanQuery] = useState("");

  // Adopt the preset whenever the parent bumps it. Empty string from
  // the parent clears the filter; null leaves the current value alone.
  useEffect(() => {
    if (presetFanQuery == null) return;
    setFanQuery(presetFanQuery);
  }, [presetFanQuery]);

  const q = useTipsList({
    from,
    to,
    account_id: accountId,
    employee_id: employeeId,
    fan_query: fanQuery,
  });

  const rows = useMemo(
    () => (q.data?.pages ?? []).flatMap((p) => p.rows),
    [q.data],
  );

  const sentinelRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    const el = sentinelRef.current;
    if (!el) return;
    const io = new IntersectionObserver(
      (entries) => {
        if (entries[0]?.isIntersecting && q.hasNextPage && !q.isFetchingNextPage) {
          q.fetchNextPage();
        }
      },
      { rootMargin: "200px" },
    );
    io.observe(el);
    return () => io.disconnect();
  }, [q.hasNextPage, q.isFetchingNextPage, q.fetchNextPage]);

  return (
    <div className="space-y-3">
      <MessagesFilters
        accountId={accountId}
        onAccountChange={setAccountId}
        employeeId={employeeId}
        onEmployeeChange={setEmployeeId}
        // Status is a PPV concept (paid/unpaid). Tips don't have it, so we
        // hide the toggle entirely (showStatus={false}) and pass the
        // still-required status props as inert no-ops.
        status="all"
        onStatusChange={() => {}}
        showStatus={false}
        fanQuery={fanQuery}
        onFanQueryChange={setFanQuery}
      />

      {q.isError && (
        <div className="p-4 text-sm text-err border border-err/30 bg-err/10 rounded-xl">
          Failed to load: {(q.error as Error)?.message || "unknown error"}
        </div>
      )}

      {q.isPending ? (
        <div className="p-8 text-center text-sm text-fg-dim">Loading…</div>
      ) : rows.length === 0 ? (
        <div className="p-8 text-center text-sm text-fg-dim border border-border rounded-xl bg-panel">
          No tips in this window.
        </div>
      ) : (
        <div
          className={cn(
            "space-y-2 transition-opacity",
            q.isFetching && q.isPlaceholderData && "opacity-60",
          )}
        >
          {rows.map((r) => (
            <TipRow key={r.transaction_id} row={r} />
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
