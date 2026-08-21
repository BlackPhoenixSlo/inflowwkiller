"use client";

/**
 * useGrowth — data layer for the Growth surfaces that mirror OnlyFans state:
 * Free-Trial links, Tracking/UTM links, and Profile-Promotion campaigns.
 *
 * All three hit owner-gated /admin/* relay routes. Trial + Promo are light
 * mirrors over OF (OF is source of truth; a local row keeps the operator's
 * name + last-seen usage). Tracking links are fully internal (public
 * /t/{slug} redirect + click analytics).
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { relay } from "@/lib/relay";

// ── Free-trial links ──────────────────────────────────────────────────

const TRIAL_KEY = "trial-links";

export interface TrialLink {
  id: number | null;
  of_trial_id: number | null;
  name: string;
  url: string | null;
  subscribe_days: number;
  subscribe_counts: number;
  claim_counts: number;
  clicks_count: number | null;
  expired_at: string | null;
  /** "unknown" when OF didn't return this link, so its state can't be read. */
  status: "active" | "finished" | "unknown";
  created_at: string | null;
  source: "mirror" | "onlyfans";
}

export function useTrialLinks(accountId: string | null) {
  return useQuery<{ links: TrialLink[]; of_available: boolean }>({
    queryKey: [TRIAL_KEY, accountId],
    enabled: !!accountId,
    staleTime: 30_000,
    queryFn: () =>
      relay.get(`/admin/trial-links?account_id=${encodeURIComponent(accountId!)}`),
  });
}

export function useCreateTrialLink(accountId: string | null) {
  const qc = useQueryClient();
  return useMutation<
    TrialLink, Error,
    { name: string; subscribe_days: number; subscribe_counts: number; expired_at?: string | null }
  >({
    mutationFn: (body) =>
      relay.post<TrialLink>("/admin/trial-links", { account_id: accountId, ...body }),
    onSuccess: () => qc.invalidateQueries({ queryKey: [TRIAL_KEY, accountId] }),
  });
}

export function useDeleteTrialLink(accountId: string | null) {
  const qc = useQueryClient();
  return useMutation<unknown, Error, number>({
    mutationFn: (id) => relay.delete(`/admin/trial-links/${id}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: [TRIAL_KEY, accountId] }),
  });
}

// ── Tracking / UTM links ──────────────────────────────────────────────

const TRACK_KEY = "tracking-links";

export interface TrackingLink {
  id: number;
  account_id: string;
  name: string;
  slug: string;
  target_url: string;
  click_count: number;
  path: string;
  created_at: string | null;
}

export interface TrackingAnalytics {
  link: TrackingLink;
  total_clicks: number;
  recent: Array<{ clicked_at: string | null; referer: string | null }>;
  by_day: Array<{ day: string; clicks: number }>;
  subscribers: number | null;
  revenue_cents: number | null;
}

export function useTrackingLinks(accountId: string | null) {
  return useQuery<TrackingLink[]>({
    queryKey: [TRACK_KEY, accountId],
    enabled: !!accountId,
    staleTime: 30_000,
    queryFn: async () => {
      const r = await relay.get<{ links: TrackingLink[] }>(
        `/admin/tracking-links?account_id=${encodeURIComponent(accountId!)}`,
      );
      return r.links ?? [];
    },
  });
}

export function useCreateTrackingLink(accountId: string | null) {
  const qc = useQueryClient();
  return useMutation<
    TrackingLink, Error, { name: string; target_url: string; slug?: string }
  >({
    mutationFn: (body) =>
      relay.post<TrackingLink>("/admin/tracking-links", { account_id: accountId, ...body }),
    onSuccess: () => qc.invalidateQueries({ queryKey: [TRACK_KEY, accountId] }),
  });
}

export function useDeleteTrackingLink(accountId: string | null) {
  const qc = useQueryClient();
  return useMutation<unknown, Error, number>({
    mutationFn: (id) => relay.delete(`/admin/tracking-links/${id}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: [TRACK_KEY, accountId] }),
  });
}

export function useTrackingAnalytics(accountId: string | null, linkId: number | null) {
  return useQuery<TrackingAnalytics>({
    queryKey: [TRACK_KEY, "analytics", accountId, linkId],
    enabled: !!accountId && !!linkId,
    staleTime: 15_000,
    queryFn: () =>
      relay.get(
        `/admin/tracking-links/${linkId}/analytics?account_id=${encodeURIComponent(accountId!)}`,
      ),
  });
}

// ── OnlyFans-native tracking links (/campaigns) ───────────────────────
// These are OF's OWN tracking links: OF reports real clicks AND how many of
// those clicks subscribed — attribution the internal /t/ redirect can't have.
const OF_TRACK_KEY = "of-tracking-links";

export interface OfTrackingLink {
  id: number | null;
  code: number | null;
  name: string;
  url: string | null;
  /** false until a real link is pasted — the URL is a strongly-implied guess. */
  url_verified: boolean;
  clicks: number;
  subscribers: number;
  created_at: string | null;
  source: "onlyfans";
}

export interface OfTrackingResult {
  links: OfTrackingLink[];
  username: string | null;
  url_verified: boolean;
  of_available: boolean;
}

export function useOfTrackingLinks(accountId: string | null) {
  return useQuery<OfTrackingResult>({
    queryKey: [OF_TRACK_KEY, accountId],
    enabled: !!accountId,
    staleTime: 30_000,
    queryFn: () =>
      relay.get(`/admin/of-tracking-links?account_id=${encodeURIComponent(accountId!)}`),
  });
}

export function useCreateOfTrackingLink(accountId: string | null) {
  const qc = useQueryClient();
  return useMutation<OfTrackingLink, Error, { name: string }>({
    mutationFn: (body) =>
      relay.post<OfTrackingLink>("/admin/of-tracking-links", { account_id: accountId, ...body }),
    onSuccess: () => qc.invalidateQueries({ queryKey: [OF_TRACK_KEY, accountId] }),
  });
}

export function useDeleteOfTrackingLink(accountId: string | null) {
  const qc = useQueryClient();
  return useMutation<unknown, Error, number>({
    mutationFn: (id) =>
      relay.delete(`/admin/of-tracking-links/${id}?account_id=${encodeURIComponent(accountId!)}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: [OF_TRACK_KEY, accountId] }),
  });
}

// ── Profile promotion campaigns ───────────────────────────────────────

const PROMO_KEY = "promotions";

export interface PromoCampaign {
  id: number | null;
  of_promo_id: number | null;
  name: string;
  /** Percent off the subscription (OF contract). null for OF-only rows (OF
   *  doesn't echo the discount on list). */
  discount_percent: number | null;
  price_cents: number;
  subscribe_counts: number;
  subscribe_days: number;
  message: string | null;
  promo_type: string;
  status: string;
  /** People who claimed the promo (OF `claimsCount`). */
  subscriber_count: number;
  /** When on, the promo_reactivate automation re-creates this campaign each
   *  time OnlyFans marks it finished. Only mirror-backed rows can opt in. */
  auto_reactivate: boolean;
  /** How many times it has been re-armed so far. */
  reactivate_count: number;
  last_reactivated_at: string | null;
  created_at: string | null;
  source: "mirror" | "onlyfans";
}

export function usePromotions(accountId: string | null) {
  return useQuery<{ campaigns: PromoCampaign[]; of_available: boolean }>({
    queryKey: [PROMO_KEY, accountId],
    enabled: !!accountId,
    staleTime: 30_000,
    queryFn: () =>
      relay.get(`/admin/promotions?account_id=${encodeURIComponent(accountId!)}`),
  });
}

export function useCreatePromotion(accountId: string | null) {
  const qc = useQueryClient();
  return useMutation<
    PromoCampaign, Error,
    {
      name: string; discount: number; subscribe_counts: number;
      subscribe_days: number; message?: string; promo_type?: string;
    }
  >({
    mutationFn: (body) =>
      relay.post<PromoCampaign>("/admin/promotions", { account_id: accountId, ...body }),
    onSuccess: () => qc.invalidateQueries({ queryKey: [PROMO_KEY, accountId] }),
  });
}

export function useDeletePromotion(accountId: string | null) {
  const qc = useQueryClient();
  return useMutation<unknown, Error, number>({
    mutationFn: (id) => relay.delete(`/admin/promotions/${id}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: [PROMO_KEY, accountId] }),
  });
}

/** Toggle "keep this promo running" for one campaign. */
export function useSetPromoAutoReactivate(accountId: string | null) {
  const qc = useQueryClient();
  return useMutation<
    { id: number; auto_reactivate: boolean }, Error,
    { id: number; auto_reactivate: boolean }
  >({
    mutationFn: ({ id, auto_reactivate }) =>
      relay.patch(`/admin/promotions/${id}`, { auto_reactivate }),
    onSuccess: () => qc.invalidateQueries({ queryKey: [PROMO_KEY, accountId] }),
  });
}
