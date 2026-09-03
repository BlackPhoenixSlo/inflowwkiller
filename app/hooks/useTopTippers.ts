"use client";

/**
 * useTopTippers — right-rail leaderboard for /messages.
 *
 * Backend: GET /admin/top-tippers. Single page (the leaderboard caps at
 * 100 rows server-side), so window-focus refetch is ON — a chatter who
 * just landed a $200 tip wants to see it bubble up immediately when they
 * tab back to the page.
 */

import { useQuery } from "@tanstack/react-query";

import { relay } from "@/lib/relay";
import { type FanId } from "@/lib/fanId";

export interface TopTipperFan {
  username: string | null;
  display_name: string | null;
  avatar_url: string | null;
}

export interface TopTipperRow {
  rank: number;
  fan_id: FanId;
  account_id: string;
  fan: TopTipperFan;
  total_cents: number;
  tip_count: number;
  last_tip_at: string | null;
}

export interface TopTippersResp {
  rows: TopTipperRow[];
}

export interface UseTopTippersParams {
  from: string | null;
  to: string | null;
  account_id?: string | null;
  employee_id?: number | null;
  limit?: number;
}

function buildQuery(params: UseTopTippersParams): string {
  const qs = new URLSearchParams();
  if (params.from) qs.set("from", params.from);
  if (params.to) qs.set("to", params.to);
  if (params.account_id) qs.set("account_id", params.account_id);
  if (params.employee_id != null) qs.set("employee_id", String(params.employee_id));
  if (params.limit != null) qs.set("limit", String(params.limit));
  return qs.toString();
}

export function useTopTippers(params: UseTopTippersParams) {
  const enabled = !!params.from && !!params.to;

  return useQuery<TopTippersResp>({
    queryKey: [
      "top-tippers",
      params.from,
      params.to,
      params.account_id ?? null,
      params.employee_id ?? null,
      params.limit ?? 20,
    ],
    enabled,
    queryFn: () =>
      relay.get<TopTippersResp>(`/admin/top-tippers?${buildQuery(params)}`),
    staleTime: 2 * 60_000,
    gcTime: 5 * 60 * 1000,
    refetchOnWindowFocus: false,
  });
}
