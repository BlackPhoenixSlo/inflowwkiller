import type { NextConfig } from "next";

/**
 * Dev rewrites: the browser only ever talks to localhost:3001 (the
 * Next.js dev server). Anything that's a relay endpoint — signed OF
 * paths, admin CRUD, SSE — gets transparently proxied to the Python
 * relay on localhost:8787. This keeps the share-token cookie + the
 * SameSite story trivial, and the relay never sees CORS preflight.
 *
 * Production: a single reverse proxy fronts both, so rewrites are a no-op.
 */

const RELAY_URL = process.env.RELAY_URL || "http://127.0.0.1:8787";

const nextConfig: NextConfig = {
  reactStrictMode: true,
  // Next 16 blocks cross-origin dev requests by default. The dev server binds
  // to `localhost:3001` but you may visit via `127.0.0.1:3001` (or LAN IP) —
  // without this, the HMR + chunk loaders 403 and hydration silently stalls
  // on the SSR placeholder.
  allowedDevOrigins: ["127.0.0.1", "localhost", "192.168.0.27", "macbook-pro.tailca1348.ts.net"],
  // App Router client-side cache. Next 15+ defaulted `dynamic` to 0s, so
  // every back-navigation re-fetches the RSC payload — surfaces as the
  // dev "Rendering…" indicator stalling on Inbox → Home even after Home
  // has already been visited once. Bumping to 60s keeps just-visited
  // routes instant; the SSE + per-query staleTime layer still controls
  // data freshness, this only governs the React tree cache.
  experimental: {
    staleTimes: {
      dynamic: 60,
      static: 300,
    },
  },
  async rewrites() {
    return [
      // Health + admin CRUD (employees, audit, accounts, proxies, sessions, rev).
      // This `/admin/:path*` catch-all already proxies every admin endpoint to
      // the relay, including the message-dims routes added for render stability
      // (18_chat_render_stability §1.1): the GET seed and the new
      // PATCH /admin/messages/{account}/{fan}/media/{mediaId}/dims. No dedicated
      // entry is needed — but note the relay-rewrite footgun: a NEW top-level
      // path OUTSIDE this prefix (or a Next page that shadows it, e.g.
      // app/admin/manage) would 404 before reaching the relay.
      { source: "/health",       destination: `${RELAY_URL}/health` },
      { source: "/admin/:path*", destination: `${RELAY_URL}/admin/:path*` },
      // Friend auth (register/login/logout/me) — must be reachable before
      // the share-token gate (the relay exempts /auth/* on its end too).
      { source: "/auth/:path*",  destination: `${RELAY_URL}/auth/:path*` },
      // Chatter principal (login/logout/register/me/me-owners/me-accounts).
      // Mirror of /auth/* but for the second principal — see
      // service/chatters.py. The relay's share-token gate exempts
      // /chatter/* too so a brand-new chatter following an invite link
      // can reach /chatter/register without a cookie. Owner-side
      // chatter management lives under /admin/chatters/* which is
      // already covered by the broader /admin/:path* rewrite above.
      { source: "/chatter/:path*", destination: `${RELAY_URL}/chatter/:path*` },
      // The signed OF mirror — anything the browser asks at /api/of/v2/*
      // we proxy untouched. Same handler that powers the existing /ui/.
      { source: "/api/of/:path*", destination: `${RELAY_URL}/api/of/:path*` },
      // CDN image proxy — relay tunnels the fetch through the account's
      // own egress IP, since OF CDN URLs are signed to a specific source IP.
      // /img is the base proxy; /img/scrub and /img/by-hash are sub-routes
      // for the storyboard cache and the stable-hash alias.
      { source: "/img",            destination: `${RELAY_URL}/img` },
      { source: "/img/:path*",     destination: `${RELAY_URL}/img/:path*` },
      // SSE channel + the legacy WebSocket endpoint (Next.js rewrites
      // pass WS upgrades through transparently).
      { source: "/events",       destination: `${RELAY_URL}/events` },
      { source: "/ws/:path*",    destination: `${RELAY_URL}/ws/:path*` },
      // Legacy /ui served by the relay's StaticFiles mount. We expose the
      // funnel at the Next dev port so the new /inbox UI is the default
      // entry, but /ui must still resolve so existing share-token links
      // (and ops cheat-sheet URLs) keep working.
      { source: "/ui",           destination: `${RELAY_URL}/ui/` },
      { source: "/ui/",          destination: `${RELAY_URL}/ui/` },
      { source: "/ui/:path*",    destination: `${RELAY_URL}/ui/:path*` },
      // The Infloww skin, same deal — the relay's second StaticFiles mount.
      // TopNav's view switcher points here, so without these three the link
      // 404s at the Next dev port before it ever reaches the relay: exactly
      // the "NEW top-level path OUTSIDE /admin/*" footgun called out above.
      // In production the single reverse proxy fronts both and these are a
      // no-op, which is why the gap only ever shows up in dev.
      { source: "/infloww",        destination: `${RELAY_URL}/infloww/` },
      { source: "/infloww/",       destination: `${RELAY_URL}/infloww/` },
      { source: "/infloww/:path*", destination: `${RELAY_URL}/infloww/:path*` },
      // Drift detection.
      { source: "/admin/rev/:path*", destination: `${RELAY_URL}/admin/rev/:path*` },
      // Public tracking-link redirect (Growth → Tracking Links). A fan hits
      // /t/<slug>; the relay records the click and 302s to the target. Must be
      // a top-level rewrite — it's outside the /admin/* prefix.
      { source: "/t/:slug", destination: `${RELAY_URL}/t/:slug` },
      // Desktop-app download page. Unlike everything above this is served by
      // Next itself out of `public/download.html` — it is deliberately NOT a
      // relay path, so it stays reachable with no share token and no login,
      // which is the whole point: it is linked from the login screens.
      { source: "/download", destination: "/download.html" },
    ];
  },
  // Skip image optimization for now — OF media URLs are CDN-hosted with
  // their own caching; Next's loader adds latency for no gain.
  images: { unoptimized: true },
};

export default nextConfig;
