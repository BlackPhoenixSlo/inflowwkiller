"use client";

/**
 * useFan — fan profile row from our SQLite (NOT OF).
 *
 * The OF chat-list already gives us display_name + avatar; we use this
 * hook for the chatter-owned overlay: custom_nickname, notes, tags,
 * lifetime_spend, plus future Grok-filled facts (real_name, country, …).
 *
 * `GET /admin/fans/{account_id}/{fan_id}` auto-creates a stub row on
 * first access — no 404 dance.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { relay, type FanRecord, type FanUpdate, type OFUserMini } from "@/lib/relay";
import { type FanId } from "@/lib/fanId";

export function useFan(accountId: string | null, fanId: FanId | null) {
  const qc = useQueryClient();
  const queryKey = ["fan", accountId, fanId] as const;

  const q = useQuery<FanRecord>({
    queryKey,
    enabled: !!accountId && fanId != null,
    queryFn: () =>
      relay.get<FanRecord>(`/admin/fans/${accountId}/${fanId}`),
    // 1 day — most fields (custom_nickname, notes, tags) are chatter-owned
    // and update via the mutation below (which writes through). Lifetime
    // spend + future Grok facts come in via the WS pump / batch jobs, so
    // a daily refresh is enough of a safety net.
    staleTime: 24 * 60 * 60 * 1000,
    refetchOnWindowFocus: false,
  });

  const update = useMutation({
    mutationFn: (patch: FanUpdate) =>
      relay.patch<FanRecord>(`/admin/fans/${accountId}/${fanId}`, patch),
    onSuccess: (next) => {
      qc.setQueryData(queryKey, next);
      // Also push the new nickname into the per-fan profile cache that
      // ChatList rows + group panes observe. Without this, editing the
      // nickname only updates the surface header (which reads `fan`
      // directly) — the rail label keeps the old value until the next
      // /users/list refetch restitches it from SQLite.
      //
      // We mirror custom_nickname into BOTH `customNickname` (legacy
      // stitch from the relay) and `displayName` (OF's own per-fan
      // rename) because the drawer write-through pushes the same value
      // to OF. Keeping them aligned in the cache means the row label
      // updates instantly regardless of which fallback the rail prefers.
      if (accountId && fanId != null) {
        qc.setQueryData<OFUserMini>(
          ["of-user", accountId, fanId],
          (prev) => ({
            ...(prev ?? { id: fanId }),
            customNickname: next.custom_nickname ?? null,
            displayName: next.custom_nickname ?? null,
          }),
        );
      }
    },
  });

  return { ...q, update };
}
