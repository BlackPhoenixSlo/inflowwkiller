// ISOLATED-world bridge — forwards MAIN-world postMessages into
// chrome.storage.local so the popup can read captures. Uses a distinct
// envelope key (__chatterly_nt_capture) from the loginExtension so the
// two can coexist in the same browser without clobbering each other.

(function () {
  "use strict";

  window.addEventListener("message", (event) => {
    if (event.source !== window) return;
    const data = event.data;
    if (!data || data.__chatterly_nt_capture !== true || !data.payload) return;

    const payload = data.payload;
    if (!payload.headers || !payload.headers["x-of-rev"] || !payload.headers["user-id"]) return;

    try { chrome.runtime.sendMessage({ type: "capture", payload }); } catch (_) {}

    chrome.storage.local.get(["lastCapture"]).then((cur) => {
      const prevRules = (cur && cur.lastCapture && cur.lastCapture.rules) || {};
      const newRules = payload.rules || {};
      chrome.storage.local.set({
        lastCapture: {
          headers: payload.headers,
          rules: {
            static_param: newRules.static_param || prevRules.static_param || null,
            start:        newRules.start        || prevRules.start        || null,
            end:          newRules.end          || prevRules.end          || null,
          },
          url: payload.url,
          capturedAt: payload.capturedAt,
          origin: location.origin,
        },
      });
    }).catch(() => {});
  });
})();
