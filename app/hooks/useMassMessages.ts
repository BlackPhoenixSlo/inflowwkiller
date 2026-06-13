"use client";

/**
 * useMassMessages — list + cancel past mass-message broadcasts.
 *
 * Reads from the relay's local SQLite cache (`mass_broadcast_cache`) via
 * `/admin/mass-messages` for a snappy first render. The Refresh button
 * triggers POST `/admin/mass-messages/refresh`, which pulls from OF +
 * upserts the cache + returns the fresh rows in one roundtrip.
 *
 * The previous live-OF passthrough (`/api/of/v2/users/me/stats/messages/group`)
 * still works and write-throughs into the same cache — used as a fallback
 * if the DB is empty on first ever visit (no cached rows yet).
 *
 * Each row stamped with `__accountId` so cancel routes through the
 * right session.
 */

import { useMemo } from "react";

import { useMutation, useQueries, useQueryClient, type QueryClient } from "@tanstack/react-query";

import { relay, type VaultFiles } from "@/lib/relay";

const KEY_PREFIX = "mass-messages";

export interface MassMessageRow {
  id: number;
  date?: string | null;
  text?: string | null;
  rawText?: string | null;
  giphyId?: string | null;
  isFree?: boolean;
  isTip?: boolean;
  mediaCount?: number;
  media?: Array<{
    id: number;
    type?: string;
    files?: VaultFiles | null;
  }>;
  previews?: number[];
  sentCount?: number;
  viewedCount?: number;
  isCanceled?: boolean;
  canUnsend?: boolean;
  unsendSeconds?: number | null;
  template?: string | null;
  fetched_at?: string | null;
  /** Which automation produced this broadcast (joined via mass_runs.queue_id).
   *  null = a manual UI broadcast or a pre-0032 run. */
  automationKind?: string | null;
  /** Funnel/campaign name when the broadcast ran a funnel. */
  funnelName?: string | null;
  __accountId?: string;
}

interface AdminListResp {
  account_id: string;
  days_back: number;
  items: MassMessageRow[];
}

interface AdminRefreshResp extends AdminListResp {
  written: number;
}

export interface MassMessagesOptions {
  daysBack?: number;
  limit?: number;
}

async function fetchCached(accountId: string, daysBack: number, limit: number): Promise<MassMessageRow[]> {
  const qs = new URLSearchParams({
    account_id: accountId,
    days_back: String(daysBack),
    limit: String(limit),
  });
  const resp = await relay.get<AdminListResp>(`/admin/mass-messages?${qs.toString()}`);
  return (resp.items ?? []).map((it) => ({ ...it, __accountId: accountId }));
}

export function useMassMessages(accountIds: string[], opts: MassMessagesOptions = {}) {
  const daysBack = opts.daysBack ?? 30;
  const limit = opts.limit ?? 100;

  const queries = useQueries({
    queries: accountIds.map((accountId) => ({
      queryKey: [KEY_PREFIX, accountId, daysBack, limit] as const,
      queryFn: () => fetchCached(accountId, daysBack, limit),
      // Cache is local SQLite — practically instant + cheap to re-read,
      // but we still get React Query's de-duplication across mounts.
      staleTime: 5 * 60 * 1000,   // 5min before considered stale
      gcTime: 30 * 60 * 1000,     // keep in memory across nav for 30min
      refetchOnWindowFocus: false,
      // Hold the previously-rendered rows during refetch/key-change so
      // the list doesn't flicker to empty when `daysBack` changes.
      placeholderData: (prev: MassMessageRow[] | undefined) => prev,
    })),
  });

  // Memoize the flatten+sort on the array of per-account `q.data` references so
  // it only re-allocates when an underlying query's data actually changes, not
  // on every parent render (useQueries hands back fresh query objects each
  // render, so depending on `queries` directly would recompute every time).
  const data = queries.map((q) => q.data);
  const rows = useMemo(
    () =>
      data
        .flatMap((d) => d ?? [])
        .sort((a, b) => {
          const ta = a.date ? new Date(a.date).getTime() : 0;
          const tb = b.date ? new Date(b.date).getTime() : 0;
          return tb - ta;
        }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    data,
  );

  const isLoading = queries.some((q) => q.isLoading);
  const isFetching = queries.some((q) => q.isFetching);
  const error = queries.find((q) => q.error)?.error as Error | undefined;

  return { rows, isLoading, isFetching, error, queries, daysBack, limit };
}

/** Hit OF + upsert into the cache for every account, then invalidate
 *  the read queries so they pick up the fresh rows. One mutation call
 *  fans out to N accounts. */
export function useRefreshMassMessages() {
  const qc = useQueryClient();
  return useMutation<
    { accountId: string; written: number }[],
    Error,
    { accountIds: string[]; daysBack: number; limit?: number }
  >({
    mutationFn: async ({ accountIds, daysBack, limit = 100 }) => {
      const results = await Promise.all(
        accountIds.map(async (accountId) => {
          const qs = new URLSearchParams({
            account_id: accountId,
            days_back: String(daysBack),
            limit: String(limit),
          });
          const resp = await relay.post<AdminRefreshResp>(
            `/admin/mass-messages/refresh?${qs.toString()}`,
            undefined,
          );
          return { accountId, written: resp.written };
        }),
      );
      return results;
    },
    onSuccess: (_data, vars) => {
      // Invalidate every per-account list query; React Query will
      // refetch from the DB and pick up the new rows.
      vars.accountIds.forEach((accountId) => {
        qc.invalidateQueries({ queryKey: [KEY_PREFIX, accountId] });
      });
    },
  });
}

export function useCancelMassMessage() {
  const qc = useQueryClient();
  return useMutation<unknown, Error, { id: number; accountId: string }>({
    mutationFn: ({ id, accountId }) =>
      relay.delete(`/api/of/v2/messages/queue/${id}`, { accountId }),
    onSuccess: (_data, { accountId, id }) => {
      // Optimistically flip the row's `isCanceled` so the UI stops
      // showing it as unsendable without waiting for the refetch.
      patchRowInCaches(qc, accountId, id);
      qc.invalidateQueries({ queryKey: [KEY_PREFIX, accountId] });
    },
  });
}

function patchRowInCaches(qc: QueryClient, accountId: string, queueId: number): void {
  qc.getQueryCache()
    .findAll({ queryKey: [KEY_PREFIX, accountId] })
    .forEach((q) => {
      const rows = q.state.data as MassMessageRow[] | undefined;
      if (!rows) return;
      const next = rows.map((r) =>
        r.id === queueId
          ? { ...r, isCanceled: true, canUnsend: false, unsendSeconds: 0 }
          : r,
      );
      qc.setQueryData(q.queryKey, next);
    });
}
