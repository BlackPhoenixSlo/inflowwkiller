/* eslint-disable */
/**
 * sw-images.js — Service Worker for the chat-image proxy.
 *
 * Caches every /img and /img/by-hash response in a Cache Storage bucket
 * keyed by the asset's stable identity (path-only of the inner OF URL,
 * or the hash for /img/by-hash). OF rotates Policy/Signature/Key-Pair-Id
 * every fetch, so naive HTTP caching by full URL misses on every paint;
 * we strip those before hashing the key.
 *
 * Strategy:
 *   • cached + fresh (< TTL_MS)   → return cached
 *   • cached + stale              → return cached, refetch in background
 *   • not cached                  → fetch, store, return
 *
 * Persistence: gigabytes (Chrome offers ~60% of free disk per origin).
 * Survives tab close, browser restart, even — popout windows hit the
 * same SW so the inbox's image fetches warm the popout's cache for free.
 *
 * Registered only in production (see app/lib/registerImageSW.ts) to
 * avoid Turbopack dev confusing the SW lifecycle.
 */

// v3: TTL is now access-based (we stamp x-cached-at on every read, not
// just on initial fetch). An avatar you view daily stays cached
// indefinitely; one you never look at falls out of soft → hard window
// and revalidates. Fixed the prior bug where popular avatars expired on
// the original `date` header regardless of how often they were used.
// v2 (legacy): introduced /img/scrub skip to fix per-frame keying.
const CACHE = "chatterly-img-v3";
// 7-day soft / 30-day hard. Avatars almost never change, and our SW
// keeps the bytes — OF rotating the signed URL no longer invalidates
// us because buildKeyRequest() strips the signature query.
const TTL_MS = 7 * 24 * 3600 * 1000;
const HARD_TTL_MS = 30 * 24 * 3600 * 1000;
// Header we set on every cache write (including touch-on-read) so age
// reflects last access rather than original fetch time.
const CACHED_AT_HEADER = "x-cached-at";

self.addEventListener("install", () => {
  // Activate immediately on first install; we don't have a previous
  // version to defer to.
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  // Drop older cache versions if the name ever bumps. Bump CACHE above
  // when the storage format changes.
  event.waitUntil(
    (async () => {
      const keys = await caches.keys();
      await Promise.all(
        keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)),
      );
      await self.clients.claim();
    })(),
  );
});

self.addEventListener("message", (event) => {
  // Identity transition wipe. Posted from UserContext.wipeIdentityCaches()
  // on login/register/logout so the next friend on the same laptop can't
  // craft a /img?u=...&account_id=<prev> URL and read the previous user's
  // cached bytes out of Cache Storage. Cache keys already include
  // account_id (see buildKeyRequest) so accidental collision is
  // impossible — this only closes the manual-snoop hole.
  if (event.data && event.data.type === "clear-cache") {
    event.waitUntil(caches.delete(CACHE));
  }
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;
  let url;
  try {
    url = new URL(req.url);
  } catch {
    return;
  }
  // Same-origin only — only intercept our own /img endpoints.
  if (url.origin !== self.location.origin) return;
  if (!url.pathname.startsWith("/img")) return;
  // /img/scrub returns per-frame storyboard jpgs, already disk-cached by
  // the relay (immutable Cache-Control). The browser's HTTP cache handles
  // it; trying to SW-cache it broke the per-frame keying and froze
  // every video at the first cached frame. Skip the SW for that route.
  if (url.pathname === "/img/scrub" || url.pathname.startsWith("/img/scrub/")) return;
  event.respondWith(handleImage(req, url));
});

async function handleImage(req, url) {
  const keyReq = buildKeyRequest(url);
  const cache = await caches.open(CACHE);

  const cached = await cache.match(keyReq);
  if (cached) {
    const age = ageMs(cached);
    if (age != null && age < TTL_MS) {
      // Fresh — return immediately and refresh the last-access stamp so
      // a frequently-viewed asset never ages out.
      touchCache(cache, keyReq, cached);
      return cached;
    }
    if (age != null && age < HARD_TTL_MS) {
      // Stale-while-revalidate: serve stale, refresh in background.
      event_revalidate(req, cache, keyReq);
      return cached;
    }
    // Past hard TTL — fall through to fetch.
  }

  try {
    const resp = await fetch(req);
    if (resp.ok || resp.status === 206) {
      // 206 happens on Safari's video preflight (Range request). Cache
      // only complete 200s — 206s would poison the cache with partials.
      if (resp.status === 200) {
        putWithStamp(cache, keyReq, resp.clone()).catch(() => {});
      }
    }
    return resp;
  } catch (e) {
    // Network error. If we had a stale entry, hand it back — better than
    // a broken tile.
    if (cached) return cached;
    throw e;
  }
}

function event_revalidate(req, cache, keyReq) {
  // Fire-and-forget background refresh. Errors are swallowed: the cached
  // entry is already serving the user, no need to surface a refresh
  // failure.
  fetch(req)
    .then((resp) => {
      if (resp.status === 200) {
        return putWithStamp(cache, keyReq, resp);
      }
    })
    .catch(() => {});
}

/**
 * Rewrite the cached entry with a fresh x-cached-at header. Cache
 * Storage doesn't expose mutable metadata, so we rebuild the Response.
 * Fire-and-forget; failure just means age stays whatever it was.
 */
async function touchCache(cache, keyReq, cached) {
  try {
    const body = await cached.clone().arrayBuffer();
    const headers = new Headers(cached.headers);
    headers.set(CACHED_AT_HEADER, new Date().toUTCString());
    const fresh = new Response(body, {
      status: cached.status,
      statusText: cached.statusText,
      headers,
    });
    await cache.put(keyReq, fresh);
  } catch {
    /* swallow */
  }
}

async function putWithStamp(cache, keyReq, resp) {
  const body = await resp.arrayBuffer();
  const headers = new Headers(resp.headers);
  headers.set(CACHED_AT_HEADER, new Date().toUTCString());
  const stamped = new Response(body, {
    status: resp.status,
    statusText: resp.statusText,
    headers,
  });
  return cache.put(keyReq, stamped);
}

/**
 * Construct a stable cache-key Request from the inbound URL. The actual
 * cached body is the response to this canonical key, so two different
 * signed URLs for the same physical asset share storage.
 *
 *   /img?u=<signed OF URL>&account_id=A
 *     → /img-cache-key/u/<sha1(path-only)>/A
 *
 *   /img/by-hash/<h>?account_id=A
 *     → /img-cache-key/h/<h>/A
 */
function buildKeyRequest(url) {
  let key;
  if (url.pathname === "/img") {
    const inner = url.searchParams.get("u") || "";
    const account = url.searchParams.get("account_id") || "_";
    const stable = stripQuery(inner).toLowerCase();
    key = `/img-cache-key/u/${stable}/${account}`;
  } else {
    // /img/by-hash/<h>
    const h = url.pathname.replace(/^\/img\/by-hash\//, "");
    const account = url.searchParams.get("account_id") || "_";
    key = `/img-cache-key/h/${h}/${account}`;
  }
  // Request constructor expects an absolute URL; same-origin is fine.
  return new Request(new URL(key, self.location.origin));
}

function stripQuery(u) {
  const i = u.indexOf("?");
  return i < 0 ? u : u.slice(0, i);
}

function ageMs(resp) {
  // Prefer x-cached-at (set on every read & write — last-access based).
  // Fall back to Date for entries from the legacy v2 cache that haven't
  // been touched yet under v3.
  const stamp = resp.headers.get(CACHED_AT_HEADER) || resp.headers.get("date");
  if (stamp) {
    const t = new Date(stamp).getTime();
    if (Number.isFinite(t)) return Date.now() - t;
  }
  return null;
}
