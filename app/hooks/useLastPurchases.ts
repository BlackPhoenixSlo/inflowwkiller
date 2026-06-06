"use client";

/**
 * useFanActivity — per-account view of fan purchase activity built from
 * OF's `/payouts/transactions` feed. One walk of the paged ledger gives
 * us two indexes:
 *
 *   • lastPurchase: Map<"acct:fan", iso>   — most recent revenue row
 *   • recentSpend:  Map<"acct:fan", cents> — sum of positive amounts in the window
 *
 * Why both: `/users/list?view=x.subscribedOnData.totalSumm` is the
 * authoritative lifetime spend, but it sometimes reports 0 for fans
 * who CLEARLY have spent (status transitions, OF cache lag, the
 * occasional missing block in batch view). The chat-list chip prefers
 * the batch number when non-zero and falls back to recentSpend so the
 * row never shows a phantom $0 while last-buy is populated.
 *
 * Cost: up to 4 paged calls (50 per page) per active account, cached
 * 2 min, short-circuits on `hasMore=false`. 200 rows covers weeks of
 * activity for any creator.
 *
 * The legacy `useLastPurchases` export is preserved for callers that
 * only need timestamps (FanDrawer's stat row), implemented in terms of
 * the unified hook so a single network fetch services both.
 */

import { useMemo } from "react";
import { useQueries } from "@tanstack/react-query";

import { relay } from "@/lib/relay";

interface TransactionRow {
  amount?: number;
  net?: number;
  createdAt?: string;
  user?: { id?: number };
  id?: string;
}
interface TransactionsResp { list?: TransactionRow[]; hasMore?: boolean }

const PAGE_SIZE = 50;
// Was 4 (200 transactions). Each open of the FanDrawer would walk all 4
// pages per account; with multiple accounts that's a dozen requests just
// to populate one fan's "last purchase" chip. One page (the most recent
// 50 transactions) is enough for the common case; FanDrawer renders "—"
// when the specific fan isn't found and the user can refresh manually.
const MAX_PAGES = 1;

async function fetchAllPages(accountId: string): Promise<TransactionRow[]> {
  const all: TransactionRow[] = [];
  for (let page = 0; page < MAX_PAGES; page++) {
    const resp = await relay.get<TransactionsResp>(
      `/api/of/v2/payouts/transactions?limit=${PAGE_SIZE}&offset=${page * PAGE_SIZE}`,
      { accountId },
    );
    const rows = resp.list ?? [];
    all.push(...rows);
    if (!resp.hasMore || rows.length < PAGE_SIZE) break;
  }
  return all;
}

export interface FanActivity {
  lastPurchase: Map<string, string>;
  recentSpend: Map<string, number>;
}

export function useFanActivity(accountIds: string[]): FanActivity {
  const queries = useQueries({
    queries: accountIds.map((aid) => ({
      queryKey: ["last-purchases", aid] as const,
      enabled: !!aid,
      // Old transactions don't change; new purchases bubble in via the
      // realtime chat_message event (price>0). 1 day is a safety net for
      // any rows missed by the WS pump (e.g. relay restart, tab opened
      // before pump caught up). Manual refresh is still available via
      // `qc.invalidateQueries(["last-purchases"])`.
      staleTime: 24 * 60 * 60_000,
      gcTime: 24 * 60 * 60_000,
      refetchOnMount: false,
      refetchOnWindowFocus: false,
      refetchOnReconnect: false,
      queryFn: () => fetchAllPages(aid),
    })),
  });

  // `useQueries` hands us a fresh array reference on every render even when
  // the underlying data hasn't changed. Depending on `queries` directly
  // would force the maps to rebuild every render. Key the memo off the
  // per-query `dataUpdatedAt` so we only recompute when real data lands.
  const updatedKey = queries.map((q) => q.dataUpdatedAt).join("|");
  return useMemo(() => {
    const lastPurchase = new Map<string, string>();
    const recentSpend = new Map<string, number>();
    queries.forEach((q, i) => {
      const aid = accountIds[i];
      if (!aid || !q.data) return;
      for (const row of q.data) {
        const uid = row.user?.id;
        if (!uid) continue;
        const ts = row.createdAt;
        const amount = typeof row.amount === "number" ? row.amount : (row.net ?? 0);
        if (amount <= 0) continue;
        const key = `${aid}:${uid}`;
        if (ts && !lastPurchase.has(key)) lastPurchase.set(key, ts);
        recentSpend.set(key, (recentSpend.get(key) ?? 0) + Math.round(amount * 100));
      }
    });
    return { lastPurchase, recentSpend };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [updatedKey, accountIds]);
}

/** Compatibility shim — most callers only need the timestamps. */
export function useLastPurchases(accountIds: string[]): Map<string, string> {
  return useFanActivity(accountIds).lastPurchase;
}
