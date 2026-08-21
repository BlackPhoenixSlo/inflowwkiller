# Fastt Desktop

A thin, secure Electron shell that ships the existing Fastt web UI as a
downloadable **Mac + Windows** app. It **remote-loads** the hosted UI
(`https://fastt.lol` by default) — it imports nothing from `../app` or
`../service` and ships no product code. Delete this folder and the web app is
untouched.

Implements [../plan/DESKTOP_APP_PLAN.md](../plan/DESKTOP_APP_PLAN.md), Phase 1
(the shell) **and** Phase 2 (native OnlyFans login capture — the extension
replacement). See "Add an OnlyFans account" below.

## What you get

- A real dock/taskbar app window onto the live UI — no browser tab to lose.
- **No tab-suspension**: the realtime SSE stream and roster badges stay live in
  the background (browsers throttle/suspend hidden tabs; a window doesn't).
- **Native OS notifications** for DM/buy events — the UI already fires web
  `Notification()`s; Electron maps them to the OS. ⚠️ **Unverified — see
  "Notifications are not confirmed working" below.**
- **Unread dock badge** — a numeric dock badge on macOS, and a matching numbered
  taskbar overlay on Windows (`assets/overlay/`, counts 1–9 then `9+`).
- **External links** open in the OS browser; same-origin popouts (group view)
  open as in-app child windows.

## Run it (dev)

```bash
cd desktop
npm install
npm start          # opens the app; first run shows a Settings screen
npm run dev        # same, but enables the DevTools menu item
```

On first launch you'll be asked for your **Server URL** (default `https://fastt.lol`)
and your **share token** (the `t=` value from your Fastt link). These are stored
in `fastt-desktop-config.json` under Electron's per-user data dir (mode `600`).
The token is seeded into the app's cookie + `localStorage` once, via the
frontend's own `?t=` mechanism — it is **not** re-appended to the URL on every
launch. Use **Re-seed Login / Fix Access** in the menu if the token rotates or
access expires.

## Add an OnlyFans account (native capture — replaces the extension)

Menu → **Add OnlyFans Account…**. A real OnlyFans login window opens; sign in
with **email + password**, then open your **Messages** so OnlyFans signs a
request. A small status panel tracks three steps — session cookies, request
headers, signing rules — and enables **Finish & Save** once all three are
captured. That POSTs the session to the relay's `/admin/session/bootstrap`
(the same endpoint the extension fed), so it's a drop-in front end for a proven
path.

How it works: the shell injects a MAIN-world interceptor at document-start via
Chrome DevTools Protocol (`Page.addScriptToEvaluateOnNewDocument`) — a faithful
port of `loginExtension/interceptor.js`, capturing the signed headers and the
webpack sign-rules (`static_param` from module 89668, `start`/`end` from 802313).
The OF login runs in a **non-persistent** partition that is wiped on teardown, so
OF cookies never linger on disk.

**Detection mitigation.** Electron's default UA + broken client-hints are a
fingerprint tell vs real Chrome (the fusion review's main objection to Phase 2).
The capture window uses CDP `Network.setUserAgentOverride` to present a real
Chrome UA **and** a consistent client-hints block (the documented fix for
electron#34762), and that same UA is what the relay replays. This makes Electron
login materially closer to real Chrome — but it is **not a guarantee** OnlyFans
won't flag it. **If an account matters, capturing it in the real Chrome extension
is still the lowest-risk path.** Prefer email/password login: social-login
pop-ups (Google/X) open in the OS browser and won't complete the in-app session.

## ⚠️ Notifications are not confirmed working (open, 2026-08-21)

Tested on macOS 15.6.1 against packaged builds on **both** Electron 41 and 43:
the in-app test (bell → settings → Test) reports `Permission denied`, meaning
`Notification.permission` is already `"denied"` and `requestPermission()` is
never reached. Two supporting facts:

- **No `lol.fastt.*` bundle appears in `~/Library/Preferences/com.apple.ncprefs.plist`**,
  which lists the 128 apps macOS knows about — including `com.github.Electron`
  and other Electron apps. A build that had ever delivered a notification would
  be listed.
- Both Electron versions fail *identically*, which points at something common to
  all builds rather than the Electron 42 `UNNotification` signing change.

Leading suspect is this shell's own `setPermissionCheckHandler` (`main.js`),
which answers `Notification.permission` and returns false unless
`requestingOrigin === appOrigin`; if Chromium passes an empty origin for
notification checks, that denies on every version. **Not yet confirmed** — an
instrumented build was prepared but the trace was not captured before this was
parked.

Why it went unnoticed: `npm start` runs inside Electron's own signed, already
registered `Electron.app`, where notifications DO work. The feature can only
fail in the packaged bundle, so testing the obvious way gives a false pass.

**Parked deliberately** — the operator decided on 2026-08-21 that this is not
blocking. Nothing else in the shell depends on it. Pick it up by running the
instrumented build and reading which `permission` / `requestingOrigin` pairs
arrive.

## Build installers

```bash
npm run dist:mac   # → dist/Fastt-0.2.0-arm64.dmg  and  -x64.dmg
npm run dist:win   # → dist/Fastt-Setup-0.2.0.exe   (builds fine ON macOS)
npm run pack       # unpacked build for quick local testing (no installer)
```

**The Windows installer now cross-builds on macOS.** electron-builder 26 ships
its own NSIS toolchain, so the old advice — "the installer must be built on a
Windows machine, it crashes under emulation on Apple Silicon" — is obsolete as of
the 0.2.0 upgrade. `npm run dist:win` produced a valid NSIS installer straight
from an M-series Mac, no Wine and no Windows VM. The GitHub Actions workflow is
still useful for a clean-room build, but is no longer required.

Builds are **unsigned** by default (`mac.identity: null`). See Signing below.

## Security posture (enforced in `main.js`)

- `nodeIntegration: false`, `contextIsolation: true`, `sandbox: true`.
- The remote page gets **no** privileged bridge — only a static, read-only
  `window.fasttShell` version object (`preload.js`). All capability (badge,
  external-link routing, permissions) lives in the main process.
- **Deny-by-default navigation**: foreign-origin top-level navigations and
  `window.open` targets go to the OS browser; only same-origin popouts become
  in-app windows.
- **Permissions**: only `notifications` are granted; camera/mic/geolocation/USB
  are denied.
- IPC handlers (settings/retry) accept **only `file://` local senders** — the
  remote `fastt.lol` page can never reach them.

## Signing (deferred — see the fusion review)

**Electron 42+ raises the stakes on macOS.** Electron 42 moved macOS
notifications to Apple's `UNNotification` API, which per Electron's breaking
changes "requires that an application be code-signed in order for notifications
to be displayed" — an unsigned app gets a `failed` event on the `Notification`
object and shows nothing. Native notifications are a headline feature of this
shell, so on Electron 42+ signing stops being cosmetic. Verify with
`scratchpad/probe-mac-notifications.sh` before assuming an unsigned build still
notifies.

Shipping unsigned is otherwise fine while your users are you + technical early
adopters (macOS: right-click → Open the first time). **Before a non-technical chatter-team
rollout, sign + notarize the mac build** — an unsigned `.dmg` shows *"app is
damaged and can't be opened"* to a normal user, and **macOS auto-update requires
a signed app** (so deferring signing also defers auto-update). Windows can stay
unsigned longer (SmartScreen "More info → Run anyway").

To sign later: get an Apple Developer ID cert ($99/yr), then set
`CSC_LINK`/`CSC_KEY_PASSWORD` + a notarize config in `package.json`'s `build.mac`
and drop `identity: null`.

## ⚠️ Pre-ship revocation probe (REQUIRED before a team rollout)

The fusion review's wrong-layer finding: a persistent Electron partition turns
the share token + session cookie into **durable on-disk credentials**, and
closing the window is **not** logout. Before handing this to a team, verify a
revoked user actually loses access. Run this matrix against the **real**
production edge:

1. Configure + authenticate the app with a `?t=` token.
2. Close and reopen the app; reboot the machine. → still logged in (expected).
3. Copy the Electron profile dir to a **second** machine, launch there.
4. On the server: revoke the employee, rotate the share token, invalidate the
   session.
5. **Verify**: every already-open window AND every subsequent API/navigation
   request fails **immediately** — not after cookie expiry. Confirm the token is
   not recoverable from disk, URL, or history to restore access.

If any case survives revocation, that's a **relay/edge/session change** that
becomes a Phase-1 requirement (not frontend-only).

## Still deliberately deferred

- **Sign + notarize the mac build** before a non-technical rollout (unsigned
  shows "app is damaged"); mac auto-update needs it too.
- **Run the revocation probe** below before a team rollout.
- **Verify a captured session end-to-end on a test account** (e.g. Ava) — that a
  captured login actually sends an OF message via the relay — before trusting the
  native capture for a real account. The capture path is faithful to the
  extension by construction + review, but has not been exercised against live OF
  in this environment.

## Limitations (v0.2)

- **No auto-update** yet — it requires signing on mac (see above). Ship it when
  you sign.
- The unread badge parses the page **title** (best-effort); if the title format
  changes the badge goes quiet, it never misfires.
