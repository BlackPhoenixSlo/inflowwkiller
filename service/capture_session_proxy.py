#!/usr/bin/env python3
"""
Capture an OnlyFans session through a per-account proxy — no Incogniton needed.

WHY THIS EXISTS
---------------
The default Incogniton-based capture (capture_session.py) loads OF through
Incogniton's own browser, which connects from your host's WAN IP. After
capture, your relay routes REST through the per-account proxy — but the
session cookies were minted at a DIFFERENT egress IP. To OF, every signed
request after the capture looks like the same user "teleported" from the
capture IP to the proxy IP. Strong fraud signal.

This module fixes that: it launches Playwright's own bundled Chromium with
the per-account proxy attached. Login + capture + every subsequent signed
request all egress from the SAME proxy IP. The model logs in once "from
Budapest"; the relay then keeps sending from Budapest forever.

USAGE
-----
Programmatic (from session_bootstrap.run_playwright_proxy):

    from capture_session_proxy import capture
    path = capture(
        proxy_url="http://user:pass@203.0.113.10:12323",
        proxy_label="hu-2",          # used only for log lines + session JSON
    )

CLI (one-off testing):

    ./venv/bin/python service/capture_session_proxy.py \\
        http://user:pass@host:port

NOTE
----
This module is HOST-ONLY (excluded from the Docker image — see .dockerignore
pattern `service/capture_*.py`). The Docker container should use paste-curl
mode when adding new accounts; this module is for the dev / power-user
running locally with `./venv/bin/uvicorn ...`.
"""
from __future__ import annotations

import json
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

HERE = Path(__file__).resolve().parent
INTERCEPTOR_JS = (HERE / "interceptor.js").read_text(encoding="utf-8")
SESSIONS_DIR = HERE / "sessions"

OF_HOME = "https://onlyfans.com/"
OF_CHATS = "https://onlyfans.com/my/chats/"

LOGIN_POLL_INTERVAL_S = 2
LOGIN_TIMEOUT_S = 600      # 10 min — generous for 2FA / captcha
HOOK_WAIT_S = 8            # let webpack hooks accumulate samples after chats load
NETWORKIDLE_TIMEOUT_MS = 30_000


def _parse_proxy(proxy_url: str) -> dict[str, str]:
    """Convert `http://user:pass@host:port` into Playwright's launch-proxy dict.

    Playwright's `browser_type.launch(proxy={server, username, password})`
    wants the bare `scheme://host:port` in `server`, with credentials passed
    separately. Bundling them into `server` works for some sites but breaks
    HTTPS CONNECT auth on others — keep them split."""
    u = urlparse(proxy_url)
    if not u.scheme or not u.hostname or not u.port:
        raise ValueError(f"proxy_url must be scheme://[user:pass@]host:port — got {proxy_url!r}")
    out: dict[str, str] = {"server": f"{u.scheme}://{u.hostname}:{u.port}"}
    if u.username:
        out["username"] = u.username
    if u.password:
        out["password"] = u.password
    return out


def _logged_in(context) -> tuple[bool, str | None]:
    """auth_id cookie is the canonical OF login signal (httpOnly, set on login)."""
    for c in context.cookies(["https://onlyfans.com"]):
        if c["name"] == "auth_id" and c["value"]:
            return True, c["value"]
    return False, None


def _wait_for_login(context, timeout_s: int = LOGIN_TIMEOUT_S) -> str:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        ok, uid = _logged_in(context)
        if ok:
            return uid  # type: ignore[return-value]
        time.sleep(LOGIN_POLL_INTERVAL_S)
    raise TimeoutError(f"auth_id cookie did not appear within {timeout_s}s")


def _fetch_signing_chunk(page, x_of_rev: str | None) -> tuple[str | None, str | None]:
    """Fetch chunk 2313.js (the signing module) from the page context so it
    inherits cookies/origin. Returns (url, code) or (url, None) on failure."""
    if not x_of_rev:
        return None, None
    url = f"https://static2.onlyfans.com/static/prod/f/{x_of_rev}/2313.js"
    try:
        code = page.evaluate(
            """async (u) => {
                const r = await fetch(u);
                if (!r.ok) return null;
                return await r.text();
            }""",
            url,
        )
        return url, code
    except Exception as e:
        print(f"[!] Could not fetch 2313.js: {e}")
        return url, None


def _browser_profile_dir(account_hint: str | None) -> Path:
    """Where to keep the persistent Chromium profile so OF + Cloudflare
    cookies survive between captures (= no captcha on every re-login).

    Re-capture flow (`account_hint` set): bucket by account so each model
    keeps its own warmed-up profile (cookies, captcha-solved flag, etc).

    Fresh-capture flow (`account_hint` is None — user clicked "Launch
    browser via proxy" expecting to log into a NEW account): bucket by a
    per-launch timestamp so the browser does NOT carry forward whichever
    account was logged in last on this proxy. Trade-off: a fresh
    Cloudflare challenge each time. Worth it — the alternative was that
    the user opened the browser and saw the previous account already
    signed in."""
    base = SESSIONS_DIR / "browser_profiles"
    base.mkdir(parents=True, exist_ok=True)
    if account_hint:
        bucket = account_hint
    else:
        # Microsecond timestamp + short uuid suffix → collision-free even
        # across rapid double-clicks. Every fresh-capture click gets its
        # own empty dir, no chance of inheriting a sibling launch's state.
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%f")
        bucket = f"fresh-{ts}-{uuid.uuid4().hex[:6]}"
    p = base / bucket
    p.mkdir(parents=True, exist_ok=True)
    return p


# Map a proxy's `verified_geo` (free-text city/region/country) to plausible
# locale + timezone + accept-language so the browser fingerprint is internally
# consistent with the egress IP geolocation. Cloudflare cross-references these:
# US IP + Hungarian locale = "lying about location" signal.
# Keys matched as substrings of the proxy's `verified_geo` (which ipinfo
# returns as "City, Region, CC" — e.g. "Budapest, Budapest, HU"). The CC
# variants are listed alongside the full country name so both formats hit.
_GEO_DEFAULTS = {
    "hungary": ("hu-HU", "Europe/Budapest", "hu-HU,hu;q=0.9,en-US;q=0.8,en;q=0.7"),
    ", hu":    ("hu-HU", "Europe/Budapest", "hu-HU,hu;q=0.9,en-US;q=0.8,en;q=0.7"),
    "slovenia": ("sl-SI", "Europe/Ljubljana", "sl-SI,sl;q=0.9,en-US;q=0.8,en;q=0.7"),
    ", si":     ("sl-SI", "Europe/Ljubljana", "sl-SI,sl;q=0.9,en-US;q=0.8,en;q=0.7"),
    "germany":  ("de-DE", "Europe/Berlin", "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7"),
    ", de":     ("de-DE", "Europe/Berlin", "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7"),
    "austria":  ("de-AT", "Europe/Vienna", "de-AT,de;q=0.9,en;q=0.8"),
    ", at":     ("de-AT", "Europe/Vienna", "de-AT,de;q=0.9,en;q=0.8"),
    "italy":    ("it-IT", "Europe/Rome", "it-IT,it;q=0.9,en;q=0.8"),
    ", it":     ("it-IT", "Europe/Rome", "it-IT,it;q=0.9,en;q=0.8"),
    "france":   ("fr-FR", "Europe/Paris", "fr-FR,fr;q=0.9,en;q=0.8"),
    ", fr":     ("fr-FR", "Europe/Paris", "fr-FR,fr;q=0.9,en;q=0.8"),
    "spain":    ("es-ES", "Europe/Madrid", "es-ES,es;q=0.9,en;q=0.8"),
    ", es":     ("es-ES", "Europe/Madrid", "es-ES,es;q=0.9,en;q=0.8"),
    "united kingdom": ("en-GB", "Europe/London", "en-GB,en;q=0.9"),
    ", gb":     ("en-GB", "Europe/London", "en-GB,en;q=0.9"),
    "netherlands": ("nl-NL", "Europe/Amsterdam", "nl-NL,nl;q=0.9,en;q=0.8"),
    ", nl":     ("nl-NL", "Europe/Amsterdam", "nl-NL,nl;q=0.9,en;q=0.8"),
    "poland":   ("pl-PL", "Europe/Warsaw", "pl-PL,pl;q=0.9,en;q=0.8"),
    ", pl":     ("pl-PL", "Europe/Warsaw", "pl-PL,pl;q=0.9,en;q=0.8"),
    "united states": ("en-US", "America/New_York", "en-US,en;q=0.9"),
    ", us":     ("en-US", "America/New_York", "en-US,en;q=0.9"),
}
_GEO_FALLBACK = ("en-US", "UTC", "en-US,en;q=0.9")


def _geo_for_proxy(proxy_label: str | None) -> tuple[str, str, str]:
    """Look up the proxy in the registry and return (locale, tz, accept_lang).

    Without a proxy registry hit or recognized geo string, falls back to a
    neutral en-US/UTC profile — better than nothing but worse than matching
    the egress city, which is what trips Cloudflare's geo-coherence check.
    """
    if not proxy_label:
        return _GEO_FALLBACK
    try:
        import proxies as proxy_registry  # noqa
        p = proxy_registry.get_by_label(proxy_label)
        if not p:
            return _GEO_FALLBACK
        geo = (p.get("verified_geo") or "").lower()
        for needle, triple in _GEO_DEFAULTS.items():
            if needle in geo:
                return triple
    except Exception:
        pass
    return _GEO_FALLBACK


def _chrome_init_script() -> str:
    """Extra anti-fingerprint patches applied before any page script runs.

    `playwright-stealth` covers the standard set (webdriver/plugins/etc).
    These add a few more axes Cloudflare actively probes:
      - hardware concurrency = a real Mac, not the headless-default 1
      - device memory = real Mac amount, not undefined
      - Chrome's runtime object pretending to be a non-extension page
      - permissions API returning sane defaults for notifications/etc
    """
    return r"""
    // Hardware that should look like a real Mac
    try {
      Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });
      Object.defineProperty(navigator, 'deviceMemory', { get: () => 8 });
      Object.defineProperty(navigator, 'maxTouchPoints', { get: () => 0 });
    } catch (e) {}

    // Real Chrome's window.chrome is a non-trivial object — the empty
    // {} that playwright-stealth installs sometimes trips checks.
    try {
      if (!window.chrome || Object.keys(window.chrome).length < 2) {
        window.chrome = {
          app: { isInstalled: false, InstallState: { DISABLED:'disabled', INSTALLED:'installed', NOT_INSTALLED:'not_installed' }, RunningState: { CANNOT_RUN:'cannot_run', READY_TO_RUN:'ready_to_run', RUNNING:'running' } },
          runtime: { OnInstalledReason: {}, OnRestartRequiredReason: {}, PlatformArch: {ARM:'arm',ARM64:'arm64',MIPS:'mips',MIPS64:'mips64',X86_32:'x86-32',X86_64:'x86-64'}, PlatformNaclArch: {}, PlatformOs: {MAC:'mac',WIN:'win',ANDROID:'android',CROS:'cros',LINUX:'linux',OPENBSD:'openbsd'}, RequestUpdateCheckStatus: {} },
          loadTimes: function() { return { commitLoadTime: Date.now()/1000 - 1 }; },
          csi: function() { return { onloadT: Date.now(), pageT: 100, startE: Date.now()-100, tran: 15 }; },
        };
      }
    } catch (e) {}

    // Permissions.query() must NOT differ between webdriver and real Chrome
    // — Cloudflare diffs them. Real Chrome returns 'prompt' for notifications.
    try {
      const origQuery = navigator.permissions && navigator.permissions.query;
      if (origQuery) {
        navigator.permissions.query = (parameters) =>
          parameters && parameters.name === 'notifications'
            ? Promise.resolve({ state: Notification.permission || 'prompt' })
            : origQuery.call(navigator.permissions, parameters);
      }
    } catch (e) {}
    """


def launch_context(p, proxy_url: str, *,
                   proxy_label: str | None = None,
                   account_hint: str | None = None,
                   headless: bool = False):
    """Launch a persistent-profile Chromium routed through `proxy_url`, apply
    the shared anti-fingerprint init scripts + playwright-stealth, warm a
    `cf_clearance` cookie, open the OF homepage, and return
    `(context, page, profile_dir)`.

    THIS IS THE ONE LAUNCHER. `capture()` (host, human logs in in the visible
    window) and `remote_login` (served, headless, human drives the challenge
    through a web screencast) both go through here so there is a single place
    the proxy/profile/stealth setup lives. No new stealth is added for the
    served flow — it inherits exactly what the host capture already uses."""
    proxy_cfg = _parse_proxy(proxy_url)
    # Profile dir: account-bucketed when re-capturing a known account;
    # otherwise a per-launch fresh-<ts>-<uuid> dir (see _browser_profile_dir).
    profile_dir = _browser_profile_dir(account_hint)
    locale, tz, accept_lang = _geo_for_proxy(proxy_label)
    print(f"[*] Launching browser via proxy {proxy_label or proxy_cfg['server']}")
    print(f"    profile: {profile_dir.relative_to(HERE.parent)}")
    print(f"    locale: {locale}  tz: {tz}  accept-language: {accept_lang}")

    launch_kwargs = dict(
        user_data_dir=str(profile_dir),
        headless=headless,
        proxy=proxy_cfg,
        # macOS Chrome UA. Sec-ch-ua headers Chrome auto-sets will agree.
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1366, "height": 900},
        locale=locale,
        timezone_id=tz,
        extra_http_headers={"Accept-Language": accept_lang},
        device_scale_factor=2,
        color_scheme="dark",
        args=[
            "--disable-blink-features=AutomationControlled",
            "--disable-features=IsolateOrigins,site-per-process,AutomationControlled",
            "--no-default-browser-check",
            "--no-first-run",
            "--password-store=basic",
            "--use-mock-keychain",
            "--start-maximized",
            "--disable-features=PrivacySandboxAdsAPIs",
        ],
        ignore_default_args=["--enable-automation"],
    )
    # Always use the bundled Chromium (sandboxed) so only our profile dir
    # holds state and a "fresh" launch can't resurface the last account.
    context = p.chromium.launch_persistent_context(**launch_kwargs)
    print("[*] using bundled Chromium (sandboxed, fully isolated profile)")

    context.add_init_script(INTERCEPTOR_JS)
    context.add_init_script(_chrome_init_script())
    try:
        from playwright_stealth import Stealth  # type: ignore
        Stealth().apply_stealth_sync(context)
        print("[*] playwright-stealth patches applied")
    except Exception as e:
        print(f"[!] playwright-stealth unavailable ({e}); continuing without")

    page = context.pages[0] if context.pages else context.new_page()

    # Warmup: visit a benign site first so OF inherits the "low risk" signal.
    warm = any(c["name"] == "cf_clearance" for c in context.cookies())
    if not warm:
        try:
            print("[*] warming up cookies via https://www.google.com (~6s)")
            page.goto("https://www.google.com/", wait_until="domcontentloaded",
                      timeout=15000)
            for x, y in [(200, 300), (450, 220), (700, 480), (300, 600)]:
                try:
                    page.mouse.move(x, y, steps=8)
                except Exception:
                    break
            page.wait_for_timeout(3000)
        except Exception as e:
            print(f"[!] warmup skipped ({e})")
    else:
        print("[*] profile already has cf_clearance — skipping warmup")

    print(f"[*] Opening {OF_HOME}")
    try:
        page.goto(OF_HOME, wait_until="domcontentloaded", timeout=NETWORKIDLE_TIMEOUT_MS)
    except PWTimeout:
        print("[!] domcontentloaded timed out — proxy may be slow; continuing")

    for x, y in [(683, 400), (300, 200), (900, 500)]:
        try:
            page.mouse.move(x, y, steps=10)
            page.wait_for_timeout(400)
        except Exception:
            break

    return context, page, profile_dir


def harvest(page, context, *, proxy_url: str | None = None,
            proxy_label: str | None = None) -> tuple[dict[str, Any], str | None]:
    """Read cookies + signing rules + localStorage off a logged-in page and
    build the canonical session dump dict. Returns `(dump, chunk_code)`.

    Caller must already have navigated somewhere that fired signed XHRs (the
    webpack hooks need a chats load) and waited for the hooks to accumulate."""
    captured: dict[str, Any] = page.evaluate(
        """() => ({
            userId: window.__ofe_userId || null,
            xBc: window.__ofe_xBc || null,
            xOfRev: window.__ofe_xOfRev || null,
            lastSign: window.__ofe_lastSign || null,
            lastTime: window.__ofe_lastTime || null,
            rules: window.__ofe_extractedRules || null,
            samples: window.__ofe_signSamples || [],
            userAgent: navigator.userAgent,
            href: location.href,
        })"""
    )
    cookies = context.cookies([
        "https://onlyfans.com",
        "https://cdn2.onlyfans.com",
        "https://static2.onlyfans.com",
    ])
    local_storage = page.evaluate(
        "() => Object.fromEntries(Object.keys(localStorage).map(k => [k, localStorage.getItem(k)]))"
    )
    chunk_url, chunk_code = _fetch_signing_chunk(page, captured.get("xOfRev"))
    if chunk_code:
        print(f"[*] Fetched 2313.js ({len(chunk_code)} bytes)")

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dump = {
        "captured_at": ts,
        "profile_id": f"playwright-proxy:{proxy_label}" if proxy_label else "playwright-proxy",
        "page_url": captured.get("href"),
        "headers": {
            "user_id": captured.get("userId"),
            "x_bc": captured.get("xBc"),
            "x_of_rev": captured.get("xOfRev"),
            "user_agent": captured.get("userAgent"),
        },
        "signing": {
            "rules": captured.get("rules"),
            "samples": captured.get("samples"),
            "last_sign_seen": captured.get("lastSign"),
            "last_time_seen": captured.get("lastTime"),
            "chunk_2313_url": chunk_url,
            "chunk_2313_file": f"chunk_2313_{ts}.js" if chunk_code else None,
        },
        "cookies": cookies,
        "local_storage": local_storage,
    }
    if proxy_label or proxy_url:
        dump["capture_proxy"] = {
            "label": proxy_label,
            "url_redacted": _redact(proxy_url) if proxy_url else None,
        }
    return dump, chunk_code


def write_session(dump: dict[str, Any], chunk_code: str | None) -> Path:
    """Persist a harvested dump (+ its 2313.js chunk) into the flat sessions/
    dir and return the path. session_bootstrap._adopt_into_account then moves
    it into sessions/accounts/<user_id>/."""
    SESSIONS_DIR.mkdir(exist_ok=True)
    ts = dump["captured_at"]
    session_path = SESSIONS_DIR / f"session_{ts}.json"
    session_path.write_text(json.dumps(dump, indent=2))
    print(f"[*] Saved session -> {session_path.relative_to(HERE.parent)}")
    if chunk_code:
        chunk_path = SESSIONS_DIR / f"chunk_2313_{ts}.js"
        chunk_path.write_text(chunk_code)
        print(f"[*] Saved 2313.js -> {chunk_path.relative_to(HERE.parent)}")
    return session_path


def capture(proxy_url: str, *,
            proxy_label: str | None = None,
            account_hint: str | None = None,
            headless: bool = False) -> Path:
    """Launch a persistent-profile Chromium routed through `proxy_url`,
    wait for the user to log in, capture cookies + signing rules, save
    session JSON, return the path. `headless=False` so the user can log in.

    Persistent profile dir (sessions/browser_profiles/<account_id-or-proxy>/)
    means cookies, localStorage, IndexedDB, and the "I've solved the captcha
    here before" flags survive across runs. The first capture on a fresh
    proxy may show a Cloudflare challenge; subsequent re-captures usually
    skip it entirely.

    Returns the path to the saved session_*.json (still in the flat
    sessions/ dir — session_bootstrap._adopt_into_account moves it into
    sessions/accounts/<user_id>/)."""
    with sync_playwright() as p:
        context, page, _profile_dir = launch_context(
            p, proxy_url, proxy_label=proxy_label,
            account_hint=account_hint, headless=headless,
        )

        ok, uid = _logged_in(context)
        if not ok:
            print(f"[*] Not logged in. Complete the login (incl. 2FA / any captcha) in the open window.")
            print(f"    The browser is routed via {proxy_label or _redact(proxy_url)} — OF will see this IP.")
            print(f"    Captchas from FRESH profiles are normal — subsequent captures with the same")
            print(f"    proxy + account will reuse this profile and usually skip the challenge.")
            print(f"    Polling cookies every {LOGIN_POLL_INTERVAL_S}s, up to {LOGIN_TIMEOUT_S}s.")
            uid = _wait_for_login(context)
        print(f"[*] Logged in — auth_id={uid}")

        print(f"[*] Navigating to {OF_CHATS} to fire signed XHRs")
        try:
            page.goto(OF_CHATS, wait_until="networkidle", timeout=NETWORKIDLE_TIMEOUT_MS)
        except PWTimeout:
            print("[!] networkidle timed out — continuing, hooks likely already fired")

        print(f"[*] Waiting {HOOK_WAIT_S}s for webpack hooks to accumulate samples...")
        time.sleep(HOOK_WAIT_S)

        dump, chunk_code = harvest(page, context, proxy_url=proxy_url, proxy_label=proxy_label)
        session_path = write_session(dump, chunk_code)

        # Close the persistent context — Playwright shuts down the
        # underlying browser process automatically when launched via
        # launch_persistent_context(). The on-disk profile dir survives so
        # the next capture inherits cookies + the captcha-solved state.
        context.close()
        return session_path


def _redact(url: str) -> str:
    """Strip user:pass from a proxy URL for safe-to-log session metadata."""
    u = urlparse(url)
    return f"{u.scheme}://{u.hostname}:{u.port}" if u.hostname else url


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: capture_session_proxy.py <proxy_url> [proxy_label]")
        sys.exit(1)
    try:
        capture(sys.argv[1], proxy_label=sys.argv[2] if len(sys.argv) > 2 else None)
    except KeyboardInterrupt:
        print("\n[!] Interrupted by user")
        sys.exit(130)
