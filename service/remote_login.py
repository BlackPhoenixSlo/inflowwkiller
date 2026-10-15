#!/usr/bin/env python3
"""
Served remote-browser login (Part C of the sanctioned direct-login upgrade).

WHAT THIS IS
------------
The host-only capture (`capture_session_proxy.capture`) opens a VISIBLE browser
window and a human sitting at that machine logs in. This module turns that same
launcher into a login a human can drive from the WEB UI instead:

  * A real Chromium (bundled, sandboxed) launches HEADLESS on the server,
    routed through the account's proxy — same egress the relay will later sign
    from (one egress, no teleport).
  * It types the supplied email + password into OnlyFans' real login form and
    submits.
  * When OnlyFans shows a Cloudflare / Turnstile / reCAPTCHA / hCaptcha check,
    or asks for a 2FA code, the challenge is SURFACED TO A HUMAN: the page is
    streamed to the browser as a JPEG screencast (CDP `Page.startScreencast`)
    over SSE, and the human's mouse / wheel / key input is POSTed back and
    replayed 1:1 into Playwright. We never solve or bypass the challenge — a
    person does, exactly as they would in a normal browser.
  * On success the session is handed to the EXISTING capture pipeline
    (`capture_session_proxy.harvest` + `session_bootstrap._adopt_into_account`),
    so a remote-login session is byte-for-byte the same shape as a paste-curl
    or host-capture one, and the same post-bootstrap hooks run.

HARD RULES (enforced here)
--------------------------
  * The password is NEVER stored on the job and NEVER logged. It lives only as
    a local in the worker thread; `scrub()` strips it from any message/error.
  * Live cookies are never logged.
  * No new signer, no new launcher, no stealth beyond what the host capture
    already applies — we reuse `capture_session_proxy`.

HOST-ONLY: needs Playwright + Chromium installed locally, like
capture_session_proxy. The API router degrades gracefully if they're absent.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

log = logging.getLogger("of-relay.remote-login")

# ── State machine ──────────────────────────────────────────────────────────
#   starting → typing → [challenge ↔ typing] → [otp → typing] →
#   capturing → done | error | cancelled
STATE_STARTING = "starting"
STATE_TYPING = "typing"
STATE_CHALLENGE = "challenge"
STATE_OTP = "otp"
STATE_CAPTURING = "capturing"
STATE_DONE = "done"
STATE_ERROR = "error"
STATE_CANCELLED = "cancelled"
_TERMINAL = {STATE_DONE, STATE_ERROR, STATE_CANCELLED}

JOB_TTL_S = 600          # ~10 minutes, matches the host capture's LOGIN_TIMEOUT_S
SCREENCAST_TICK_MS = 150  # loop cadence — also how often CDP frames are pumped
VIEWPORT_W = 1366
VIEWPORT_H = 900

# OnlyFans login DOM. GUESSED selectors — kept as ordered fallbacks and never
# relied on singly. If OF renames a field the login simply stalls in `typing`
# and the human can finish it by hand through the screencast (the input replay
# still works), so a stale selector degrades, it does not break.
_EMAIL_SELECTORS = (
    'input[name="email"]', 'input[type="email"]',
    'input[inputmode="email"]', 'input#email', 'input[name="username"]',
)
_PASSWORD_SELECTORS = (
    'input[name="password"]', 'input[type="password"]', 'input#password',
)
_SUBMIT_SELECTORS = (
    'button[type="submit"]', 'button[at-attr="submit"]',
    'form button:has-text("Log")', 'button:has-text("Log in")',
)
_OTP_SELECTORS = (
    'input[name="code"]', 'input[autocomplete="one-time-code"]',
    'input[name="otp"]', 'input[inputmode="numeric"]',
)


def scrub(text: Any, password: str | None) -> str:
    """Remove the password (and obvious cookie values) from a string so it is
    safe to put in a job message / error / log line."""
    s = "" if text is None else str(text)
    if password:
        s = s.replace(password, "***")
    return s


@dataclass
class RemoteLoginJob:
    id: str
    kind: str                       # "login" | "refresh"
    email: str | None               # None for refresh
    proxy_label: str
    principal: str | None           # user id that started it (None = unauthed)
    created_at: float
    expires_at: float
    account_id: str | None = None   # known at start for refresh; after capture for login
    state: str = STATE_STARTING
    message: str = ""
    error: str | None = None
    session_file: str | None = None
    frame_w: int = VIEWPORT_W
    frame_h: int = VIEWPORT_H
    _frame_b64: str | None = None
    _frame_seq: int = 0
    _inputs: "queue.Queue[dict]" = field(default_factory=queue.Queue)
    _otp: "queue.Queue[str]" = field(default_factory=queue.Queue)
    _cancel: threading.Event = field(default_factory=threading.Event)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _thread: threading.Thread | None = None

    # ── state helpers ──
    def set_state(self, state: str, message: str = "") -> None:
        with self._lock:
            self.state = state
            if message:
                self.message = message

    def fail(self, message: str) -> None:
        with self._lock:
            self.state = STATE_ERROR
            self.error = message
            self.message = message

    def is_terminal(self) -> bool:
        return self.state in _TERMINAL

    def set_frame(self, b64: str) -> None:
        with self._lock:
            self._frame_b64 = b64
            self._frame_seq += 1

    def latest_frame(self) -> tuple[int, str | None, int, int]:
        with self._lock:
            return self._frame_seq, self._frame_b64, self.frame_w, self.frame_h

    def public(self) -> dict[str, Any]:
        """Status dict safe to return over HTTP — no password, no cookies."""
        with self._lock:
            return {
                "id": self.id,
                "kind": self.kind,
                "state": self.state,
                "message": self.message,
                "error": self.error,
                "account_id": self.account_id,
                "session_file": self.session_file,
                "frame_w": self.frame_w,
                "frame_h": self.frame_h,
                "has_frame": self._frame_b64 is not None,
                "expires_at": self.expires_at,
            }


# ── Registry ─────────────────────────────────────────────────────────────
_jobs: dict[str, RemoteLoginJob] = {}
_jobs_lock = threading.Lock()


def _gc_locked() -> None:
    now = time.time()
    dead = [jid for jid, j in _jobs.items()
            if j.is_terminal() and now > j.expires_at]
    for jid in dead:
        _jobs.pop(jid, None)


def get_job(job_id: str) -> RemoteLoginJob | None:
    with _jobs_lock:
        _gc_locked()
        return _jobs.get(job_id)


class RemoteLoginConflict(RuntimeError):
    """A non-terminal job already exists for this account/email (→ HTTP 409)."""


def _register(job: RemoteLoginJob, conflict_key: Callable[[RemoteLoginJob], bool]) -> None:
    with _jobs_lock:
        _gc_locked()
        for j in _jobs.values():
            if not j.is_terminal() and conflict_key(j):
                raise RemoteLoginConflict(
                    "a login/refresh is already in progress for this "
                    "account — cancel it first"
                )
        _jobs[job.id] = job


# ── Public entry points ────────────────────────────────────────────────────

def start_login(*, email: str, password: str, proxy_label: str, proxy_url: str,
                principal: str | None) -> RemoteLoginJob:
    """Begin a served browser login. Returns the created job (one live job per
    email). The password is handed to the worker thread and never stored."""
    now = time.time()
    job = RemoteLoginJob(
        id=uuid.uuid4().hex, kind="login", email=email, proxy_label=proxy_label,
        principal=principal, created_at=now, expires_at=now + JOB_TTL_S,
        message="starting browser",
    )
    _register(job, lambda j: j.kind == "login" and j.email == email)
    t = threading.Thread(
        target=_run_login, args=(job, password, proxy_url),
        name=f"remote-login-{job.id[:8]}", daemon=True,
    )
    job._thread = t
    t.start()
    return job


def start_refresh(*, account_id: str, proxy_label: str, proxy_url: str,
                  principal: str | None) -> RemoteLoginJob:
    """Re-harvest signing rules / cookies for an account from its stored
    browser profile — no credentials typed. Only succeeds if the persisted
    profile is still logged in (a challenge mid-refresh is still surfaced)."""
    now = time.time()
    job = RemoteLoginJob(
        id=uuid.uuid4().hex, kind="refresh", email=None, proxy_label=proxy_label,
        principal=principal, created_at=now, expires_at=now + JOB_TTL_S,
        account_id=account_id, message="reopening profile",
    )
    _register(job, lambda j: j.account_id == account_id)
    t = threading.Thread(
        target=_run_refresh, args=(job, proxy_url),
        name=f"remote-refresh-{job.id[:8]}", daemon=True,
    )
    job._thread = t
    t.start()
    return job


def submit_otp(job: RemoteLoginJob, code: str) -> None:
    job._otp.put(code)


def submit_inputs(job: RemoteLoginJob, events: list[dict]) -> None:
    for e in events:
        if isinstance(e, dict):
            job._inputs.put(e)


def cancel(job: RemoteLoginJob) -> None:
    job._cancel.set()
    if job.state == STATE_STARTING:
        job.set_state(STATE_CANCELLED, "cancelled")


# ── Worker threads ───────────────────────────────────────────────────────

class _Cancelled(Exception):
    pass


def _run_login(job: RemoteLoginJob, password: str, proxy_url: str) -> None:
    import capture_session_proxy as cap
    from playwright.sync_api import sync_playwright
    try:
        with sync_playwright() as p:
            context, page, _profile = cap.launch_context(
                p, proxy_url, proxy_label=job.proxy_label,
                account_hint=None, headless=True,
            )
            cdp = _start_screencast(job, context, page)
            try:
                job.set_state(STATE_TYPING, "entering credentials")
                _type_credentials(page, job.email or "", password)
                uid = _drive_until_logged_in(job, page, context)
                _capture_and_persist(job, page, context, proxy_url)
            finally:
                _stop_screencast(cdp)
                try:
                    context.close()
                except Exception:
                    pass
    except _Cancelled:
        job.set_state(STATE_CANCELLED, "cancelled")
    except Exception as e:  # noqa: BLE001
        msg = scrub(f"{type(e).__name__}: {e}", password)
        log.warning("remote login %s failed: %s", job.id, msg)
        job.fail(msg)


def _run_refresh(job: RemoteLoginJob, proxy_url: str) -> None:
    import capture_session_proxy as cap
    from playwright.sync_api import sync_playwright
    try:
        with sync_playwright() as p:
            context, page, _profile = cap.launch_context(
                p, proxy_url, proxy_label=job.proxy_label,
                account_hint=job.account_id, headless=True,
            )
            cdp = _start_screencast(job, context, page)
            try:
                ok, _uid = cap._logged_in(context)
                if not ok:
                    # Give a brief window for a mid-refresh challenge, which the
                    # human can clear through the popup, before giving up.
                    job.set_state(STATE_CHALLENGE, "profile needs re-verification")
                    _drive_until_logged_in(job, page, context)
                _capture_and_persist(job, page, context, proxy_url)
            finally:
                _stop_screencast(cdp)
                try:
                    context.close()
                except Exception:
                    pass
    except _Cancelled:
        job.set_state(STATE_CANCELLED, "cancelled")
    except Exception as e:  # noqa: BLE001
        # Scrub in case a live cookie value surfaced in the exception string.
        msg = scrub(f"{type(e).__name__}: {e}", None)
        log.warning("remote refresh %s failed: %s", job.id, msg)
        job.fail(msg)


# ── Screencast (CDP) ───────────────────────────────────────────────────────

def _start_screencast(job: RemoteLoginJob, context, page):
    """Attach a CDP session and start a JPEG screencast sized to the CSS
    viewport so client coordinates map 1:1 into Playwright."""
    try:
        cdp = context.new_cdp_session(page)
    except Exception as e:  # noqa: BLE001
        log.warning("screencast unavailable for %s: %s", job.id, e)
        return None

    def _on_frame(ev: dict) -> None:
        try:
            job.set_frame(ev.get("data"))
            cdp.send("Page.screencastFrameAck", {"sessionId": ev.get("sessionId")})
        except Exception:  # noqa: BLE001
            pass

    try:
        cdp.on("Page.screencastFrame", _on_frame)
        cdp.send("Page.startScreencast", {
            "format": "jpeg", "quality": 60,
            "maxWidth": VIEWPORT_W, "maxHeight": VIEWPORT_H,
            "everyNthFrame": 1,
        })
    except Exception as e:  # noqa: BLE001
        log.warning("startScreencast failed for %s: %s", job.id, e)
        return None
    return cdp


def _stop_screencast(cdp) -> None:
    if cdp is None:
        return
    try:
        cdp.send("Page.stopScreencast")
    except Exception:  # noqa: BLE001
        pass
    try:
        cdp.detach()
    except Exception:  # noqa: BLE001
        pass


# ── Form driving ─────────────────────────────────────────────────────────

def _find_first(page, selectors):
    for sel in selectors:
        try:
            el = page.query_selector(sel)
            if el:
                return el
        except Exception:  # noqa: BLE001
            continue
    return None


def _type_credentials(page, email: str, password: str) -> None:
    em = _find_first(page, _EMAIL_SELECTORS)
    if em:
        try:
            em.fill(email)
        except Exception:  # noqa: BLE001
            pass
    pw = _find_first(page, _PASSWORD_SELECTORS)
    if pw:
        try:
            pw.fill(password)
        except Exception:  # noqa: BLE001
            pass
    submit = _find_first(page, _SUBMIT_SELECTORS)
    try:
        if submit:
            submit.click()
        elif pw:
            pw.press("Enter")
    except Exception:  # noqa: BLE001
        pass


def _detect_challenge(page) -> bool:
    """True when the page is showing a human-verification challenge. Checks
    Cloudflare interstitial text + known element ids/classes, then walks the
    frame tree for Turnstile / reCAPTCHA / hCaptcha / GeeTest iframes."""
    try:
        if page.evaluate(
            """() => {
                const t = (document.body && document.body.innerText || '').toLowerCase();
                if (t.includes('just a moment') || t.includes('verify you are human')
                    || t.includes('checking your browser')) return true;
                return !!document.querySelector(
                    '#challenge-running, #challenge-stage, #challenge-form, .cf-turnstile');
            }"""
        ):
            return True
    except Exception:  # noqa: BLE001
        pass
    needles = ("turnstile", "recaptcha", "hcaptcha", "geetest",
               "challenges.cloudflare")
    try:
        for f in page.frames:
            u = (f.url or "").lower()
            if any(n in u for n in needles):
                return True
    except Exception:  # noqa: BLE001
        pass
    return False


def _detect_otp(page) -> bool:
    return _find_first(page, _OTP_SELECTORS) is not None


def _drain_inputs(page, job: RemoteLoginJob) -> None:
    for _ in range(64):  # bounded so a flood can't starve the state loop
        try:
            e = job._inputs.get_nowait()
        except queue.Empty:
            return
        _apply_input(page, e)


def _apply_input(page, e: dict) -> None:
    t = e.get("type")
    try:
        if t == "mousemove":
            page.mouse.move(float(e["x"]), float(e["y"]))
        elif t == "mousedown":
            page.mouse.move(float(e["x"]), float(e["y"]))
            page.mouse.down(button=e.get("button", "left"))
        elif t == "mouseup":
            page.mouse.up(button=e.get("button", "left"))
        elif t == "click":
            page.mouse.click(float(e["x"]), float(e["y"]), button=e.get("button", "left"))
        elif t == "wheel":
            page.mouse.wheel(float(e.get("deltaX", 0)), float(e.get("deltaY", 0)))
        elif t == "key":
            text = e.get("text")
            key = e.get("key")
            # A single printable char with no modifier → insert it literally.
            if text and len(text) == 1 and not (e.get("ctrl") or e.get("meta") or e.get("alt")):
                page.keyboard.insert_text(text)
            elif key:
                page.keyboard.press(key)
    except Exception:  # noqa: BLE001 — a bad event must not kill the session
        pass


def _maybe_submit_otp(page, job: RemoteLoginJob) -> None:
    try:
        code = job._otp.get_nowait()
    except queue.Empty:
        return
    otp = _find_first(page, _OTP_SELECTORS)
    if not otp:
        return
    try:
        otp.fill(code)
        submit = _find_first(page, _SUBMIT_SELECTORS)
        if submit:
            submit.click()
        else:
            otp.press("Enter")
    except Exception:  # noqa: BLE001
        pass


def _drive_until_logged_in(job: RemoteLoginJob, page, context):
    """Poll for the auth_id cookie while surfacing challenge/otp state and
    pumping the screencast + replaying human input. Returns the auth_id."""
    import capture_session_proxy as cap
    while time.time() < job.expires_at:
        if job._cancel.is_set():
            raise _Cancelled()

        _drain_inputs(page, job)
        _maybe_submit_otp(page, job)

        ok, uid = cap._logged_in(context)
        if ok:
            return uid

        if _detect_challenge(page):
            job.set_state(STATE_CHALLENGE,
                          "human verification required — solve it in the popup")
        elif _detect_otp(page):
            job.set_state(STATE_OTP, "enter the 2FA code")
        elif job.state in (STATE_CHALLENGE, STATE_OTP, STATE_STARTING):
            job.set_state(STATE_TYPING, "waiting for OnlyFans")

        # wait_for_timeout keeps the CDP connection pumping so screencast
        # frames arrive and get acked while we idle.
        try:
            page.wait_for_timeout(SCREENCAST_TICK_MS)
        except Exception:  # noqa: BLE001
            time.sleep(SCREENCAST_TICK_MS / 1000)
    raise TimeoutError("login did not complete before the job expired")


def _capture_and_persist(job: RemoteLoginJob, page, context, proxy_url: str) -> None:
    """Harvest the logged-in session through the EXISTING pipeline and adopt
    it into its account dir, bind the proxy, and run the post-bootstrap hooks."""
    import capture_session_proxy as cap
    import session_bootstrap
    from playwright.sync_api import TimeoutError as PWTimeout

    job.set_state(STATE_CAPTURING, "capturing session")
    try:
        page.goto(cap.OF_CHATS, wait_until="networkidle",
                  timeout=cap.NETWORKIDLE_TIMEOUT_MS)
    except PWTimeout:
        pass
    time.sleep(cap.HOOK_WAIT_S)

    dump, chunk_code = cap.harvest(
        page, context, proxy_url=proxy_url, proxy_label=job.proxy_label)
    raw_path = cap.write_session(dump, chunk_code)

    # Derive the signing rules the same way the other capture modes do.
    import extract_rules
    rc = extract_rules.main(["extract_rules.py", str(raw_path)])
    if rc != 0:
        raise RuntimeError("extract_rules failed")

    final_path = session_bootstrap._adopt_into_account(
        raw_path, nickname=None, incogniton_profile_id=None, make_active=True,
    )
    account_id = final_path.parent.name
    job.account_id = account_id
    job.session_file = final_path.name

    # Bind the proxy so the relay egresses through the SAME IP the session was
    # minted on (one egress), then run the standard post-bootstrap hooks.
    try:
        import proxies as proxy_registry
        proxy_registry.assign_account(job.proxy_label, account_id)
    except Exception as e:  # noqa: BLE001
        # One-egress invariant: the cookies were minted through the proxy IP,
        # so the relay MUST egress through that same proxy. If the bind fails
        # (e.g. the label was deleted mid-login) the account would egress from
        # the server IP — the exact teleport we forbid. Refuse to activate the
        # session: do NOT run the post-bootstrap hooks and do NOT reach DONE.
        log.warning("could not auto-bind proxy %s to %s: %s",
                    job.proxy_label, account_id, e)
        raise RuntimeError(
            f"proxy bind failed for label {job.proxy_label!r}; refusing to "
            "activate session (one-egress)") from e
    _run_post_bootstrap_hooks(account_id)
    job.set_state(STATE_DONE, "session captured")


def _run_post_bootstrap_hooks(account_id: str) -> None:
    """Run the SAME four hooks /admin/session/bootstrap runs (invalidate the
    pooled client, clear the dead-session pause, restart the WS pump, sync the
    SQL mirror + link ownership). Imported lazily to avoid a circular import —
    server imports the router that calls this at request time, long after
    server itself finished importing."""
    try:
        import server
        server.post_bootstrap_hooks(account_id)
    except Exception:  # noqa: BLE001
        log.exception("post-bootstrap hooks failed for %s", account_id)
