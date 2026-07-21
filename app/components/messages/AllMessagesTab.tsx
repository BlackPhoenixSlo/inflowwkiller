"use client";

/**
 * AllMessagesTab — outbound + inbound, free + paid view of /admin/paid-messages.
 *
 * Defaults type=all, direction=out per 18_stats_per_fan_csv.md §Filters list:
 *   • Most users land here to see "all the messages this fan got from us"
 *     rather than "everything every fan sent us today". `direction=out`
 *     respects that without dropping inbound entirely (one toggle to view).
 *   • `type=all` is the meaningful generalization over PPV-tab — keeping
 *     it as the default makes the tab name honest.
 *
 * `fan_id` is wired through from the page-level URL state so a deep-link
 * (clicking "all msgs" from the PPV tab, or following a per-fan card)
 * pre-populates the filter. Clearing it falls back to "all fans."
 */

import { useEffect, useMemo, useRef, useState } from "react";

import CsvExportButton from "@/components/messages/CsvExportButton";
import GroupMassButton from "@/components/messages/GroupMassButton";
import MessageRowGeneric from "@/components/messages/MessageRowGeneric";
import MessagesFilters from "@/components/messages/MessagesFilters";
import { Button } from "@/components/ui/primitives";
import {
  usePaidMessages,
  type MessageDirection,
  type MessageType,
  type PaidStatus,
} from "@/hooks/usePaidMessages";
import { cn } from "@/lib/utils";

interface Props {
  from: string | null;
  to: string | null;
  /** Page-level URL state — when set, the row list filters to this fan
   *  and the filter strip shows the fan-id chip. */
  fanId?: number | null;
  onClearFanId?: () => void;
}

export default function AllMessagesTab({ from, to, fanId, onClearFanId }: Props) {
  const [accountId, setAccountId] = useState<string | null>(null);
  const [employeeId, setEmployeeId] = useState<number | null>(null);
  const [status, setStatus] = useState<PaidStatus>("all");
  const [type, setType] = useState<MessageType>("all");
  const [direction, setDirection] = useState<MessageDirection>("out");
  const [fanQuery, setFanQuery] = useState("");

  // When the user flips Direction → "in", the Employee filter doesn't
  // apply (inbound has no sending employee). Clear it so a leftover value
  // doesn't quietly drop the result set to zero on the next fetch.
  const handleDirectionChange = (d: MessageDirection) => {
    setDirection(d);
    if (d === "in" && employeeId != null) setEmployeeId(null);
  };
  // type=free has no PPV concept, so Status loses meaning. Reset to "all"
  // so the value doesn't filter rows once Status hides.
  const handleTypeChange = (t: MessageType) => {
    setType(t);
    if (t === "free" && status !== "all") setStatus("all");
  };

  const params = useMemo(
    () => ({
      from,
      to,
      account_id: accountId,
      employee_id: employeeId,
      status,
      type,
      direction,
      fan_id: fanId ?? null,
      fan_query: fanQuery,
    }),
    [from, to, accountId, employeeId, status, type, direction, fanId, fanQuery],
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
          type={type}
          onTypeChange={handleTypeChange}
          direction={direction}
          onDirectionChange={handleDirectionChange}
        />
        <div className="hidden md:flex items-center gap-2">
          <GroupMassButton accountId={accountId} />
          <CsvExportButton params={params} disabled={!from || !to} />
        </div>
      </div>

      {fanId != null && (
        <div className="flex items-center gap-2 text-sm md:text-xs text-fg-dim">
          <span>Filtered to fan #{fanId}</span>
          {onClearFanId && (
            <button
              type="button"
              onClick={onClearFanId}
              className="text-accent underline underline-offset-2 md:no-underline md:underline-offset-auto md:hover:underline inline-flex items-center px-3 py-2 -my-2 min-h-11 md:inline-block md:px-0 md:py-0 md:my-0 md:min-h-0"
            >
              clear
            </button>
          )}
        </div>
      )}

      {q.isError && (
        <div className="p-4 text-sm text-err border border-err/30 bg-err/10 rounded-xl">
          Failed to load: {(q.error as Error)?.message || "unknown error"}
        </div>
      )}

      {q.isPending ? (
        <div className="p-8 text-center text-sm text-fg-dim">Loading…</div>
      ) : rows.length === 0 ? (
        <div className="p-8 text-center text-sm text-fg-dim border border-border rounded-xl bg-panel">
          No messages match these filters.
        </div>
      ) : (
        <div
          className={cn(
            "space-y-2 transition-opacity",
            q.isFetching && q.isPlaceholderData && "opacity-60",
          )}
        >
          {rows.map((r) => (
            <MessageRowGeneric
              key={`${r.account_id}:${r.fan_id}:${r.message_id}`}
              row={r}
            />
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
