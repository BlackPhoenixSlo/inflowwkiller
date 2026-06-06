"use client";

/**
 * registerImageSW — install the /sw-images.js Service Worker.
 *
 * Production-only: Turbopack's dev SW lifecycle is fragile and an
 * orphaned SW serving stale JS bundles is exactly the kind of stuck-tab
 * pain TRIAGE.txt step 4C warns about. In dev we want every fetch to
 * hit the relay so /admin/img-cache/stats reflects reality.
 *
 * The SW lives at /sw-images.js (public root) so its scope is the whole
 * origin — necessary to intercept /img and /img/by-hash from any path.
 */

const SW_URL = "/sw-images.js";

export function registerImageSW(): void {
  if (typeof window === "undefined") return;
  if (process.env.NODE_ENV !== "production") return;
  if (!("serviceWorker" in navigator)) return;

  // navigator.serviceWorker only works over HTTPS or localhost. Tailscale
  // Funnel is HTTPS so production is fine. LAN HTTP would silently fail
  // — log a warning so the browser console hints why caching is off.
  const proto = window.location.protocol;
  const host = window.location.hostname;
  const isLocalhost = host === "localhost" || host === "127.0.0.1";
  if (proto !== "https:" && !isLocalhost) {
    console.info("[sw] not registering — insecure origin");
    return;
  }

  navigator.serviceWorker
    .register(SW_URL, { scope: "/" })
    .then(async (reg) => {
      // Force-update check so a freshly-deployed SW with a bug fix takes
      // over the page without requiring the user to close every tab. The
      // browser normally only re-checks the SW script on navigation; this
      // makes us re-check on every page mount.
      try { await reg.update(); } catch { /* non-fatal */ }
    })
    .catch((err) => {
      console.warn("[sw] register failed", err);
    });
}
