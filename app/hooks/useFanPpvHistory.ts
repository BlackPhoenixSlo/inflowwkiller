"use client";

/**
 * useFanPpvHistory — recent PPV purchases for the fan drawer chart.
 *
 * Backend: GET /admin/fans/{aid}/{fid}/ppv-history?limit=12. Reads from
 * our local `messages` table (the WS pump keeps it within seconds of
 * OF), so this is one cheap index scan — no OF round-trip.
 *
 * 5 min staleTime: purchases are rare enough that the drawer doesn't
 * need to re-fetch on every focus, but fresh enough that opening the
 * drawer after a new unlock shows it.
 */

import { useQuery } from "@tanstack/react-query";

import { relay } from "@/lib/relay";

export interface PpvHistoryItem {
  transaction_id: number;
  /** Null for ledger rows ingested via /payouts/transactions — that feed
   *  doesn't surface the originating message id. Most historical PPVs
   *  for long-tenure fans fall in this bucket. */
  message_id: number | null;
  price_cents: number;
  /** Phase F ledger fields — present on OF-payout-ingested rows, null
   *  on WS-pump-only rows (where we only see the gross unlock event). */
  net_cents: number | null;
  fee_cents: number | null;
  vat_cents: number | null;
  purchased_at: string | null;
  sent_at: string | null;
  sent_by_employee_id: number | null;
  employee_name: string | null;
  employee_color: string | null;
}

export interface PpvHistorySummary {
  count: number;
  max_cents: number;
  avg_cents: number;
  /** All-time per-kind totals across our local transactions ledger —
   *  filled even when `items` is empty. The FanDrawer uses these as a
   *  fallback for OF's subscribedOnData.{tips,messages,posts}Summ when
   *  those are missing (OF often returns nothing for long-tenure or
   *  banned accounts). */
  tips_cents: number;
  messages_cents: number;
  posts_cents: number;
}

export interface PpvHistoryResp {
  items: PpvHistoryItem[];
  summary: PpvHistorySummary;
}

export function useFanPpvHistory(
  accountId: string | null,
  fanId: number | null,
  limit = 12,
) {
  return useQuery<PpvHistoryResp>({
    queryKey: ["fan-ppv-history", accountId, fanId, limit] as const,
    enabled: !!accountId && fanId != null,
    queryFn: () =>
      relay.get<PpvHistoryResp>(
        `/admin/fans/${accountId}/${fanId}/ppv-history?limit=${limit}`,
      ),
    // Keep prior page visible while a bigger-limit refetch is in flight
    // (Load-more button). Without this the list flashes empty for the
    // duration of the OF round-trip.
    placeholderData: (prev) => prev,
    staleTime: 5 * 60 * 1000,
    refetchOnWindowFocus: false,
  });
}
