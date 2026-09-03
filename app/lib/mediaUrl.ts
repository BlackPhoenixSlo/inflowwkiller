/**
 * mediaUrl.ts — building the relay's image + scrub-frame URLs.
 *
 * Split out of relay.ts: these are URL builders for the browser's <img> tags,
 * not part of the typed fetch client. They share only `resolveShareToken`
 * with it, so keeping them here lets relay.ts be about transport and wire
 * shapes while this file owns "how do we point at a proxied asset".
 */

import { resolveShareToken } from "@/lib/shareToken";

/**
 * Wrap an OF CDN URL so it loads through the relay's `/img` proxy. We have
 * to do this because OF signs CDN URLs with `AWS:SourceIp=<egress IP>/32` —
 * the browser's IP doesn't match the account's proxy egress, so the image
 * 403s. `/img` tunnels the fetch via the right account's HTTP client.
 *
 * Returns the original URL if either `url` or `accountId` is missing.
 */
export function proxyImage(url: string | null | undefined, accountId: string | null | undefined): string {
  if (!url) return "";
  // Browser-local URLs (blob:/data:) aren't fetchable through the relay
  // — they're already in the browser, just pass them through.
  if (url.startsWith("blob:") || url.startsWith("data:")) return url;
  if (!accountId) return url;
  const tok = resolveShareToken();
  const params = new URLSearchParams();
  params.set("u", url);
  params.set("account_id", accountId);
  if (tok) params.set("t", tok);
  return `/img?${params.toString()}`;
}

/**
 * Build a URL for the i-th scrub frame (0..11) of a video. The relay
 * lazily extracts a 12-frame storyboard for each video on first hit;
 * subsequent fetches serve cached JPGs straight from disk. Returns ""
 * if we don't have everything we need to build the URL.
 *
 * Passing `duration` (seconds, from VaultMedia.duration which OF already
 * tells us in the vault listing) lets the relay skip its own ffprobe
 * step and start the extraction immediately.
 */
export function proxyScrubFrame(
  url: string | null | undefined,
  accountId: string | null | undefined,
  frameIdx: number,
  duration?: number | null,
  /** Per-hover session id. The cancel POST that fires on hover-end
   *  carries the SAME id so the server can scope the abort to this
   *  exact hover session — a delayed cancel from a previous hover of
   *  the same video can't abort a freshly-started build. */
  sessionId?: string | null,
): string {
  if (!url || !accountId) return "";
  if (url.startsWith("blob:") || url.startsWith("data:")) return "";
  const tok = resolveShareToken();
  const params = new URLSearchParams();
  params.set("u", url);
  params.set("account_id", accountId);
  params.set("i", String(frameIdx));
  if (typeof duration === "number" && duration > 0) {
    params.set("dur", String(duration));
  }
  if (sessionId) params.set("sid", sessionId);
  if (tok) params.set("t", tok);
  return `/img/scrub?${params.toString()}`;
}
