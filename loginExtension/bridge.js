// ISOLATED-world bridge — receives postMessage from interceptor.js (MAIN
// world) and persists the latest capture into chrome.storage.local so the
// popup can read it. The MAIN world can't touch chrome.*, so this bridge
// is the only path off the page.

(function () {
  "use strict";

  window.addEventListener("message", (event) => {
    if (event.source !== window) return;
    const data = event.data;
    if (!data || data.__chatterly_capture !== true || !data.payload) return;

    const payload = data.payload;
    if (!payload.headers || !payload.headers["x-of-rev"] || !payload.headers["user-id"]) return;

    try {
      chrome.runtime.sendMessage({
        type: "capture",
        payload: payload,
      });
    } catch (_) {
      // Service worker may be asleep — storage write is the real source of truth.
    }

    // Merge with existing rules so a fresh header-only publish doesn't wipe
    // a previously captured static_param (rules trickle in across multiple
    // postMessages: first a header flush, then later when sha1 fires).
    chrome.storage.local.get(["lastCapture"]).then((cur) => {
      const prevRules = (cur && cur.lastCapture && cur.lastCapture.rules) || {};
      const newRules = payload.rules || {};
      const mergedRules = {
        static_param: newRules.static_param || prevRules.static_param || null,
        start:        newRules.start        || prevRules.start        || null,
        end:          newRules.end          || prevRules.end          || null,
      };
      chrome.storage.local.set({
        lastCapture: {
          headers: payload.headers,
          rules: mergedRules,
          url: payload.url,
          capturedAt: payload.capturedAt,
          origin: location.origin,
        },
      });
    }).catch(() => {});
  });
})();
