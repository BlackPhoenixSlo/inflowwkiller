"use client";

/**
 * useRosterCounts — per-model inbox badge counts for the left roster strip.
 *
 *   • unread    → number of CONVERSATIONS with unread messages (blue badge:
 *                 fans waiting to be read).
 *   • owe_reply → conversations READ but the fan spoke last, i.e. we owe a
 *                 reply (orange badge: seen, not yet answered).
 *
 * Backed by GET /admin/accounts/roster-counts (one cheap local-DB aggregate,
 * scoped to the signed-in principal). Owner-scoped: the /admin endpoint returns
 * empty counts under a chatter-only session, so we don't fire it there (no
 * badges for chatters — graceful). Polls every 60s to match the chat-list cadence.
 */

import { useQuery } from "@tanstack/react-query";

import { relay } from "@/lib/relay";
import { useUser } from "@/contexts/UserContext";
import { useChatter } from "@/contexts/ChatterContext";

export interface RosterCount {
  unread: number;
  owe_reply: number;
}

interface RosterCountsResp {
  counts: Record<string, RosterCount>;
}

const EMPTY: Record<string, RosterCount> = {};

export function useRosterCounts(): Record<string, RosterCount> {
  const { user } = useUser();
  const { chatter } = useChatter();
  const isChatterOnly = !user && !!chatter;

  const q = useQuery<RosterCountsResp>({
    queryKey: ["roster-counts", user?.user_id ?? "anon"],
    queryFn: () => relay.get<RosterCountsResp>("/admin/accounts/roster-counts"),
    enabled: !isChatterOnly && !!user,
    refetchInterval: 60_000,
    staleTime: 30_000,
    refetchOnWindowFocus: false,
  });

  return q.data?.counts ?? EMPTY;
}
