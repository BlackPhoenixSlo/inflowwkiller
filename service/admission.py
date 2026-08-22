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

THREADING, NOT ASYNCIO, AND THAT IS THE POINT. These lanes guard SYNC call sites
(`/img` is a plain `def` streaming endpoint; `vault_frames.warm_one_sync` runs
inside `asyncio.to_thread`). The relay also has asyncio-side ceilings —
`server._MAX_INFLIGHT_VAULT_READS`, `vault_stills._MAX_INFLIGHT_FETCHES`,
`vault_ai_api._VISION_GLOBAL_CONCURRENCY` — which park waiters on the event loop
instead of on a thread. Those are deliberately NOT modelled here: an asyncio
semaphore binds to a running loop and a threading one does not, so one type
covering both would have to hide that difference, and the difference is the
whole reason each exists. Two mechanisms, honestly named, beats one that lies.

This module imports nothing from the relay. It is a leaf on purpose, so
`vault_frames` can take a lane at module scope instead of reaching through
`server` at call time — `vault_frames` only imports `server` lazily to dodge an
import cycle, and every name it pulls through that hole is a reason the hole
stays open.
"""
from __future__ import annotations

import logging
import os
import threading

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
        self.cap = max(1, int(os.environ.get(env_var, str(default_cap))))
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


# Every lane, so diagnostics can be DERIVED rather than restated. `admin_streams`
# merges `all_stats()`; adding a lane above and forgetting it here is the only
# way it can go unreported, which is one place to look instead of two files.
LANES = (VIDEO_STREAM, IMG_FETCH, STORYBOARD_BUILD)


def all_stats() -> dict[str, int]:
    """Every lane's numbers, merged. Keys are prefixed by lane name, so they
    cannot collide."""
    out: dict[str, int] = {}
    for lane in LANES:
        out.update(lane.stats())
    return out
