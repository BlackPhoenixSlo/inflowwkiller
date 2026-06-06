// Service worker — coordinates capture flow + multi-account switcher.
//
// Responsibilities:
//   1. Wake/log on capture messages from bridge.js
//   2. Reset lastCapture when the user explicitly starts a new sign-in
//   3. Expose getCookies() to the popup (popup can't read onlyfans cookies
//      directly, only the SW can via chrome.cookies + host permission)
//   4. Multi-account: snapshot current OF session (cookies + capture) into
//      chrome.storage.local.accounts keyed by user-id, clear/restore cookies
//      on demand so one browser can cycle multiple OF logins without ever
//      hitting /logout (server-side sessions stay alive for reuse).

const OF_COOKIE_DOMAIN = "onlyfans.com";
const OF_BASE_URL = "https://onlyfans.com/";

// ── Cookie helpers ────────────────────────────────────────────────

function cookieUrl(c) {
  const proto = c.secure ? "https" : "http";
  const host = c.domain.startsWith(".") ? c.domain.slice(1) : c.domain;
  return `${proto}://${host}${c.path}`;
}

async function getOfCookies() {
  return await chrome.cookies.getAll({ domain: OF_COOKIE_DOMAIN });
}

async function clearOfCookies() {
  const cookies = await getOfCookies();
  await Promise.all(
    cookies.map((c) =>
      chrome.cookies
        .remove({ url: cookieUrl(c), name: c.name, storeId: c.storeId })
        .catch(() => null)
    )
  );
}

async function restoreCookies(cookies) {
  for (const c of cookies || []) {
    try {
      const set = {
        url: cookieUrl(c),
        name: c.name,
        value: c.value,
        path: c.path,
        secure: c.secure,
        httpOnly: c.httpOnly,
        sameSite: c.sameSite || "unspecified",
        storeId: c.storeId,
      };
      // hostOnly cookies must not carry a `domain` field on set()
      if (!c.hostOnly && c.domain) set.domain = c.domain;
      if (!c.session && c.expirationDate) set.expirationDate = c.expirationDate;
      await chrome.cookies.set(set);
    } catch (e) {
      console.warn("[Chatterly] cookie restore failed", c.name, e);
    }
  }
}

// ── Account identity ──────────────────────────────────────────────

async function getCurrentUserId() {
  const { lastCapture } = await chrome.storage.local.get(["lastCapture"]);
  const fromCapture = lastCapture && lastCapture.headers && lastCapture.headers["user-id"];
  if (fromCapture) return String(fromCapture);
  try {
    const c = await chrome.cookies.get({ url: OF_BASE_URL, name: "auth_id" });
    if (c && c.value) return String(c.value);
  } catch (_) {}
  return null;
}

async function snapshotCurrentAccount() {
  const userId = await getCurrentUserId();
  if (!userId) return null;
  const cookies = await getOfCookies();
  if (!cookies.length) return null;
  const { lastCapture, accounts = {} } = await chrome.storage.local.get([
    "lastCapture",
    "accounts",
  ]);
  const prev = accounts[userId] || {};
  accounts[userId] = {
    userId,
    label: prev.label || null,
    cookies,
    capture: lastCapture || prev.capture || null,
    savedAt: new Date().toISOString(),
  };
  await chrome.storage.local.set({ accounts });
  return userId;
}

// ── Tab helpers ───────────────────────────────────────────────────

async function focusOrOpenOfTab() {
  const tabs = await chrome.tabs.query({ url: "https://onlyfans.com/*" });
  if (tabs.length > 0) {
    await chrome.tabs.update(tabs[0].id, { active: true });
    await chrome.windows.update(tabs[0].windowId, { focused: true });
    return tabs[0];
  }
  return await chrome.tabs.create({ url: OF_BASE_URL });
}

async function reloadOfTabs() {
  const tabs = await chrome.tabs.query({ url: "https://onlyfans.com/*" });
  if (!tabs.length) return await chrome.tabs.create({ url: OF_BASE_URL });
  await Promise.all(tabs.map((t) => chrome.tabs.reload(t.id).catch(() => null)));
  await chrome.tabs.update(tabs[0].id, { active: true });
  await chrome.windows.update(tabs[0].windowId, { focused: true });
  return tabs[0];
}

// ── Message router ────────────────────────────────────────────────

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (!msg) return;

  if (msg.type === "capture") {
    console.log("[Chatterly] capture received", {
      user_id: msg.payload?.headers?.["user-id"],
      x_of_rev: msg.payload?.headers?.["x-of-rev"],
      at: msg.payload?.capturedAt,
    });
    return;
  }

  if (msg.type === "getCookies") {
    getOfCookies()
      .then((cookies) => sendResponse({ cookies }))
      .catch((err) => sendResponse({ cookies: [], error: String(err?.message || err) }));
    return true;
  }

  if (msg.type === "resetCapture") {
    chrome.storage.local.remove(["lastCapture"]).then(() => sendResponse({ ok: true }));
    return true;
  }

  if (msg.type === "listAccounts") {
    chrome.storage.local.get(["accounts"]).then(({ accounts = {} }) => {
      getCurrentUserId().then((activeId) => {
        sendResponse({ accounts, activeId });
      });
    });
    return true;
  }

  // Snapshot current → wipe cookies + lastCapture → reload OF for fresh login.
  if (msg.type === "saveAndClear") {
    (async () => {
      try {
        const savedAs = await snapshotCurrentAccount();
        await clearOfCookies();
        await chrome.storage.local.remove(["lastCapture"]);
        await reloadOfTabs();
        sendResponse({ ok: true, savedAs });
      } catch (e) {
        sendResponse({ ok: false, error: String(e?.message || e) });
      }
    })();
    return true;
  }

  // Snapshot current (if different) → wipe cookies → restore target →
  // reinstate that account's lastCapture so curl is buildable immediately.
  if (msg.type === "switchAccount") {
    (async () => {
      try {
        const targetId = String(msg.userId || "");
        if (!targetId) throw new Error("missing userId");
        const { accounts = {} } = await chrome.storage.local.get(["accounts"]);
        const target = accounts[targetId];
        if (!target) throw new Error("account not found: " + targetId);

        const activeId = await getCurrentUserId();
        if (activeId && activeId !== targetId) await snapshotCurrentAccount();

        await clearOfCookies();
        await restoreCookies(target.cookies);
        if (target.capture) {
          await chrome.storage.local.set({ lastCapture: target.capture });
        } else {
          await chrome.storage.local.remove(["lastCapture"]);
        }
        await reloadOfTabs();
        sendResponse({ ok: true, switchedTo: targetId });
      } catch (e) {
        sendResponse({ ok: false, error: String(e?.message || e) });
      }
    })();
    return true;
  }

  if (msg.type === "deleteAccount") {
    (async () => {
      try {
        const userId = String(msg.userId || "");
        if (!userId) throw new Error("missing userId");
        const { accounts = {} } = await chrome.storage.local.get(["accounts"]);
        delete accounts[userId];
        await chrome.storage.local.set({ accounts });
        sendResponse({ ok: true });
      } catch (e) {
        sendResponse({ ok: false, error: String(e?.message || e) });
      }
    })();
    return true;
  }
});

// Auto-create / refresh an account record whenever a new user-id shows up
// in lastCapture. Keeps the saved-logins list populated without manual
// snapshots — the explicit Save/Switch flows still re-snapshot cookies
// to capture session rotation.
chrome.storage.onChanged.addListener((changes, area) => {
  if (area !== "local" || !changes.lastCapture) return;
  const next = changes.lastCapture.newValue;
  if (!next || !next.headers || !next.headers["user-id"]) return;
  const userId = String(next.headers["user-id"]);
  chrome.storage.local.get(["accounts"]).then(async ({ accounts = {} }) => {
    const prev = accounts[userId];
    // Only auto-snapshot if first sighting OR if it's been >30s since last save
    // (avoids re-snapshotting on every sign() tick within an active session).
    const stale = !prev || Date.now() - new Date(prev.savedAt).getTime() > 30_000;
    if (!stale) return;
    await snapshotCurrentAccount();
  });
});

chrome.runtime.onInstalled.addListener(() => {
  console.log("[Chatterly] login capture extension installed");
});
