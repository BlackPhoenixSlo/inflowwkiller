# Fastt Login Capture (Chrome extension)

Replaces the old `--profile capture` Docker sidecar with a one-click browser
extension. Sign in to OnlyFans, click **Copy curl**, paste into the relay's
**Setup** tab.

## Install (unpacked)

1. Open `chrome://extensions` (or `arc://extensions`, `edge://extensions`).
2. Toggle **Developer mode** on.
3. Click **Load unpacked** and pick this folder (`loginExtension/`).
4. Pin the extension (puzzle-piece icon → pin) so the popup is one click away.

## Use it

1. Click the extension icon → **Log in to OnlyFans**.
2. Sign in normally. The interceptor watches the first signed
   `/api2/v2/` request and stores the live headers
   (`user-id`, `x-bc`, `x-of-rev`, `sign`, `time`, `user-agent`) plus all
   `onlyfans.com` cookies.
3. Pop the extension again → **Copy curl** → paste into the relay's
   *Setup* tab → *Paste curl* mode → submit.

## Why this and not the old Docker capture sidecar

The `--profile capture` build pulled a ~1.3 GB Playwright base image and on
some hosts stalled for hours during the Chromium install. The user already
runs Chrome — this extension just reuses the live session, no second browser
required.

## x-of-rev freshness

`x-of-rev` is OnlyFans' build-revision hash. It bumps every time they ship
a frontend deploy. The relay signs requests with whatever `static_param`
was published for that rev, so when the rev changes the cached signing
rules go stale and signed calls start returning 401.

Each new sign-in via this extension captures **the live rev that was on
the wire at that moment** (every signed request carries `x-of-rev` —
the interceptor reads it from `setRequestHeader` / `fetch` init). So when
the relay surfaces a "re-capture required" banner, the fix is:

1. Click **Reset** in the popup (so a stale capture doesn't fool you).
2. Click **Log in to OnlyFans**, sign in (or just reload OnlyFans if
   already signed in — any signed XHR triggers a fresh capture).
3. **Copy curl** → paste into the relay → done. The new curl carries the
   current rev and the relay re-derives signing rules for it.
