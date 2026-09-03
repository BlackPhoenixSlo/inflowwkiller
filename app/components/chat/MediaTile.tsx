"use client";

/**
 * MediaTile — image tile that owns its own fetch lifecycle.
 *
 * Two reasons this exists instead of a plain <img loading="lazy">:
 *
 * 1. ABORT ON CHAT SWITCH. loading="lazy" defers START, but once a fetch
 *    is in flight the browser does NOT cancel it when the <img> unmounts
 *    or scrolls offscreen. Clicking from chat A to chat B leaves A's 30
 *    image fetches grinding for another 10s, holding all 6 browser HTTP
 *    slots against chat B's new /messages call. AbortController in the
 *    cleanup function makes unmount = cancel = chat B's slots freed.
 *
 * 2. CAPTURE X-Img-Hash. The relay returns a stable hash per asset in a
 *    response header. <img> doesn't expose response headers. fetch()
 *    does, so we read it and remember mediaId → hash, then on the next
 *    render of the same image we hit /img/by-hash/<h> as a stable cache
 *    key — survives OF signature rotation between /messages refreshes.
 *
 * Priority: `eager` tiles fire immediately with fetchpriority="high".
 * The rest wait until an IntersectionObserver sees the placeholder enter
 * a 200px-rootMargin window, then fire with fetchpriority="low".
 */

import { useEffect, useRef, useState } from "react";
import { sameUserId } from "@/lib/fanId";
import { cn } from "@/lib/utils";

// Module-level mapping captured from the X-Img-Hash response header.
// Shared across all MediaTile instances + chat renders, so /img/by-hash
// gets re-used on subsequent paints without re-deriving the hash.
// Keyed by STRING id: Fansly media ids are snowflakes above JS's 2^53, and a
// bundle's ids differ only in their last digits, so numeric keys collide and
// five sibling tiles would share one hash (painting the same image 5x).
const mediaIdToHash = new Map<string, string>();

// Reserved skeleton box. The real per-image dimensions are NOT known on
// first paint (they only land later with message_media dims / T-GRID), so
// we reserve a neutral MIN-HEIGHT rather than a hard aspect-ratio. An
// aspect-[4/5] would letterbox wide images and just move the reflow; a
// min-height floor keeps the box non-zero so the bytes paint into a box
// that's already there and the text below never bounces. The image fills
// the (parent-sized) box with object-cover. This is a standalone floor —
// the caller (MediaStrip) sizes the actual box via a definite-height
// parent + the h-full baked below, so placeholder and image always match.
const SKELETON_MIN_H = "min-h-[6rem]";

function rewriteToByHash(legacyUrl: string, hash: string): string {
  // Take the /img?u=… URL we'd otherwise fetch and rewrite it to the
  // stable /img/by-hash/<h>?account_id=…&t=… form. Preserves any other
  // query params (account_id, share token) by parsing them off.
  const qIdx = legacyUrl.indexOf("?");
  if (qIdx < 0) return legacyUrl;
  const search = new URLSearchParams(legacyUrl.slice(qIdx + 1));
  search.delete("u");
  return `/img/by-hash/${hash}?${search.toString()}`;
}

interface MediaTileProps {
  mediaId: number | string | null | undefined;
  proxiedUrl: string;         // already wrapped by proxyImage()
  eager: boolean;
  className?: string;
  alt?: string;
  // The original signed CDN URL (without our /img wrapping). Used as a
  // fallback when /img/by-hash returns 404 — we re-fetch with the
  // legacy /img?u= path which the server uses to refresh the hash map.
  fallbackUrl: string;
}

export function MediaTile({
  mediaId,
  proxiedUrl,
  eager,
  className,
  alt = "",
  fallbackUrl,
}: MediaTileProps) {
  const [blobUrl, setBlobUrl] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);
  const placeholderRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!proxiedUrl) return;
    const ctrl = new AbortController();
    let cancelled = false;
    let createdBlobUrl: string | null = null;
    let io: IntersectionObserver | null = null;

    const fetchOnce = async (urlToFetch: string, allowFallback: boolean): Promise<void> => {
      const resp = await fetch(urlToFetch, {
        signal: ctrl.signal,
        // Supported on Chromium/Safari/FF 132+; older browsers ignore it
        // harmlessly. TypeScript DOM lib accepts it from TS 5.4+.
        priority: eager ? "high" : "low",
      });
      if (!resp.ok) {
        // /img/by-hash returns 404 when the hash map evicted the entry
        // (LRU at the 50k cap) or 410 when the OF signed URL aged out
        // beyond the relay's 1h TTL. Either way, re-fetching the legacy
        // /img?u= URL repopulates the hash and serves fresh bytes.
        if (allowFallback && (resp.status === 404 || resp.status === 410)) {
          if (mediaId != null) mediaIdToHash.delete(String(mediaId));
          await fetchOnce(proxiedUrl, false);
          return;
        }
        if (!cancelled) setFailed(true);
        return;
      }
      const hash = resp.headers.get("x-img-hash");
      if (hash && mediaId != null) mediaIdToHash.set(String(mediaId), hash);
      const blob = await resp.blob();
      if (cancelled) return;
      createdBlobUrl = URL.createObjectURL(blob);
      setBlobUrl(createdBlobUrl);
    };

    const start = async () => {
      try {
        const knownHash = mediaId != null ? mediaIdToHash.get(String(mediaId)) : undefined;
        const initial = knownHash
          ? rewriteToByHash(proxiedUrl, knownHash)
          : proxiedUrl;
        await fetchOnce(initial, /* allowFallback */ knownHash != null);
      } catch (e) {
        // AbortError is the common case (chat switch, scroll-away).
        // It's not an error, don't paint the failed state.
        if (e instanceof DOMException && e.name === "AbortError") return;
        if (!cancelled) setFailed(true);
      }
    };

    if (eager) {
      start();
    } else if (placeholderRef.current) {
      io = new IntersectionObserver(
        ([entry], obs) => {
          if (entry.isIntersecting) {
            obs.disconnect();
            io = null;
            start();
          }
        },
        // 200px above + below the viewport so the fetch finishes just
        // before the tile is visible. Tunable; lower = more aggressive
        // cancel-on-scroll, higher = fewer placeholder flashes.
        { rootMargin: "200px" },
      );
      io.observe(placeholderRef.current);
    } else {
      // Mounted with no ref (shouldn't happen in practice) — be safe.
      start();
    }

    return () => {
      cancelled = true;
      ctrl.abort();
      io?.disconnect();
      if (createdBlobUrl) URL.revokeObjectURL(createdBlobUrl);
    };
    // Deliberately exclude fallbackUrl from deps: changing it shouldn't
    // restart a successful load. proxiedUrl + mediaId + eager fully
    // capture the identity of what's being fetched.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [proxiedUrl, mediaId, eager]);

  if (failed) {
    return (
      <div
        className={cn(
          "w-full h-full bg-bg/30 rounded-md grid place-items-center text-[10px] opacity-70",
          SKELETON_MIN_H,
          className,
        )}
      >
        failed
      </div>
    );
  }
  if (!blobUrl) {
    // Skeleton: a reserved box (never 0-height) the image paints into.
    // h-full fills the parent's reserved box; SKELETON_MIN_H is the floor
    // when there's no sized parent. No aspect-ratio — see SKELETON_MIN_H.
    return (
      <div
        ref={placeholderRef}
        className={cn("w-full h-full bg-bg/30 rounded-md", SKELETON_MIN_H, className)}
      />
    );
  }
  // The image fills the same reserved box with object-cover (crop, not
  // letterbox), so swapping skeleton → image causes zero vertical shift.
  return (
    <img
      src={blobUrl}
      alt={alt}
      decoding="async"
      className={cn("w-full h-full object-cover", className)}
    />
  );
}

/**
 * Pick the media ids that should fetch eagerly when a chat opens. The
 * rule: most recent inbound message's first media, plus most recent
 * outbound message's first media. Walks newest → oldest, stops as soon
 * as it has one of each.
 *
 * If a chat has only inbound media, only that one is eager. If it has
 * none, the set is empty and every tile lazy-loads via the
 * IntersectionObserver path.
 */
export function pickEagerMediaIds(
  messages: { id: number | string; fromUser?: { id?: number | string };
              media?: { id?: number | string }[] }[],
  ownerUserId: number | string | null,
): Set<string> {
  const out = new Set<string>();
  let haveOut = false;
  let haveIn = false;
  for (let i = messages.length - 1; i >= 0; i--) {
    const m = messages[i];
    const firstMediaId = m.media?.[0]?.id;
    if (firstMediaId == null) continue;
    const isOutgoing = sameUserId(m.fromUser?.id, ownerUserId);
    if (isOutgoing && !haveOut) {
      out.add(String(firstMediaId));
      haveOut = true;
    } else if (!isOutgoing && !haveIn) {
      out.add(String(firstMediaId));
      haveIn = true;
    }
    if (haveOut && haveIn) break;
  }
  return out;
}
