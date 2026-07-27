"use client";

/**
 * useServerScheduledSends — near-term scheduled sends for one chat, server-side.
 *
 * Source of truth is the relay's `scheduled_jobs` queue (kind="scheduled_send"),
 * fired by the executor at `run_at`. This REPLACES the old in-browser local-wait
 * timer (usePendingScheduled): because the schedule is a DB row, not a tab-bound
 * setTimeout, it
 *   • survives reload / tab close, and
 *   • is shared across every chatter on the account — they all read the same
 *     rows and any of them can cancel before it fires.
 *
 * We poll on a short interval so a fired send's ghost clears (the row leaves
 * 'pending'/'running') and sends scheduled by OTHER chatters appear, without a
 * manual refresh. useInboxRealtime also invalidates this key on outbound SSE so
 * the ghost drops the instant the real bubble lands.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { relay, type OFMessage } from "@/lib/relay";

const KEY = "scheduled-sends";

export interface ServerScheduledSend {
  job_id: number;
  /** `scheduled_send` = a human queued this exact text (body known, cancellable).
   *  `ai_reply` = Human Rhythm paused ai_chatter mid-thread and scheduled its own
   *  wake; `text` is always "" because the draft doesn't exist at defer time, and
   *  it is not cancellable. Absent on responses from an older relay — treat a
   *  missing kind as `scheduled_send`, which is what that relay only ever sent. */
  kind?: "scheduled_send" | "ai_reply";
  fan_id: number;
  run_at: string;        // ISO 8601 (UTC, "…Z")
  status: string;        // 'pending' | 'running'
  text: string;
  price?: number;
  locked_text?: boolean;
  media_count?: number;
  giphy_id?: string | null;
}

interface ListResp {
  list?: ServerScheduledSend[];
}

export function scheduledSendsKey(accountId: string, fanId: number) {
  return [KEY, accountId, fanId] as const;
}

/** Reader hook — ChatSurface calls this; empty array when nothing is queued. */
export function useServerScheduledSends(accountId: string | null, fanId: number | null) {
  return useQuery<ServerScheduledSend[]>({
    queryKey: scheduledSendsKey(accountId ?? "", fanId ?? 0),
    queryFn: async () => {
      const resp = await relay.get<ListResp>(
        `/api/of/v2/scheduled-sends?fan_id=${fanId}`,
        { accountId },
      );
      return (resp.list ?? []).sort(
        (a, b) => new Date(a.run_at).getTime() - new Date(b.run_at).getTime(),
      );
    },
    enabled: !!accountId && fanId != null,
    staleTime: 8_000,
    refetchInterval: 12_000,
    refetchOnWindowFocus: true,
  });
}

export function useCancelServerScheduled() {
  const qc = useQueryClient();
  return useMutation<unknown, Error, { jobId: number; accountId: string; fanId: number }>({
    mutationFn: ({ jobId, accountId }) =>
      relay.delete(`/api/of/v2/scheduled-sends/${jobId}`, { accountId }),
    // Optimistically drop the ghost so the cancel feels instant, then refetch.
    onMutate: ({ jobId, accountId, fanId }) => {
      qc.setQueryData<ServerScheduledSend[]>(
        scheduledSendsKey(accountId, fanId),
        (prev) => (prev ?? []).filter((s) => s.job_id !== jobId),
      );
    },
    onSettled: (_d, _e, { accountId, fanId }) =>
      qc.invalidateQueries({ queryKey: scheduledSendsKey(accountId, fanId) }),
  });
}

/** Convert a queued send into an OFMessage-shaped pseudo-row so the existing
 *  MessageList renderer shows it as a faded, dashed "scheduled" bubble with a
 *  cancel button — no special-casing in the list. `ownerUserId` makes it render
 *  on the outgoing (right) side.
 *
 *  An `ai_reply` row rides the same path but withholds `_scheduleJobId`, which
 *  is what the cancel button gates on — a pending AI reply is a countdown, not
 *  something a chatter can call back. */
export function scheduledToPseudoMessage(
  s: ServerScheduledSend,
  ownerUserId: number | null,
): OFMessage {
  const ai = s.kind === "ai_reply";
  return {
    id: ai ? `ai-${s.job_id}` : `sched-${s.job_id}`,
    _scheduleJobId: ai ? undefined : s.job_id,
    _aiPending: ai,
    _isFutureScheduled: true,
    _fireAt: s.run_at,
    _pending: true,
    text: s.text,
    createdAt: s.run_at,
    fromUser: { id: ownerUserId ?? -1, name: "you" },
    media: [],
    mediaCount: s.media_count ?? 0,
    price: s.price ?? 0,
    lockedText: s.locked_text,
    giphyId: s.giphy_id ?? undefined,
  } as unknown as OFMessage;
}
