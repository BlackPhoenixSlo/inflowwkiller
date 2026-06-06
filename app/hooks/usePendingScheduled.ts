"use client";

/**
 * usePendingScheduled — in-flight local-wait sends for one chat.
 *
 * Local-wait sends (delay < 10 min) sit in-page via setTimeout instead
 * of going through OF's queue. We surface them to the message list as
 * faint "Sending in N min" bubbles + a cancel button.
 *
 * Storage: TanStack Query cache (key `["pending-scheduled", accountId,
 * fanId]`). The timer Map lives at module scope so cancel can clear it
 * across React unmounts. Both the send hook (writer) and the message
 * list (reader) consult the same key — no separate context needed.
 *
 * IMPORTANT: tab close / hard reload throws away everything in this
 * store. The Composer toast spells this out at queue time; the bubble
 * itself shows "local" so the operator never assumes OF has it.
 */

import { useQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";

import type { OFMedia, VaultMedia } from "@/lib/relay";

export interface PendingScheduled {
  tempId: number;             // negative, like optimistic messages
  text: string;
  fireAt: string;             // ISO
  attached: VaultMedia[];
  price?: number;
  lockedText?: boolean;
  fromUserId?: number;
  fromUserName?: string;
}

const KEY = "pending-scheduled";

export function pendingScheduledKey(accountId: string, fanId: number) {
  return [KEY, accountId, fanId] as const;
}

// Active setTimeout handles, keyed by tempId. Module-level so cancel
// works even if the component that scheduled the send unmounted.
const timerByTempId = new Map<number, ReturnType<typeof setTimeout>>();

export function trackPendingScheduled(
  qc: QueryClient,
  accountId: string,
  fanId: number,
  item: PendingScheduled,
  handle: ReturnType<typeof setTimeout>,
) {
  timerByTempId.set(item.tempId, handle);
  qc.setQueryData<PendingScheduled[]>(pendingScheduledKey(accountId, fanId), (prev) => {
    const next = (prev ?? []).filter((p) => p.tempId !== item.tempId);
    next.push(item);
    return next;
  });
}

export function clearPendingScheduled(
  qc: QueryClient,
  accountId: string,
  fanId: number,
  tempId: number,
) {
  const t = timerByTempId.get(tempId);
  if (t) {
    clearTimeout(t);
    timerByTempId.delete(tempId);
  }
  qc.setQueryData<PendingScheduled[]>(pendingScheduledKey(accountId, fanId), (prev) =>
    (prev ?? []).filter((p) => p.tempId !== tempId),
  );
}

/** Reader hook — message list calls this. Empty array when nothing pending. */
export function usePendingScheduled(accountId: string | null, fanId: number | null) {
  return useQuery<PendingScheduled[]>({
    queryKey: pendingScheduledKey(accountId ?? "", fanId ?? 0),
    queryFn: () => [],
    enabled: !!accountId && fanId != null,
    staleTime: Infinity,
    initialData: [],
  });
}

/** Convert a pending entry into an OFMessage-shaped pseudo-row so the
 *  existing MessageList renderer can show it as a bubble without
 *  special-casing the type. We pre-fill `_pending` so the existing
 *  faded-bubble style kicks in. */
export function pendingToPseudoMessage(p: PendingScheduled): {
  id: number; _tempId: number; _pending: true; _isFutureScheduled: true;
  text: string; createdAt: string;
  fromUser: { id: number; name: string };
  media: OFMedia[]; mediaCount: number; price?: number; _fireAt: string;
} {
  return {
    id: p.tempId,
    _tempId: p.tempId,
    _pending: true,
    _isFutureScheduled: true,
    text: p.text,
    createdAt: p.fireAt,
    fromUser: { id: p.fromUserId ?? -1, name: p.fromUserName ?? "you" },
    media: p.attached.map((m) => ({
      id: m.id,
      type: m.type,
      files: {
        thumb: m.files?.thumb?.url ? { url: m.files.thumb.url } : null,
        source: (() => {
          const u = m.files?.full?.url ?? m.files?.preview?.url;
          return u ? { url: u } : null;
        })(),
      },
    })),
    mediaCount: p.attached.length,
    price: p.price,
    _fireAt: p.fireAt,
  };
}
