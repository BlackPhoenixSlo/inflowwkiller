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
  /** Max PPV sends per rolling day/week/month; a hit cap holds + releases later.
   *  ABSENT = the runtime house default (2/14/60 → one blast per 12h); an explicit
   *  all-zero dict = spacing off. The tab always saves all three keys. */
  ppv_caps?: PpvCaps;
  /** Also broadcast each PPV to ALL subscribers at the default price (known fans excluded). */
  reach_all?: boolean;
  /** Don't re-message a fan for N hours (contact guard). 0 = no pause. */
  pause_hours?: number;
  /** Global price limits (cents): every COMPUTED send/post price — tier cells, the
   *  everyone-broadcast, feed posts — is clamped into [min, max] at send time.
   *  Defaults 300/20000 ($3–$200). Authored base prices are kept as typed. */
  price_min_cents?: number;
  price_max_cents?: number;
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
  /** Top of the priciest piece of content's DERIVED price band (cents; 0 = no
   *  content / no evidence yet). The Max below it silently caps what the 1:1
   *  seller may ever ask for that clip, so the tab warns once. */
  content_band_max_cents: number;
  /** OF's hard ceiling on a priced message ($200) — the highest a Max can go. */
  price_ceil_cents: number;
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

export interface PpvPreviewArgs {
  basePriceCents: number;
  /** DRAFT price limits (cents) — unsaved UI state; the preview must show what a
   *  save would send. Absent → the stored config's limits. */
  priceMinCents?: number;
  priceMaxCents?: number;
}

/** Dry-run: how the account's current fans split into price cells (no send). */
/** Per-row vault reasoning that rides ALONGSIDE a suggestion (kept out of the
 *  PpvItem so those stay exactly config-shaped). */
export interface PpvSuggestionNote {
  why: string;
  note: string;
  thin: boolean;
  /** Nothing in the bundle may legally be shown free and no tame frame was
   *  available to attach — it would ship as a locked box. */
  preview_unsafe: boolean;
  photos: number;
  videos: number;
  closers: number;
  reused: number;
  tiers: Record<string, number>;
}

export interface PpvSuggestions {
  ppvs: PpvItem[];
  notes: Record<string, PpvSuggestionNote>;
  summary: Record<string, number>;
  lanes: Record<string, number>;
}

/** Propose a week of bundles from the vault. Read-only, free (no LLM, no OF).
 *  Every row arrives `enabled: false` / `feed_enabled: false`, so accepting a
 *  suggestion can never start a send — arming stays a separate, deliberate act. */
export function useSuggestPpvs(accountId: string | null) {
  return useMutation<PpvSuggestions, Error, void>({
    mutationFn: () =>
      relay.get<PpvSuggestions>(
        `/admin/ppv-library-config/suggest?account_id=${encodeURIComponent(accountId!)}`,
      ),
  });
}

export function usePpvPreview(accountId: string | null) {
  return useMutation<PpvPreview, Error, PpvPreviewArgs>({
    mutationFn: ({ basePriceCents, priceMinCents, priceMaxCents }) =>
      relay.post(`/admin/ppv-library-config/preview`, {
        account_id: accountId,
        base_price_cents: basePriceCents,
        ...(priceMinCents != null ? { price_min_cents: priceMinCents } : {}),
        ...(priceMaxCents != null ? { price_max_cents: priceMaxCents } : {}),
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

export interface PostPpvArgs {
  ppvId?: string;
  /** WYSIWYG override: post exactly this caption (e.g. one already shown by a preview call). */
  caption?: string;
  /** Operator's chosen media in the EXACT order to post. When present & non-empty it
   *  OVERRIDES the PPV's own media_ids. Absent/empty => behavior unchanged. */
  mediaFiles?: number[];
  /** Operator's chosen free-preview subset. Must be a subset of the effective media set. */
  previews?: number[];
}

/** Post one PPV to the FEED as a paid post at its base price. Pass a `ppvId` to
 *  post that one; omit it for a random ENABLED PPV (skips the last one posted). */
export function usePostPpvToFeed(accountId: string | null) {
  return useMutation<PostPpvResult, Error, string | undefined | PostPpvArgs>({
    mutationFn: (arg) => {
      const ppvId = typeof arg === "string" || arg === undefined ? arg : arg.ppvId;
      const caption = typeof arg === "object" ? arg.caption : undefined;
      const mediaFiles = typeof arg === "object" ? arg.mediaFiles : undefined;
      const previews = typeof arg === "object" ? arg.previews : undefined;
      return relay.post(`/admin/ppv-library-config/post-now`, {
        account_id: accountId,
        ...(ppvId ? { ppv_id: ppvId } : {}),
        ...(caption ? { caption } : {}),
        ...(mediaFiles && mediaFiles.length ? { media_files: mediaFiles } : {}),
        ...(previews ? { previews } : {}),
      });
    },
  });
}

export interface PreviewPpvToFeedResult {
  account_id: string;
  ppv_id: string;
  name: string;
  caption: string;
  used_feed_caption: boolean;
  price: number;
  media_count: number;
  preview_count: number;
  /** The candidate PPV's media, in stored order. */
  media_ids: number[];
  /** The star (preview_options) free-preview subset, always a subset of media_ids. */
  previews: number[];
}

/** Preview WHICH PPV + WHAT caption a "post to feed now" would use — no post, no
 *  state change. Confirm by handing `{ ppvId, caption }` from the result to
 *  `usePostPpvToFeed` for a WYSIWYG post-now. */
export function usePreviewPpvToFeed(accountId: string | null) {
  return useMutation<PreviewPpvToFeedResult, Error, string | undefined>({
    mutationFn: (ppvId) =>
      relay.post(`/admin/ppv-library-config/post-now/preview`, {
        account_id: accountId,
        ...(ppvId ? { ppv_id: ppvId } : {}),
      }),
  });
}
