"use client";

/**
 * usePaidMessages — infinite-scroll PPV list for /messages.
 *
 * Backend: GET /admin/paid-messages (keyset on (sent_at, message_id) DESC, so
 * collapsed mass-broadcast rows interleave by send time). The hook
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
import { type FanId } from "@/lib/fanId";

export interface PaidMessageFan {
  username: string | null;
  display_name: string | null;
  avatar_url: string | null;
}

export interface PaidMessageRow {
  account_id: string;
  fan_id: FanId;
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
  /** Denormalized "mass-<who>" attribution tag stamped on mass rows at send
   *  time (e.g. "mass-kingsley1"). "" / null for non-mass or automation sends. */
  sender_name?: string | null;
  /** Which automation sent this (welcome_chatter_for_info, welcome, send_mass_message, …).
   *  null = human send or a legacy pre-0032 row. */
  automation_kind?: string | null;
  /** True when this row is a COLLAPSED mass broadcast (one row standing in for
   *  N per-fan sends). The fields below are only meaningful then. */
  is_mass_summary?: boolean;
  /** Recipients in the broadcast (authoritative — from mass_broadcast_cache's
   *  sent_count, falling back to the run/placeholder count). */
  recipient_count?: number;
  /** Fans who opened the broadcast (mass_broadcast_cache.viewed_count). Mass
   *  summary rows only. */
  viewed_count?: number;
  /** Funnel/campaign name when the broadcast ran a funnel. */
  funnel_name?: string | null;
  mass_run_id?: number | null;
}

export interface PaidMessagesPage {
  rows: PaidMessageRow[];
  /** Tiebreaker half of the keyset cursor (message_id of the last row). */
  next_before_id: number | null;
  /** Primary half of the keyset cursor (ISO send-time of the last row). */
  next_before_sent_at: string | null;
}

/** Composite keyset cursor: page by (sent_at, message_id) DESC so mass-broadcast
 *  summaries (synthetic 5e15 message_ids) interleave chronologically. */
export interface PaidMessagesCursor {
  sent_at: string | null;
  id: number;
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
  fan_id?: FanId | null;
  fan_query?: string | null;
  limit?: number;
}

export function buildPaidMessagesQuery(
  params: UsePaidMessagesParams,
  cursor: PaidMessagesCursor | null = null,
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
  if (extra?.format !== "csv" && cursor != null) {
    // Composite (sent_at, message_id) cursor — both halves so the backend can
    // resume the time-ordered keyset.
    qs.set("before_id", String(cursor.id));
    if (cursor.sent_at != null) qs.set("before_sent_at", cursor.sent_at);
  }
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
    initialPageParam: null as PaidMessagesCursor | null,
    queryFn: ({ pageParam }) =>
      relay.get<PaidMessagesPage>(
        `/admin/paid-messages?${buildPaidMessagesQuery(params, pageParam as PaidMessagesCursor | null)}`,
      ),
    getNextPageParam: (lastPage): PaidMessagesCursor | undefined =>
      lastPage.next_before_id != null
        ? { sent_at: lastPage.next_before_sent_at, id: lastPage.next_before_id }
        : undefined,
    staleTime: 30_000,
    gcTime: 5 * 60 * 1000,
    refetchOnWindowFocus: false,
    maxPages: 20,
    placeholderData: keepPreviousData,
  });
}
