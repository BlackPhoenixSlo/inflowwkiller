# Chatterly No-Teleport Login

Sibling of [`loginExtension/`](../loginExtension/) with one extra superpower:
it pushes onlyfans.com traffic through a proxy **before** sign-in, so OF
mints the cookies on the same IP the relay will later use. No first-call
teleport, no "the account jumped continents" flag.

## Install

1. Open `chrome://extensions` → Developer mode on.
2. Load unpacked → pick this folder (`noTeleportLoginExtension/`).
3. Pin it.

## Flow

1. **Pick a proxy.** Either:
   - Type the URL manually: `socks5://user:pass@host:port` or
     `http://user:pass@host:port`, or
   - Set your relay URL (e.g. `http://127.0.0.1:8787`) + share token, click
     **Load**, choose from the dropdown — the popup fills the URL for you.
2. **Activate proxy for OnlyFans.** The PAC script Chrome installs only
   matches `onlyfans.com` / `*.onlyfans.com`; every other site keeps going
   DIRECT, so your normal browsing is untouched.
3. **Open OnlyFans.** Sign in. The interceptor captures
   `user-id`, `x-bc`, `x-of-rev`, `sign`, `time`, `user-agent` from the
   first signed `/api2/v2/` request and `static_param` from the live
   `sha1()` call inside webpack chunk `2313`.
4. **Copy curl** → paste into the relay's Setup → Paste cURL card. Since
   the curl carries `x-relay-static-param`, the relay can bootstrap even
   on a brand-new OF revision.
5. **Off.** When you're done, click **Off** in the popup so onlyfans.com
   stops routing through the proxy.

After bootstrapping, attach the SAME proxy to this account in the relay
(Setup → Proxies → pick the bound proxy → assign). Now login egress ==
runtime egress: no teleport, ever.

## Why a separate extension?

`loginExtension/` is the "I already have a clean OF session in this browser,
just give me the curl" path. This one is "I'm setting up a brand new
account and want the cookies anchored to my proxy from the very first byte."
They're complementary; install both side-by-side if you want.

## Caveats

- **Proxy auth uses MV3 `webRequestAuthProvider`** — Chrome 108+ only.
- **Scope is browser-wide** (Chrome's proxy API can't be per-tab). The PAC
  narrows the actual routing to onlyfans.com, but the *setting* applies to
  every Chrome window. If you have multiple profiles, only the profile
  this extension is installed in gets the override.
- **SOCKS5 with auth**: Chrome supports `socks5://user:pass@…` via the
  `onAuthRequired` listener the same way HTTP-CONNECT does, but some
  residential SOCKS5 providers only accept auth via the proxy URL —
  test once with `curl --socks5 user:pass@host:port https://api.ipify.org`
  first if you see "auth failed" loops.
- **Don't leave the proxy on** when you're done. The popup's **Off** button
  reverts to system defaults. Uninstalling the extension also clears it.
