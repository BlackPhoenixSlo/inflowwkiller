"use client";

/**
 * useTipsList — infinite-scroll tips list for /messages > Tips tab.
 *
 * Backend: GET /admin/tips-list (keyset on transaction_id DESC). Pagination
 * means window-focus refetch is OFF (refetching page 1 after the user has
 * scrolled four pages deep would leave a duplicate-row seam in the middle).
 * `maxPages: 20` is the memory cap; at limit=50 that's 1000 rows before
 * the user should narrow filters.
 */

import { keepPreviousData, useInfiniteQuery } from "@tanstack/react-query";

import { relay } from "@/lib/relay";

export interface TipsListFan {
  username: string | null;
  display_name: string | null;
  avatar_url: string | null;
}

export interface TipsListRow {
  transaction_id: number;
  account_id: string;
  fan_id: number | null;
  fan: TipsListFan;
  amount_cents: number;
  occurred_at: string | null;
  message_id: number | null;
  tip_note: string | null;
  attributed_employee_id: number | null;
  attributed_employee_name: string | null;
  /** "tip_message" when the tip is chat-linked AND the linked message has
   *  a sent_by_employee_id. Null otherwise — including the chat-linked
   *  case where the message exists but has no attribution. */
  attribution_source: "tip_message" | null;
}

export interface TipsListPage {
  rows: TipsListRow[];
  limit: number;
  next_before_id: number | null;
}

export interface UseTipsListParams {
  from: string | null;
  to: string | null;
  account_id?: string | null;
  employee_id?: number | null;
  fan_query?: string | null;
  limit?: number;
}

function buildQuery(params: UseTipsListParams, beforeId: number | null): string {
  const qs = new URLSearchParams();
  if (params.from) qs.set("from", params.from);
  if (params.to) qs.set("to", params.to);
  if (params.account_id) qs.set("account_id", params.account_id);
  if (params.employee_id != null) qs.set("employee_id", String(params.employee_id));
  if (params.fan_query && params.fan_query.length >= 2) {
    qs.set("fan_query", params.fan_query);
  }
  qs.set("limit", String(params.limit ?? 50));
  if (beforeId != null) qs.set("before_id", String(beforeId));
  return qs.toString();
}

export function useTipsList(params: UseTipsListParams) {
  // Both date sides are required by the backend; gate the fetch until the
  // page-level picker has set them.
  const enabled = !!params.from && !!params.to;

  return useInfiniteQuery<TipsListPage>({
    queryKey: [
      "tips-list",
      params.from,
      params.to,
      params.account_id ?? null,
      params.employee_id ?? null,
      params.fan_query && params.fan_query.length >= 2 ? params.fan_query : null,
      params.limit ?? 50,
    ],
    enabled,
    initialPageParam: null as number | null,
    queryFn: ({ pageParam }) =>
      relay.get<TipsListPage>(
        `/admin/tips-list?${buildQuery(params, pageParam as number | null)}`,
      ),
    getNextPageParam: (lastPage) => lastPage.next_before_id ?? undefined,
    staleTime: 30_000,
    gcTime: 5 * 60 * 1000,
    refetchOnWindowFocus: false,
    maxPages: 20,
    placeholderData: keepPreviousData,
  });
}
