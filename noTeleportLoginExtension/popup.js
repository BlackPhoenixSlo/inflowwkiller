// Popup logic — collects proxy details, asks the SW to activate them,
// opens OF for sign-in, then exports the curl with x-relay-static-param.

const REQUIRED_COOKIES = ["auth_id", "sess"];
const $ = (id) => document.getElementById(id);

const proxyState = $("proxy-state");
const proxyPick  = $("proxy-pick");
const proxyUrl   = $("proxy-url");
const relayUrl   = $("relay-url");
const relayTok   = $("relay-token");
const statusLine = $("status-line");
const metaBox    = $("meta");
const copyBtn    = $("copy-btn");
const toast      = $("toast");

let currentCapture = null;

function showToast(msg, kind = "ok") {
  toast.textContent = msg;
  toast.className = "toast show " + (kind === "err" ? "err" : "ok");
  clearTimeout(showToast._t);
  showToast._t = setTimeout(() => toast.classList.remove("show"), 3000);
}

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function shellQuote(s) {
  return "'" + String(s).replace(/'/g, "'\\''") + "'";
}

// ── Proxy URL parser ──────────────────────────────────────────
// Accepts: scheme://[user[:pass]@]host:port
// Schemes: http, https, socks4, socks5
function parseProxyUrl(s) {
  s = String(s || "").trim();
  if (!s) return null;
  // Default to http:// if user dropped scheme — Chrome's proxy API
  // requires one, and HTTP-CONNECT is the most common DC-proxy default.
  if (!/^\w+:\/\//.test(s)) s = "http://" + s;
  let u;
  try { u = new URL(s); } catch { return null; }
  const scheme = u.protocol.replace(":", "").toLowerCase();
  if (!["http", "https", "socks4", "socks5"].includes(scheme)) return null;
  if (!u.hostname || !u.port) return null;
  return {
    scheme,
    host: u.hostname,
    port: parseInt(u.port, 10),
    username: decodeURIComponent(u.username || ""),
    password: decodeURIComponent(u.password || ""),
    label: s.replace(/\/\/[^@]*@/, "//"),  // hide creds in the label
  };
}

function buildCurl(capture, cookies) {
  const h = capture.headers;
  const r = capture.rules || {};
  const url = capture.url || "https://onlyfans.com/api2/v2/users/me";
  const cookieHeader = cookies
    .filter((c) => c.value && c.name)
    .map((c) => `${c.name}=${c.value}`)
    .join("; ");

  const lines = [
    `curl ${shellQuote(url)} \\`,
    `  -H ${shellQuote("accept: application/json, text/plain, */*")} \\`,
    `  -H ${shellQuote("user-agent: " + (h["user-agent"] || navigator.userAgent))} \\`,
    `  -H ${shellQuote("user-id: " + h["user-id"])} \\`,
    `  -H ${shellQuote("x-bc: " + h["x-bc"])} \\`,
    `  -H ${shellQuote("x-of-rev: " + h["x-of-rev"])} \\`,
    `  -H ${shellQuote("sign: " + h["sign"])} \\`,
    `  -H ${shellQuote("time: " + h["time"])} \\`,
    `  -H ${shellQuote("referer: https://onlyfans.com/")} \\`,
    `  -H ${shellQuote("app-token: 33d57ade8c02dbc5a333db99ff9ae26a")}`,
  ];
  if (r.static_param) {
    lines[lines.length - 1] += " \\";
    lines.push(`  -H ${shellQuote("x-relay-static-param: " + r.static_param)}`);
  }
  if (r.start) {
    lines[lines.length - 1] += " \\";
    lines.push(`  -H ${shellQuote("x-relay-sign-start: " + r.start)}`);
  }
  if (r.end) {
    lines[lines.length - 1] += " \\";
    lines.push(`  -H ${shellQuote("x-relay-sign-end: " + r.end)}`);
  }
  if (cookieHeader) {
    lines[lines.length - 1] += " \\";
    lines.push(`  -b ${shellQuote(cookieHeader)}`);
  }
  return lines.join("\n");
}

// ── Capture status panel ──────────────────────────────────────

function renderStatus() {
  if (!currentCapture) {
    statusLine.innerHTML = '<span class="warn">Waiting for sign-in…</span>';
    metaBox.textContent = "";
    copyBtn.disabled = true;
    return;
  }
  const h = currentCapture.headers || {};
  const r = currentCapture.rules || {};
  const haveStatic = !!r.static_param;
  statusLine.innerHTML = haveStatic
    ? '<span class="ok">✓ Captured (with signing rules)</span>'
    : '<span class="warn">✓ Headers captured — open a chat to fire sign()…</span>';
  metaBox.innerHTML =
    `<div>user-id: ${escapeHtml(h["user-id"] || "?")}</div>` +
    `<div>x-of-rev: ${escapeHtml(h["x-of-rev"] || "?")}</div>` +
    `<div>static_param: ${haveStatic ? escapeHtml(r.static_param.slice(0,24) + "…") : '<span class="warn">missing</span>'}</div>` +
    `<div>captured: ${escapeHtml(currentCapture.capturedAt || "?")}</div>`;
  copyBtn.disabled = false;
}

// ── Proxy state panel ─────────────────────────────────────────

function renderProxyState(state) {
  if (state && state.active && state.proxy) {
    const p = state.proxy;
    proxyState.className = "proxy-state on";
    proxyState.innerHTML =
      `<strong>proxy ON</strong> · ${escapeHtml(p.scheme)}://${escapeHtml(p.host)}:${p.port}` +
      (p.username ? ` · auth: ${escapeHtml(p.username)}` : "") +
      `<br>scope: onlyfans.com only · everything else stays DIRECT`;
  } else {
    proxyState.className = "proxy-state off";
    proxyState.textContent = "proxy: off · onlyfans.com goes direct";
  }
}

async function refreshProxyState() {
  return new Promise((res) => {
    chrome.runtime.sendMessage({ type: "proxyState" }, (s) => {
      renderProxyState(s || {});
      res(s);
    });
  });
}

// ── Relay proxy list (optional) ───────────────────────────────

async function loadRelayProxies() {
  const url = relayUrl.value.trim().replace(/\/+$/, "");
  if (!url) { showToast("set a relay URL first", "err"); return; }
  const tok = relayTok.value.trim();
  const target = `${url}/admin/proxies${tok ? `?t=${encodeURIComponent(tok)}` : ""}`;
  let data;
  try {
    const r = await fetch(target, { credentials: "omit" });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    data = await r.json();
  } catch (e) {
    showToast("relay fetch failed: " + e.message, "err");
    return;
  }
  await chrome.storage.local.set({ relayConfig: { url, token: tok } });
  const proxies = (data && data.proxies) || [];
  proxyPick.innerHTML = '<option value="">— enter manually below —</option>' +
    proxies.map((p, i) => {
      const acct = p.assigned_account ? ` → ${p.assigned_account.nickname || p.assigned_account.id}` : "";
      return `<option value="${i}">${escapeHtml(p.label)} · ${escapeHtml(p.host)}:${p.port}${acct}</option>`;
    }).join("");
  // Stash the parsed list so onchange can rehydrate user:pass from creds.
  proxyPick.__list = proxies;
  showToast(`loaded ${proxies.length} proxy(s) from relay`);
}

proxyPick.addEventListener("change", () => {
  const idx = parseInt(proxyPick.value, 10);
  const list = proxyPick.__list || [];
  if (Number.isNaN(idx) || !list[idx]) { return; }
  const p = list[idx];
  // The relay's /admin/proxies returns {label, scheme, host, port, username, password, ...}.
  // Build a proxy URL for the manual field so the user can see/edit it.
  const scheme = p.scheme || "http";
  const auth = p.username
    ? `${encodeURIComponent(p.username)}:${encodeURIComponent(p.password || "")}@`
    : "";
  proxyUrl.value = `${scheme}://${auth}${p.host}:${p.port}`;
});

// ── Activate / deactivate / login / copy / reset ──────────────

$("activate-btn").addEventListener("click", async () => {
  const parsed = parseProxyUrl(proxyUrl.value);
  if (!parsed) {
    showToast("invalid proxy URL — need scheme://user:pass@host:port", "err");
    return;
  }
  await new Promise((res) =>
    chrome.runtime.sendMessage({ type: "activateProxy", proxy: parsed }, res)
  );
  const s = await refreshProxyState();
  if (s && s.active) {
    showToast(`proxy ON — ${parsed.host}:${parsed.port}`);
  } else {
    showToast("activate failed — check console", "err");
  }
});

$("deactivate-btn").addEventListener("click", async () => {
  await new Promise((res) => chrome.runtime.sendMessage({ type: "deactivateProxy" }, res));
  await refreshProxyState();
  showToast("proxy OFF");
});

$("relay-load").addEventListener("click", loadRelayProxies);

$("login-btn").addEventListener("click", async () => {
  // Warn loudly if the user clicks Login without an active proxy — defeats
  // the whole point of this extension.
  const s = await new Promise((res) =>
    chrome.runtime.sendMessage({ type: "proxyState" }, res)
  );
  if (!s || !s.active) {
    if (!confirm(
      "Proxy is OFF — OF will mint cookies on this device's WAN IP.\n\n" +
      "If you bind a proxy in the relay later, OF will see the account " +
      "teleport. Continue anyway?"
    )) return;
  }
  const tabs = await chrome.tabs.query({ url: "https://onlyfans.com/*" });
  if (tabs.length > 0) {
    await chrome.tabs.update(tabs[0].id, { active: true });
    await chrome.windows.update(tabs[0].windowId, { focused: true });
  } else {
    await chrome.tabs.create({ url: "https://onlyfans.com/" });
  }
});

$("copy-btn").addEventListener("click", async () => {
  if (!currentCapture) return;
  const cookies = await new Promise((res) =>
    chrome.runtime.sendMessage({ type: "getCookies" }, (r) => res((r && r.cookies) || []))
  );
  const names = new Set(cookies.map((c) => c.name));
  const missing = REQUIRED_COOKIES.filter((n) => !names.has(n));
  if (missing.length) {
    showToast("missing cookies: " + missing.join(", ") + " — sign in first", "err");
    return;
  }
  const curl = buildCurl(currentCapture, cookies);
  try {
    await navigator.clipboard.writeText(curl);
    showToast("curl copied — paste into the relay's Setup tab");
  } catch {
    const ta = document.createElement("textarea");
    ta.value = curl;
    document.body.appendChild(ta);
    ta.select();
    document.execCommand("copy");
    document.body.removeChild(ta);
    showToast("curl copied (fallback)");
  }
});

$("reset-btn").addEventListener("click", async () => {
  await new Promise((res) => chrome.runtime.sendMessage({ type: "resetCapture" }, res));
  currentCapture = null;
  renderStatus();
  showToast("capture cleared");
});

// ── Init ──────────────────────────────────────────────────────

(async () => {
  // Restore saved relay config + last capture.
  const stored = await chrome.storage.local.get(["relayConfig", "lastCapture"]);
  if (stored.relayConfig) {
    relayUrl.value = stored.relayConfig.url || "";
    relayTok.value = stored.relayConfig.token || "";
  } else {
    relayUrl.value = "http://127.0.0.1:8787";
  }
  currentCapture = stored.lastCapture || null;
  renderStatus();
  await refreshProxyState();

  chrome.storage.onChanged.addListener((changes, area) => {
    if (area !== "local") return;
    if (changes.lastCapture) {
      currentCapture = changes.lastCapture.newValue || null;
      renderStatus();
      if (currentCapture) showToast("new capture stored");
    }
    if (changes.proxyActive || changes.activeProxy) {
      refreshProxyState();
    }
  });
})();
