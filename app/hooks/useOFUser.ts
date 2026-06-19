"use client";

/**
 * useOFUser — single fan's full OF profile, including the `subscribedOnData`
 * block that holds the live spend breakdown + subscription state. Powers
 * the FanDrawer's stats grid (matches what the desktop app shows).
 *
 * Short 30s staleTime: this is the AUTHORITATIVE read for the drawer's
 * nickname + private note (and the live spend), so opening the drawer or
 * flipping back to the tab triggers a BACKGROUND refetch — the cached value
 * shows instantly and is replaced the moment OF answers. Anything that mutates
 * the fan out-of-band (an automation pushing a new nickname/note, the operator
 * editing on another device) is picked up within one open/focus instead of
 * being pinned for days. (Previously 3 days, which made the panel show a stale
 * nickname/note long after OF had changed.)
 *
 * Errors are swallowed by React Query → we surface them through the
 * existing `error` slot, but the drawer keeps showing the DB-derived
 * fields so a flaky OF call doesn't blank the panel.
 */

import { useQuery } from "@tanstack/react-query";

import { relay } from "@/lib/relay";

export interface OFSubscribedOnData {
  price?: number;
  newPrice?: number;
  regularPrice?: number;
  subscribePrice?: number;
  subscribeAt?: string | null;
  expiredAt?: string | null;
  renewedAt?: string | null;
  status?: string | null;
  isMuted?: boolean;
  unsubscribeReason?: string | null;
  duration?: string | null;
  tipsSumm?: number;
  subscribesSumm?: number;
  messagesSumm?: number;
  postsSumm?: number;
  streamsSumm?: number;
  totalSumm?: number;
}

export interface OFListState {
  id: string | number;
  type: string;
  name?: string;
  hasUser?: boolean;
  canAddUser?: boolean;
  cannotAddUserReason?: string | null;
}

export interface OFUser {
  id?: number;
  name?: string;
  username?: string;
  avatar?: string | null;
  lastSeen?: string | null;
  /** OF's per-subscription rename ("custom name" in their UI) — set
   *  via PUT /subscriptions/{id} `{displayName}`. Empty string means
   *  no rename. */
  displayName?: string | null;
  /** OF's private creator-side note on this fan — set via PUT
   *  /subscriptions/{id} `{notice}`. */
  notice?: string | null;
  subscribedOnData?: OFSubscribedOnData;
  listsStates?: OFListState[];
  /** Native OnlyFans restrict state. A restricted fan can still type, but
   *  OF stops their messages reaching us (`canReceiveChatMessage` flips
   *  false). Toggled via POST/DELETE /users/{id}/restrict. DISTINCT from
   *  our internal "restrict from automations" skip_list (DB-only). */
  isRestricted?: boolean;
  canRestrict?: boolean;
  canReceiveChatMessage?: boolean;
  [k: string]: unknown;
}

export function useOFUser(accountId: string, fanId: number) {
  return useQuery<OFUser>({
    queryKey: ["of-user", accountId, fanId],
    enabled: !!accountId && !!fanId,
    queryFn: () =>
      relay.get<OFUser>(`/api/of/v2/users/${fanId}`, { accountId }),
    staleTime: 30_000,
    refetchOnWindowFocus: true,
    refetchOnMount: true,
  });
}
