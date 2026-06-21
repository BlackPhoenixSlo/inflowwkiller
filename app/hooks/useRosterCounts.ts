"use client";

/**
 * useRosterCounts — per-model inbox badge counts for the left roster strip.
 *
 *   • unread    → number of CONVERSATIONS with unread messages (blue badge:
 *                 fans waiting to be read).
 *   • owe_reply → conversations READ but the fan spoke last, i.e. we owe a
 *                 reply (orange badge: seen, not yet answered).
 *
 * Backed by GET /admin/accounts/roster-counts, scoped to the signed-in
 * principal. Both principals get badges: an owner sees its own models, a
 * chatter sees the union across every linked owner (the same set its roster
 * strip already renders). Polls every 60s to match the chat-list cadence.
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

  const q = useQuery<RosterCountsResp>({
    // Key per-principal so an owner's counts can't bleed into a chatter's
    // view (or vice-versa) on an identity switch in the same browser.
    queryKey: ["roster-counts", user?.user_id ?? chatter?.chatter_id ?? "anon"],
    queryFn: () => relay.get<RosterCountsResp>("/admin/accounts/roster-counts"),
    enabled: !!user || !!chatter,
    refetchInterval: 60_000,
    staleTime: 30_000,
    refetchOnWindowFocus: false,
  });

  return q.data?.counts ?? EMPTY;
}
