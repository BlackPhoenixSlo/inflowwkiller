"use client";

/**
 * usePosts — paginated reader for the model's OWN feed posts.
 *
 * Reads /api/of/v2/posts ("My own posts") live off OF, scoped to one
 * account via X-Account-Id (posts are per-creator, not a DB aggregate
 * like the PPV/Tips tabs). Keyset is offset-based (limit/offset) to mirror
 * the relay's list_posts signature; we infer hasMore from a full page.
 *
 * OF's /posts can come back as a bare array or wrapped in {list}/{posts};
 * normalize() tolerates all three so a wire-shape drift doesn't blank the
 * feed.
 */

import { useInfiniteQuery, useQuery } from "@tanstack/react-query";

import { relay } from "@/lib/relay";

const PAGE = 12;

export interface OFPostMedia {
  id: number;
  type?: string;
  canView?: boolean;
  thumb?: string | null;
  preview?: string | null;
  squarePreview?: string | null;
  full?: string | null;
  source?: { source?: string | null } | null;
  files?: {
    preview?: { url?: string | null } | null;
    thumb?: { url?: string | null } | null;
  } | null;
}

export interface OFPost {
  id: number;
  text?: string;
  rawText?: string;
  price?: number | string | null;
  postedAt?: string;
  postedAtPrecise?: string;
  likesCount?: number;
  favoritesCount?: number;
  commentsCount?: number;
  tipsAmount?: string;
  mediaCount?: number;
  isPinned?: boolean;
  media?: OFPostMedia[];
  /** Media ids shown FREE as a teaser on a PAID post (the rest are locked
   *  behind the paywall). Empty/absent on a fully-locked or free post. OF's
   *  read shape uses `previews` (plural) on the post detail but `preview`
   *  (singular) on some list payloads — we tolerate both. */
  previews?: number[];
  preview?: number[];
}

interface PostsPage {
  posts: OFPost[];
  hasMore: boolean;
}

function normalize(raw: unknown): OFPost[] {
  if (Array.isArray(raw)) return raw as OFPost[];
  if (raw && typeof raw === "object") {
    const o = raw as Record<string, unknown>;
    if (Array.isArray(o.list)) return o.list as OFPost[];
    if (Array.isArray(o.posts)) return o.posts as OFPost[];
  }
  return [];
}

export function usePosts(accountId: string | null) {
  return useInfiniteQuery<PostsPage>({
    queryKey: ["my-posts", accountId],
    enabled: !!accountId,
    initialPageParam: 0,
    getNextPageParam: (last, all) => (last.hasMore ? all.length * PAGE : undefined),
    queryFn: async ({ pageParam, signal }) => {
      const params = new URLSearchParams();
      params.set("limit", String(PAGE));
      params.set("offset", String(pageParam ?? 0));
      const raw = await relay.get<unknown>(
        `/api/of/v2/posts?${params.toString()}`,
        { accountId: accountId ?? undefined },
        signal,
      );
      const posts = normalize(raw);
      return { posts, hasMore: posts.length >= PAGE };
    },
    staleTime: 60_000,
    refetchOnWindowFocus: false,
  });
}

export type FeedMediaType = "all" | "photo" | "video";

interface FeedPage {
  posts: OFPost[];
  hasMore: boolean;
  nextCursor: string | null;
}

/**
 * useMyFeed — the account's OWN profile-wall posts.
 *
 * Distinct from usePosts (which reads /posts, OF's subscriptions HOME
 * timeline that mixes in other creators). This hits
 * /users/{accountId}/posts — only the model's own posts — and pages with
 * OF's cursor protocol: `format=infinite` makes OF return `hasMore` +
 * `tailMarker`, and we feed that marker back as `before_publish_time` to
 * step to the older page. account_id IS the OF numeric user id in this
 * system, so it doubles as the profile id in the path.
 */
export function useMyFeed(accountId: string | null, type: FeedMediaType = "all") {
  return useInfiniteQuery<FeedPage>({
    queryKey: ["my-feed", accountId, type],
    enabled: !!accountId,
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.nextCursor ?? undefined,
    queryFn: async ({ pageParam, signal }) => {
      const params = new URLSearchParams();
      params.set("limit", "24");
      params.set("format", "infinite");
      if (type !== "all") params.set("type", type);
      if (pageParam) params.set("before_publish_time", String(pageParam));
      const raw = await relay.get<unknown>(
        `/api/of/v2/users/${encodeURIComponent(accountId!)}/posts?${params.toString()}`,
        { accountId: accountId ?? undefined },
        signal,
      );
      const posts = normalize(raw);
      const o = (raw && typeof raw === "object" ? raw : {}) as Record<string, unknown>;
      // OF returns the cursor as `tailMarker` (string) under format=infinite.
      // Only continue paging when OF says hasMore AND hands us a real marker —
      // a missing marker means we'd otherwise loop the same page forever.
      const tail = typeof o.tailMarker === "string" && o.tailMarker ? o.tailMarker : null;
      const hasMore = Boolean(o.hasMore) && posts.length > 0 && !!tail;
      return { posts, hasMore, nextCursor: hasMore ? tail : null };
    },
    staleTime: 60_000,
    refetchOnWindowFocus: false,
  });
}

export type TopBy = "purchases" | "likes" | "comments" | "views";

/**
 * useTopPosts — ranked top-performing posts in a date range.
 *
 * Backs the Top Posts view: OF's `/posts/top?by=<metric>` is the only
 * source for purchases/earnings ranking (the feed payload carries none of
 * that). Items come back already sorted by the metric, so order = rank.
 * Response is `{items|list}` — normalize tolerates either plus a bare array.
 */
export function useTopPosts(
  accountId: string | null,
  by: TopBy,
  from: string | null,
  to: string | null,
) {
  return useQuery<OFPost[]>({
    queryKey: ["top-posts", accountId, by, from, to],
    enabled: !!accountId,
    queryFn: async ({ signal }) => {
      const params = new URLSearchParams();
      params.set("by", by);
      params.set("limit", "50");
      if (from) params.set("start", from);
      if (to) params.set("end", to);
      const raw = await relay.get<unknown>(
        `/api/of/v2/posts/top?${params.toString()}`,
        { accountId: accountId ?? undefined },
        signal,
      );
      return normalizeItems(raw);
    },
    staleTime: 60_000,
    refetchOnWindowFocus: false,
  });
}

/** /posts/top wraps the ranked posts in `items` (vs the feed's `list`). */
function normalizeItems(raw: unknown): OFPost[] {
  if (Array.isArray(raw)) return raw as OFPost[];
  if (raw && typeof raw === "object") {
    const o = raw as Record<string, unknown>;
    if (Array.isArray(o.items)) return o.items as OFPost[];
    if (Array.isArray(o.list)) return o.list as OFPost[];
    if (Array.isArray(o.posts)) return o.posts as OFPost[];
  }
  return [];
}

/** First non-empty media thumbnail for a post, walking OF's overlapping
 *  shape variants (flat string fields vs the nested files object). */
export function postThumbUrl(m: OFPostMedia): string | null {
  return (
    m.squarePreview ||
    m.preview ||
    m.thumb ||
    m.files?.preview?.url ||
    m.files?.thumb?.url ||
    m.full ||
    m.source?.source ||
    null
  );
}

/** Strip OF's HTML body (it ships <p>/<br> wrapped) down to plain text. */
export function postPlainText(p: OFPost): string {
  const html = p.rawText ?? p.text ?? "";
  return html
    .replace(/<br\s*\/?>/gi, " ")
    .replace(/<[^>]+>/g, " ")
    .replace(/&amp;/g, "&")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/\s+/g, " ")
    .trim();
}
