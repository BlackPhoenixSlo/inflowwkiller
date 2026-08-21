"use strict";

// ───────────────────────────────────────────────────────────────────────────
// Fastt desktop shell — main process.
//
// A thin, deliberately-boring Electron window onto the HOSTED Fastt web UI
// (https://fastt.lol by default). It imports NOTHING from app/ or service/ and
// ships no product code — it only knows a URL and a relay share token. Deleting
// this folder leaves the web app untouched.
//
// Design comes from the fusion review (plan/DESKTOP_APP_PLAN.md → "FUSION
// HARDENING") and was hardened by a second adversarial multi-agent review:
//   • Remote-load only (same origin as fastt.lol ⇒ cookies/SSE/rewrites work
//     with zero relay changes). NEVER bundle the UI (that's a relay redesign).
//   • Deny-by-default navigation on will-navigate / will-redirect /
//     will-frame-navigate; external links → OS browser; same-origin popouts
//     (group view) allowed as child windows on the shared partition.
//   • Least privilege: nodeIntegration off, contextIsolation on, sandbox on, no
//     Node/IPC bridge exposed to the remote page (only a static version object).
//     The local settings/error pages get their own trusted preload.
//   • The trust origin is read LIVE from config inside each handler, so changing
//     the server in Settings can't leave a window trusting the old origin.
//   • Seed the relay share token into the partition (cookie + localStorage via
//     the frontend's own ?t= mechanism) ONCE, only on a 2xx load, keeping the
//     token out of URLs/logs/history everywhere else.
//   • A real in-app "Log Out / Clear Session" destroys the on-disk credential —
//     the fusion revocation concern.
//
// What this shell deliberately does NOT do: capture OnlyFans sessions. Per the
// fusion review that stays in the Chrome extension (moving it into Electron is a
// detection regression against the creator's account). The app consumes the
// session the relay already holds; it never creates one.
// ───────────────────────────────────────────────────────────────────────────

const { app, BrowserWindow, Menu, shell, ipcMain, session, screen, nativeImage } = require("electron");
const path = require("path");
const crypto = require("crypto");
const { pathToFileURL } = require("url");
const { Store } = require("./lib/store");
const { parseUnread, applyBadge } = require("./lib/badge");
const { describeSigning, summarize } = require("./lib/signing");
const { registerCaptureIpc, openOfCaptureFlow } = require("./capture/of-capture");

const IS_DEV = process.argv.includes("--dev");
const PARTITION = "persist:fastt";
const DEFAULT_URL = "https://fastt.lol";
const SHELL_VERSION = app.getVersion();
const EXTERNAL_SCHEMES = new Set(["http:", "https:", "mailto:", "tel:", "sms:"]);

app.setName("Fastt");

// ── Single-instance: a second launch focuses whatever window we have ────────
if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on("second-instance", () => {
    const w = liveWindow();
    if (w) {
      if (w.isMinimized()) w.restore();
      w.show();
      w.focus();
    } else if (store) {
      isConfigured() ? loadApp() : openSettingsWindow();
    }
  });
}

let store; //          config store (created after app ready — needs userData path)
let mainWindow = null;
let settingsWindow = null;
let errorWindow = null;
let boundsSaveTimer = null;

function liveWindow() {
  // Prefer a currently-VISIBLE window (the error/settings surface when one is up)
  // over a backgrounded main window, so a second launch focuses what the user
  // is actually looking at.
  const wins = [errorWindow, settingsWindow, mainWindow].filter((w) => w && !w.isDestroyed());
  return wins.find((w) => w.isVisible()) || wins[0] || null;
}

// ── URL / token helpers ─────────────────────────────────────────────────────

function normalizeBase(rawUrl) {
  // Returns the http(s) ORIGIN only (no path) or null. We only ever load the app
  // root and append /inbox ourselves, so keeping a user-supplied path would give
  // /inbox/inbox and a sub-path-scoped cookie.
  try {
    const u = new URL(String(rawUrl).trim());
    if (u.protocol !== "http:" && u.protocol !== "https:") return null;
    return u.origin;
  } catch {
    return null;
  }
}

function appOriginOf(base) {
  try {
    return new URL(base).origin;
  } catch {
    return null;
  }
}

function regDomain(host) {
  // Approx registrable domain (last two labels). Correct for our 2-label domains
  // (fastt.lol, onlyfans.com); a multi-part TLD like co.uk would over-narrow, but
  // we only use it to keep a window navigating within the SITE it's already on.
  if (!host) return "";
  const parts = String(host).split(".");
  return parts.slice(-2).join(".");
}

function seedMarker(base, token) {
  return crypto.createHash("sha256").update(`${base} ${token}`).digest("hex").slice(0, 24);
}

function currentConfig() {
  const base = normalizeBase(store.get("serverUrl", DEFAULT_URL)) || normalizeBase(DEFAULT_URL);
  const token = String(store.get("shareToken", "") || "");
  return { base, token };
}

function isConfigured() {
  // "configured" is tracked explicitly so a deliberately tokenless (share-gate
  // disabled) install still auto-loads /inbox instead of re-prompting forever.
  const { base, token } = currentConfig();
  return !!store.get("configured", false) || !!(base && token);
}

// Proactively (re)write the relay share-token cookie into the partition so even
// the top-level HTML request carries it and it never lapses mid-use.
async function refreshShareCookie(base, token) {
  if (!token) return;
  const ses = session.fromPartition(PARTITION);
  try {
    await ses.cookies.set({
      url: base,
      name: "share_token",
      value: token,
      path: "/",
      httpOnly: true,
      secure: base.startsWith("https:"),
      sameSite: "lax",
      expirationDate: Math.floor(Date.now() / 1000) + 7 * 24 * 3600,
    });
  } catch (err) {
    console.error("[seed] cookie set failed:", err && err.message);
  }
}

// Decide the URL to load. On the first load for a given (server, token) we append
// ?t= so the frontend's resolveShareToken() persists it to localStorage (needed
// for SSE, which sends no cookies). After that we load the bare /inbox.
async function computeLoadUrl() {
  const { base, token } = currentConfig();
  if (!base) return null;
  const inbox = `${base}/inbox`;
  if (!token) return inbox; // gate-off / not yet tokened → let the UI's own login show.

  await refreshShareCookie(base, token);

  const marker = seedMarker(base, token);
  if (store.get("seededFor") !== marker) {
    return `${inbox}?t=${encodeURIComponent(token)}`;
  }
  return inbox;
}

function markSeeded() {
  const { base, token } = currentConfig();
  if (base && token) store.set("seededFor", seedMarker(base, token));
}

// ── Security: applied to EVERY web contents (main + popouts + local pages) ───

function guardNavigation(event, url) {
  // Deny-by-default. Allowed in-window: (1) the fastt app origin, and (2) staying
  // within the SITE the window is already on. The main window is always on
  // fastt.lol, so (2) does not widen its policy — it exists so the OnlyFans
  // capture window can navigate onlyfans.com (login redirects) in-window instead
  // of being ejected. Foreign http(s) + mailto/tel/sms → OS browser; every other
  // scheme (file:, custom) is blocked outright.
  const appOrigin = appOriginOf(currentConfig().base);
  let u;
  try {
    u = new URL(url);
  } catch {
    event.preventDefault();
    return;
  }
  if (appOrigin && u.origin === appOrigin) return;
  let curHost = null;
  try {
    curHost = new URL(event.sender.getURL()).hostname;
  } catch {
    /* no current url */
  }
  if (curHost && u.hostname && regDomain(u.hostname) === regDomain(curHost)) return;
  event.preventDefault();
  if (EXTERNAL_SCHEMES.has(u.protocol)) shell.openExternal(url);
}

function hardenContents(contents) {
  // Gate TOP-LEVEL navigation only: will-navigate + its server-side 30x redirects.
  // Intentionally NOT will-frame-navigate — it fires for every sub-frame, and
  // ejecting cross-origin IFRAMES (recaptcha, payment, embeds — present on the OF
  // login page) to the OS browser would break them and spam external opens.
  // Sub-frames load in-frame like a normal browser; the browser sandboxes them.
  contents.on("will-navigate", guardNavigation);
  contents.on("will-redirect", guardNavigation);

  // window.open / target=_blank: same-origin → in-app child window on the shared
  // partition (keeps group-view popouts + BroadcastChannel working); anything
  // else → OS browser, never a new in-app window.
  contents.setWindowOpenHandler(({ url }) => {
    const appOrigin = appOriginOf(currentConfig().base);
    let u;
    try {
      u = new URL(url);
    } catch {
      return { action: "deny" };
    }
    if (appOrigin && u.origin === appOrigin) {
      return {
        action: "allow",
        overrideBrowserWindowOptions: {
          backgroundColor: "#0b0b0f",
          webPreferences: {
            preload: path.join(__dirname, "preload.js"),
            contextIsolation: true,
            nodeIntegration: false,
            sandbox: true,
            backgroundThrottling: false,
            devTools: IS_DEV,
            partition: PARTITION,
          },
        },
      };
    }
    if (EXTERNAL_SCHEMES.has(u.protocol)) shell.openExternal(url);
    return { action: "deny" };
  });

  contents.on("will-attach-webview", (event) => event.preventDefault());

  // Right-click menu: spellcheck suggestions + cut/copy/paste. A typing-heavy
  // chat tool needs these; without a handler the OS/Chromium menu never shows.
  contents.on("context-menu", (_e, params) => {
    const template = [];
    for (const s of params.dictionarySuggestions) {
      template.push({ label: s, click: () => contents.replaceMisspelling(s) });
    }
    if (params.dictionarySuggestions.length) template.push({ type: "separator" });
    if (params.misspelledWord) {
      template.push(
        {
          label: "Add to Dictionary",
          click: () => contents.session.addWordToSpellCheckerDictionary(params.misspelledWord),
        },
        { type: "separator" },
      );
    }
    const ef = params.editFlags;
    template.push(
      { role: "cut", enabled: ef.canCut },
      { role: "copy", enabled: ef.canCopy },
      { role: "paste", enabled: ef.canPaste },
      { role: "selectAll" },
    );
    Menu.buildFromTemplate(template).popup();
  });
}

function lockDownSession() {
  const ses = session.fromPartition(PARTITION);
  const allow = new Set(["notifications"]); // the UI already fires web Notifications.

  ses.setPermissionRequestHandler((wc, permission, cb) => {
    if (!allow.has(permission)) return cb(false);
    const appOrigin = appOriginOf(currentConfig().base);
    let reqOrigin = null;
    try {
      reqOrigin = new URL(wc.getURL()).origin;
    } catch {
      /* no url yet */
    }
    cb(!appOrigin || reqOrigin === appOrigin);
  });
  ses.setPermissionCheckHandler((_wc, permission, requestingOrigin) => {
    if (!allow.has(permission)) return false;
    const appOrigin = appOriginOf(currentConfig().base);
    return !appOrigin || requestingOrigin === appOrigin;
  });
}

// ── Window bounds ─────────────────────────────────────────────────────────────

function sanitizeBounds(saved) {
  const def = { width: 1440, height: 900 };
  if (!saved || typeof saved !== "object") return def;
  const width = Number.isFinite(saved.width) ? saved.width : def.width;
  const height = Number.isFinite(saved.height) ? saved.height : def.height;
  if (!Number.isFinite(saved.x) || !Number.isFinite(saved.y)) return { width, height };
  // Drop x/y (let Electron center) if the saved rect no longer overlaps any
  // display — otherwise a monitor that's been unplugged strands the window
  // off-screen and invisible.
  const overlaps = screen.getAllDisplays().some((d) => {
    const w = d.workArea;
    return saved.x < w.x + w.width && saved.x + width > w.x && saved.y < w.y + w.height && saved.y + height > w.y;
  });
  return overlaps ? { x: saved.x, y: saved.y, width, height } : { width, height };
}

function persistBounds() {
  if (!mainWindow || mainWindow.isDestroyed()) return;
  if (mainWindow.isFullScreen() || mainWindow.isMinimized()) return;
  store.set("bounds", mainWindow.getBounds());
}

function scheduleBoundsSave() {
  clearTimeout(boundsSaveTimer);
  boundsSaveTimer = setTimeout(persistBounds, 400);
}

// ── Windows ─────────────────────────────────────────────────────────────────

function createMainWindow() {
  const bounds = sanitizeBounds(store.get("bounds", null));
  mainWindow = new BrowserWindow({
    ...bounds,
    minWidth: 900,
    minHeight: 600,
    backgroundColor: "#0b0b0f",
    show: false,
    titleBarStyle: process.platform === "darwin" ? "hiddenInset" : "default",
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      webviewTag: false,
      spellcheck: true,
      backgroundThrottling: false, // keep SSE/reconnect alive while minimized/occluded
      devTools: IS_DEV,
      partition: PARTITION,
      additionalArguments: [`--fastt-shell-version=${SHELL_VERSION}`],
    },
  });

  mainWindow.once("ready-to-show", () => mainWindow.show());
  mainWindow.on("resize", scheduleBoundsSave);
  mainWindow.on("move", scheduleBoundsSave);
  mainWindow.on("close", persistBounds);
  mainWindow.on("closed", () => {
    mainWindow = null;
  });

  const wc = mainWindow.webContents;

  // Unread badge from the page title (best-effort; see lib/badge.js).
  wc.on("page-title-updated", (_e, title) => {
    applyBadge({ app, win: mainWindow, nativeImage, count: parseUnread(title) });
  });

  // Mark the (server, token) pair seeded ONLY on a genuine 2xx main-frame load —
  // never on an HTTP error/gate page, which would suppress the ?t= reseed.
  wc.on("did-navigate", (_e, navUrl, httpResponseCode) => {
    const { base } = currentConfig();
    if (base && navUrl.startsWith(base) && httpResponseCode >= 200 && httpResponseCode < 300) {
      markSeeded();
      closeErrorWindow();
    }
  });

  // Connection failure → local retry window (token stripped from the URL).
  wc.on("did-fail-load", (_e, errorCode, errorDesc, validatedURL, isMainFrame) => {
    if (!isMainFrame) return;
    if (errorCode === -3) return; // ERR_ABORTED — superseded/redirected load.
    let cleanUrl = "";
    try {
      const u = new URL(validatedURL);
      u.searchParams.delete("t");
      cleanUrl = `${u.origin}${u.pathname}`;
    } catch {
      /* leave blank */
    }
    showErrorWindow(errorDesc, cleanUrl);
  });

  // Renderer crash / hang → recover instead of a permanent dead blank window.
  wc.on("render-process-gone", (_e, details) => {
    if (details && details.reason !== "clean-exit") {
      showErrorWindow("The app stopped responding. Retry to reload.", "");
    }
  });
  wc.on("unresponsive", () => {
    try {
      wc.reloadIgnoringCache();
    } catch {
      /* ignore */
    }
  });

  return mainWindow;
}

async function loadApp() {
  const target = await computeLoadUrl();
  if (!target) {
    openSettingsWindow();
    return;
  }
  if (!mainWindow || mainWindow.isDestroyed()) createMainWindow();
  try {
    await mainWindow.loadURL(target);
  } catch (err) {
    // did-fail-load already renders the retry window; swallow the aborted/network
    // rejection so it doesn't surface as an unhandled promise rejection.
    if (IS_DEV) console.warn("[loadApp] loadURL rejected:", err && err.message);
  }
}

function openSettingsWindow() {
  if (settingsWindow && !settingsWindow.isDestroyed()) {
    settingsWindow.show();
    settingsWindow.focus();
    return;
  }
  // Only parent/modal to the main window when it is actually VISIBLE. A macOS
  // document-modal sheet on a hidden/absent parent may never present, stranding
  // the user with no way forward from the error/logout state.
  const anchor = mainWindow && !mainWindow.isDestroyed() && mainWindow.isVisible() ? mainWindow : undefined;
  settingsWindow = new BrowserWindow({
    width: 520,
    height: 460,
    resizable: false,
    minimizable: false,
    maximizable: false,
    backgroundColor: "#0b0b0f",
    title: "Fastt — Settings",
    parent: anchor,
    modal: !!anchor,
    show: false,
    webPreferences: {
      preload: path.join(__dirname, "config-preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      devTools: IS_DEV,
    },
  });
  settingsWindow.setMenuBarVisibility(false);
  settingsWindow.once("ready-to-show", () => settingsWindow.show());
  settingsWindow.on("closed", () => {
    settingsWindow = null;
  });
  settingsWindow.loadFile(path.join(__dirname, "renderer", "config.html"));
}

function showErrorWindow(desc, cleanUrl) {
  const search = new URLSearchParams({ desc: desc || "", url: cleanUrl || "" }).toString();
  const file = path.join(__dirname, "renderer", "error.html");
  // Do NOT hide the main window — the error window layers on top as a focused
  // window. Hiding it and relying on ready-to-show (which is .once()) to re-show
  // after recovery would strand the user on an invisible window.
  if (errorWindow && !errorWindow.isDestroyed()) {
    errorWindow.loadFile(file, { search });
    errorWindow.show();
    errorWindow.focus();
    return;
  }
  errorWindow = new BrowserWindow({
    width: 520,
    height: 420,
    resizable: false,
    minimizable: false,
    maximizable: false,
    backgroundColor: "#0b0b0f",
    title: "Fastt",
    show: false,
    webPreferences: {
      preload: path.join(__dirname, "config-preload.js"), // trusted local bridge
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      devTools: IS_DEV,
    },
  });
  errorWindow.setMenuBarVisibility(false);
  errorWindow.once("ready-to-show", () => errorWindow.show());
  errorWindow.on("closed", () => {
    errorWindow = null;
  });
  errorWindow.loadFile(file, { search });
}

function closeErrorWindow() {
  if (errorWindow && !errorWindow.isDestroyed()) errorWindow.close();
}

// After a native OnlyFans capture saves an account, refresh the hosted UI so the
// new account shows up in the Accounts list without a manual Cmd+R. A plain
// reload isn't enough — the UI hydrates a PERSISTED React-Query cache
// (localStorage `chatterly-rq-cache`) and shows the stale account list. Clearing
// that cache key first forces a fresh fetch on reload.
function reloadMainAfterCapture() {
  if (!mainWindow || mainWindow.isDestroyed()) return;
  mainWindow.webContents
    .executeJavaScript("try{localStorage.removeItem('chatterly-rq-cache');}catch(e){}; location.reload();")
    .catch(() => {
      try {
        mainWindow.webContents.reload();
      } catch {
        /* ignore */
      }
    });
}

async function clearSession() {
  const ses = session.fromPartition(PARTITION);
  try {
    await ses.clearStorageData({
      storages: ["cookies", "localstorage", "indexdb", "serviceworkers", "cachestorage"],
    });
  } catch (err) {
    console.error("[logout] clearStorageData failed:", err && err.message);
  }
  store.delete("shareToken");
  store.set("seededFor", "");
  store.set("configured", false);
  // Tear the authenticated window down entirely (a fresh one is created on the
  // next load). destroy() is synchronous, so openSettingsWindow() below sees
  // no live main window and opens a normal, non-modal settings window.
  if (mainWindow && !mainWindow.isDestroyed()) mainWindow.destroy();
  closeErrorWindow();
  openSettingsWindow();
}

// ── IPC (only from our LOCAL trusted pages: config.html / error.html) ────────

function isTrustedLocalSender(event) {
  const url = event.senderFrame ? event.senderFrame.url : "";
  return typeof url === "string" && url.startsWith("file://");
}

ipcMain.handle("config:get", (event) => {
  if (!isTrustedLocalSender(event)) return null;
  const { base, token } = currentConfig();
  return { serverUrl: base || DEFAULT_URL, hasToken: !!token, shellVersion: SHELL_VERSION };
});

ipcMain.handle("config:save", async (event, incoming) => {
  if (!isTrustedLocalSender(event)) return { ok: false, error: "untrusted sender" };
  const base = normalizeBase(incoming && incoming.serverUrl);
  if (!base) return { ok: false, error: "Enter a valid http(s) server URL." };

  const patch = { serverUrl: base, configured: true };
  const token = incoming && typeof incoming.shareToken === "string" ? incoming.shareToken.trim() : "";
  if (token) {
    patch.shareToken = token;
    patch.seededFor = ""; // force a fresh ?t= seed for the new token
  }
  store.setMany(patch);

  if (settingsWindow && !settingsWindow.isDestroyed()) settingsWindow.close();
  await loadApp();
  return { ok: true };
});

ipcMain.handle("app:retry", async (event) => {
  if (!isTrustedLocalSender(event)) return;
  closeErrorWindow();
  await loadApp();
});

ipcMain.handle("app:open-settings", (event) => {
  if (!isTrustedLocalSender(event)) return;
  openSettingsWindow();
});

ipcMain.handle("app:quit", (event) => {
  if (!isTrustedLocalSender(event)) return;
  app.quit();
});

// Triggered by the native "Connect OnlyFans" button that preload.js injects into
// the hosted page. Only the app's OWN remote origin may fire it, and all it does
// is open a login window — no data crosses.
ipcMain.handle("shell:add-of-account", (event) => {
  const appOrigin = appOriginOf(currentConfig().base);
  let senderOrigin = null;
  try {
    senderOrigin = new URL(event.senderFrame.url).origin;
  } catch {
    /* unknown */
  }
  if (appOrigin && senderOrigin !== appOrigin) return;
  openOfCaptureFlow(() => currentConfig(), reloadMainAfterCapture);
});

// ── Menu ─────────────────────────────────────────────────────────────────────

function buildMenu() {
  const isMac = process.platform === "darwin";
  const reseed = {
    label: "Re-seed Login / Fix Access",
    click: async () => {
      store.set("seededFor", "");
      await loadApp();
    },
  };
  const settings = { label: "Settings…", accelerator: "CmdOrCtrl+,", click: () => openSettingsWindow() };
  const logout = { label: "Log Out / Clear Session", click: () => clearSession() };
  const addOfAccount = {
    label: "Add OnlyFans Account…",
    click: () => openOfCaptureFlow(() => currentConfig(), reloadMainAfterCapture),
  };

  const template = [
    ...(isMac
      ? [
          {
            label: app.name,
            submenu: [
              { role: "about" },
              { type: "separator" },
              settings,
              { type: "separator" },
              { role: "services" },
              { type: "separator" },
              { role: "hide" },
              { role: "hideOthers" },
              { role: "unhide" },
              { type: "separator" },
              { role: "quit" },
            ],
          },
        ]
      : []),
    {
      label: "File",
      submenu: isMac ? [{ role: "close" }] : [settings, { type: "separator" }, { role: "quit" }],
    },
    {
      label: "Account",
      submenu: [addOfAccount, reseed, { type: "separator" }, logout],
    },
    {
      label: "Edit",
      submenu: [
        { role: "undo" },
        { role: "redo" },
        { type: "separator" },
        { role: "cut" },
        { role: "copy" },
        { role: "paste" },
        { role: "selectAll" },
      ],
    },
    {
      label: "View",
      submenu: [
        {
          label: "Reload",
          accelerator: "CmdOrCtrl+R",
          click: () => mainWindow && !mainWindow.isDestroyed() && mainWindow.webContents.reload(),
        },
        { label: "Reconnect", click: () => loadApp() },
        { type: "separator" },
        { role: "resetZoom" },
        { role: "zoomIn" },
        { role: "zoomOut" },
        { type: "separator" },
        { role: "togglefullscreen" },
        ...(IS_DEV ? [{ type: "separator" }, { role: "toggleDevTools" }] : []),
      ],
    },
    {
      label: "Window",
      submenu: [{ role: "minimize" }, { role: "zoom" }, ...(isMac ? [{ role: "front" }] : [{ role: "close" }])],
    },
    {
      role: "help",
      submenu: [
        {
          label: "Open Fastt in Browser",
          click: () => {
            const { base } = currentConfig();
            if (base) shell.openExternal(base);
          },
        },
      ],
    },
  ];

  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

// ── Lifecycle ─────────────────────────────────────────────────────────────────

app.whenReady().then(async () => {
  store = new Store(app.getPath("userData"));

  // Report the shipped bundle's signing state once at boot. This is a FACT
  // line, not an alarm — but it has to exist, because on Electron 42+ macOS
  // notifications only deliver for a signed app and their absence is otherwise
  // completely invisible. Non-blocking and never throws; see lib/signing.js.
  describeSigning({ isPackaged: app.isPackaged })
    .then((d) => console.log("[fastt-shell]", summarize(d)))
    .catch(() => {});

  app.setAboutPanelOptions({
    applicationName: "Fastt",
    applicationVersion: SHELL_VERSION,
    copyright: "© 2026 Fastt",
  });

  // Harden every web contents that ever gets created — main, popouts, and the
  // local settings/error pages.
  app.on("web-contents-created", (_e, contents) => hardenContents(contents));

  lockDownSession();
  registerCaptureIpc({
    statusPreload: path.join(__dirname, "capture", "status-preload.js"),
    statusHtml: path.join(__dirname, "renderer", "capture.html"),
    interceptorPreload: path.join(__dirname, "capture", "of-interceptor-preload.js"),
  });
  buildMenu();

  if (!isConfigured()) {
    openSettingsWindow();
  } else {
    await loadApp();
  }

  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      isConfigured() ? loadApp() : openSettingsWindow();
    } else if (mainWindow && !mainWindow.isDestroyed()) {
      mainWindow.show();
    }
  });
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});
