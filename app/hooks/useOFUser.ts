"use client";

/**
 * useOFUser — single fan's full OF profile, including the `subscribedOnData`
 * block that holds the live spend breakdown + subscription state. Powers
 * the FanDrawer's stats grid (matches what the desktop app shows).
 *
 * 5-minute staleTime, NO refetch-on-mount, focus refetch kept. Every drawer
 * or actions-menu open used to spend a live OF /users/{id} on the USER lane —
 * the lane reserved for what a click is actually waiting on — for data that
 * barely moves. The freshness that matters here is PUSHED, not polled: inbound
 * money invalidates this key through `invalidateFanRevenue`
 * ([useInboxRealtime.ts]) and the restrict toggle invalidates it directly
 * ([ChatActionsMenu.tsx]). Window focus is the cheap catch-all for a fan
 * changed somewhere else entirely. (Before that it was 3 days, which pinned a
 * stale nickname/note long after OF had changed; 30s + refetch-on-mount fixed
 * the staleness by paying the lane cost on every open.)
 *
 * The one consumer that needs more than this is FanDrawer's EDITABLE
 * nickname/note pair: a stale prefill can be typed over and saved, silently
 * reverting whatever changed the value. The drawer closes that window itself
 * by refetching when the operator ENGAGES an edit affordance — a deliberate
 * click, rare, and the one moment freshness is load-bearing. Don't restore
 * `refetchOnMount` here to serve that case; it would put the per-open cost
 * back on every consumer to protect two fields in one of them.
 *
 * Errors are swallowed by React Query → we surface them through the
 * existing `error` slot, but the drawer keeps showing the DB-derived
 * fields so a flaky OF call doesn't blank the panel.
 */

import { useQuery } from "@tanstack/react-query";

import { relay } from "@/lib/relay";
import { type FanId } from "@/lib/fanId";

/** How long a cached read is trusted. Exported because FanDrawer's pre-edit
 *  refresh guards on the SAME window — the risk it closes exists precisely
 *  because reads inside this window are served from cache. */
export const OF_USER_STALE_MS = 5 * 60_000;

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

export function useOFUser(accountId: string, fanId: FanId) {
  return useQuery<OFUser>({
    queryKey: ["of-user", accountId, fanId],
    enabled: !!accountId && !!fanId,
    queryFn: () =>
      relay.get<OFUser>(`/api/of/v2/users/${fanId}`, { accountId }),
    // Policy + its invalidation sources are documented in the header above.
    // Also persisted across reloads ([providers.tsx] PERSIST_PREFIXES
    // "of-user"), so the 5 minutes survive a refresh.
    //
    // Audited before relaxing: no send path reads this. The drawer renders
    // subscribedOnData.price as a display Stat, and isRestricted is ORed
    // with the chat row + the local registry, so a stale copy can only be
    // late, never authoritative. The exception — the drawer's editable
    // nickname/note — is handled at the edit site, not here.
    staleTime: OF_USER_STALE_MS,
    refetchOnWindowFocus: true,
    refetchOnMount: false,
  });
}
