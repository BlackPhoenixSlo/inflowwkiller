"use client";

/**
 * useScheduledMessages — list + cancel OF's queued sends.
 *
 * Source of truth is `/schedules` (the calendar OF web's own /my/queue
 * page reads from). `/messages/queue` is an older endpoint that returns
 * stale or empty data for sends scheduled via the new web flow, which
 * is why our Settings tab was always empty.
 *
 * /schedules wraps each row as `{type:'chat'|'post', publishDateTime,
 * entity:{...real payload}}` — we flatten chat-type entries onto the
 * OFScheduledMessage shape and ignore posts (Fastt is messaging-only
 * for now).
 *
 * Each row gets `__accountId` so the cancel mutation routes back through
 * the right session — same pattern as the unified chat-list.
 */

import { useMutation, useQueries, useQueryClient } from "@tanstack/react-query";

import { relay, type OFScheduledMessage } from "@/lib/relay";

const KEY_PREFIX = "scheduled";

interface ScheduleItem {
  type?: string;
  publishDateTime?: string;
  entity?: Record<string, unknown>;
}

interface SchedulesResp {
  list?: ScheduleItem[];
}

const TZ = (typeof Intl !== "undefined" && Intl.DateTimeFormat().resolvedOptions().timeZone) || "UTC";

/** Range: today → +1y, mirroring legacy /ui/ which mirrors what OF web sends
 *  when no date is picked. yyyy-mm-dd format. */
function rangeParams(): string {
  const today = new Date();
  const oneYear = new Date();
  oneYear.setFullYear(today.getFullYear() + 1);
  // Format in local time so the date boundary agrees with the time_zone hint
  // below — toISOString() would format in UTC and drift a day near midnight.
  const fmt = (d: Date) => {
    const y = d.getFullYear();
    const m = String(d.getMonth() + 1).padStart(2, "0");
    const day = String(d.getDate()).padStart(2, "0");
    return `${y}-${m}-${day}`;
  };
  return `limit=50&publish_date=${fmt(today)}&publish_date_end=${fmt(oneYear)}&time_zone=${encodeURIComponent(TZ)}`;
}

export function useScheduledMessages(accountIds: string[]) {
  const queries = useQueries({
    queries: accountIds.map((accountId) => ({
      queryKey: [KEY_PREFIX, accountId],
      queryFn: async (): Promise<OFScheduledMessage[]> => {
        const resp = await relay.get<SchedulesResp>(
          `/api/of/v2/schedules?${rangeParams()}`,
          { accountId },
        );
        const list = resp.list ?? [];
        return list
          .filter(
            (it) =>
              it.type === "chat" &&
              it.entity &&
              Number.isFinite(Number((it.entity as Record<string, unknown>).id)),
          )
          .map((it) => {
            const e = it.entity as Record<string, unknown>;
            return {
              id: Number(e.id),
              text: typeof e.text === "string" ? e.text : "",
              scheduledDate: (it.publishDateTime as string) || (e.scheduledDate as string) || undefined,
              price: typeof e.price === "number" ? e.price : 0,
              lockedText: !!e.lockedText,
              mediaCount: typeof e.mediaCount === "number" ? e.mediaCount : 0,
              media: (e.media as OFScheduledMessage["media"]) ?? [],
              userIds: (e.userIds as number[]) ?? [],
              userLists: (e.userLists as string[]) ?? [],
              groups: (e.groups as string[]) ?? [],
              __accountId: accountId,
            } satisfies OFScheduledMessage;
          });
      },
      staleTime: 30 * 1000,
      refetchOnWindowFocus: false,
    })),
  });

  const rows = queries
    .flatMap((q) => q.data ?? [])
    // OF returns newest-first; flip to soonest-first since the UI is "what's
    // about to go out". Missing scheduledDate sinks to the bottom.
    .sort((a, b) => {
      const ta = a.scheduledDate ? new Date(a.scheduledDate).getTime() : Infinity;
      const tb = b.scheduledDate ? new Date(b.scheduledDate).getTime() : Infinity;
      return ta - tb;
    });

  const isLoading = queries.some((q) => q.isLoading);
  const isFetching = queries.some((q) => q.isFetching);
  const error = queries.find((q) => q.error)?.error as Error | undefined;

  return { rows, isLoading, isFetching, error, queries };
}

export function useCancelScheduled() {
  const qc = useQueryClient();
  return useMutation<unknown, Error, { id: number; accountId: string }>({
    mutationFn: ({ id, accountId }) =>
      relay.delete(`/api/of/v2/messages/queue/${id}`, { accountId }),
    onSuccess: (_data, { accountId }) =>
      qc.invalidateQueries({ queryKey: [KEY_PREFIX, accountId] }),
  });
}
