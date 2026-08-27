"""Admission lanes — the relay's bounded-concurrency primitive, stated once.

A "lane" is a named ceiling on how many of one KIND of expensive thing may be
in flight in this process at a time, plus the counters that make the ceiling
observable. Three of them existed as three hand-rolled copies of the same five
parts (a cap constant, a wait constant, a semaphore, a rejection counter, and an
acquire-log-reject block); this module is those five parts written once.

WHY THIS EXISTS AT ALL. Descriptors, threads and cores are PROCESS resources.
When the relay ran out of descriptors on 2026-08-08 the errors did not surface
in the vault pane that caused it — sqlite started answering "unable to open
database file", and seven OTHER accounts' chat reads and admin pages failed.
Anything that can be started N-at-a-time by a person scrolling therefore needs a
ceiling, and a ceiling nobody can read the current value of is a ceiling nobody
maintains — hence `stats()`.

TWO TYPES, HONESTLY NAMED, NOT ONE THAT LIES. `Lane` guards SYNC call sites and
parks waiters on a THREAD (`/img` is a plain `def` streaming endpoint;
`vault_frames.warm_one_sync` runs inside `asyncio.to_thread`). `LoopLane` guards
async ones and parks waiters on the EVENT LOOP. They are not one class with a
flag, because an asyncio semaphore binds to a running loop and a threading one
does not, and what a waiter COSTS is the whole reason each exists — a `Lane`
waiter is holding a thread hostage, so it gives up after `wait_s`; a `LoopLane`
waiter holds nothing, so it simply queues.

`vault_stills._MAX_INFLIGHT_FETCHES` and `vault_ai_api._VISION_GLOBAL_CONCURRENCY`
are still hand-rolled copies of `LoopLane` in their own modules; they belong
here, and moving them is the obvious follow-up.

This module imports nothing from the relay. It is a leaf on purpose, so
`vault_frames` can take a lane at module scope instead of reaching through
`server` at call time — `vault_frames` only imports `server` lazily to dodge an
import cycle, and every name it pulls through that hole is a reason the hole
stays open.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import weakref
from typing import Any

# The only import, and it is another leaf (it pulls in nothing from the relay
# either). Lanes are built at MODULE IMPORT, before the loop or the DB exist,
# so whatever feeds them has to be importable and readable synchronously here.
import lane_config

log = logging.getLogger("of-relay.admission")


class Lane:
    """A named ceiling on concurrent work, with the counters to see it working.

    `enter()` returns True if a slot was taken and False if the wait expired.
    False means "not now", never "not possible" — callers must map it to
    something retryable. A caller that took a slot MUST pair it with `exit()` in
    a `finally`, and must not call `exit()` otherwise.
    """

    def __init__(self, name: str, env_var: str, default_cap: int,
                 wait_s: float) -> None:
        self.name = name
        # Env-overridable so a cap can be retuned on the VPS without a deploy —
        # these numbers are sized against a 2-core box and the right value is an
        # operational question, not a source-code one.
        # Kept so `describe()` can name the tuning surface without a second
        # table that could drift from the constructor call.
        self.env_var = env_var
        # env var > value saved from the Concurrency panel > this default.
        # See lane_config's docstring for why the env var wins.
        self.cap = lane_config.resolve(env_var, default_cap)
        self.wait_s = wait_s
        # BOUNDED, so that releasing a slot nobody holds raises instead of
        # silently inflating the ceiling. See `exit`.
        self._sem = threading.BoundedSemaphore(self.cap)
        self._lock = threading.Lock()
        self.busy = 0
        self.rejected = 0

    def enter(self, label: str = "") -> bool:
        if not self._sem.acquire(timeout=self.wait_s):
            with self._lock:
                self.rejected += 1
            log.warning("%s lane full: waited %ss, %s already running (%s)",
                        self.name, self.wait_s, self.cap, label or "-")
            return False
        with self._lock:
            self.busy += 1
        return True

    def exit(self) -> None:
        """Hand back a slot taken by `enter`.

        An over-release raises `ValueError` from the bounded semaphore and is
        NOT swallowed: it is the one signal that an enter/exit pair has come
        apart, and a lane that quietly grows past its cap is worse than the
        crash that tells you about it. `busy` is decremented only on a release
        that actually happened, so the gauge cannot drift below the truth.
        """
        self._sem.release()
        with self._lock:
            self.busy = max(0, self.busy - 1)

    def free(self) -> int:
        """Slots currently available. For tests and diagnostics — never for
        deciding whether to `enter()` (that would be a race)."""
        return max(0, self.cap - self.busy)

    def stats(self) -> dict[str, int]:
        """This lane's numbers, keyed for the /admin/streams payload. Derived
        rather than restated, so a new lane cannot be added and then forgotten
        in the diagnostics that would have shown it saturating."""
        return {f"{self.name}_cap": self.cap,
                f"{self.name}_busy": self.busy,
                f"{self.name}_rejected": self.rejected}


class LoopLane:
    """`Lane`'s asyncio peer: a named ceiling whose waiters park on the event
    loop instead of on a thread.

    A separate class rather than a flag on `Lane`, for the reason the module
    docstring gives: what a waiter COSTS is different, and one class covering
    both would have to hide that. It lives here rather than at a call site
    because the same ten lines had already been hand-rolled three times (the
    by-id vault read, `vault_stills._SLOTS`, `vault_ai_api._vision_sema`) —
    which is exactly the situation that produced `Lane` in the first place.

    Two things follow from parking a coroutine rather than a thread:

    • It never rejects. `Lane.enter()` gives up after `wait_s` because a queued
      caller there is holding a thread hostage while it waits. A queued caller
      here holds nothing, so queueing is simply the right answer and a timeout
      would only invent a failure mode the callers would have to handle.
    • The semaphore is PER RUNNING LOOP. An `asyncio.Semaphore` that has parked
      a waiter belongs to the loop it parked it on, and the test harness gives
      every case its own `asyncio.run()`. Weak keys, so a finished loop takes
      its semaphore with it.

    A slot is held for the whole `async with` body — including a blocking call
    dispatched to a thread inside it. That is the point: the ceiling has to
    outlive any cancellation of the awaiting task, because cancelling a task
    does not stop the thread it is waiting on.
    """

    def __init__(self, name: str, env_var: str, default_cap: int) -> None:
        self.name = name
        # Kept so `describe()` can name the tuning surface without a second
        # table that could drift from the constructor call.
        self.env_var = env_var
        self.cap = lane_config.resolve(env_var, default_cap)
        self._sems: "weakref.WeakKeyDictionary[Any, asyncio.Semaphore]" = weakref.WeakKeyDictionary()
        # `busy` may be touched from more than one loop thread, and the module
        # docstring's rule is that a ceiling nobody can read is a ceiling nobody
        # maintains — so it is counted, and counted safely.
        self._lock = threading.Lock()
        self.busy = 0
        self.peak = 0

    def _semaphore(self) -> asyncio.Semaphore:
        # `cap` is read the first time a given loop needs a semaphore, so
        # changing it afterwards only reaches loops that have not started yet.
        # That is what makes it settable from a test (one loop per case) and
        # what makes it useless as a runtime knob (one loop for the process).
        loop = asyncio.get_running_loop()
        sem = self._sems.get(loop)
        if sem is None:
            sem = self._sems[loop] = asyncio.Semaphore(self.cap)
        return sem

    async def __aenter__(self) -> "LoopLane":
        await self._semaphore().acquire()
        with self._lock:
            self.busy += 1
            self.peak = max(self.peak, self.busy)
        return self

    async def __aexit__(self, *_exc: object) -> None:
        # Resolved by RUNNING loop, so this releases the same semaphore
        # `__aenter__` took — there is no per-entry state to carry.
        self._semaphore().release()
        with self._lock:
            self.busy = max(0, self.busy - 1)

    def stats(self) -> dict[str, int]:
        """This lane's numbers for /admin/streams. `peak` rather than
        `rejected`: nothing is ever turned away here, so the only question a
        reader can have is whether the ceiling is being reached at all."""
        return {f"{self.name}_cap": self.cap,
                f"{self.name}_busy": self.busy,
                f"{self.name}_peak": self.peak}


# ── The lanes themselves ────────────────────────────────────────────
#
# Instances live HERE, not in the module that happens to have the busiest call
# site, because they describe resources of the PROCESS and more than one module
# needs them. `vault_frames` in particular imports `server` lazily inside a
# function purely to dodge an import cycle; every name it pulls through that
# hole is a reason the hole stays open, and a lane is a name it can now get from
# a leaf instead.

# /img requests carrying a Range header — a browser <video> seeking. Sized for
# "watching one clip + one preloaded sibling + headroom for the overlap when
# switching tiles". A rejection is cheap here: the <video> element reissues
# Range requests natively, so a transient 503 costs a few hundred ms.
VIDEO_STREAM = Lane("video_stream", "VIDEO_STREAM_CONCURRENCY", 6, 2.0)

# /img cache MISSES. Hits never reach this lane — a hit is a FileResponse off
# disk, and queueing it would be a latency regression for a warm pane with no
# safety benefit. A miss is a different animal: a descriptor, a thread and an OF
# round-trip, and on a cold store EVERY tile is one. That is what emptied the
# process's descriptor table on 2026-08-08 — 48 errors in two one-second bursts,
# after which seven OTHER accounts' chat reads and admin pages failed too.
#
# ⚠️ This lane REVERSES a deliberate earlier exemption. Thumbnails used to
# bypass the Range cap entirely, on the stated grounds that "the grid stays
# snappy even when the cap is saturated". That reasoning was right about LATENCY
# and wrong about SURVIVAL: an unbounded grid is what killed the process. The
# cost of reversing it is real and worth naming — a request with NO Range header
# may still be a whole-file mp4, and `server._MAX_STREAM_SEC` lets one hold its
# slot for 60s, so slow whole-file fetches and fast thumbnails now share a lane
# and can head-of-line block each other.
#
# The 8/15s pair is sized against that worst case rather than picked. 8 sits
# above the two neighbouring vault caps (6) because this lane also carries chat
# bubbles and avatars a person is WATCHING, not just grid tiles. 15s is far
# longer than a thumbnail could need but short enough to fail before a browser
# gives up on its own — a late thumbnail still paints, whereas a 503 is a broken
# image the browser will not retry. If `img_fetch_rejected` ever climbs while
# the cache is warm, the whole-file streams are the thing to look at, not the
# cap.
IMG_FETCH = Lane("img_fetch", "IMG_FETCH_CONCURRENCY", 8, 15.0)

# mp4 download + ffmpeg, for both build paths: a `/img/scrub` hover and
# `vault_frames.warm_one_sync` (the collect warm phase, and a DESCRIBE meeting a
# clip nobody warmed). The second is why this is not a scrub-only concern — a
# describe sweep runs `vault_ai_api._VISION_GLOBAL_CONCURRENCY` items at once and
# an un-warmed clip does its download and its ffmpeg INSIDE that vision slot, so
# eight cold clips meant eight ffmpegs.
#
# `server._storyboard_locks` is not a substitute: it stops the SAME video being
# built twice and says nothing about N different ones. Alone among these lanes
# the scarce resource is CPU rather than descriptors, and the box has 2 cores —
# so this is deliberately the tightest number here. Serving an already-built
# frame never touches it.
#
# A rejection here must be mapped to something RETRYABLE by the caller. It says
# the lane was full; it says nothing about the clip. `vault_frames._WARM_REASON`
# maps it to `fetch_failed` for exactly that reason — treating it as terminal is
# what once gave 1,777 merely-unsigned items a 7-day cooldown.
STORYBOARD_BUILD = Lane("storyboard_build", "STORYBOARD_BUILD_CONCURRENCY", 2, 20.0)


# ── The loop lanes ──────────────────────────────────────────────────
#
# Same law as above — the instances live here, not at their busiest call site.

# `/api/of/v2/vault/media/{id}` — the by-id read. `_proxy` already caps UPSTREAM
# calls per account, so OF never sees a storm; the problem is WHERE the waiting
# happens, because that cap is a threading semaphore acquired INSIDE
# `asyncio.to_thread`. The upstream is protected, the executor is not. And this
# is the route the UI asks for a whole library at a time: `useVaultMediaByIds`
# issues one request per stored id and PPVLibraryTab hands it every id there is,
# so 200 ids wanted 200 threads for 5 usable slots — which starves the
# automation lane sharing that executor (the 2026-07-04 "socket hang up") and
# holds 200 inbound sockets and their DB connections open while they wait.
# Gating on the ASYNC side, before the dispatch, makes a queued request cost a
# parked coroutine instead. Same default as VAULT_STILL_CONCURRENCY, separate
# budget: a vault pane storm must not spend the stills allowance or vice versa.
VAULT_MEDIA_READ = LoopLane("vault_media_read", "VAULT_MEDIA_CONCURRENCY", 6)

# `/health?all_accounts=1` — the /setup table's per-account OF probe. It calls
# `_load_client()` + `c.me()` DIRECTLY rather than through `_proxy`, so no
# priority lane stands between a fan-out and the shared proxy/executor/descriptor
# ceilings, and a per-account cap would be the wrong shape anyway: this spends
# one call on each of N accounts rather than N on one. Sized well below the vault
# lanes because it is an admin table nobody is watching — it must never be the
# reason a chat read waits.
HEALTH_PROBE = LoopLane("health_probe", "HEALTH_ALL_CONCURRENCY", 4)


# Every lane, so diagnostics can be DERIVED rather than restated. `admin_streams`
# merges `all_stats()`; adding a lane above and forgetting it here is the only
# way it can go unreported, which is one place to look instead of two files.
LANES = (VIDEO_STREAM, IMG_FETCH, STORYBOARD_BUILD)
LOOP_LANES = (VAULT_MEDIA_READ, HEALTH_PROBE)


def describe() -> list[dict[str, Any]]:
    """Every lane as a ROW, for the operator panel.

    `all_stats()` below flattens to prefixed keys because `/admin/streams`
    merges it into one counter bag; a reader that wants "the lanes" then has to
    reassemble them by string-splitting. This returns them already assembled,
    and it is derived from the same LANES/LOOP_LANES tuples, so a lane added
    there cannot be missing here.

    ⚠️ THE CAPS ARE NOW WRITABLE, reversing a deliberate earlier decision.
    The previous text here said `cap` was "deliberately NOT settable over HTTP.
    These ceilings are what stopped the 2026-08-08 descriptor exhaustion from
    recurring, and a control that can widen them is a control that can reproduce
    it." That reasoning is still TRUE and is the cost being accepted: the
    Concurrency panel can now widen any of these, and the panel's own door is
    unauthenticated by explicit choice (see `lane_config_api`, which serves the
    same resource twice: ungated at `/lanes-panel/config`, which no Next rewrite
    exposes, and gated at `/admin/lane-config` for the in-app card). What
    changed is the judgement, not the risk — the ceilings had never been retuned
    once because retuning meant editing an env file the deploy rsync excludes,
    which is the failure this module's own docstring names.

    Two things keep the reversal honest. A saved value applies at RESTART, so a
    bad number still costs a restart rather than an outage — the property the
    old text was protecting. And `lane_config.describe()` reports whether an env
    var is pinning a lane, so the panel cannot pretend to have changed something
    it did not.

    These rows deliberately stay OBSERVATIONAL — cap, busy, peak, rejected. Where
    a value came from and what it will be after a restart is `lane_config`'s
    view, served at `/lanes-panel/config`; restating it here would be two answers
    to one question, and the second one is the one that goes stale.
    """
    rows: list[dict[str, Any]] = []
    for lane in LANES:
        rows.append({
            "name": lane.name,
            "kind": "thread",
            "cap": lane.cap,
            "busy": lane.busy,
            "rejected": lane.rejected,
            "peak": None,
            # A thread waiter is holding a thread hostage, so this lane gives up
            # after `wait_s` and the caller gets a retryable 503.
            "wait_s": lane.wait_s,
            "env_var": lane.env_var,
        })
    for lane in LOOP_LANES:
        rows.append({
            "name": lane.name,
            "kind": "loop",
            "cap": lane.cap,
            "busy": lane.busy,
            # Nothing is ever turned away here — a coroutine waiter holds
            # nothing, so it queues. `peak` is the only question worth asking:
            # has the ceiling been reached at all?
            "rejected": None,
            "peak": lane.peak,
            "wait_s": None,
            "env_var": lane.env_var,
        })
    return rows


def all_stats() -> dict[str, int]:
    """Every lane's numbers, merged. Keys are prefixed by lane name, so they
    cannot collide."""
    out: dict[str, int] = {}
    for lane in (*LANES, *LOOP_LANES):
        out.update(lane.stats())
    return out
