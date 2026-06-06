"use client";

/**
 * useAccountAvatar — per-account OF profile avatar fetch for the inbox
 * roster icon strip + ScopeSwitcher rows.
 *
 * Hits `/api/of/v2/users/me` with `X-Account-Id` pinned to the requested
 * account. Cached under `["account-avatar", accountId]` with `staleTime:
 * Infinity` because avatars rarely change and an avatar miss falls back
 * silently to a colored-dot + initial — never user-blocking. The OF call
 * carries `priority: "background"` so it never beats a user-initiated
 * fetch (vault open, chat click) into the relay's per-account lane.
 *
 * Errors swallowed to null so a broken session on one account doesn't
 * cascade into the roster UI.
 */

import { useQuery } from "@tanstack/react-query";

import { relay } from "@/lib/relay";

export interface AccountAvatar {
  id?: number;
  name?: string | null;
  username?: string | null;
  avatar?: string | null;
  avatarThumbs?: { c50?: string | null; c144?: string | null } | null;
}

export function useAccountAvatar(accountId: string | null | undefined) {
  return useQuery<AccountAvatar | null>({
    queryKey: ["account-avatar", accountId] as const,
    enabled: !!accountId,
    queryFn: async () => {
      try {
        return await relay.get<AccountAvatar>(
          "/api/of/v2/users/me",
          { accountId: accountId!, priority: "background" },
        );
      } catch {
        return null;
      }
    },
    staleTime: Infinity,
    gcTime: 24 * 60 * 60_000,
    refetchOnWindowFocus: false,
    retry: false,
  });
}

/** Pull the best-available thumbnail URL out of an `AccountAvatar`. Falls
 *  through c50 → c144 → bare `avatar`. Returns null if all empty. */
export function pickAvatarUrl(a: AccountAvatar | null | undefined): string | null {
  if (!a) return null;
  return a.avatarThumbs?.c50 || a.avatarThumbs?.c144 || a.avatar || null;
}
