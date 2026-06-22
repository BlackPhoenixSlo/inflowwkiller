import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { relay, type RelayContext } from "@/lib/relay";

const KEY = "ppv-library-config";
const BG_CTX: RelayContext = { priority: "background" };

/** One premade PPV. The same media is fanned out to spend×recency fan segments
 *  at a per-segment price (base × matrix multiplier); the runner random-picks a
 *  caption line + a preview each send. Cadence = 604800 / sends_per_week. */
export interface PpvItem {
  id: string;
  name?: string;
  media_ids: number[];
  caption_pool_key: string;
  caption_texts?: string[];
  /** Captions used ONLY when posting this PPV to the feed (public voice). Empty → reuse caption_texts/pool. */
  feed_captions?: string[];
  /** Feed caption STYLE pool (public voice, auto-picked). "" = none → manual feed_captions or message fallback. */
  feed_caption_pool_key?: string;
  /** When this PPV's mass send fires, ALSO post it to the feed (paid, base price). Default off. */
  also_post_to_feed?: boolean;
  /** Available for feed posting (button + random picker + auto-post). Independent of `enabled`. Default on. */
  feed_enabled?: boolean;
  base_price_cents: number;
  preview_options: number[];
  sends_per_week: number;
  resend_monthly: boolean;
  exclude_buyers?: boolean;
  enabled: boolean;
}

export interface PpvCaps {
  per_day?: number;
  per_week?: number;
  per_month?: number;
}
export interface PpvLibraryConfig {
  enabled?: boolean;
  /** Creator-local quiet window [startHour, endHour] (0-23); null = 24/7. */
  quiet_hours?: [number, number] | null;
  /** Max PPV sends per rolling day/week/month; a hit cap holds + releases later. */
  ppv_caps?: PpvCaps;
  /** Also broadcast each PPV to ALL subscribers at the default price (known fans excluded). */
  reach_all?: boolean;
  /** Don't re-message a fan for N hours (contact guard). 0 = no pause. */
  pause_hours?: number;
  ppvs?: PpvItem[];
}

export interface PpvPreviewCell {
  cell: string;
  spend: string;
  recency: string;
  recipients: number;
  price: number;
}
export interface PpvPreview {
  account_id: string;
  total_fans: number;
  cells: PpvPreviewCell[];
}

export interface PriceMatrix {
  spend_bands: Array<{ name: string; min_cents: number; max_cents: number | null; mult: number }>;
  recency_bands: Array<{ name: string; max_days: number | null; mult: number }>;
}

interface PpvLibraryConfigResponse {
  account_id: string;
  config: PpvLibraryConfig;
  defaults: PpvLibraryConfig;
  pools: string[];
  caption_pools: Record<string, string[]>;
  feed_pools: string[];
  feed_caption_pools: Record<string, string[]>;
  matrix: PriceMatrix;
}

export function usePpvLibraryConfig(accountId: string | null) {
  return useQuery<PpvLibraryConfigResponse>({
    queryKey: [KEY, accountId],
    enabled: !!accountId,
    queryFn: () =>
      relay.get<PpvLibraryConfigResponse>(
        `/admin/ppv-library-config?account_id=${encodeURIComponent(accountId!)}`,
        BG_CTX,
      ),
    staleTime: 60_000,
    refetchOnWindowFocus: false,
  });
}

export function useSavePpvLibraryConfig(accountId: string | null) {
  const qc = useQueryClient();
  return useMutation<
    { account_id: string; config: PpvLibraryConfig; rules: Record<string, number> },
    Error,
    PpvLibraryConfig
  >({
    mutationFn: (config) =>
      relay.put(`/admin/ppv-library-config`, { account_id: accountId, config }),
    onSuccess: () => qc.invalidateQueries({ queryKey: [KEY, accountId] }),
  });
}

/** Dry-run: how the account's current fans split into price cells (no send). */
export function usePpvPreview(accountId: string | null) {
  return useMutation<PpvPreview, Error, number>({
    mutationFn: (basePriceCents) =>
      relay.post(`/admin/ppv-library-config/preview`, {
        account_id: accountId,
        base_price_cents: basePriceCents,
      }),
  });
}

export interface PostPpvResult {
  account_id: string;
  ppv_id: string;
  name: string;
  of_post_id: number;
  price: number;
  caption: string;
  used_feed_caption: boolean;
  media_count: number;
  preview_count: number;
}

/** Post one PPV to the FEED as a paid post at its base price. Pass a `ppvId` to
 *  post that one; omit it for a random ENABLED PPV (skips the last one posted). */
export function usePostPpvToFeed(accountId: string | null) {
  return useMutation<PostPpvResult, Error, string | undefined>({
    mutationFn: (ppvId) =>
      relay.post(`/admin/ppv-library-config/post-now`, {
        account_id: accountId,
        ...(ppvId ? { ppv_id: ppvId } : {}),
      }),
  });
}
