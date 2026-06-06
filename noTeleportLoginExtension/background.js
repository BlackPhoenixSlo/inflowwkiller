// Service worker — the proxy-routing brain.
//
// Two responsibilities:
//
//   1. Switch the browser's onlyfans.com traffic through a chosen proxy
//      using chrome.proxy.settings + a PAC script that scopes the routing
//      to onlyfans.com ONLY. Every other site keeps going DIRECT so the
//      user's normal browsing isn't affected.
//
//   2. Supply the proxy credentials on demand via
//      chrome.webRequest.onAuthRequired (the only way to feed
//      user:pass to a proxy in MV3 without burning them into the URL,
//      which Chrome refuses to honour for security reasons).
//
// The proxy is bound to `scope: "regular"` because MV3 service workers
// can't use `"incognito_persistent"`/`"incognito_session_only"` reliably
// across SW restarts. To minimize blast radius the PAC matches ONLY
// onlyfans.com — every other host falls through to DIRECT.
//
// On uninstall / disable Chrome auto-reverts proxy settings, so a forgotten
// active proxy can't outlive the extension.

const ONLYFANS_HOST_REGEX = /(^|\.)onlyfans\.com$/;

// ── Proxy on/off ──────────────────────────────────────────────

async function activateProxy(proxy) {
  // proxy: { scheme, host, port, username, password, label }
  if (!proxy || !proxy.host || !proxy.port) {
    throw new Error("activateProxy: missing host/port");
  }
  const scheme = (proxy.scheme || "http").toLowerCase();
  if (!["http", "https", "socks4", "socks5"].includes(scheme)) {
    throw new Error(`unsupported proxy scheme: ${scheme}`);
  }

  // PAC return string. Format Chrome accepts: "PROXY h:p", "HTTPS h:p",
  // "SOCKS5 h:p", "SOCKS h:p" (= SOCKS4). HTTP CONNECT proxies use "PROXY".
  const pacScheme =
    scheme === "https" ? "HTTPS" :
    scheme === "socks5" ? "SOCKS5" :
    scheme === "socks4" ? "SOCKS" :
    "PROXY";  // http
  const target = `${pacScheme} ${proxy.host}:${proxy.port}`;

  const pacScript = `
    function FindProxyForURL(url, host) {
      var h = host.toLowerCase();
      if (h === "onlyfans.com" || h.endsWith(".onlyfans.com")) {
        return ${JSON.stringify(target)};
      }
      return "DIRECT";
    }
  `;

  await chrome.proxy.settings.set({
    value: {
      mode: "pac_script",
      pacScript: { data: pacScript, mandatory: true },
    },
    scope: "regular",
  });

  // Persist creds + the active proxy snapshot so the onAuthRequired
  // listener (which runs in fresh SW invocations) can read them back.
  await chrome.storage.local.set({
    proxyActive: true,
    activeProxy: proxy,
  });
}

async function deactivateProxy() {
  await chrome.proxy.settings.clear({ scope: "regular" });
  await chrome.storage.local.set({ proxyActive: false, activeProxy: null });
}

// ── Proxy auth ────────────────────────────────────────────────
//
// When OF traffic hits the proxy, the proxy responds 407 Proxy Auth Required
// (or the SOCKS handshake asks for user:pass). Chrome fires onAuthRequired;
// returning {authCredentials: {...}} from a blocking listener feeds them in.
// MV3 needs the `webRequestAuthProvider` permission for this.

chrome.webRequest.onAuthRequired.addListener(
  (details, callback) => {
    // Only handle proxy challenges — server-side 401s on OF (e.g. expired
    // cookies) get the default browser prompt so the user notices.
    if (!details.isProxy) {
      if (callback) callback({});
      return {};
    }
    chrome.storage.local.get(["activeProxy", "proxyActive"]).then((s) => {
      if (!s.proxyActive || !s.activeProxy) {
        if (callback) callback({});
        return;
      }
      const p = s.activeProxy;
      if (!p.username) {
        if (callback) callback({});
        return;
      }
      if (callback) callback({
        authCredentials: { username: p.username, password: p.password || "" },
      });
    });
    return {};  // ignored in MV3 blocking mode — callback is the path
  },
  { urls: ["https://onlyfans.com/*", "https://*.onlyfans.com/*"] },
  ["asyncBlocking"]
);

// ── Message router ────────────────────────────────────────────

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (!msg || !msg.type) return;

  if (msg.type === "activateProxy") {
    activateProxy(msg.proxy)
      .then(() => sendResponse({ ok: true }))
      .catch((e) => sendResponse({ ok: false, error: String(e && e.message || e) }));
    return true;
  }

  if (msg.type === "deactivateProxy") {
    deactivateProxy()
      .then(() => sendResponse({ ok: true }))
      .catch((e) => sendResponse({ ok: false, error: String(e && e.message || e) }));
    return true;
  }

  if (msg.type === "proxyState") {
    chrome.storage.local.get(["proxyActive", "activeProxy"]).then((s) => {
      sendResponse({
        active: !!s.proxyActive,
        proxy: s.activeProxy || null,
      });
    });
    return true;
  }

  if (msg.type === "getCookies") {
    chrome.cookies.getAll({ domain: "onlyfans.com" })
      .then((cookies) => sendResponse({ cookies: cookies || [] }))
      .catch((err) => sendResponse({ cookies: [], error: String(err && err.message || err) }));
    return true;
  }

  if (msg.type === "resetCapture") {
    chrome.storage.local.remove(["lastCapture"]).then(() => sendResponse({ ok: true }));
    return true;
  }

  if (msg.type === "capture") {
    console.log("[FasttNoTeleport] capture received", {
      user_id:   msg.payload?.headers?.["user-id"],
      x_of_rev:  msg.payload?.headers?.["x-of-rev"],
      static_param: msg.payload?.rules?.static_param ? "(captured)" : "(missing)",
      at: msg.payload?.capturedAt,
    });
    return;
  }
});

chrome.runtime.onInstalled.addListener(() => {
  console.log("[FasttNoTeleport] installed — proxy is off until you activate it from the popup");
});

// Sanity log on startup so devs can confirm the SW is alive.
chrome.runtime.onStartup?.addListener(() => {
  console.log("[FasttNoTeleport] SW startup");
});
