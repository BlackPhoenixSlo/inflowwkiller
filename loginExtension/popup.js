// Popup UI logic.
//
// Flow:
//   • Render status from chrome.storage.local.lastCapture
//   • "Log in" → open onlyfans.com in a new/focused tab
//   • "Copy curl" → assemble curl from lastCapture + chrome.cookies and
//     write it to the clipboard
//   • "Reset" → clear lastCapture so a stale value doesn't fool you

const OF_URL = "https://onlyfans.com/";
const LOGIN_URL = "https://onlyfans.com/";  // OF shows the login modal on root if signed-out
const REQUIRED_COOKIES = ["auth_id", "sess"];  // OF's session cookies

const $ = (id) => document.getElementById(id);
const loginBtn = $("login-btn");
const copyBtn = $("copy-btn");
const resetBtn = $("reset-btn");
const addAccountBtn = $("add-account-btn");
const accountsList = $("accounts-list");
const statusLine = $("status-line");
const metaBox = $("meta");
const toast = $("toast");

let currentCapture = null;

function showToast(msg, kind = "ok") {
  toast.textContent = msg;
  toast.classList.remove("show");
  toast.style.color = kind === "err" ? "var(--err)" : "var(--ok)";
  toast.style.background = kind === "err"
    ? "rgba(248,81,73,0.12)"
    : "rgba(46,160,67,0.12)";
  toast.style.borderColor = kind === "err"
    ? "rgba(248,81,73,0.4)"
    : "rgba(46,160,67,0.4)";
  // Force reflow so re-adding .show triggers the transition fresh.
  void toast.offsetWidth;
  toast.classList.add("show");
  clearTimeout(showToast._t);
  showToast._t = setTimeout(() => toast.classList.remove("show"), 2500);
}

function renderStatus() {
  if (!currentCapture) {
    statusLine.innerHTML = '<span class="warn">Waiting for sign-in…</span>';
    metaBox.textContent = "";
    copyBtn.disabled = true;
    addAccountBtn.disabled = true;
    return;
  }
  const h = currentCapture.headers || {};
  const r = currentCapture.rules || {};
  const haveStatic = !!r.static_param;
  statusLine.innerHTML = haveStatic
    ? '<span class="ok">✓ Captured (with signing rules)</span>'
    : '<span class="warn">✓ Headers captured — waiting for sign() to fire…</span>';
  metaBox.innerHTML =
    `<div>user-id: ${escapeHtml(h["user-id"] || "?")}</div>` +
    `<div>x-of-rev: ${escapeHtml(h["x-of-rev"] || "?")}</div>` +
    `<div>static_param: ${haveStatic ? escapeHtml(r.static_param.slice(0, 24) + "…") : "<span class=\"warn\">not yet — open a chat in OF to trigger sign()</span>"}</div>` +
    `<div>captured: ${escapeHtml(currentCapture.capturedAt || "?")}</div>`;
  copyBtn.disabled = false;
  addAccountBtn.disabled = false;
}

function fmtWhen(iso) {
  if (!iso) return "";
  const t = new Date(iso).getTime();
  if (!t) return "";
  const s = Math.floor((Date.now() - t) / 1000);
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

async function renderAccounts() {
  const { accounts, activeId } = await new Promise((res) =>
    chrome.runtime.sendMessage({ type: "listAccounts" }, (r) => res(r || {}))
  );
  const entries = Object.values(accounts || {}).sort((a, b) =>
    (b.savedAt || "").localeCompare(a.savedAt || "")
  );
  if (!entries.length) {
    accountsList.innerHTML = '<div class="empty">No accounts saved yet — sign in to capture one.</div>';
    return;
  }
  accountsList.innerHTML = "";
  for (const acct of entries) {
    const row = document.createElement("div");
    row.className = "acct-row" + (String(acct.userId) === String(activeId) ? " active" : "");
    const isActive = String(acct.userId) === String(activeId);
    row.innerHTML =
      `<div class="id">${escapeHtml(acct.label || acct.userId)}</div>` +
      `<div class="when">${escapeHtml(fmtWhen(acct.savedAt))}</div>` +
      (isActive
        ? `<span class="badge">active</span>`
        : `<button data-act="switch" data-id="${escapeHtml(acct.userId)}">Use</button>`) +
      `<button class="danger" data-act="delete" data-id="${escapeHtml(acct.userId)}" title="Forget this login">×</button>`;
    accountsList.appendChild(row);
  }
}

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function shellQuote(s) {
  // POSIX-safe single-quote: end quote, escape ', re-open.
  return "'" + String(s).replace(/'/g, "'\\''") + "'";
}

async function getOnlyFansCookies() {
  return new Promise((resolve) => {
    chrome.runtime.sendMessage({ type: "getCookies" }, (resp) => {
      resolve((resp && resp.cookies) || []);
    });
  });
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
  // Runtime-only fields the wire doesn't carry but the relay needs for
  // a brand-new x-of-rev. The relay's curl parser recognises these as
  // overrides and never forwards them upstream.
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

function cookieSanityCheck(cookies) {
  const names = new Set(cookies.map((c) => c.name));
  const missing = REQUIRED_COOKIES.filter((n) => !names.has(n));
  return missing;
}

async function openOnlyFansTab() {
  const tabs = await chrome.tabs.query({ url: "https://onlyfans.com/*" });
  if (tabs.length > 0) {
    await chrome.tabs.update(tabs[0].id, { active: true });
    await chrome.windows.update(tabs[0].windowId, { focused: true });
    return tabs[0];
  }
  return await chrome.tabs.create({ url: LOGIN_URL });
}

async function init() {
  const { lastCapture } = await chrome.storage.local.get(["lastCapture"]);
  currentCapture = lastCapture || null;
  renderStatus();
  renderAccounts();

  chrome.storage.onChanged.addListener((changes, area) => {
    if (area !== "local") return;
    if (changes.lastCapture) {
      currentCapture = changes.lastCapture.newValue || null;
      renderStatus();
      if (currentCapture) showToast("New capture stored");
    }
    if (changes.accounts) renderAccounts();
  });

  loginBtn.addEventListener("click", async () => {
    loginBtn.disabled = true;
    try {
      await openOnlyFansTab();
      showToast("OnlyFans opened — sign in, then return here");
    } catch (e) {
      showToast("Couldn't open OnlyFans: " + e.message, "err");
    } finally {
      setTimeout(() => (loginBtn.disabled = false), 600);
    }
  });

  copyBtn.addEventListener("click", async () => {
    if (!currentCapture) return;
    const cookies = await getOnlyFansCookies();
    const missing = cookieSanityCheck(cookies);
    if (missing.length > 0) {
      showToast("Missing cookies: " + missing.join(", ") + " — sign in first", "err");
      return;
    }
    const curl = buildCurl(currentCapture, cookies);
    try {
      await navigator.clipboard.writeText(curl);
      showToast("Curl copied — paste into the relay's Setup tab");
    } catch (e) {
      // Fallback: select a hidden textarea
      const ta = document.createElement("textarea");
      ta.value = curl;
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      document.body.removeChild(ta);
      showToast("Curl copied (fallback)");
    }
  });

  resetBtn.addEventListener("click", async () => {
    await new Promise((res) =>
      chrome.runtime.sendMessage({ type: "resetCapture" }, () => res())
    );
    currentCapture = null;
    renderStatus();
    showToast("Cleared — sign in again to re-capture");
  });

  addAccountBtn.addEventListener("click", async () => {
    addAccountBtn.disabled = true;
    try {
      const resp = await new Promise((res) =>
        chrome.runtime.sendMessage({ type: "saveAndClear" }, (r) => res(r || {}))
      );
      if (!resp.ok) throw new Error(resp.error || "save failed");
      showToast(
        resp.savedAs
          ? `Saved account ${resp.savedAs} — sign in to the next one`
          : "Cleared — sign in to the next account"
      );
      renderAccounts();
    } catch (e) {
      showToast("Couldn't save: " + e.message, "err");
    } finally {
      setTimeout(() => (addAccountBtn.disabled = !currentCapture), 600);
    }
  });

  accountsList.addEventListener("click", async (e) => {
    const btn = e.target.closest("button[data-act]");
    if (!btn) return;
    const act = btn.dataset.act;
    const userId = btn.dataset.id;
    if (!userId) return;

    if (act === "switch") {
      btn.disabled = true;
      try {
        const resp = await new Promise((res) =>
          chrome.runtime.sendMessage(
            { type: "switchAccount", userId },
            (r) => res(r || {})
          )
        );
        if (!resp.ok) throw new Error(resp.error || "switch failed");
        showToast(`Switched to ${userId} — OnlyFans reloading`);
      } catch (err) {
        showToast("Switch failed: " + err.message, "err");
        btn.disabled = false;
      }
      return;
    }

    if (act === "delete") {
      if (!confirm(`Forget login for ${userId}?`)) return;
      const resp = await new Promise((res) =>
        chrome.runtime.sendMessage({ type: "deleteAccount", userId }, (r) => res(r || {}))
      );
      if (resp.ok) {
        showToast(`Forgot ${userId}`);
        renderAccounts();
      } else {
        showToast("Delete failed: " + (resp.error || "?"), "err");
      }
    }
  });
}

init();
