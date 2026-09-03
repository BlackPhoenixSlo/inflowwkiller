/**
 * shareToken.ts — the read-only share link's token.
 *
 * Its own module because both the fetch client (relay.ts) and the image URL
 * builders (mediaUrl.ts) need it; keeping it in either one would make the
 * other import a module it otherwise has no business depending on.
 *
 * The token arrives as `?t=` on the share URL and is remembered in
 * localStorage so it survives in-app navigation that drops the query string.
 */

/**
 * Read the share token from the URL or from a previously-stored localStorage
 * value. Mirrors the existing /ui/'s behavior so a user who pastes
 * `?t=...&...` once doesn't have to repeat it.
 */
export function resolveShareToken(): string | null {
  if (typeof window === "undefined") return null;
  const fromUrl = new URLSearchParams(window.location.search).get("t");
  if (fromUrl) {
    try {
      window.localStorage.setItem("chatterly:share_token", fromUrl);
    } catch {
      /* ignore quota / safari private */
    }
    return fromUrl;
  }
  try {
    return window.localStorage.getItem("chatterly:share_token");
  } catch {
    return null;
  }
}
