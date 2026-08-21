"use strict";

// ───────────────────────────────────────────────────────────────────────────
// Native OnlyFans session capture — the Electron replacement for the Chrome
// login-capture extension (Phase 2).
//
// Open an OF login window, run the MAIN-world interceptor as a PRELOAD (the
// window sets contextIsolation:false, so the preload can patch XHR/fetch/webpack
// at document-start), let the creator log in, accumulate the signed headers +
// webpack signing rules + session cookies, then POST a paste-curl to the relay's
// existing /admin/session/bootstrap.
//
// IMPORTANT (why no CDP): onlyfans.com is fronted by Cloudflare, which detects
// Chrome DevTools Protocol (debugger.attach / Runtime.enable) and hard-blocks it
// — that produced a blank challenge window. A plain preload trips none of that,
// so the Cloudflare "verify you're human" challenge can render and be solved.
//
// DETECTION NOTE (from the fusion review): logging into OF from Electron is still
// riskier than the real Chrome extension — Cloudflare fingerprints deeply (TLS,
// canvas) and may still challenge or block. We present a real Chrome UA + matching
// client-hint headers to reduce the gap, but this is NOT a guarantee. Residual
// risk is documented in README; for a valuable account the extension is safer.
// ───────────────────────────────────────────────────────────────────────────

const { BrowserWindow, session, ipcMain } = require("electron");

const OF_URL = "https://onlyfans.com/";
const CAPTURE_PARTITION = "of-capture"; // NON-persistent → OF creds never touch disk.
const APP_TOKEN = "33d57ade8c02dbc5a333db99ff9ae26a";
const REQUIRED_HDRS = ["user-id", "x-bc", "x-of-rev", "sign", "time"];
const POLL_MS = 1500;

// The UA we present to OnlyFans must match the engine actually rendering the
// page. A literal rots into the exact tell it was added to prevent: this was
// pinned at "131" (Nov 2024) and by Aug 2026 real stable Chrome was 151 — a
// browser two years out of date is more conspicuous than plain Electron.
// process.versions.chrome is the one value that cannot drift from the binary.
// Chrome freezes the minor/build/patch at 0.0.0 in its reduced UA, so the
// major alone is the whole story.
const CHROME_MAJOR = String(process.versions.chrome || "").split(".")[0];

// A malformed UA ("Chrome/.0.0.0") is worse than no capture at all — it is a
// unique fingerprint. Fail loudly instead of quietly signing requests with it.
if (!/^\d+$/.test(CHROME_MAJOR)) {
  throw new Error(
    "of-capture: cannot derive Chrome major from process.versions.chrome=" +
      JSON.stringify(process.versions.chrome)
  );
}
function uaProfile() {
  const isMac = process.platform === "darwin";
  return {
    userAgent: isMac
      ? `Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/${CHROME_MAJOR}.0.0.0 Safari/537.36`
      : `Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/${CHROME_MAJOR}.0.0.0 Safari/537.36`,
    platform: isMac ? '"macOS"' : '"Windows"',
  };
}

let flow = null; // single active capture flow

function freshState() {
  return {
    headers: {},
    rules: {},
    cookies: { auth_id: false, sess: false },
    status: "waiting", // waiting | ready | sending | done | error
    error: null,
    savedAs: null,
    detection: `Chrome ${CHROME_MAJOR} UA + client-hints (no CDP — Cloudflare-safe)`,
    diag: { url: "", httpStatus: null, lastError: "" },
  };
}

function hasHeaders(s) {
  return REQUIRED_HDRS.every((k) => s.headers[k]);
}
function hasRules(s) {
  // Relay only REQUIRES static_param; it derives start/end from the wire `sign`.
  return !!s.rules.static_param;
}
function hasCookies(s) {
  return s.cookies.auth_id && s.cookies.sess;
}
function isReady(s) {
  return hasHeaders(s) && hasRules(s) && hasCookies(s);
}

function stateSummary(s) {
  return {
    status: s.status,
    error: s.error,
    savedAs: s.savedAs,
    detection: s.detection,
    userId: s.headers["user-id"] || null,
    ofRev: s.headers["x-of-rev"] || null,
    haveHeaders: hasHeaders(s),
    haveRules: hasRules(s),
    haveCookies: hasCookies(s),
    ready: isReady(s),
    diag: s.diag,
  };
}

function pushState() {
  if (flow && flow.statusWindow && !flow.statusWindow.isDestroyed()) {
    flow.statusWindow.webContents.send("capture:state", stateSummary(flow.state));
  }
}

function shellQuote(s) {
  return "'" + String(s).replace(/'/g, "'\\''") + "'";
}

function buildCurl(s, cookieList) {
  const h = s.headers;
  const r = s.rules;
  const url = h.__url || "https://onlyfans.com/api2/v2/users/me";
  const cookieHeader = cookieList
    .filter((c) => c.value && c.name)
    .map((c) => `${c.name}=${c.value}`)
    .join("; ");
  const lines = [
    `curl ${shellQuote(url)} \\`,
    `  -H ${shellQuote("accept: application/json, text/plain, */*")} \\`,
    `  -H ${shellQuote("user-agent: " + (h["user-agent"] || uaProfile().userAgent))} \\`,
    `  -H ${shellQuote("user-id: " + h["user-id"])} \\`,
    `  -H ${shellQuote("x-bc: " + h["x-bc"])} \\`,
    `  -H ${shellQuote("x-of-rev: " + h["x-of-rev"])} \\`,
    `  -H ${shellQuote("sign: " + h["sign"])} \\`,
    `  -H ${shellQuote("time: " + h["time"])} \\`,
    `  -H ${shellQuote("referer: https://onlyfans.com/")} \\`,
    `  -H ${shellQuote("app-token: " + APP_TOKEN)}`,
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

function mergeCapture(s, cap) {
  if (!cap) return;
  if (cap.headers) {
    for (const k of Object.keys(cap.headers)) {
      if (cap.headers[k]) s.headers[k] = cap.headers[k];
    }
  }
  if (cap.url) s.headers.__url = cap.url;
  if (cap.rules) {
    if (cap.rules.static_param) s.rules.static_param = cap.rules.static_param;
    if (cap.rules.start) s.rules.start = cap.rules.start;
    if (cap.rules.end) s.rules.end = cap.rules.end;
  }
}

async function pollOnce() {
  if (!flow || !flow.ofWindow || flow.ofWindow.isDestroyed()) return;
  const s = flow.state;
  if (s.status === "sending" || s.status === "done") return;
  try {
    const cookies = await session.fromPartition(CAPTURE_PARTITION).cookies.get({ url: OF_URL });
    flow.cookieList = cookies;
    const names = new Set(cookies.map((c) => c.name));
    s.cookies.auth_id = names.has("auth_id");
    s.cookies.sess = names.has("sess");
  } catch {
    /* ignore */
  }
  if (s.status !== "error" && isReady(s)) s.status = "ready";
  pushState();
}

async function submitToRelay() {
  const s = flow.state;
  if (!isReady(s)) {
    s.status = "error";
    s.error = "Not ready yet — sign in and open your Messages so OnlyFans signs a request.";
    pushState();
    return;
  }
  const { base, token } = flow.getConfig();
  if (!base) {
    s.status = "error";
    s.error = "No server configured. Set it in Settings first.";
    pushState();
    return;
  }
  try {
    const bu = new URL(base);
    const isLocal = bu.hostname === "localhost" || bu.hostname === "127.0.0.1";
    if (bu.protocol !== "https:" && !isLocal) {
      s.status = "error";
      s.error = "Refusing to send the OnlyFans session over an insecure (http) connection — use an https server URL.";
      pushState();
      return;
    }
  } catch {
    /* validated */
  }

  s.status = "sending";
  s.error = null;
  pushState();

  const curl = buildCurl(s, flow.cookieList || []);
  const url = `${base}/admin/session/bootstrap${token ? `?t=${encodeURIComponent(token)}` : ""}`;
  try {
    const resp = await fetch(url, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ mode: "paste-curl", curl, make_active: true, nickname: null }),
    });
    const text = await resp.text();
    if (!resp.ok) {
      s.status = "error";
      s.error = `Relay ${resp.status}: ${text.slice(0, 300)}`;
      pushState();
      return;
    }
    let parsed = null;
    try {
      parsed = JSON.parse(text);
    } catch {
      /* ok */
    }
    s.status = "done";
    s.savedAs = (parsed && (parsed.user_id || parsed.account_id || parsed.userId)) || s.headers["user-id"] || "saved";
    pushState();
    // Refresh the hosted UI so the new account appears without a manual Cmd+R.
    if (flow && typeof flow.onSaved === "function") {
      try {
        flow.onSaved();
      } catch (_) {
        /* non-fatal */
      }
    }
    closeOfWindow();
    if (flow) {
      clearInterval(flow.pollTimer);
      flow.pollTimer = null;
      // Show the "saved" confirmation briefly, then auto-close the popup
      // (its 'closed' handler tears the flow down).
      const sw = flow.statusWindow;
      setTimeout(() => {
        if (sw && !sw.isDestroyed()) sw.close();
      }, 1400);
    }
  } catch (err) {
    s.status = "error";
    s.error = `Could not reach the relay: ${err && err.message}`;
    pushState();
  }
}

function closeOfWindow() {
  if (flow && flow.ofWindow && !flow.ofWindow.isDestroyed()) {
    flow.ofWindow.destroy();
    flow.ofWindow = null;
  }
}

function teardown() {
  if (!flow) return;
  clearInterval(flow.pollTimer);
  closeOfWindow();
  session
    .fromPartition(CAPTURE_PARTITION)
    .clearStorageData({ storages: ["cookies", "localstorage", "indexdb", "serviceworkers", "cachestorage"] })
    .catch(() => {});
  if (flow.statusWindow && !flow.statusWindow.isDestroyed()) flow.statusWindow.destroy();
  flow = null;
}

function applyClientHints(ses) {
  // Native (no-CDP) UA + client-hint consistency: make the Sec-CH-UA headers say
  // Chrome, matching the UA string, instead of leaking Electron/Chromium.
  const ua = uaProfile();
  ses.webRequest.onBeforeSendHeaders((details, cb) => {
    const h = details.requestHeaders;
    h["User-Agent"] = ua.userAgent;
    h["sec-ch-ua"] = `"Google Chrome";v="${CHROME_MAJOR}", "Chromium";v="${CHROME_MAJOR}", "Not_A Brand";v="24"`;
    h["sec-ch-ua-mobile"] = "?0";
    h["sec-ch-ua-platform"] = ua.platform;
    cb({ requestHeaders: h });
  });
}

async function createOfWindow() {
  const ua = uaProfile();
  const win = new BrowserWindow({
    width: 1120,
    height: 820,
    backgroundColor: "#ffffff",
    title: "Sign in to OnlyFans",
    autoHideMenuBar: true,
    show: false,
    webPreferences: {
      partition: CAPTURE_PARTITION,
      preload: flow.interceptorPreload,
      contextIsolation: false, // preload runs in the page main world (capture needs it; no CDP)
      nodeIntegration: false, //  the PAGE still gets no Node — only the preload does
      sandbox: false, //          preload needs Node (ipcRenderer + monkeypatch)
      devTools: true,
      backgroundThrottling: false,
    },
  });
  applyClientHints(session.fromPartition(CAPTURE_PARTITION));
  win.webContents.setUserAgent(ua.userAgent);
  flow.ofWindow = win;

  const wc = win.webContents;
  win.on("closed", () => {
    if (flow) flow.ofWindow = null;
  });
  win.once("ready-to-show", () => win.show());
  wc.on("did-navigate", (_e, navUrl, httpResponseCode) => {
    if (flow) {
      flow.state.diag.url = navUrl;
      flow.state.diag.httpStatus = httpResponseCode;
      pushState();
    }
  });
  wc.on("console-message", (_e, level, message) => {
    if (flow && level >= 2 && message) {
      flow.state.diag.lastError = String(message).slice(0, 200);
      pushState();
    }
  });
  wc.on("did-finish-load", () => console.log("[capture] OF loaded:", wc.getURL()));
  wc.on("did-fail-load", (_e, code, desc, url, isMain) => {
    if (!isMain || code === -3) return;
    console.warn("[capture] OF did-fail-load", code, desc, url);
    if (flow) {
      flow.state.status = "error";
      flow.state.error = `OnlyFans didn't load (${desc || code}). Click Reopen Login to retry.`;
      pushState();
    }
    if (!win.isDestroyed() && !win.isVisible()) win.show();
  });
  wc.on("render-process-gone", (_e, d) => console.warn("[capture] OF render gone:", d && d.reason));

  win.loadURL(OF_URL).catch((e) => {
    if (!e || e.errno !== -3) console.warn("[capture] OF loadURL:", e && e.message);
  });
  setTimeout(() => {
    if (win && !win.isDestroyed() && !win.isVisible()) win.show();
  }, 6000);
}

function createStatusWindow(preloadPath, htmlPath) {
  const win = new BrowserWindow({
    width: 460,
    height: 620,
    resizable: false,
    backgroundColor: "#0b0b0f",
    title: "Add OnlyFans Account",
    alwaysOnTop: true,
    webPreferences: {
      preload: preloadPath,
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      devTools: false,
    },
  });
  win.setMenuBarVisibility(false);
  flow.statusWindow = win;
  win.on("closed", () => teardown());
  win.loadFile(htmlPath).then(() => pushState());
}

function isTrustedLocalSender(event) {
  const url = event.senderFrame ? event.senderFrame.url : "";
  return typeof url === "string" && url.startsWith("file://");
}

function registerCaptureIpc(paths) {
  registerCaptureIpc._paths = paths;

  // Capture data pushed from the OF window's interceptor preload. Accept ONLY
  // from the live capture window's web contents.
  ipcMain.on("of-capture:data", (event, payload) => {
    if (!flow || !flow.ofWindow || flow.ofWindow.isDestroyed()) return;
    if (event.sender.id !== flow.ofWindow.webContents.id) return;
    mergeCapture(flow.state, payload);
    const s = flow.state;
    if (s.status !== "sending" && s.status !== "done" && s.status !== "error" && isReady(s)) s.status = "ready";
    pushState();
  });

  // Status-window (local file://) controls.
  ipcMain.handle("capture:get-state", (event) => {
    if (!isTrustedLocalSender(event)) return null;
    return flow ? stateSummary(flow.state) : null;
  });
  ipcMain.handle("capture:finish", async (event) => {
    if (!isTrustedLocalSender(event) || !flow) return;
    await submitToRelay();
  });
  ipcMain.handle("capture:reopen", (event) => {
    if (!isTrustedLocalSender(event) || !flow) return;
    if (!flow.ofWindow || flow.ofWindow.isDestroyed()) {
      createOfWindow().catch((e) => {
        flow.state.status = "error";
        flow.state.error = `Couldn't open OnlyFans: ${e && e.message}`;
        pushState();
      });
    } else {
      flow.ofWindow.show();
      flow.ofWindow.focus();
    }
  });
  ipcMain.handle("capture:cancel", (event) => {
    if (!isTrustedLocalSender(event)) return;
    teardown();
  });
}

async function openOfCaptureFlow(getConfig, onSaved) {
  if (flow) {
    if (flow.statusWindow && !flow.statusWindow.isDestroyed()) {
      flow.statusWindow.show();
      flow.statusWindow.focus();
    }
    return;
  }
  const paths = registerCaptureIpc._paths || {};
  flow = {
    getConfig,
    onSaved,
    state: freshState(),
    ofWindow: null,
    statusWindow: null,
    cookieList: [],
    pollTimer: null,
    interceptorPreload: paths.interceptorPreload,
  };

  createStatusWindow(paths.statusPreload, paths.statusHtml);
  try {
    await createOfWindow();
  } catch (e) {
    flow.state.status = "error";
    flow.state.error = `Couldn't open OnlyFans: ${e && e.message}`;
    pushState();
    return;
  }
  flow.pollTimer = setInterval(() => {
    pollOnce().catch(() => {});
  }, POLL_MS);
}

module.exports = { registerCaptureIpc, openOfCaptureFlow };
