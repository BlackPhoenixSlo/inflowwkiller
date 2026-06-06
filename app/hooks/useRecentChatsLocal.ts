"use client";

/**
 * useRecentChatsLocal — instant-load seed for the inbox.
 *
 * Reads the LOCAL `chats` table via /admin/chats/recent. The table is
 * populated by the WS transcoder, so it carries every chat we've ever
 * seen an event for — including names + avatars cached on the fans
 * row. Sub-50ms because SQLite is indexed on last_message_at.
 *
 * Used by ChatList as a fallback display while the cold `/chats` OF
 * fetch is still in flight: 10–25 rows render instantly, the user can
 * click into one, and the live OF data replaces it once it arrives.
 */

import { useQuery } from "@tanstack/react-query";

import { relay, type OFChatItem } from "@/lib/relay";

interface RecentChatsResp {
  list: OFChatItem[];
  source: string;
}

export function useRecentChatsLocal(opts: {
  enabled?: boolean;
  limit?: number;
  /** When set, the backend filters to this account_id so seed rows in a
   *  single-model view don't bleed in other accounts' chats. */
  accountId?: string | null;
} = {}): { rows: OFChatItem[]; isLoading: boolean } {
  const { enabled = true, limit = 25, accountId = null } = opts;
  const q = useQuery<RecentChatsResp>({
    queryKey: ["chats-local-seed", limit, accountId],
    enabled,
    queryFn: () => {
      const qs = new URLSearchParams();
      qs.set("limit", String(limit));
      if (accountId) qs.set("account_id", accountId);
      return relay.get<RecentChatsResp>(`/admin/chats/recent?${qs.toString()}`);
    },
    // Fresh enough for a session. The OF data replaces it in <2s anyway.
    staleTime: 5 * 60 * 1000,
    gcTime: 30 * 60 * 1000,
    refetchOnWindowFocus: false,
    refetchOnMount: false,
  });
  return {
    rows: q.data?.list ?? [],
    isLoading: q.isLoading,
  };
}
