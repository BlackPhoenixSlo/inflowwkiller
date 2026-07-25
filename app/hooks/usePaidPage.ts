"use client";

/**
 * usePaidPage — does this account charge for a subscription?
 *
 * A FREE page sells on its wall (a "paid post" = free preview + locked media).
 * A page with a subscription price cannot: the sub IS the paywall, so OF has no
 * paid-post lane for it. Every surface that offers a priced feed post has to
 * know, or it offers something the account can't do.
 *
 * Ground truth is `subscribePrice` on /users/me — the same field the server's
 * `service/account_page.py` gate reads, so the UI and the enforcement agree.
 * `paidFeed` is NOT it (it disagrees with itself across two live paid pages).
 *
 * `isPaidPage` is false while the profile is loading or unavailable, matching
 * the server: unknown keeps the CURRENT behavior rather than greying out a
 * control on a page that may well be free.
 */

import { useAccountProfile } from "@/hooks/useStats";

export function usePaidPage(accountId: string | null | undefined) {
  const q = useAccountProfile(accountId);
  const price = q.data?.subscribePrice;
  const known = typeof price === "number";
  return {
    /** True only when OF says the page charges — i.e. no paid-post lane. */
    isPaidPage: known && price > 0,
    /** Monthly subscription price in dollars; null until known. */
    subscribePrice: known ? price : null,
    isLoading: q.isLoading,
  };
}

/** The one sentence every surface shows when it hides a paid-post control. */
export const PAID_PAGE_NOTE =
  "This page charges for a subscription — OF has no paid-post lane for it. " +
  "Post free, or sell it as a PPV in DMs.";
