"use strict";

const path = require("path");

// Best-effort unread badge. The hosted UI already reflects unread counts in the
// document title (roster badges / DM toasts feed it). Rather than couple to the
// page's internals — which would break the "shell touches no app/ code" promise
// — we parse the title string the browser reports via `page-title-updated`.
//
// Recognised shapes (first match wins):
//   "(3) Fastt"      → 3      classic web-app unread-in-title convention
//   "Fastt • 3"      → 3
//   "Fastt (12)"     → 12
// No number → 0 (badge cleared). This is intentionally forgiving: if the title
// format ever changes the badge simply goes quiet, it never throws or misfires.

const LEADING_PAREN = /^\((\d+)\)/; //           "(3) …"
const TRAILING_PAREN = /\((\d+)\)\s*$/; //        "… (3)"
const BULLET = /[•·]\s*(\d+)\s*$/; //             "… • 3"

function parseUnread(title) {
  if (typeof title !== "string" || !title) return 0;
  const m = title.match(LEADING_PAREN) || title.match(TRAILING_PAREN) || title.match(BULLET);
  if (!m) return 0;
  const n = parseInt(m[1], 10);
  return Number.isFinite(n) && n > 0 ? n : 0;
}

// Windows draws no number for `setBadgeCount` — it needs a taskbar overlay
// image. We ship pre-rendered discs (assets/overlay/unread-{1..9,9plus}.png)
// and pick one per count. Rendering them at runtime would need a canvas the
// main process doesn't have; ten 32px PNGs are ~4KB total, so they're baked.
const OVERLAY_DIR = path.join(__dirname, "..", "assets", "overlay");
const overlayCache = new Map(); // key -> nativeImage | null (null = tried, unusable)

function overlayFor(nativeImage, count) {
  if (!nativeImage || !(count > 0)) return null;
  const key = count > 9 ? "9plus" : String(count);
  if (overlayCache.has(key)) return overlayCache.get(key);
  let img = null;
  try {
    const candidate = nativeImage.createFromPath(path.join(OVERLAY_DIR, `unread-${key}.png`));
    // A missing file yields an EMPTY image rather than throwing; passing that
    // to setOverlayIcon would blank the taskbar icon instead of badging it.
    if (candidate && !candidate.isEmpty()) img = candidate;
  } catch {
    /* unreadable asset — fall through to no overlay. */
  }
  overlayCache.set(key, img);
  return img;
}

// Apply a count to the OS. `app.setBadgeCount` is the numeric dock badge on
// macOS. (Electron 43 dropped Linux support for it; we ship mac + win only.)
// `overlayIcon` stays an explicit override so tests can inject one; when it is
// omitted we resolve the shipped asset for `count`.
function applyBadge({ app, win, nativeImage, count, overlayIcon }) {
  try {
    if (app && typeof app.setBadgeCount === "function") {
      app.setBadgeCount(count); // 0 clears it.
    }
  } catch {
    /* setBadgeCount can throw on some Linux desktops — ignore. */
  }

  if (process.platform === "win32" && win && !win.isDestroyed()) {
    try {
      const icon = overlayIcon || overlayFor(nativeImage, count);
      if (count > 0 && icon) {
        win.setOverlayIcon(icon, `${count} unread`);
      } else {
        win.setOverlayIcon(null, "");
      }
    } catch {
      /* overlay unsupported / bad image — ignore. */
    }
  }
}

module.exports = { parseUnread, applyBadge, overlayFor };
