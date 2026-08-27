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
export interface CaptionLimits {
  /** Server's ceiling on boxes per set. The panel clamps to THIS, not a literal. */
  boxes_max: number;
  styles_max: number;
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
  /** Owner second leg: when the ownership guard removes the fans who already
   *  unlocked a PPV, send them a DIFFERENT library PPV in the same tick instead
   *  of nothing. Default OFF — this is a per-account rollout, not a house default:
   *  on some accounts the 1:1 seller lane already over-serves buyers. */
  owner_second_leg?: boolean;
  /** Don't re-message a fan for N hours (contact guard). 0 = no pause. */
  pause_hours?: number;
  /** Global price limits (cents): every COMPUTED send/post price — tier cells, the
   *  everyone-broadcast, feed posts — is clamped into [min, max] at send time.
   *  Defaults 300/20000 ($3–$200). Authored base prices are kept as typed. */
  price_min_cents?: number;
  price_max_cents?: number;
  /** Whale gate: fans at or above this lifetime spend (cents) leave the PPV lane
   *  entirely — no tier send, and they stay in the broadcast's exclude set, so
   *  they get nothing from this automation. 0/absent = OFF. Same key name as the
   *  gate in ai_chatter / autoreply / nudge_online. */
  max_lifetime_spend_cents?: number;
  /** The "Send to everyone" audience, as OF `userLists` values: the built-in
   *  names "fans"/"following" and/or custom folder ids, as strings.
   *  ABSENT/null = the historical fans + following. An EMPTY ARRAY is a real
   *  choice ("reach nobody new") and must never be coerced back to the default. */
  broadcast_lists?: string[] | null;
  /** Folders that broadcast never reaches, as OF list ids. IDS ONLY — OF
   *  silently ignores "fans"/"following" inside `excludedLists`, so the server
   *  422s rather than storing an exclusion that cannot work. */
  broadcast_exclude_lists?: number[];
  ppvs?: PpvItem[];
  /** ppv_send writes ONE caption line per run, above the pool line. Default off. */
  ai_caption_at_send?: boolean;
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
  /** The whale gate the preview was computed under (cents), or null when off.
   *  Echoed back by the server so the readout can name the ceiling without
   *  re-deriving the normalization the validator just applied. */
  spend_cap_cents?: number | null;
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
  /** Server-owned ceilings for the caption-set panel. */
  caption_limits?: CaptionLimits;
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
  /** DRAFT whale gate (cents) — the number the operator is choosing RIGHT NOW.
   *  0 is meaningful ("preview with the cap off"), so it must be sent, not
   *  treated as absent; only `undefined` means "read the stored config". */
  maxLifetimeSpendCents?: number;
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
    mutationFn: ({ basePriceCents, priceMinCents, priceMaxCents,
                   maxLifetimeSpendCents }) =>
      relay.post(`/admin/ppv-library-config/preview`, {
        account_id: accountId,
        base_price_cents: basePriceCents,
        ...(priceMinCents != null ? { price_min_cents: priceMinCents } : {}),
        ...(priceMaxCents != null ? { price_max_cents: priceMaxCents } : {}),
        // `!== undefined`, not `!= null`: 0 is the operator asking to preview
        // the lane with the gate OFF, and the tab's own copy promises that
        // preview reflects the UNSAVED value.
        ...(maxLifetimeSpendCents !== undefined
          ? { max_lifetime_spend_cents: maxLifetimeSpendCents } : {}),
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

export interface CaptionBox {
  text: string;
  /** Which hook style wrote it — detail | house | blunt | question. */
  style: string;
  /** Where the lane's house line sits, or null for a bare hook. */
  frame: "top" | "bottom" | null;
}

export interface CaptionBoxSetResult {
  account_id: string;
  /** Ready-to-save box texts, already inside the save-time limits. */
  captions: string[];
  boxes: CaptionBox[];
  skipped: { media_id?: number; style?: string; reason: string }[];
  generated: number;
  cost_millicents: number;
  /** How many house lines this PPV's lane actually has. 0 → bare hooks only. */
  pool_lines?: number;
}

export interface CaptionBoxSetArgs {
  /** The PPV's media, in its own order — DRAFT ids, so this works before Save. */
  mediaIds: number[];
  /** The PPV's lane. Its house lines become the frames. */
  captionPoolKey: string;
  boxes: number;
  /** How many boxes carry the lane line. This IS the "X% of sends" number. */
  framed: number;
  /** How many of the framed boxes put the line ABOVE the hook. */
  frameTop: number;
  styles: string[];
}

/** A rotating SET of caption boxes: styled hooks written from the media's own
 *  description, each composed with one of that lane's house lines.
 *
 *  Suggest-only. The set is appended to the editor and the operator still
 *  presses Save. The sender already picks one box at random per send — which is
 *  what turns the set into a rotation, and what makes "8 framed of 10" mean
 *  "80% of sends" without a single line of send-path code. */
export function useCaptionBoxSet(accountId: string | null) {
  return useMutation<CaptionBoxSetResult, Error, CaptionBoxSetArgs>({
    mutationFn: ({ mediaIds, captionPoolKey, boxes, framed, frameTop, styles }) =>
      relay.post(`/admin/ppv-library-config/caption-box-set`, {
        account_id: accountId,
        media_ids: mediaIds,
        compose: {
          caption_pool_key: captionPoolKey,
          boxes, framed, frame_top: frameTop, styles,
        },
      }),
  });
}
