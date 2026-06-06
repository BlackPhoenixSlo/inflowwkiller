"use client";

/**
 * useChatFolders — fetch the model account's pinnable fan-lists so the
 * inbox can render them as filter chips (📂 List name). Mirrors the
 * legacy /ui/ behaviour: only the lists already pinned to chat appear
 * as chips; the ✎ picker opens the full list (pinned + unpinned) and
 * lets the user toggle.
 *
 * Only fires in single-account scope. Unified mode has no single "my
 * pinned folders" since each account has its own — we hide the strip
 * there.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { relay } from "@/lib/relay";

export interface ChatFolder {
  id: number | string;
  name?: string;
  /** OF's flag — true means this list shows up as a sidebar chip. */
  isPinnedToChat?: boolean;
  usersCount?: number;
  subscribersCount?: number;
}

interface FoldersResp {
  list?: ChatFolder[];
  hasMore?: boolean;
}

/** All pinnable lists (both pinned + not) — used by the picker. */
export function useAllChatFolders(accountId: string | null) {
  return useQuery({
    queryKey: ["chat-folders", accountId ?? "", "all"],
    enabled: !!accountId,
    staleTime: 3 * 24 * 60 * 60_000,
    queryFn: async (): Promise<ChatFolder[]> => {
      if (!accountId) return [];
      const r = await relay.get<FoldersResp>(
        `/api/of/v2/chats/folders?limit=50`,
        { accountId },
      );
      const list = r.list ?? [];
      // Pinned-first, then alpha. Matches /ui/ ordering.
      return list.slice().sort((a, b) => {
        const ap = a.isPinnedToChat ? 1 : 0;
        const bp = b.isPinnedToChat ? 1 : 0;
        if (ap !== bp) return bp - ap;
        return (a.name || "").localeCompare(b.name || "");
      });
    },
  });
}

/** Only the lists currently pinned — these render as chips. */
export function useChatFolders(accountId: string | null) {
  return useQuery({
    queryKey: ["chat-folders", accountId ?? "", "pinned"],
    enabled: !!accountId,
    staleTime: 3 * 24 * 60 * 60_000,
    queryFn: async (): Promise<ChatFolder[]> => {
      if (!accountId) return [];
      const r = await relay.get<FoldersResp>(
        `/api/of/v2/chats/folders?limit=50`,
        { accountId },
      );
      return (r.list ?? []).filter((f) => f.isPinnedToChat);
    },
  });
}

export function usePinChatFolder(accountId: string | null) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (vars: { listId: number | string; pinned: boolean }) => {
      if (!accountId) throw new Error("no account in scope");
      return relay.patch(
        `/api/of/v2/lists/${encodeURIComponent(String(vars.listId))}/pin-chat`,
        { pinned: vars.pinned },
        { accountId },
      );
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["chat-folders", accountId ?? ""] });
    },
  });
}
