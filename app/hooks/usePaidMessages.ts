"use client";

/**
 * usePaidMessages — infinite-scroll PPV list for /messages.
 *
 * Backend: GET /admin/paid-messages (keyset on message_id DESC). The hook
 * is paginated, so window-focus refetch is OFF — refetching the first
 * page after scrolling four pages deep would create duplicate-row seams.
 * The user can pull-to-refresh by remounting (date-range change) or hit
 * a manual invalidate; that's enough.
 *
 * `maxPages: 20` is a memory cap. At limit=50 that's 1000 rows in cache
 * which is plenty before the user should narrow the filter.
 */

import { keepPreviousData, useInfiniteQuery } from "@tanstack/react-query";

import { relay } from "@/lib/relay";

export interface PaidMessageFan {
  username: string | null;
  display_name: string | null;
  avatar_url: string | null;
}

export interface PaidMessageRow {
  account_id: string;
  fan_id: number;
  message_id: number;
  fan: PaidMessageFan;
  body: string;
  /** Backend cap (200 for type=paid, 2000 for type=all/free). Set when truncated. */
  body_truncated?: boolean;
  media_count: number;
  price_cents: number;
  is_paid: boolean;
  /** "in" | "out" | "system" — present once the All-Messages tab landed. */
  direction?: "in" | "out" | "system";
  is_tip?: boolean;
  sent_at: string | null;
  /** May be null even when `is_paid=true` (lag from the WS pump). UI
   *  renders this case as "just paid" — do NOT fabricate a timestamp. */
  purchased_at: string | null;
  sent_by_employee_id: number | null;
  employee_name: string | null;
}

export interface PaidMessagesPage {
  rows: PaidMessageRow[];
  next_before_id: number | null;
}

export type PaidStatus = "paid" | "unpaid" | "all";
export type MessageType = "paid" | "free" | "all";
export type MessageDirection = "out" | "in" | "any";

export interface UsePaidMessagesParams {
  from: string | null;
  to: string | null;
  account_id?: string | null;
  employee_id?: number | null;
  status?: PaidStatus;
  /** Defaults to "paid" backend-side so omitting preserves PPV semantics. */
  type?: MessageType;
  /** Defaults to "out" backend-side; same reason. */
  direction?: MessageDirection;
  /** Exact filter on Message.fan_id (per-fan deep link). */
  fan_id?: number | null;
  fan_query?: string | null;
  limit?: number;
}

export function buildPaidMessagesQuery(
  params: UsePaidMessagesParams,
  beforeId: number | null = null,
  extra?: { format?: "json" | "csv" },
): string {
  const qs = new URLSearchParams();
  if (params.from) qs.set("from", params.from);
  if (params.to) qs.set("to", params.to);
  if (params.account_id) qs.set("account_id", params.account_id);
  if (params.employee_id != null) qs.set("employee_id", String(params.employee_id));
  if (params.status && params.status !== "all") qs.set("status", params.status);
  if (params.type && params.type !== "paid") qs.set("type", params.type);
  if (params.direction && params.direction !== "out") qs.set("direction", params.direction);
  if (params.fan_id != null) qs.set("fan_id", String(params.fan_id));
  if (params.fan_query && params.fan_query.length >= 2) qs.set("fan_query", params.fan_query);
  if (extra?.format && extra.format !== "json") qs.set("format", extra.format);
  if (extra?.format !== "csv") qs.set("limit", String(params.limit ?? 50));
  if (extra?.format !== "csv" && beforeId != null) qs.set("before_id", String(beforeId));
  return qs.toString();
}

export function usePaidMessages(params: UsePaidMessagesParams) {
  // `enabled` blocks the call until both date sides are populated — the
  // endpoint requires both. The page guarantees defaults, but during the
  // brief render before the picker hydrates either may be null.
  const enabled = !!params.from && !!params.to;

  return useInfiniteQuery<PaidMessagesPage>({
    queryKey: [
      "paid-messages",
      params.from,
      params.to,
      params.account_id ?? null,
      params.employee_id ?? null,
      params.status ?? "all",
      params.type ?? "paid",
      params.direction ?? "out",
      params.fan_id ?? null,
      params.fan_query && params.fan_query.length >= 2 ? params.fan_query : null,
      params.limit ?? 50,
    ],
    enabled,
    initialPageParam: null as number | null,
    queryFn: ({ pageParam }) =>
      relay.get<PaidMessagesPage>(
        `/admin/paid-messages?${buildPaidMessagesQuery(params, pageParam as number | null)}`,
      ),
    getNextPageParam: (lastPage) => lastPage.next_before_id ?? undefined,
    staleTime: 30_000,
    gcTime: 5 * 60 * 1000,
    refetchOnWindowFocus: false,
    maxPages: 20,
    placeholderData: keepPreviousData,
  });
}
