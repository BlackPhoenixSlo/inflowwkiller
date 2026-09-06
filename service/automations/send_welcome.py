"""
service/automations/send_welcome.py — Automation A09: send_welcome.

Spec: library/one_section_of_automations/09_send_welcome.md.

TRIGGER-SOURCE VERDICT (verified 2026-06-04): the WS pump does NOT emit a
new-subscriber/subscription event into `event_inbox` in any consumable form.
`event_transcoder.transcode` only fully transcodes `api2_chat_message` and the
PPV-unlock family; EVERY other event (subscribes included) falls through to a
best-effort fan-touch + a one-shot "unknown shape" log — nothing an automation
can hang off. So this automation is SCHEDULED, not event-driven: an
`automation_rules` row with `trigger_json = {"every_seconds": 300}` enqueues a
job every ~5 min (the executor materializes it like any periodic rule); each run
polls OF's subscribe-notifications feed and welcomes every new subscriber not
already in `welcome_sent`.

It mirrors the scrape_chats reference in automation_executor.py:
  • of_client ONLY (no DOM), constructed via the executor's `_make_client` seam
    so tests inject a fake with no network.
  • its OWN AsyncSession per write.
  • the welcome itself is DETERMINISTIC — `_local_greeting` + `_activity_bubble`,
    no LLM, so a daily-cap trip can never cost a fan their welcome. persona /
    location / time-of-day come from `account_ai_config`. The single remaining
    llm_client.chat() call is the optional activity-bubble restyle (one per slot,
    cached); that call writes the `grok_calls` audit row and enforces the
    per-account daily cost cap atomically, so we don't re-implement either.
  • the existing optimistic send path: `of_client.send_message` →
    `attribution.write_outbound_attribution` (credits the system Automation
    employee, since no X-Employee-Id exists for a background run). The WS pump
    skips outbound, so this is the only producer of the outbound `messages` row.
  • dedup via `welcome_sent` (account_id, fan_id) — persisted, so a restart
    re-polls, sees the fan already welcomed, and skips: a sub yields EXACTLY one
    welcome. `welcome_sent` is written only AFTER a confirmed 200 send, so a send
    failure never marks a fan welcomed-without-a-welcome.
  • NOT-NEW gate: the `type=subscribed` feed includes RENEWALS / re-subs and
    `welcome_sent` has no row for fans pre-dating this automation, so an
    ESTABLISHED fan (even a whale mid-funnel) is skipped rather than re-welcomed
    as if brand new. A genuinely new sub may still carry a little history first
    (a mass blast → 1-2 outbound; their own opening DMs → a handful inbound), so
    the gate only trips once a fan EXCEEDS the tolerance: >`new_max_outbound`
    (default 2) outbound OR >`new_max_inbound` (default 8) inbound. Plus the
    cross-automation contact guard (`contact_guard_excludes`) so a fan another
    automation just touched isn't double-messaged. Both run on the notification
    path only — `test_fan` forces a send past them.
  • per-(account, fan) send-lease so A05/A06/A07/A11 can't double-message the
    same fan in an overlapping cycle; run_once's per-(account, kind) lock stops
    a slow tick stacking on the next.
  • FOLLOW-BACK (2026-09-06, payload `follow_back`, default ON): every fan who
    gets a welcome is also FOLLOWED back in the same tick, so OF fires a
    "started following you" push alongside the DM. It runs after the welcome is
    marked and its errors are swallowed — a follow that fails must never cost a
    fan their welcome. ⚠️ MONEY: by operator decision the paid-profile gate is
    OFF by default here (unlike auto_follow, which always gates), so a new sub
    who is themself a PRICED creator will CHARGE this account to follow back;
    set `follow_back_gate: true` to buy the profile check back (+1 read/sub).

SEND SHAPE (2026-07): the deterministic welcome goes out as TWO paced bubbles —
bubble 1 is the stutter greeting with the time-of-day image attached, bubble 2 is
the activity line AI-restyled into the creator's casual texting voice (verbatim
template on any LLM failure; V1's canned third line is retired). The restyle is
ONE cached LLM call per slot line shared across fans; a fan who already texted us
gets a fresh per-fan call (see _restyle_cache). An operator-written `question`
(payload) rides as an optional THIRD bubble, sent word-for-word — never restyled,
never near an LLM. An operator-picked `gif_id` rides as an optional FOURTH
bubble — a send carrying a top-level `giphyId` beside EMPTY text (the verified
wire shape, same as ai_chatter's cat stickers), so it is composed apart from the
text but sent as part of the SAME burst: one failure policy for all four.

Each text bubble is held with the live "...is typing" indicator
(webhook_config_json.typing_wpm / typing_indicator — same knobs as
welcome_chatter_for_info); the GIF is picked rather than typed, so it gets a flat
beat and no indicator. How LONG that hold is depends on `human_pace` below: with
it OFF (an explicit `false` on the rule, or any run under CHATTERLY_TEST_MODE)
the hold is the flat typing time, which is what this file did before pacing
existed; with it on — the production default — the hold comes from the sampler.

PACED SHAPE (2026-09-06, payload `human_pace`, ⚠️ default ON in production —
plans/welcome-pacing): with the knob on, each bubble's hold comes from
`pacing.welcome_burst_pace` instead of the flat typing time — a QUIET lead-in
with the indicator dark (she has not started typing yet), then the typing phase
— so the four bubbles land over a couple of minutes rather than inside one. A
paced burst is mostly sleeping and a run holds one of the executor's 4 global
slots for its WALL time, so the per-fan bodies then run CONCURRENTLY under a
semaphore with a wall-time admission budget; a fan not admitted has no
`welcome_sent` claim and the next tick re-serves him.

⚠️ ABSENT MEANS ON (operator decision, 2026-09-06 — see `_on_unless_off`). This
knob shipped absent-means-off behind the add-on fence and was then flipped, so an
existing welcome rule that has never been re-saved paces from the next tick.
EXCEPT under CHATTERLY_TEST_MODE, where absent still means OFF: the quiet draws
are not wpm-derived, so the test seams that zero typing time do not reach them,
and 84 pre-existing cases call run() with no payload at all. An explicit
`"human_pace": false` gives back exactly what this file did before pacing
existed — one fan at a time, one flat hold per bubble, no jitter, no budget,
nothing drawn — and that path is still pinned by
`case_flags_absent_every_hold_is_todays_flat_hold`.

STOP ON REPLY (2026-09-06, payload `stop_on_reply`, ⚠️ default ON in production
— same flip and same test-mode carve-out as `human_pace` above —
plans/welcome-pacing §C): a paced burst takes a couple of minutes, and a new
subscriber is the fastest replier there is, so the fan can answer bubble 1 while
bubbles 2-4 are still queued. With the knob on, the burst is checked at every
phase boundary it can honestly be checked at — the top of each bubble after the
greeting, after a quiet phase (before she starts typing), after the GIF's beat,
and once more after the LAST bubble of the burst — and stops the moment he has
said something. She still FINISHES the bubble
she was already typing: that is the operator's rule, and it is why the turn then
has to be handed back. When a finishing bubble lands ON TOP of his reply, this
sender enqueues a forced-turn job (`turn_handoff_ids`) for whichever chat engine
owns brand-new subs, scheduled past the welcome rest — without it `stop_on_reply`
would ship a girl who stops spamming and starts ignoring people, because both
engines gate on "the fan spoke last" and the finishing bubble moved that. The
whole thing is dead weight with an explicit `false`: no query, no split hold,
no job.

Payload knobs (all optional): `limit` (notifications fetched), `max_welcomes`
(per-run batch cap), `model` (LLM override), `dry_run` (generate but don't send),
`with_image` (attach a time-of-day bot-folder vault image, default True — same
picker as send_followup), `restyle` (AI-restyle the activity bubble, default
True; False sends the verbatim template line, zero LLM cost), `time_only`
(bubble 2 drops the activity and says ONLY the day/time-of-day/location —
"it's Thursday afternoon in US"; default False), `skip_time_bubble` (drop
bubble 2 ENTIRELY — greeting+image, then the question, then the GIF; outranks
`time_only` and any pin, and skips the restyle LLM call with it; default
False), `question` (an operator-written
question appended VERBATIM as a third bubble — no restyle, no LLM; blank/absent
= off), `gif_id` (a giphy id sent as a fourth, text-less bubble; blank/absent =
off), `human_pace` (pace the burst with real quiet gaps and welcome several fans
CONCURRENTLY — see `_PACE_CONCURRENCY` / `_RUN_WALL_BUDGET_S`; ⚠️ default ON,
off only on an explicit `false` or under CHATTERLY_TEST_MODE, and a `test_fan`
send is exempt so the UI's "send test" stays instant), the two
raw-JSON overrides `pace_open_quiet_s` / `pace_gap_quiet_s` ([lo, hi] seconds for
the quiet phases, clamped in the sampler), `stop_on_reply` (stop the burst as
soon as the fan says something back, and hand the turn to a chat engine when a
finishing bubble ate his reply; ⚠️ default ON, same carve-out as
`human_pace`), `test_fan` (+ `test_name`) to force one hardcoded recipient.

Returns a stats dict → automation_runs.stats_json.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timedelta
from random import Random
from collections.abc import Awaitable, Callable
from typing import Any, NamedTuple

from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

import automation_executor as ax  # _make_client / _parse_iso / fan-lease seams
import llm_client                  # call .chat at runtime so tests can patch it
from . import rhythm  # tz_hours_for — THE clock (fixed offset first, zone as fallback)
from attribution import write_outbound_attribution
from audiences import contact_guard_excludes, resolve_window_hours
from automation_registry import register
from ._common import (apply_word_restriction, hold_with_typing, load_voice_blocks,
                      load_hard_skip_ids, load_strip_emojis,
                      load_typing_indicator, load_typing_wpm, name_token,
                      resolve_fan_name, resolve_model, send_dropping_bad_media,
                      skip_unreachable_fan, strip_emojis, typing_delay_seconds)
from .pacing import (ROLE_GAP as _ROLE_GAP, ROLE_GIF as _ROLE_GIF,
                     ROLE_OPENER as _ROLE_OPENER, ROLE_TAIL as _ROLE_TAIL,
                     welcome_burst_pace)  # the burst's own sampler (§A1)
# The follow-notification cooldown ledger AND the follow itself, both shared with
# auto_follow so the two follow subsystems cannot double-notify one fan and
# cannot disagree about which profiles cost money. auto_follow imports nothing
# from here, so this direction is safe.
from .auto_follow import gated_follow, stamp_ping
from db.engine import get_session
from db.models import (AccountAiConfig, AutomationRule, Fan, FanProfile,
                       Message, WelcomeSent)
from llm_client import LLMCapExceeded
# The exact body a ledger-derived tip row carries, from the module that WRITES
# it. `stop_on_reply` has to recognise those rows to NOT stop on them (§C4), and
# a second hand-copied literal here is a cosmetic edit over there away from
# aborting every burst on a bare tip.
from tip_ledger import TIP_LEDGER_PREFIX


log = logging.getLogger("of-relay.automation.send_welcome")

_DEFAULT_NOTIF_LIMIT = 50      # how many subscribe-notifications to pull per tick
_DEFAULT_MAX_WELCOMES = 25     # batch cap per run (logged when it bites)
_GUARD_DEFAULT_H = 12.0        # cross-automation contact-guard window (payload override)
# How long a fan rests after his welcome lands, before the chat engine may answer.
#
# A new subscriber is the hottest lead there is and he replies FAST — measured
# 07-26, one answered 50 seconds after the welcome. He then waited ~14 minutes,
# because two brakes were running and the wrong one won: this sender set a
# deliberate 10-minute rest (`_FAN_COOLDOWN_S`) and then left its 15-minute fan
# LEASE lying around to expire (`_LEASE_TTL_S`), and the generic infra timeout
# silently outranked the policy. The lease is now handed back on a confirmed
# send, so this constant is the only thing pacing him — one brake, one number,
# and it is the one somebody chose.
_WELCOME_REST_S = 150          # 2.5 min — long enough not to talk over the welcome
# Beat before bubble 4. A GIF is PICKED, not typed, so it gets a flat pause and no
# "...is typing" frame — typing_delay_seconds would price it as if she typed it.
_GIF_HOLD_S = 3.0

# ── `human_pace` (plans/welcome-pacing §A) — ON unless the rule says `false` ──
# (…and OFF unless the rule says `true` under CHATTERLY_TEST_MODE. One seam for
# both readings: `_on_unless_off`, which carries the whole argument.)
#
# How many fans' bursts run AT ONCE. A paced burst is ~85-99s of mostly sleeping
# and a run holds one of the executor's 4 GLOBAL slots (`_MAX_CONCURRENT_RUNS`)
# for its WALL duration, so the binding quantity is wall pin, not the sum of the
# holds. 6 drains a 25-fan surge in about two runs instead of an hour.
_PACE_CONCURRENCY = 6
# ADMISSION budget, in wall seconds. Checked before a fan STARTS, never during a
# burst: an over-budget run stops admitting and the in-flight fans finish, so the
# overshoot is bounded by one burst. Deferred fans have no `welcome_sent` claim,
# so the next tick (default cadence 300s) re-serves them off the same feed.
_RUN_WALL_BUDGET_S = 300.0
# Each paced fan waits this long before its first bubble so N greetings do not
# fire in the same second. Drawn from its own seeded stream so it cannot shift
# the burst's own draws.
#
# ⚠️ KNOWN, AND NOT A CEILING ON THE POST RATE. This band is the ONLY thing
# spreading the greetings: bubble 0's quiet is drawn only when an image is
# ATTEMPTED (`welcome_burst_pace`, "opener"), so on a `with_image: False`
# account all `_PACE_CONCURRENCY` greetings fire inside these four seconds —
# roughly 2.5x the DM POST rate this sender has ever produced, on a path that
# routes through neither `of_write_paced`'s spacing nor `of_write_with_retry`.
# The plan decided that deliberately (§A3: the "Please allow 10 seconds" throttle
# is per-write, and this feature does not change the send path). Named here so
# the first paced day is watched with it in mind: if OF starts 429ing, widening
# this band is the one-line first move, and routing this sender through
# `of_write_paced` is the real fix — a decision with its own blast radius, not a
# constant to nudge.
_PACE_START_JITTER_S = (0.5, 4.0)
# `hold_with_typing` sleeps exactly what it is asked to; `_emit_typing_for` does
# NOT subtract the awaited emit from its budget, so ELAPSED = requested + emit
# overhead. Instrumented, not fixed (that fix moves real timing for all 16 call
# sites on every account with every flag off). Log a fan-level WARN past this.
#
# ⚠️ WHAT `hold_overrun_max_s` ACTUALLY MEASURES, because H8 keys the Fansly
# rollout off it: wall time around the hold, from a task that shares an event
# loop with up to `_PACE_CONCURRENCY` - 1 other bursts. That is emit overhead
# PLUS scheduling delay plus anything else blocking the loop in the same window.
# It is an UPPER BOUND on the emit overhead, never a measurement of it. Reading a
# 3s figure as "Fansly's typing REST is slow" when it was six tasks waking at
# once is the mistake this comment exists to prevent — compare a
# `_PACE_CONCURRENCY = 1` run first, and treat the gap between the two as the
# contention term.
_HOLD_OVERRUN_WARN_S = 2.0

# ── `stop_on_reply` (plans/welcome-pacing §C) ─────────────────────────────
#
# ON unless the rule says `false` (test mode inverted, exactly like `human_pace`
# above) — the switch itself is `_on_unless_off` below.
#
# ⚠️ Its "did he use WORDS" predicate is `_newest_worded_inbound`, and the second
# of its two SQL clauses excludes the body a LEDGER-derived tip row carries
# ("💸 Sent a $5.00 tip" — transaction_ingest writes it for a tip the 5-minute
# transaction poll discovered). Nobody typed that string; it is our own
# bookkeeping wearing an inbound row.
#
# `TIP_LEDGER_PREFIX` is IMPORTED, never re-typed. Two independent literals with
# no import between them is how a cosmetic edit to the writer turns every bare tip
# into "he used words" over here — every burst aborting on a bare tip, the exact
# behaviour the operator vetoed — with the suite still green. The engines ask the
# same question through `_common.inbound_is_words`, which imports the same
# constant for the same reason.
#
# It lives in the LEAF `service/tip_ledger.py` rather than in `transaction_ingest`
# beside its writer, because `_common` is the base of the automation tree and must
# read it too: importing an ingest orchestrator from there dragged the whole
# OF/Fansly client stack under every automation. See the leaf for the argument.


# ── The handoff job's delay ───────────────────────────────────────────────
#
# How long AFTER the welcome rest the handoff job is due. Both engines check
# `ax.fan_on_cooldown` in their send loops and this sender starts a
# `_WELCOME_REST_S` cooldown in its own `finally`, so a job due any earlier is a
# job that dies on our own brake. The pad also keeps V2's "don't talk over the
# welcome" promise and reads like a person: she notices his message a few
# minutes later, not in the same breath as the GIF.
_HANDOFF_DELAY_PAD_S = 15


@dataclass(slots=True)
class _Tally:
    """Every per-run counter, in one object.

    These used to be a wall of locals the send loop mutated in place. The per-fan
    body is now its own coroutine and several can be in flight at once, so the
    alternative is `nonlocal` on twenty names in two nested functions. They are
    mutated only on the event loop — single-threaded asyncio — so nothing here
    needs a lock.

    Every field is declared with its own TYPE and default. It used to be a
    `__slots__` tuple filled by a `setattr(self, name, 0)` loop plus three
    fix-ups afterwards, which meant three of the fields were `int` for one
    statement before becoming what they actually are — and a reader had to run
    the loop in their head to find out that two of these are booleans and one is
    a float."""

    sent: int = 0
    errors: int = 0
    skipped_locked: int = 0
    skipped_cooldown: int = 0
    image_attached: int = 0
    restyled: int = 0          # fresh LLM restyles this run
    restyled_cached: int = 0   # bubbles served from the per-slot restyle cache
    pinned_used: int = 0       # bubbles served from an operator-pinned line (no LLM)
    gifs_sent: int = 0         # bubble-4 GIFs that landed
    # Follow-back tallies. `followed_back` counts real notifications (fresh
    # follows + re-arms); the skip buckets only move when the gate is on.
    followed_back: int = 0
    follow_back_already: int = 0
    follow_back_paid_skipped: int = 0
    follow_back_no_price: int = 0
    follow_back_errors: int = 0
    deferred_budget: int = 0   # fans not admitted before the wall budget ran out
    # stop_on_reply (§C): bursts cut short by the fan speaking, and what we did
    # about the turn afterwards. `handoff_enqueued` only ever moves when a bubble
    # of OURS landed after his reply — the (b) half of the mix.
    aborted_on_reply: int = 0
    handoff_enqueued: int = 0
    handoff_no_engine: int = 0
    handoff_skipped_restricted: int = 0
    # Fans whose ANCHOR read failed, so the knob was switched off for them and
    # their burst ran with no reply guard at all. Without this the run reports
    # `stop_on_reply: true` and zero aborts, which reads as "nobody replied" —
    # the one thing it does not mean.
    reply_guard_off: int = 0
    # The daily-cap trip, twice: `cap_hit` is what the stats report, and
    # `restyle_capped` is the RUN-LOCAL latch that stops every fan left in the
    # run from re-asking for a restyle that will only be refused again.
    cap_hit: bool = False
    restyle_capped: bool = False
    # Worst (elapsed - requested) across this run's paced holds — §A5's
    # instrumentation, in seconds.
    hold_overrun_max_s: float = 0.0


# Follow-back: OF's own re-engagement lever, fired at the moment of subscribe.
# Following a fan sends them a "started following you" push, which lands in the
# same minute as the welcome DM and puts the creator's name in front of them
# twice. Default ON — a new sub is exactly who you want to follow back.
_FOLLOW_BACK_DEFAULT = True
# Whether to price-check each fan before following. OF's /subscribe is a PAYING
# endpoint: it is free only when the target's own subscribePrice is 0, so a new
# sub who is themself a paid creator would CHARGE this account to follow back.
# auto_follow always gates. Here the default is OFF by operator decision
# (2026-09-06) — follow everyone, spend one fewer read per sub, accept the risk.
# Flip the `follow_back_gate` knob to true to buy the gate back per rule.
_FOLLOW_BACK_GATE_DEFAULT = False

# First-letter → alliterative adjective for the stutter greeting. e.g. S → "Sexy
# Sofie". Drives every welcome now that the nameless riff is deterministic too.
_ADJS = {
    "A": "Adoring", "B": "Brave", "C": "Cute", "D": "Dreamy", "E": "Epic", "F": "Flirty",
    "G": "Gorgeous", "H": "Handsome", "I": "Incredible", "J": "Juicy", "K": "Kind", "L": "Lovely",
    "M": "Mighty", "N": "Naughty", "O": "Original", "P": "Playful", "Q": "Quick", "R": "Radiant",
    "S": "Sexy", "T": "Tasty", "U": "Unique", "V": "Vibrant", "W": "Wild", "X": "Xtra",
    "Y": "Yummy", "Z": "Zesty",
}

# The male table. These describe the FAN, who is male in both lanes — so this is
# not a pronoun fix. It is a REGISTER fix: "Yummy Mike" / "Juicy Mike" / "Cute
# Mike" is what SHE calls him, and a dom does not hand out those words. His
# vocabulary is what a man in charge notices about someone who just subscribed —
# eager, hungry, obedient, willing — which does the same alliterative job and
# lands the power dynamic in the first three words of the first message.
_ADJS_HIM = {
    "A": "Ambitious", "B": "Bold", "C": "Cocky", "D": "Devoted", "E": "Eager",
    "F": "Fearless", "G": "Game", "H": "Hungry", "I": "Impatient", "J": "Jumpy",
    "K": "Keen", "L": "Loyal", "M": "Mighty", "N": "Needy", "O": "Obedient",
    "P": "Patient", "Q": "Quick", "R": "Ready", "S": "Solid", "T": "Tough",
    "U": "Unruly", "V": "Vicious", "W": "Willing", "X": "Xtra", "Y": "Yearning",
    "Z": "Zealous",
}
_ADJ_DEFAULT = {"her": "Flirty", "him": "Eager"}
# What to call a subscriber whose handle yields no usable word at all.
_NAMELESS_GREET = {"her": "cutie", "him": "boy"}


def _adjs(voice: str) -> tuple[dict, str]:
    """(table, default) for this lane. Anything but "him" gets hers, unchanged."""
    if str(voice or "").strip().lower() == "him":
        return _ADJS_HIM, _ADJ_DEFAULT["him"]
    return _ADJS, _ADJ_DEFAULT["her"]


# ── New-subscriber parsing (defensive — OF notification shape varies) ──

def _find_user(item: dict) -> dict | None:
    """Pull the subscriber user-blob out of one notification item. OF nests it
    under a handful of keys depending on the feed/version, so we sniff several."""
    for key in ("user", "fromUser", "subscriber", "author"):
        u = item.get(key)
        if isinstance(u, dict) and u.get("id"):
            return u
    data = item.get("data")
    if isinstance(data, dict):
        for key in ("user", "relatedUser", "subscriber"):
            u = data.get(key)
            if isinstance(u, dict) and u.get("id"):
                return u
    return None


def _extract_new_subscribers(resp: object) -> list[dict]:
    """Notifications response → de-duped [{id, username, name}] in feed order."""
    if isinstance(resp, list):
        items = resp
    elif isinstance(resp, dict):
        items = resp.get("list") or resp.get("notifications") or resp.get("items") or []
    else:
        items = []
    out: list[dict] = []
    seen: set[int] = set()
    for it in items:
        if not isinstance(it, dict):
            continue
        # OF dropped the `type=` query filter (it now 400s), so the feed is mixed
        # (tips/comments/mentions/subscribes). Keep only subscribe events — items
        # with a subscribe-ish `type` (e.g. "subscribed"). Untyped items (test
        # fixtures / unknown shapes) pass through for back-compat.
        t = str(it.get("type") or "").lower()
        if t and "subscrib" not in t:
            continue
        user = _find_user(it)
        if not user:
            continue
        try:
            uid = int(user.get("id"))
        except (TypeError, ValueError):
            continue
        if uid in seen:
            continue
        seen.add(uid)
        out.append({"id": uid, "username": user.get("username"), "name": user.get("name")})
    return out


def _clock_hours(cfg: dict) -> float:
    """Her local offset in HOURS from a cfg dict carrying the RAW clock columns.

    The ONE place in this module that reads either clock column. Everything below
    takes hours and no longer cares which column answered — `rhythm.tz_hours_for`
    decides (the fixed offset, with a legacy IANA zone as the fallback), and a
    clockless account resolves to 0.0 == UTC, which is what a welcome has always
    used. A draft config from the Brain panel goes through here too, so a preview
    and a real send cannot disagree about the hour."""
    return rhythm.tz_hours_for(cfg.get("timezone"), cfg.get("utc_offset"))


def _model_hour(utc_offset: float | int | None) -> int:
    """Current hour in the model's timezone (utcnow + offset hours). Accepts
    fractional hours — a legacy IANA zone can still resolve to e.g. Kolkata's
    +5:30 (see `_clock_hours`)."""
    try:
        off = float(utc_offset)
    except (TypeError, ValueError):
        off = 0.0
    return (datetime.utcnow() + timedelta(hours=off)).hour


def _time_activity(hour: int, acts: dict) -> tuple[str, str]:
    """6-bucket time-of-day → (label, activity-string) per the spec mapping.
    Missing slots fall back to '' (spec edge case)."""
    if 5 <= hour < 9:
        return ("morning", acts.get("morning_1", ""))
    if 9 <= hour < 12:
        return ("morning", acts.get("morning_2", ""))
    if 12 <= hour < 15:
        return ("afternoon", acts.get("afternoon_1", ""))
    if 15 <= hour < 18:
        return ("afternoon", acts.get("afternoon_2", ""))
    if 18 <= hour < 21:
        return ("evening", acts.get("evening", ""))
    return ("night", acts.get("night", ""))


def _photo_index(hour: int) -> int:
    """5-9→0, 9-12→1, 12-15→2, 15-18→3, 18-21→4, else 5 (same 6-bucket mapping
    as send_followup — selects which bot-folder image to attach). Deterministic
    per hour."""
    if 5 <= hour < 9:
        return 0
    if 9 <= hour < 12:
        return 1
    if 12 <= hour < 15:
        return 2
    if 15 <= hour < 18:
        return 3
    if 18 <= hour < 21:
        return 4
    return 5


# The 6 time-of-day slot keys, ordered by _photo_index, matching time_activities.
_SLOT_KEYS = ("morning_1", "morning_2", "afternoon_1", "afternoon_2", "evening", "night")


def _slot_key(hour: int) -> str:
    """Slot name for the hour — same 6 buckets as _time_activity/_photo_index."""
    return _SLOT_KEYS[_photo_index(hour)]


# Representative hour inside each time-of-day bucket — the inverse of _slot_key. A
# preview can pin ANY slot (not just the creator's current one) by asking for that
# slot's representative hour. Each value sits strictly inside its _photo_index bucket
# so the round-trip holds: _slot_key(_slot_hour(k)) == k for every slot key.
_SLOT_REPR_HOUR = {
    "morning_1": 7, "morning_2": 10, "afternoon_1": 13,
    "afternoon_2": 16, "evening": 19, "night": 22,
}


def _slot_hour(slot: str | None) -> int | None:
    """Representative hour for a slot key, or None for an unknown/empty slot (caller
    then falls back to the creator's current local hour)."""
    return _SLOT_REPR_HOUR.get(slot or "")


def _pinned_line(cfg: dict, hour: int) -> str | None:
    """The operator-approved FIXED activity line for this slot (the Brain "pin"),
    or None if the slot isn't pinned. The stored weekday is swapped to today's so a
    daily welcome never shows a stale day. Everything else is sent exactly as pinned
    — no LLM restyle, deterministic. Robust to a missing/echoed weekday: an absent
    or already-current day is a no-op replace, so the line still sends cleanly."""
    pins = cfg.get("welcome_pins") or {}
    pin = pins.get(_slot_key(hour))
    if not isinstance(pin, dict):
        return None
    line = str(pin.get("line") or "").strip()
    if not line:
        return None
    old_wd = str(pin.get("weekday") or "").strip()
    cur_wd = _model_weekday(_clock_hours(cfg))
    if old_wd and old_wd.lower() != cur_wd.lower():
        # Preserve the casing the operator wrote — a lowercase "thursday" in a
        # casual line stays lowercase, ALL-CAPS stays ALL-CAPS — instead of forcing
        # strftime's Title-case and capitalising the day mid-sentence.
        def _sub(m: "re.Match") -> str:
            s = m.group(0)
            if s.isupper():
                return cur_wd.upper()
            if s.islower():
                return cur_wd.lower()
            return cur_wd  # Title / mixed → the canonical Title-case weekday
        line = re.sub(rf"\b{re.escape(old_wd)}\b", _sub, line, flags=re.IGNORECASE)
    return line


def _slot_image_id(cfg: dict, hour: int) -> int | None:
    """Configured per-slot vault image id for the current time of day, or None.
    `cfg['time_images']` is {slot_key: media_id}; takes precedence over the
    legacy folder picker so an account can pin one image per slot (set via the
    templates UI). Falls back to None when the slot is unset/non-numeric."""
    imgs = cfg.get("time_images") or {}
    val = imgs.get(_slot_key(hour))
    try:
        return int(val) if val is not None else None
    except (TypeError, ValueError):
        return None


# ── DB seams (own session each — house pattern) ───────────────────────

async def _load_ai_config(account_id: str) -> dict:
    """Detached snapshot of account_ai_config (or {} when absent)."""
    async with get_session() as s:
        cfg = await s.get(AccountAiConfig, account_id)
        if cfg is None:
            return {}
        acts: dict = {}
        if cfg.time_activities_json:
            try:
                acts = json.loads(cfg.time_activities_json) or {}
            except Exception:
                acts = {}
        imgs: dict = {}
        if cfg.time_images_json:
            try:
                imgs = json.loads(cfg.time_images_json) or {}
            except Exception:
                imgs = {}
        pins: dict = {}
        if getattr(cfg, "welcome_pinned_json", None):
            try:
                pins = json.loads(cfg.welcome_pinned_json) or {}
            except Exception:
                pins = {}
        # The two clock columns RAW, exactly as stored. This dict used to carry a
        # `utc_offset` that was already RESOLVED, which gave the key two meanings —
        # resolved here, raw in the draft the Brain panel posts — and a shallow
        # merge of the draft then silently replaced one with the other. Raw in,
        # `_clock_hours` out: the merge compares like with like, and exactly one
        # function knows how a row becomes an hour.
        return {
            "persona": cfg.persona,
            "utc_offset": cfg.utc_offset,
            "timezone": getattr(cfg, "timezone", None),
            "location": cfg.location,
            "time_activities": acts,
            "time_images": imgs,
            "welcome_pins": pins,
            "model": cfg.model,
        }




async def _load_welcomed(account_id: str) -> set[int]:
    async with get_session() as s:
        rows = (await s.execute(
            select(WelcomeSent.fan_id).where(WelcomeSent.account_id == str(account_id))
        )).all()
    return {int(r[0]) for r in rows}


async def _established_fan_ids(
    account_id: str, fan_ids: list[int], *, max_outbound: int, max_inbound: int
) -> set[int]:
    """Subset of `fan_ids` that look like an EXISTING relationship (so they must
    NOT be welcomed as if brand new), not a genuinely fresh subscriber.

    Why this gate exists: the OF subscribe-notifications feed (`type=subscribed`)
    carries RENEWALS and re-subscribes, not just first-time subs, and the
    `welcome_sent` ledger has no row for fans who pre-date this automation. So
    `welcome_sent`-only dedup re-welcomes established fans (even a long-tenure
    whale mid-funnel) the moment their sub renews / they reappear in the feed.

    A genuinely new sub can still carry a LITTLE history before the welcome tick,
    so we DON'T treat any message as disqualifying:
      • a mass blast may land first → a couple OUTBOUND rows (incl. the optimistic
        placeholders mass sends write), and
      • the fan may fire off a few opening messages → several INBOUND rows.
    We treat a fan as established only once they EXCEED a tolerance: more than
    `max_outbound` outbound OR more than `max_inbound` inbound messages. One
    grouped scan over the (≤`limit`) notification fans per tick.

    The scan itself is HOISTED to audience_include.established_fan_ids so the
    audience roster-diff auto-add applies the identical renewal tolerance; this
    stays as the module's local name so its call sites and tests don't move."""
    import audience_include
    return await audience_include.established_fan_ids(
        account_id, fan_ids, max_outbound=max_outbound, max_inbound=max_inbound)


async def _mark_welcomed(account_id: str, fan_id: int, username: str | None) -> None:
    """Idempotent welcome_sent claim — written only after a confirmed send."""
    async with get_session() as s:
        await s.execute(
            sqlite_insert(WelcomeSent)
            .values(
                account_id=str(account_id),
                fan_id=int(fan_id),
                fan_username=username,
                sent_at=datetime.utcnow(),
            )
            .on_conflict_do_nothing(index_elements=["account_id", "fan_id"])
        )


def _model_weekday(utc_offset) -> str:
    """Weekday name in the creator's timezone (utcnow + offset hours)."""
    try:
        off = float(utc_offset)
    except (TypeError, ValueError):
        off = 0.0
    return (datetime.utcnow() + timedelta(hours=off)).strftime("%A")


# Canonical name parser now lives in _common.name_token (shared by every sender so
# they all derive greet-names identically); kept as a module-local alias so the
# existing call sites below read unchanged.
_name_token = name_token


async def _resolve_welcome_name(account_id: str, fan_id: int, sub: dict) -> str:
    """Best real first name to greet by, generated from whatever we have — the guy's
    real name, a team-curated/AI nickname, or the OF display name (W4: "generate the
    nickname from info we have"). Returns '' when all we have is a random handle /
    number, so the caller falls back to the LLM riff. Brand-new subs usually have no
    Fan row → we fall through to the notification's display name.

    Precedence (CURATED beats the RAW OF name): the team relabels fans via
    `custom_nickname` ('Garrett/City/Tag') — that's what the whole UI shows — but a
    fan's raw OF account name may be something else entirely (e.g. 'Kyle'); otherwise
    a fan curated as 'Garrett' gets welcomed as 'Kyle'. That IS `resolve_fan_name`'s
    order now, so this hands the row to the shared resolver instead of keeping a
    second one — the two used to disagree on 154 fans, and welcome minted names the
    chat lane refused to say ('Sparky10' → "hey Sparky"). Only the two sources a
    WELCOME has ride along: the gen_info profile nickname (same shape as
    generated_nickname) and the live notification name (same shape as a display
    name, and dead last, so it fills in for a brand-new sub with no Fan row)."""
    async with get_session() as s:
        prof = (await s.execute(select(FanProfile.nickname).where(
            FanProfile.account_id == str(account_id),
            FanProfile.fan_id == int(fan_id)))).scalar_one_or_none()
        fan = (await s.execute(select(
            Fan.real_name, Fan.generated_nickname, Fan.custom_nickname,
            Fan.of_display_name, Fan.home_country, Fan.home_city
        ).where(Fan.account_id == str(account_id), Fan.fan_id == int(fan_id)))).first()
    row = dict(zip(("real_name", "generated_nickname", "custom_nickname",
                    "of_display_name", "home_country", "home_city"), fan or ()))
    row["generated_nickname"] = row.get("generated_nickname") or prof
    row["of_display_name"] = row.get("of_display_name") or sub.get("name")
    # resolve_fan_name may return a full display name ('garrett baydala'); a welcome
    # greets by the first token. name_token is idempotent on one already ('Garrett').
    return _name_token(resolve_fan_name(row))


# A welcome is always [greeting] + the slot's activity line. The two halves are
# INDEPENDENT: the greeting is the only fan-specific part, and the activity bubble
# is a pure function of (cfg, hour). Keeping them apart is what lets a fan we
# can't name take the LLM greeting and the SAME deterministic activity line as
# everyone else — so the pin and the restyle downstream need no special case.

def _local_greeting(name: str, voice: str = "her") -> str:
    """V1 'precious' bubble 1 (NO LLM): 'Hey S-S-S-Sexy Sofie ! !!' — the stutter
    prefix sits on the first letter and leads into the alliterative nickname. The
    vault image rides this bubble.

    This is the FIRST thing a new subscriber ever reads, so the adjective sets the
    register before anything else does — see `_ADJS_HIM`."""
    L = name[0].upper()
    table, default = _adjs(voice)
    return f"Hey {L}-{L}-{L}-{table.get(L, default)} {name} ! !!"


# Longest run of letters in a handle — 'xx_rider_92' → 'rider'. 3+ so a separator
# fragment ('xx', 'zz') never wins over the actual word.
_HANDLE_WORD = re.compile(r"[A-Za-z]{3,}")


def _greet_token(sub: dict, voice: str = "her") -> str:
    """What to greet a fan by when `_resolve_welcome_name` found no real name in any
    source — all that's left is the raw OF handle. Two shapes, resolved in code:

        xx_rider_92  → 'rider'          (the word inside the handle)
        u4471223     → 'cutie'          (a bare id carries no word at all)

    🚨 A bare id used to mint 'fan #4471223' here, and the caller is
    `_local_greeting`, which alliterates whatever it is handed AS A NAME. The
    first thing 59 real subscribers ever read — the most recent on 2026-08-21 —
    was "Hey F-F-F-Flirty fan #521599677 ! !!". The docstring's own rule ("a bare
    id is an id, not a name") was right and the branch contradicted it: an id is
    not a word, so it belongs on the nameless path with every other handle we
    cannot say out loud, not in a template built to say a name.

    This used to be an LLM call per nameless fan. It produced exactly this shape
    ('Hey B-B-B-Bold bigdaddy69 ! !!'), so it was paying per fan for a table lookup
    the named path already does for free — and a daily-cap trip cost those fans
    their welcome entirely. Deterministic means no cost, no cap, no variance."""
    target = (sub.get("name") or sub.get("username") or "").strip()
    words = _HANDLE_WORD.findall(target)
    if words:
        return max(words, key=len).lower()
    # Nothing usable in the handle at all. NOT `v.fan_address` — hers is "cutie"
    # here and "babe" there, and reaching for the bundle silently changed what 17
    # live accounts greet a nameless subscriber with. Its own dict, its own words.
    return _NAMELESS_GREET["him" if str(voice or "").strip().lower() == "him"
                           else "her"]


def _activity_bubble(cfg: dict, hour: int | None = None, *,
                     time_only: bool = False) -> list[str]:
    """Bubble 2 — 'just woke up and made myself a coffee... it's Friday morning in
    Vancouver, Canada' — as a 0-or-1 element list, empty when the account has no
    activity for this slot. Verbatim here; run() AI-restyles it into casual texting
    tone before sending (verbatim is the fallback when the LLM is capped/down).

    Carries NOTHING fan-specific by construction — identical for every fan in the
    same slot, which is exactly what lets `_restyle_cache` be keyed on the line
    alone and shared across the run.

    `hour` lets a preview pin an arbitrary slot; None → the creator's current local
    hour. The V1 third line ('Will reply when I am back :)') is retired — it read
    canned; two paced bubbles land more human.

    `time_only` drops the ACTIVITY half and keeps only the clock: "it's Thursday
    afternoon in US". Same two-bubble shape, a much shorter second one — an opener
    that states where she is and what time it is there, with no scene attached. It
    does NOT depend on `time_activities`, so it still produces a line for a slot the
    creator never filled in (the activity path returns [] there)."""
    off = _clock_hours(cfg)
    if hour is None:
        hour = _model_hour(off)
    tod, activity = _time_activity(hour, cfg.get("time_activities") or {})
    if not activity and not time_only:
        return []
    # `where` carries its own " in " so the line degrades cleanly to "...it's Friday
    # morning" on an account with no location set — a dangling "in" is the kind of
    # thing a fan reads as a broken bot.
    location = (cfg.get("location") or "").strip()
    where = f" in {location}" if location else ""
    clock = f"it's {_model_weekday(off)} {tod}{where}"
    return [clock] if time_only else [f"{activity}... {clock}"]


# The time-only bubble OPENS with "it's" — "it's monday morning in US". The
# template already does; the restyle is what drops it ("thursday afternoon, in the
# US", "Thursday night, US."). Asking the prompt for it is not enough — a sampled
# rewrite obeys most of the time, and "most" is a line a fan reads. So the opener is
# re-attached in code after every path (fresh restyle, cached restyle, verbatim
# fallback), and the prompt asks for it only so the model doesn't fight the shape.
_ITS_PREFIX = re.compile(r"^\s*(it\s*['’´`]?\s*s|it\s+is)\b", re.IGNORECASE)


def _lead_with_its(line: str) -> str:
    """`line` guaranteed to start with an "it's" (already-present forms — it's / its /
    it´s / it is — are left exactly as the model wrote them)."""
    s = (line or "").strip()
    if not s or _ITS_PREFIX.match(s):
        return s
    return f"it's {s}"


# ── AI restyle of the activity bubble (casual texting tone) ───────────

_RESTYLE_TEMPERATURE = 0.9


def _one_line(text: str | None) -> str:
    """First non-empty line, unquoted. Both LLM calls in this module contract for a
    single line; this is where that contract is enforced when the model rambles."""
    out = (text or "").strip().strip('"').strip("'")
    return next((ln.strip() for ln in out.splitlines() if ln.strip()), "")


def _compose_restyle_system(cfg: dict, *, time_only: bool = False) -> str:
    persona = (cfg.get("persona")
               or "You are a warm, flirty OnlyFans creator texting a brand-new "
                  "subscriber.").strip()
    # The time-only line has NO activity in it, and the normal instruction ("keep
    # what you're doing") reads as a licence to supply one — the model would invent
    # a scene, which is the exact thing this mode exists to drop. So it gets its own
    # rule: keep the clock, add nothing.
    rule = (
        "Rewrite the given line from your welcome DM so it reads like a real, "
        "casual text you just fired off — relaxed texting tone, natural phrasing, "
        "a touch playful. It says ONLY the day / time of day and where you are. "
        "START the line with \"it's\". KEEP BOTH: the day + time of day, AND the "
        "place name exactly as written (never drop or vague-up the place). Add "
        "nothing else — do NOT invent what you're doing, plans, or a question: no "
        "activity at all. ONE short line only. Output only the rewritten line — "
        "no quotes, no preamble."
    ) if time_only else (
        "Rewrite the given line from your welcome DM so it reads like a real, "
        "casual text you just fired off — relaxed texting tone, natural phrasing, "
        "a touch playful. KEEP every fact (what you're doing, the weekday / time "
        "of day, where you're from). Do not add new facts or questions. ONE short "
        "line only. Output only the rewritten line — no quotes, no preamble."
    )
    return "\n\n".join([persona, rule])


# One restyle per (account, verbatim line), cached in-process: the line is
# IDENTICAL for every fan in the same time slot (it embeds only the activity +
# weekday + time-of-day + location — nothing fan-specific), and sampling showed
# the LLM's rewrites are near-identical paraphrases of it. So pay for ONE call
# and reuse it for the rest of the slot/day (the key rolls over naturally when
# the slot activity or weekday changes). EXCEPTION: a fan who has ALREADY
# texted us gets a fresh per-fan call — someone actively watching the chat
# shouldn't receive a visibly copy-pasted line. In-memory only: a restart just
# re-pays one call per slot.
_RESTYLE_CACHE_MAX = 64
_restyle_cache: dict[tuple[str, str], str] = {}


async def _fans_with_inbound(account_id: str, fan_ids: list[int]) -> set[int]:
    """Subset of `fan_ids` that has ≥1 INBOUND message — fans who already texted
    us. Their activity bubble gets a fresh per-fan restyle instead of the cached
    slot line. One grouped scan per tick (own session — house pattern)."""
    if not fan_ids:
        return set()
    async with get_session() as s:
        rows = (await s.execute(
            select(Message.fan_id).where(
                Message.account_id == str(account_id),
                Message.fan_id.in_([int(f) for f in fan_ids]),
                Message.direction == "in",
            ).distinct()
        )).all()
    return {int(r[0]) for r in rows}


async def _restyle_activity(
    account_id: str, fan_id: int, cfg: dict, model: str, line: str, *,
    time_only: bool = False,
) -> str:
    """One LLM call → the activity bubble in the creator's casual texting voice.
    Raises (incl. LLMCapExceeded) to the caller, which falls back to the verbatim
    template line — a restyle failure must never cost a fan their welcome."""
    res = await llm_client.chat(
        model=model,
        messages=[
            {"role": "system", "content": _compose_restyle_system(cfg, time_only=time_only)},
            {"role": "user", "content": line},
        ],
        purpose="welcome",
        account_id=account_id,
        fan_id=fan_id,
        temperature=_RESTYLE_TEMPERATURE,
    )
    return _one_line(res.content) or line


# ── Vault image (network-rewrite of the DOM "bot" folder click) ───────

# Folders to source the welcome image from, in priority order. Legacy "bot" (V1)
# first so existing accounts are unchanged; fall back to a welcome folder so images
# still attach on accounts that have no "bot" folder (e.g. jakabasej's "welcome
# script"). We deliberately do NOT fall through to arbitrary folders — picking from
# Streams/Posts/Stories would DM the wrong media. A future templates UI (W5) will
# make this per-account configurable.
_IMAGE_FOLDER_NAMES = ("bot", "welcome script", "welcome")


def _bot_folder_media_id(client, hour: int) -> int | None:
    """Pick a time-of-day vault photo (id at _photo_index(hour), clamped) from the
    first folder in `_IMAGE_FOLDER_NAMES` that exists and has photos. Best-effort:
    any failure / no named folder with photos → None (send text). Shared rule with
    send_followup so both pick from the same folder by the same time-of-day index;
    the cached vault preview (CACHING_PLAN.md) is keyed by the image's stable
    host+path, so the same id costs the bytes only once across welcomes/followups."""
    try:
        lists = client.vault_lists(view="main", limit=100)
    except Exception:
        log.debug("send_welcome vault_lists failed", exc_info=True)
        return None
    folders = lists.get("list") if isinstance(lists, dict) else lists
    by_name: dict[str, int] = {}
    for f in (folders or []):
        if isinstance(f, dict) and f.get("id") is not None:
            by_name.setdefault(str(f.get("name", "")).strip().lower(), f.get("id"))

    for nm in _IMAGE_FOLDER_NAMES:
        folder_id = by_name.get(nm)
        if folder_id is None:
            continue
        try:
            media = client.vault_media(list_id=int(folder_id), type="photo", limit=50)
        except Exception:
            log.debug("send_welcome vault_media failed folder=%s", folder_id, exc_info=True)
            continue
        items = media.get("list") if isinstance(media, dict) else media
        items = [it for it in (items or []) if isinstance(it, dict) and it.get("id")]
        if not items:
            continue
        idx = min(_photo_index(hour), len(items) - 1)
        try:
            return int(items[idx]["id"])
        except (TypeError, ValueError, KeyError):
            continue
    return None


# ── Follow-back (fires at the moment of subscribe) ────────────────────
#
# There is no code here. Following a new subscriber back is `auto_follow`'s
# `gated_follow(unfollow_first=False)` — the same profile read, the same
# price gate, the same /subscribe-vs-/resubscribe arms — and this lane calls it.
#
# It used to be a second copy: `_follow_back_outcome` + `_follow_back`, which
# were `_classify` + `gated_follow` with five labels renamed. Proven identical
# over 255 profile shapes, 0 disagreements. That is a MONEY gate — it is what
# decides whether OF's /subscribe CHARGES this account — and the copy ran
# fleet-wide on every new subscriber, while the original carried the
# verified-live finding about which payload field means which edge, the measured
# cost data (~3% of stored fans are priced, ≈$177 for a 711-fan backfill) and a
# documented failure mode. A payload change from OF lands in one of them.
#
# `_StrandedError` cannot reach this lane: only the "pinged" arm raises it, and
# that arm needs `unfollow_first=True`.
#
# ⚠️ MONEY, unchanged by the de-duplication: with `follow_back_gate` off — the
# operator-chosen default — `money_gate=False` calls follow_user with NO profile
# read, so a new sub who is themself a paid creator CHARGES this account their
# price. Deliberate (2026-09-06): one fewer read per sub, and a fan subscribing
# to us is almost never a priced creator. `follow_back_gate: true` buys it back.


def _test_mode() -> bool:
    """Are we running under the test harness?

    ⚠️ PARSED, not just "is the variable set". `CHATTERLY_TEST_MODE=0` is a thing
    a person types when they mean "no", and a bare truthiness test on the string
    made it mean "yes" — silently disabling both on-by-default knobs on whatever
    ran with it. The rest of the tree only ever sets it to "1"; this is about the
    person who sets it to something else.
    """
    v = (os.environ.get("CHATTERLY_TEST_MODE") or "").strip().lower()
    return v not in ("", "0", "false", "no", "off")


def _on_unless_off(payload: dict, key: str) -> bool:
    """An ON-BY-DEFAULT payload knob: absent ⇒ on, explicit `false` ⇒ off.

    `human_pace` and `stop_on_reply` were flipped on by default by the operator
    (2026-09-06), so an existing welcome rule that has never been re-saved gets
    both. BrainPanel reads `!== false` and the catalog declares `default: True`,
    so all three sides agree and the checkbox never shows a state the sender
    disagrees with (the V5 mismatch, deliberately not repeated).

    ⚠️ EXCEPT under CHATTERLY_TEST_MODE, where absent ⇒ OFF. Both knobs move real
    wall-clock time: `human_pace` draws quiet seconds that are NOT wpm-derived, so
    `load_typing_wpm`'s test-mode zero does not reach them (H6), and 84 pre-existing
    cases call `run_once` with no payload at all. Defaulting them on in tests would
    make the suite sleep for minutes and would silently re-point every legacy case
    at a path it was never written to exercise. A test that wants either behaviour
    passes it explicitly — which every case added for §A and §C already does. This
    is the same seam `load_typing_wpm` (`_common.py:1796`) and `load_typing_indicator`
    (`:1825`) already use, for the same reason.

    The env read is `_test_mode()`, not `os.environ.get(...)` inline: this is a
    PAYLOAD accessor, and a reader who does not already know had no way to see
    that the answer depends on the process environment. Now the name says so, and
    `CHATTERLY_TEST_MODE=0` means what it looks like it means."""
    v = payload.get(key)
    if v is None:
        return not _test_mode()
    return bool(v)


# ── stop_on_reply: did he say something, and whose turn is it now? ────

async def _newest_worded_inbound(account_id: str, fan_id: int
                                 ) -> tuple[datetime, int] | None:
    """`(created_at, message_id)` of his newest inbound row that used WORDS, or
    None when there is no such row. RAISES on a query error — each caller decides
    what a failed read means for it (see `_fan_replied_since`).

    ⚠️ WORDS DECIDE, not the message TYPE. Operator, 2026-09-06: *"dont stop on
    single tip only on text"*. So `is_tip` is deliberately absent from the
    predicate — the question is whether he said something to her:

      • a text message        → words. The literal ask.
      • a tip WITH a note     → words. The note is addressed at her, and it is the
                                strongest engagement signal on the platform.
      • a BARE tip            → not words. Applause, not a conversational turn:
                                there is nothing to answer, the finishing welcome
                                reads fine over it, and the ledger-derived rows
                                land up to 5 minutes late (transaction_ingest's
                                poll), so aborting on one would be unreliable
                                exactly when it fired.
      • media with NO caption → not words. "only on text" admits no wordless
                                message. A CAPTIONED photo carries text and does
                                stop the burst.

    Ordered by `(created_at, message_id)`: `message_id` is OF's own BigInteger and
    both timestamps are provider-stamped, so the tiebreak needs no clock of ours.

    🚩 One consequence worth knowing: a fan who answers with a bare photo and no
    caption gets the rest of the burst over the top of it. If that ever reads
    wrong, re-admitting empty-bodied non-tip rows is one clause here."""
    async with get_session() as s:
        row = (await s.execute(
            select(Message.created_at, Message.message_id).where(
                Message.account_id == str(account_id),
                Message.fan_id == int(fan_id),
                Message.direction == "in",
                Message.is_unsent.is_(False),
                Message.body != "",
                Message.body.notlike(f"{TIP_LEDGER_PREFIX}%"),
            )
            .order_by(Message.created_at.desc(), Message.message_id.desc())
            .limit(1)
        )).first()
    return (row[0], int(row[1])) if row is not None else None


async def _fan_replied_since(account_id: str, fan_id: int,
                             anchor: tuple[datetime, int] | None
                             ) -> tuple[datetime, int] | None:
    """His newest worded inbound IF it is newer than `anchor`, else None.

    The anchor is a ROW, not a clock (§C1) — snapshotted at the top of his task —
    so a sub who messaged us BEFORE his welcome still gets the full burst, and no
    part of this depends on our own wall clock agreeing with OF's.

    ANY failure reads as "no reply seen": logged, swallowed, burst continues.
    This knob may only ever REMOVE bubbles, and a read that fails must never be
    the thing that costs a fan his welcome.

    The COMPARISON is inside the same `try` as the query, deliberately. It is a
    tuple compare against a row this process did not build — a `created_at` that
    came back None makes `newest > anchor` a TypeError, and an exception escaping
    from here does not degrade to "finish the burst": it kills the fan's whole
    task, mid-welcome, on the one path whose entire contract is that it cannot."""
    try:
        newest = await _newest_worded_inbound(account_id, fan_id)
        if newest is None:
            return None
        return newest if (anchor is None or newest > anchor) else None
    except Exception:
        log.warning("send_welcome reply check failed account=%s fan=%s — "
                    "finishing the burst", account_id, fan_id, exc_info=True)
        return None


async def _rule_enabled(account_id: str, kind: str) -> bool:
    """Is there an ENABLED `automation_rules` row of this kind on the account?
    The house way to ask whether an automation is switched on for an account when
    it keeps no config blob of its own (precedent: `customs_watch.flags`)."""
    async with get_session() as s:
        row = (await s.execute(
            select(AutomationRule.id).where(
                AutomationRule.account_id == str(account_id),
                AutomationRule.kind == str(kind),
                AutomationRule.is_enabled.is_(True),
            ).limit(1)
        )).first()
    return row is not None


async def _handoff_engine(account_id: str) -> str | None:
    """Which chat engine should answer the reply our welcome talked over — or
    None when this account runs no chat engine at all.

    `welcome_chatter_for_info` owns brand-new subs by construction: ai_chatter's
    payer floor hands every fan who has never bought CONTENT to it, and a
    brand-new sub has bought nothing (a subscription is not a purchase). So:

      1. ai_chatter is enabled AND owns the whole account → it replaces the
         gatherer entirely, so it is the only voice there is.
      2. else an enabled welcome_chatter_for_info rule → the gatherer.
      3. else ai_chatter enabled in SUBSET mode → it, forced past its own payer
         floor. Better the seller's voice once than silence.
      4. else → nobody. Nothing would have answered him today either.

    Exactly ONE job is ever enqueued, so a subset-mode account (both engines live)
    cannot produce two voices: the new sub is not in `engaged_subset`, rule 2
    picks the gatherer, and rule 1 already declined."""
    from .ai_chatter import is_enabled as _ai_enabled
    from .ai_chatter import owns_whole_account as _ai_owns_all
    ai_on = await _ai_enabled(account_id)
    if ai_on and await _ai_owns_all(account_id):
        return "ai_chatter"
    if await _rule_enabled(account_id, "welcome_chatter_for_info"):
        return "welcome_chatter_for_info"
    return "ai_chatter" if ai_on else None


async def _enqueue_turn_handoff(account_id: str, fan_id: int) -> str | None:
    """Hand this fan's TURN to a chat engine. Returns the kind enqueued, or None
    when no engine exists.

    WHY THIS EXISTS AT ALL: the bubble that finished landing after his reply moved
    `last_dir` to "out", and BOTH engines gate their candidates on the fan having
    spoken last. Without this job nothing answers him until he double-texts —
    which is a worse product than the burst it fixed.

    The payload carries three keys and they do three different jobs:
      `only_fan_ids`     scope the run to him (no full-account sweep),
      `force_ids`        skip the discretionary gates (mid-funnel, promo-spam,
                         the payer floor, the content-payer skiplist),
      `turn_handoff_ids` the ONE new seam — the turn gate itself, which no
                         existing force bypasses in either engine. It is
                         self-verifying on the engine side: if a human or another
                         automation answered him inside the window, the engine
                         drops the job rather than double-replying.
    Blacklist, `automation_paused_until`, the muted-creator hard skip, the fan
    lease and the fan cooldown ALL still bind — see the enumeration in §C3.

    `run_at` clears our own 150s rest (both engines check `fan_on_cooldown` in
    their send loops); the executor's 30s tick then picks it up within half a
    minute, so he is answered about three minutes after he wrote."""
    kind = await _handoff_engine(account_id)
    if kind is None:
        return None
    await ax.enqueue_job(
        account_id, kind,
        payload={"only_fan_ids": [int(fan_id)], "force_ids": [int(fan_id)],
                 "turn_handoff_ids": [int(fan_id)]},
        run_at=datetime.utcnow() + timedelta(
            seconds=_WELCOME_REST_S + _HANDOFF_DELAY_PAD_S))
    return kind


# ── The burst's text, composed once for both paths ────────────────────

# The bubble ROLES a welcome burst is made of, in send order, are `pacing`'s and
# are IMPORTED at the top of this file under these same `_ROLE_*` names. `pacing`
# is the module that DISPATCHES on them (`welcome_burst_pace`), so it owns the
# vocabulary: a rename there now breaks this import loudly instead of quietly
# demoting the role to the documented "unknown role is paced as tail" fallback on
# a live send path. The Brain panel's preview caption echoes the same strings out
# of `bubble_roles` rather than re-deriving them from a bubble count.


async def _compose_bubbles(
    *, greeting: str, cfg: dict, hour: int, skip_time_bubble: bool,
    time_only: bool, ignore_pin: bool, question: str, strip_emoji_on: bool,
    restyle_fn: Callable[[str], Awaitable[str]] | None,
) -> tuple[list[str], list[str], bool]:
    """The whole text of one welcome burst: `(bubbles, roles, pinned)`.

    ONE composer for the live run and the Brain panel's preview, because the
    thing they must agree about is the PRECEDENCE, and it was written out twice:

        skip_time_bubble > time_only > pin > normal activity

    …followed, both times, by the same five tails — the "it's" opener, the
    verbatim question, the OF word restriction, the account-wide emoji strip and
    the blank filter. Five places knew that order (these two, plus three in
    BrainPanel), and the plan's own answer to keeping them aligned was "review
    discipline", which is what you reach for when there is no seam. A preview
    that composes a different burst from the one that ships is the single worst
    failure this panel has, because the operator approves what he was shown.

    `roles` runs parallel to `bubbles` — see `_ROLE_*`. It survives the blank
    filter, so a slot whose line strips down to nothing cannot leave the question
    wearing the time line's rhythm (or the caption calling it the time line).

    `restyle_fn` is the biggest difference between the two callers (`ignore_pin`
    is the other, and it is preview-only too — a Regenerate must not be answered
    with the operator's pinned line). The run path's takes the shared per-slot
    cache under a lock and counts what it did; the preview's deliberately bypasses
    both, so a regenerate cannot prime or pollute what the live run reuses.
    None ⇒ send the verbatim template line and make no LLM call at all.

    ⚠️ It is never called for a bubble that will not ship — the daily cap must
    not be spent on a line `skip_time_bubble` already dropped."""
    # The pin lookup does not even happen once the bubble it would fill has been
    # dropped or overruled: every pin ever minted is a rerolled ACTIVITY line, so
    # neither `time_only`'s clock line nor a removed bubble can be filled by one.
    pin = (None if (skip_time_bubble or ignore_pin or time_only)
           else _pinned_line(cfg, hour))
    pinned = False
    roles = [_ROLE_OPENER]
    if skip_time_bubble:
        bubbles = [greeting]
    elif pin is not None:
        bubbles = [greeting, pin]
        roles.append(_ROLE_GAP)
        pinned = True
    else:
        activity = _activity_bubble(cfg, hour, time_only=time_only)
        bubbles = [greeting, *activity]
        roles += [_ROLE_GAP] * len(activity)
        if activity and restyle_fn is not None:
            bubbles[1] = await restyle_fn(bubbles[1])
        if activity:
            # Re-attached in CODE after every path (fresh restyle, cached
            # restyle, verbatim fallback) — a sampled rewrite obeys "start with
            # it's" most of the time, and "most" is a line a fan reads.
            if time_only:
                bubbles[1] = _lead_with_its(bubbles[1])

    # The operator's question, word-for-word, after BOTH the pinned and the
    # composed paths — a pin replaces the activity line, never the question.
    if question:
        bubbles.append(question)
        roles.append(_ROLE_TAIL)

    # Last-mile per bubble: double the first vowel of any OF-restricted word (V1
    # ran apply_word_restriction on EVERY welcome). Covers the local template,
    # the restyled line and the LLM riff.
    bubbles = [apply_word_restriction(b) for b in bubbles]
    if strip_emoji_on:
        bubbles = [strip_emojis(b) for b in bubbles]
    kept = [(b, r) for b, r in zip(bubbles, roles) if b.strip()]
    return [b for b, _ in kept], [r for _, r in kept], pinned


class _Landed(NamedTuple):
    """The OF row one of our bubbles became — and whether OF actually told us.

    `provider_stamped` is the field that matters. The whole abort classification
    is one comparison, "whose row is newer, his reply or the bubble that landed
    on top of it", and it is only honest while BOTH sides are OF's own numbers:
    his row is provider-stamped by definition (the WS pump wrote it from OF's
    payload), so a bubble of ours carrying OUR wall clock and a fabricated id is
    not comparable to it at all. It used to be compared anyway —
    `_parse_iso(...) or datetime.utcnow()` paired with `int(msg_id or 0)` — and
    the fabricated `0` lost every same-second tie, so an id-less send read as
    "his turn" and silently declined the handoff. That is a fan nobody answers.

    When the stamp is ours, the comparison is skipped and the turn is handed off.
    The asymmetry is deliberate: a handoff we did not need costs at most one
    reply he was getting anyway (the engine-side guard is self-verifying, and a
    fan who really did speak last is a normal candidate there), while a handoff
    we needed and skipped costs him every reply until he double-texts."""
    created_at: datetime
    message_id: int
    provider_stamped: bool


# ── The hold(s) for ONE bubble, as data ───────────────────────────────

class _HoldSegment(NamedTuple):
    """One `hold_with_typing` call, plus whether a reply check may follow it.

    `check_after` is the whole reason this is data rather than four hand-written
    branches: it is the ONE place that answers "may the fan interrupt here?", and
    the answer is a property of the segment, not of where the code happens to be.
    A segment the fan may interrupt is one he can SEE is not being typed."""
    seconds: float
    indicator: bool
    quiet_s: float
    think_at_s: float
    think_for_s: float
    check_after: bool


def _hold_segments(*, pace, typing_s: float, is_gif: bool, typing_on: bool,
                   interruptible: bool) -> list[_HoldSegment]:
    """Every hold this bubble makes, in order.

    This used to be a 2x2 shape matrix written out longhand — paced/unpaced x
    gif/text — with `typing_on and not gid` spelled three times and the abort
    check pasted after two of the four arms. The four shapes are all this:

      GIF                        one dark hold, and the check goes AFTER it. She
                                 picked it, she did not type it, so every second
                                 of the beat is honestly interruptible.
      UNPACED text               one hold, no check after: that hold IS the
                                 typing phase and she finishes what she started.
      PACED text, no quiet       the same, for the same reason.
      PACED text, quiet lead-in, TWO holds that SUM to `pace.total_s`, with the
        being checked            check on the phase boundary. A reply during the
                                 dark lead-in means she had not started typing —
                                 which is exactly what the fan observed — so that
                                 bubble is simply never sent. The think phases
                                 ride the second segment, because that is where
                                 the typing phase is.

    ⚠️ THE INVARIANT: the segments SUM to the one number the caller was going to
    hold for. `Pace.total_s` is the only value in this subsystem that moves a
    clock (pacing.py), and splitting a hold must not quietly add time to it. It
    is asserted below rather than promised in a comment repeated at two sites.

    `interruptible` is the caller's "this bubble is being checked at all"
    (`stop_on_reply` on, and past the greeting). With it False no segment carries
    a check and no bubble is ever split, so the shapes — and the recorded call
    COUNT, which several cases assert on — are byte-identical to today's.

    `pace` is None on the unpaced path, where the hold is the flat typing time
    (or `_GIF_HOLD_S`) this sender has always used."""
    indicator = typing_on and not is_gif
    if pace is None:
        total = _GIF_HOLD_S if is_gif else typing_s
        segments = [_HoldSegment(total, indicator, 0.0, 0.0, 0.0,
                                 is_gif and interruptible)]
    elif interruptible and not is_gif and pace.quiet_s > 0:
        segments = [
            _HoldSegment(pace.quiet_s, False, 0.0, 0.0, 0.0, True),
            _HoldSegment(pace.total_s - pace.quiet_s, indicator, 0.0,
                         pace.think_at_s, pace.think_for_s, False),
        ]
    else:
        segments = [_HoldSegment(pace.total_s, indicator, pace.quiet_s,
                                 pace.think_at_s, pace.think_for_s,
                                 is_gif and interruptible)]
    want = (pace.total_s if pace is not None
            else (_GIF_HOLD_S if is_gif else typing_s))
    assert abs(sum(seg.seconds for seg in segments) - want) < 1e-9, (segments, want)
    return segments


# ── Compose-only preview (no send, no state write) ────────────────────

async def preview_compose(
    account_id: str, payload: dict | None = None, *,
    fan_id: int | None = None, test_name: str | None = None,
    model: str | None = None, restyle: bool = False, slot: str | None = None,
    config: dict | None = None, ignore_pin: bool = False,
) -> dict:
    """Compose the welcome a real run WOULD produce for one fan — the text + the
    chosen time-of-day image id — WITHOUT sending and WITHOUT writing send-state.
    Powers the Brain panel's "Preview"/"Regenerate" buttons (mirrors
    nudge_online.preview_compose).

    Name→text resolution mirrors run(): a resolvable real name takes the
    deterministic 'precious' local template; only a random handle / number with no
    usable name falls back to a single LLM riff. When no `fan_id` is given we greet a
    representative name so the verbatim preview is deterministic and free.

    `slot` pins any of the 6 time-of-day slots (else the creator's current local
    hour); an unknown slot falls back to now. `config` is a DRAFT override
    (unsaved on-screen edits — persona / activities / images / model) shallow-merged
    over the saved brain so a preview is WYSIWYG.

    `restyle=True` runs the SAME AI restyle of the activity bubble that a real run
    sends, so the preview shows the actual shipped text (and each regenerate rerolls
    a fresh sample). It is a real, cap-governed, audited `llm_client.chat` call — a
    cap hit degrades to the verbatim line (`cap_hit`), never an error. It deliberately
    does NOT read or write the shared per-slot `_restyle_cache` (so a preview can't
    prime or pollute what the live run reuses). Beyond that LLM audit/cost row this
    writes nothing: no send, no `welcome_sent`, no `messages`, no vault network call.

    If the slot is PINNED (operator kept a line), the preview shows that exact line
    (weekday refreshed) and `pinned=True`, skipping the restyle — this is what will
    ship. `ignore_pin=True` (the "Regenerate" button) bypasses the pin to sample a
    fresh candidate the operator can keep in its place.

    `time_only=True` mirrors the rule's own knob: bubble 2 is the short clock line
    (no activity). It bypasses the pin for the same reason the sender does — a pin
    is a stored ACTIVITY line, so honouring it would show (and ship) the long line
    the checkbox just turned off. That clock line still wears the `gap` role (it
    IS bubble 2, and `pacing` must pace it as one), so the roles alone cannot say
    whether what the operator is looking at may be PINNED — `pinnable` says it.

    `skip_time_bubble=True` mirrors the rule's own knob and OUTRANKS both: there is
    no bubble 2 at all, so the pin lookup, the activity line, the restyle (and its
    LLM call) and the "it's" opener are all skipped. Same precedence as the sender
    — `skip_time_bubble` > `time_only` > pin > normal. The flag itself is NOT
    echoed in the result: `bubble_roles` is, and a missing `gap` role is the same
    fact for every reason a burst can lack an activity bubble, not just this one.

    `question` mirrors the rule's `question` knob: the operator's own question,
    appended word-for-word as the last bubble (never restyled) — carried from the
    form so the preview shows the full burst before the rule is saved.

    ⚠️ THE RULE KNOBS ARRIVE AS ONE `payload` DICT, read with the SAME
    expressions `run()` uses, and that is the point. They used to be four named
    parameters threaded through eight hops — form state → mutation input type →
    HTTP body → a Pydantic model that silently DROPS anything it does not declare
    → the route's named forwarding → here. Every hop is a place to forget one,
    the code's own comment said forgetting one makes the preview "quietly show a
    burst nobody will receive", and a warning is not a mitigation. Now a knob the
    panel puts on the rule reaches this function whether or not anybody
    remembered it, and it is read here exactly as the sender reads it. Precedent:
    `mass_nudge.preview_compose(account_id, payload, ...)` on this same route.

    The remaining named arguments are the ones that are NOT rule knobs —
    `restyle` / `slot` / `config` / `ignore_pin` / `model` are preview controls
    (which slot to show, the unsaved draft brain, "Regenerate") and belong to the
    panel, not to the rule.

    Returns the send-shape as `bubbles` (image rides on bubble 1) + joined `text`,
    plus `image`, `name`, `slot`, and `restyled`/`cap_hit`/`pinned` flags."""
    # Read with `run()`'s own expressions, so "what the preview shows" and "what
    # the sender sends" cannot answer the same knob differently.
    payload = payload or {}
    time_only = bool(payload.get("time_only"))
    skip_time_bubble = bool(payload.get("skip_time_bubble"))
    question = str(payload.get("question") or "").strip()
    gif_id = str(payload.get("gif_id") or "").strip()

    _wv = (await load_voice_blocks(account_id)).voice
    cfg = await _load_ai_config(account_id)
    # Draft override wins over the saved brain (None never clobbers); the UI sends the
    # full time_activities/time_images dicts, so a shallow merge is correct. Both
    # sides hold the RAW clock columns and `_clock_hours` resolves whatever wins, so
    # the draft's clock cannot land in a different unit than the brain's — that
    # mismatch is what previewed Isabelle 3h away from what her welcome sent.
    if config:
        cfg = {**cfg, **{k: v for k, v in config.items() if v is not None}}
    strip_emoji_on = await load_strip_emojis(account_id)  # account-wide emoji strip

    # Pin the requested slot's representative hour; unknown/empty slot → current hour.
    hour = _slot_hour(slot)
    if hour is None:
        hour = _model_hour(_clock_hours(cfg))

    if fan_id is not None:
        sub = {"id": int(fan_id), "name": test_name, "username": None}
        name = await _resolve_welcome_name(account_id, int(fan_id), sub)
    else:
        # No fan chosen → a representative resolvable name so the preview shows the
        # signature local template deterministically (no LLM call, no cost).
        name = _name_token(test_name) or "Alex"
        sub = {"id": 0, "name": test_name or name, "username": None}

    greeting = _local_greeting(name or _greet_token(sub, _wv), _wv)
    restyled = False
    cap_hit = False

    async def _restyle_line(line: str) -> str:
        """The preview's restyle — exactly the rewrite that ships, deliberately
        CACHE-BYPASSED: a fresh sample per regenerate, and it must never prime or
        pollute the per-slot cache the live run reuses. A cap hit or any failure
        degrades to the verbatim line, never an error."""
        nonlocal restyled, cap_hit
        if not restyle:
            return line
        rmodel = (model or cfg.get("model")
                  or await resolve_model(account_id, "welcome", None))
        try:
            styled = await _restyle_activity(
                account_id, int(fan_id or 0), cfg, rmodel, line,
                time_only=time_only)
        except LLMCapExceeded:
            cap_hit = True
            return line
        except Exception:
            log.debug("preview restyle failed account=%s — verbatim line", account_id,
                      exc_info=True)
            return line
        if styled and styled != line:
            restyled = True
            return styled
        return line

    # Composed by the SAME function the live run uses — one precedence, one set
    # of tails, so a preview cannot show a burst the sender would not send.
    bubbles, roles, pinned = await _compose_bubbles(
        greeting=greeting, cfg=cfg, hour=hour,
        skip_time_bubble=skip_time_bubble, time_only=time_only,
        ignore_pin=ignore_pin, question=question,
        strip_emoji_on=strip_emoji_on, restyle_fn=_restyle_line)
    # Bubble 4 is returned SEPARATELY, never appended to `bubbles`: it carries no
    # text, and the panel renders it as an image. Folding it in would put a bare
    # giphy id through apply_word_restriction and into the joined `text`.
    #
    # It IS echoed rather than left to the panel, even though nothing here reads
    # it, so that one preview response is one whole send shape. The panel renders
    # the block from this dict alone; reading the GIF from live form state instead
    # would let a stale composition sit beside a GIF the operator swapped after
    # pressing Preview — the burst on screen would be one nobody ever composed.
    return {"text": "\n\n".join(bubbles), "bubbles": bubbles,
            "image": _slot_image_id(cfg, hour),
            "name": name, "slot": _slot_key(hour),
            "restyled": restyled, "cap_hit": cap_hit, "pinned": pinned,
            # May the operator KEEP the line he is looking at? Decided HERE,
            # where the knobs are, and echoed as one bit — because every attempt
            # to re-derive it on the client has been wrong. A pin is a stored
            # ACTIVITY line (`_compose_bubbles` refuses to look one up under
            # `time_only`, above), but `time_only`'s clock line carries the SAME
            # `gap` role, so "did the server emit a gap bubble" answers
            # `skip_time_bubble` and the unfilled slot and NOT this one. Pinning
            # the clock line stores something that never ships while the checkbox
            # is on, cannot be un-pinned from this screen (`pinned` stays False,
            # so the Unpin button never appears), and becomes that slot's
            # permanent activity line the moment the checkbox comes off.
            #
            # `ignore_pin` (Regenerate) is deliberately NOT in here: it composes a
            # fresh ACTIVITY line precisely so the operator can keep it.
            "pinnable": (_ROLE_GAP in roles) and not time_only,
            # ROLES, echoed one per bubble — not left to the panel to count.
            # Nothing about a welcome burst's LENGTH says what its bubbles are:
            # `skip_time_bubble` and an unfilled slot both drop the middle one,
            # and the operator's question slides into index 1. A positional
            # caption ("the 2nd line is the AI-restyled activity line") then
            # describes a bubble that is not there — and tells the operator to
            # turn on a restyle for his own verbatim question. The panel names
            # each bubble from THIS list instead.
            "bubble_roles": roles,
            # (No `skip_time_bubble` echo. It was kept "for the panel's checkbox
            # wiring", and the panel's checkbox reads the RULE's own payload
            # (`welcomeRule.payload?.skip_time_bubble`); nothing anywhere read it
            # off the preview. `bubble_roles` already answers the only question a
            # consumer had — "is there an activity bubble in THIS preview" — and
            # answers it for the unfilled-slot shape the flag says nothing about.)
            "gif_id": gif_id or None}


# ── One fan's welcome, and everything a run decided before it ─────────

@dataclass(frozen=True, slots=True)
class _RunCtx:
    """Everything `run()` resolved ONCE, handed to each fan's coroutine.

    `_welcome_one` used to be a 473-line closure nested inside an 818-line
    `run()`, reaching 26 of its locals by scope. Two costs, and the second is the
    one that mattered: nothing in the module could be read top-down (the reader
    had to hold `run()`'s whole prologue in their head to know what `paced` or
    `t` were), and the function could not be CALLED except by driving all of
    `run()` — which is why fifteen cases go through `run_once` + FakeOF + a
    monkeypatched hold just to read a tuple. The package's next-largest nested
    function is 40 lines.

    FROZEN because none of it is per-fan: everything here was decided before the
    first fan started and must read the same to all six concurrent bursts.
    Frozen stops the FIELD being rebound, not the object mutating, which is
    exactly the guarantee wanted — so FOUR members are mutable objects, not two:

      • `tally`        — WRITTEN by every burst. Its counters are incremented
                         from each one; safe because asyncio is single-threaded
                         and they are only ever touched on the event loop.
      • `restyle_lock` — WRITTEN by every burst, and exists to be contended.
      • `cfg`          — a dict, so mutable by construction. Read-only in fact
                         (no `ctx.cfg[...] =` / `.update(` anywhere), but nothing
                         enforces that; a burst that wrote to it would be writing
                         to every other burst's config.
      • `texted_ids`   — a set, same story (no `.add(` / `.discard(` anywhere).

    The last two are documentation truth rather than a live bug. `texted_ids:
    frozenset[int]` would make this paragraph shorter by construction, which is
    the direction to go if it ever needs touching.
    """

    account_id: str
    voice: str                 # the creator's lane ("her" / "him")
    cfg: dict                  # the merged brain (persona / activities / images)
                               # (mutable by type, read-only in fact — see above)
    hour: int                  # the creator's local hour → the time-of-day slot
    client: Any                # the account's OF/Fansly client (shared, per X2)
    bot_media_id: int | None   # the slot's vault image, resolved once per run
    tally: _Tally              # ⚠️ WRITTEN by every burst — see above
    texted_ids: set[int]       # fans who already messaged us → fresh restyle
                               # (mutable by type, read-only in fact — see above)
    restyle_lock: asyncio.Lock # ⚠️ contended by every burst — see above

    # ── Payload knobs, already resolved to what this run will do.
    dry_run: bool
    restyle: bool
    time_only: bool
    skip_time_bubble: bool
    question: str
    gif_id: str
    follow_back: bool
    follow_back_gate: bool
    paced: bool                # `human_pace`, past its `test_fan` exemption
    stop_on_reply: bool
    early_mark: bool           # §C5 — claim `welcome_sent` at bubble 0
    pace_open_quiet: Any       # raw [lo, hi] overrides; clamped in the sampler
    pace_gap_quiet: Any

    # ── Account-wide settings, read once.
    model: str
    typing_wpm: float
    typing_on: bool
    strip_emoji_on: bool


async def _welcome_one(ctx: _RunCtx, sub: dict) -> None:
    """One fan's whole welcome: cooldown/lease, compose, burst, follow-back,
    rest + lease hand-back.

    Everything the RUN decided arrives in `ctx` (see `_RunCtx`); `sub` is the one
    thing that is per-fan. With `human_pace` on, six of these are in flight at
    once (§A3); with it off `run()` still awaits them one at a time and this
    does exactly what the loop it grew out of did.

    ⚠️ It owns the fan LEASE from the moment it takes one, and the `finally` at
    the bottom is what hands it back — rest first, then the lease, because ORDER
    IS LOAD-BEARING. Nothing may return out of the middle of the `try` without
    going through it."""
    fan_id = sub["id"]
    # Another automation messaged this fan recently → rest it (W3 cooldown).
    if await ax.fan_on_cooldown(ctx.account_id, fan_id):
        ctx.tally.skipped_cooldown += 1
        return
    # One bot message per fan per cycle — don't race A05/A06/A07/A11.
    if not await ax.acquire_fan_lease(ctx.account_id, fan_id, "send_welcome"):
        ctx.tally.skipped_locked += 1
        return
    sent_ok = False
    try:
        # ── THE ANCHOR (§C1). His newest WORDED inbound as of RIGHT NOW —
        # taken here, the earliest point at which per-fan state exists, so
        # every check below asks the one honest question: "is there a newer
        # one than there was when I started?" A pre-existing inbound IS the
        # anchor, which is what keeps a sub who messaged us before his welcome
        # getting the whole burst, exactly as he does today.
        #
        # If the SNAPSHOT itself fails we run this fan with the knob off
        # rather than against a None anchor: None means "he had said nothing",
        # and any pre-existing message would then read as a brand-new reply
        # and abort the burst on the strength of a failed query.
        check_reply = ctx.stop_on_reply
        anchor: tuple[datetime, int] | None = None
        if check_reply:
            try:
                anchor = await _newest_worded_inbound(ctx.account_id, fan_id)
            except Exception:
                check_reply = False
                ctx.tally.reply_guard_off += 1
                log.warning("send_welcome reply anchor failed account=%s "
                            "fan=%s — burst runs unguarded", ctx.account_id,
                            fan_id, exc_info=True)
        try:
            # Only the greeting TOKEN varies — a resolvable name (from the notif,
            # the fan's real_name, or a stored nickname), else a token derived
            # from the raw handle. Same deterministic stutter either way, and no
            # LLM: the daily cap can no longer cost a nameless fan their welcome.
            name = await _resolve_welcome_name(ctx.account_id, fan_id, sub)
            greeting = _local_greeting(name or _greet_token(sub, ctx.voice), ctx.voice)
        except Exception:
            ctx.tally.errors += 1
            log.warning("send_welcome generate failed account=%s fan=%s",
                        ctx.account_id, fan_id, exc_info=True)
            return

        # The bubble shape — greeting, then the pinned / clock / activity
        # line, then the question — and the precedence between the three
        # knobs that decide the middle one, both in `_compose_bubbles` with
        # the preview. An operator PIN is the approved line for this slot and
        # overrides the restyle flag and the texted-fan fresh call; the two
        # payload knobs outrank it, because a pin is a stored ACTIVITY line
        # and neither the clock line nor a removed bubble can be filled by
        # one. See that function for the whole order and why.
        #
        # ⚠️ Each bubble's ROLE travels beside it, because its POSITION is
        # not stable: the middle bubble is absent under `skip_time_bubble`
        # AND whenever the slot has no activity line, and either way the
        # operator's QUESTION lands at index 1. The sampler dispatches on the
        # role for exactly that reason.
        #
        # `_compose_bubbles` is shared with `preview_compose` — one
        # precedence (skip_time_bubble > time_only > pin > normal), one set
        # of tails. The restyle closure below is the only thing that differs.
        async def _restyle_line(line: str) -> str:
            """The live run's restyle: ONE cached rewrite per slot line,
            shared by every fan (the rewrites are near-identical paraphrases
            — no point paying per fan); a fan who already texted us gets a
            fresh per-fan call instead. Best-effort — any failure, cap
            included, falls back to the verbatim template line, because a
            restyle hiccup must never cost a fan their welcome. Once the
            daily cap trips we stop attempting restyles for the rest of the
            run."""
            if not ctx.restyle or ctx.tally.restyle_capped:
                return line
            ck = (str(ctx.account_id), line)
            fresh = fan_id in ctx.texted_ids
            # Only the CACHEABLE path is serialised (see `restyle_lock`): a
            # fan who already texted us pays a fresh per-fan call that shares
            # nothing, so making him queue would buy latency and nothing
            # else. Uncontended on the serial path — with `human_pace` off
            # nothing else is ever running.
            async with (nullcontext() if fresh else ctx.restyle_lock):
                cached = None if fresh else _restyle_cache.get(ck)
                if cached is not None:
                    if cached != line:
                        ctx.tally.restyled_cached += 1
                    return cached
                try:
                    styled = await _restyle_activity(
                        ctx.account_id, fan_id, ctx.cfg, ctx.model, line,
                        time_only=ctx.time_only)
                except LLMCapExceeded:
                    ctx.tally.cap_hit = True
                    ctx.tally.restyle_capped = True
                    log.warning("send_welcome restyle capped account=%s — verbatim "
                                "activity line for the rest of this run", ctx.account_id)
                    return line
                except Exception:
                    log.warning("send_welcome restyle failed account=%s fan=%s — "
                                "sending verbatim line", ctx.account_id, fan_id,
                                exc_info=True)
                    return line
                if not fresh:
                    # Cache even a verbatim echo — a model that refuses to
                    # rewrite shouldn't be re-asked for every fan this slot.
                    _restyle_cache[ck] = styled
                    while len(_restyle_cache) > _RESTYLE_CACHE_MAX:
                        _restyle_cache.pop(next(iter(_restyle_cache)))
                if styled != line:
                    ctx.tally.restyled += 1
                    return styled
                return line

        bubbles, roles, was_pinned = await _compose_bubbles(
            greeting=greeting, cfg=ctx.cfg, hour=ctx.hour,
            skip_time_bubble=ctx.skip_time_bubble, time_only=ctx.time_only,
            ignore_pin=False, question=ctx.question,
            strip_emoji_on=ctx.strip_emoji_on, restyle_fn=_restyle_line)
        if was_pinned:
            ctx.tally.pinned_used += 1
        if not bubbles:
            ctx.tally.errors += 1
            return

        if ctx.dry_run:
            ctx.tally.sent += 1  # would-send; do NOT mark welcome_sent on a dry run
            return

        # The burst: image on bubble 1 only; each bubble held for its human
        # typing time first (live "...is typing" frames when enabled) — same
        # pacing as welcome_chatter_for_info, so the welcome lands like a person texting,
        # not a bot blast.
        media_ids = [ctx.bot_media_id] if ctx.bot_media_id is not None else []

        # The burst as ONE plan: every text bubble, then the GIF. Bubble 4 is
        # a (text="", giphy_id=…) send — OF carries a GIF as a top-level
        # `giphyId` beside EMPTY text — which is why it joins HERE and not
        # `bubbles`: the composition above would have word-restricted it,
        # emoji-stripped it, then dropped it as blank.
        #
        # Riding the same loop is what keeps ONE burst policy for four bubbles:
        # bubble 0 failing is fatal, any later failure stops the burst, and
        # every landed send is persisted the same way. Sending the GIF after
        # the loop instead fired it even when the burst had already been
        # abandoned — a fan whose question bubble failed got a lone greeting
        # and then an unexplained animation.
        #
        # `bubbles` is non-empty here (guarded above), so the GIF is never
        # index 0: a GIF cannot ship as a welcome on its own.
        plan: list[tuple[str, str | None, str]] = [
            (b, None, r) for b, r in zip(bubbles, roles)]
        if ctx.gif_id:
            plan.append(("", ctx.gif_id, _ROLE_GIF))

        landed = 0
        marked = False
        # (created_at, message_id) of the last bubble of this burst that
        # actually landed — the other half of the abort classification below.
        last_landed: _Landed | None = None
        # His reply, when one cut the burst short. None ⇒ the burst ran out.
        aborted: tuple[datetime, int] | None = None
        for idx, (part, gid, role) in enumerate(plan):
            # Is this bubble one the fan is allowed to interrupt at all?
            # Bubble 0 never is — it IS the welcome, and there is nothing yet
            # for him to have replied to.
            checking = check_reply and idx >= 1
            # ── CHECKPOINT 1 (§C2.1): the top of every TEXT bubble after the
            # greeting, with or without `human_pace`.
            #
            # The GIF is deliberately excluded: its whole hold is
            # interruptible, so the check that follows it (below) is strictly
            # stronger than this one — rows only ever accumulate, so anything
            # this would find, that one finds too. Running both was two
            # identical round-trips on every GIF for one decision.
            if checking and not gid:
                aborted = await _fan_replied_since(ctx.account_id, fan_id, anchor)
                if aborted is not None:
                    break
            # A GIF is PICKED, not typed: no typing time and no "...is typing"
            # frame, just the beat between bubbles.
            typing_s = 0.0 if gid else typing_delay_seconds(part, ctx.typing_wpm)
            # `Pace.total_s` is the ONLY number that moves a clock — the phase
            # arguments just decide when the indicator is on inside that same
            # total (pacing.py's INVARIANT). Seeded per (account, fan, bubble)
            # so a burst replays. Unpaced ⇒ no sampler call at all, so nothing
            # is drawn and no seeded sequence anywhere shifts.
            pace = welcome_burst_pace(
                role=role, has_image=bool(media_ids),
                typing_s=typing_s, text=part,
                rng=Random(f"welcome_pace:{ctx.account_id}:{fan_id}:{idx}"),
                open_quiet=ctx.pace_open_quiet,
                gap_quiet=ctx.pace_gap_quiet) if ctx.paced else None
            t0 = time.monotonic()
            # ── THE HOLD, and CHECKPOINTS 2 (§C2.2, the quiet/typing phase
            # boundary) and 3 (§C2.3, after the GIF's beat). Both are now the
            # same statement over `_hold_segments`, which is the one place
            # that decides which parts of a bubble the fan may interrupt.
            for seg in _hold_segments(pace=pace, typing_s=typing_s,
                                      is_gif=bool(gid), typing_on=ctx.typing_on,
                                      interruptible=checking):
                await hold_with_typing(
                    ctx.account_id, fan_id, seg.seconds,
                    typing_indicator=seg.indicator, quiet_s=seg.quiet_s,
                    think_at_s=seg.think_at_s, think_for_s=seg.think_for_s)
                if seg.check_after:
                    aborted = await _fan_replied_since(ctx.account_id, fan_id,
                                                       anchor)
                    if aborted is not None:
                        break
            if aborted is not None:
                break
            if pace is not None:
                # INSTRUMENTATION ONLY (§A5). `_emit_typing_for` subtracts the
                # SLEEP from its remaining budget but not the awaited
                # `emit_typing` itself, so elapsed = requested + emit overhead.
                # Every ceiling in the tree bounds the REQUESTED side; this
                # measures the gap so the follow-up fix has a number to argue
                # from. On Fansly that emit is a REST POST in a thread, which
                # is why a Fansly account does not get `human_pace` until an
                # OF day reports an overrun near zero.
                over = (time.monotonic() - t0) - pace.total_s
                if over > ctx.tally.hold_overrun_max_s:
                    ctx.tally.hold_overrun_max_s = over
                if over > _HOLD_OVERRUN_WARN_S:
                    log.warning("send_welcome hold overran by %.1fs account=%s "
                                "fan=%s bubble=%d (requested %.1fs)",
                                over, ctx.account_id, fan_id, idx, pace.total_s)
            # (CHECKPOINT 3 — the GIF's, §C2.3 — is the `check_after` on its
            # single segment above: a reply detected at ANY point before the
            # send suppresses it, because she picked the GIF rather than
            # typing it and there is no "she was mid-sentence" to honour.
            # Best effort — no atomicity with the wire is claimed or needed.)
            try:
                if gid:
                    result = await asyncio.to_thread(
                        lambda g=gid: ctx.client.send_message(fan_id, "", giphy_id=g))
                else:
                    # A refused ATTACHMENT degrades to text-only rather than
                    # losing the greeting. Otherwise one dead vault id blocks
                    # EVERY welcome on the account: a failed bubble-0 leaves the
                    # welcome_sent claim unwritten, so the next sweep regenerates
                    # and re-fails, forever.
                    outcome = await send_dropping_bad_media(
                        ctx.client, fan_id, part, media_ids if idx == 0 else [],
                        log=log, send_purpose="gated")
                    result = outcome.result
            except Exception as e:
                if idx == 0:
                    ctx.tally.errors += 1
                    # Permanent (deleted/blocked) → quarantine AND claim
                    # welcome_sent — that claim is THIS sender's own gate, so the
                    # new-sub sweep never regenerates a welcome for a fan we can't
                    # deliver to. Transient errors leave the claim unwritten and
                    # retry next tick.
                    if await skip_unreachable_fan(ctx.account_id, fan_id, e, log=log):
                        await _mark_welcomed(ctx.account_id, fan_id, sub.get("username"))
                    log.warning("send_welcome send failed account=%s fan=%s",
                                ctx.account_id, fan_id, exc_info=True)
                else:
                    # The greeting already landed → the fan IS welcomed; losing
                    # the follow-up bubble is cosmetic. Stop the burst, still mark.
                    log.warning("send_welcome bubble %d failed account=%s fan=%s",
                                idx + 1, ctx.account_id, fan_id, exc_info=True)
                break
            landed += 1
            # A landed send is either the GIF or a text bubble, and only a text
            # bubble can carry the time-of-day image — so `outcome` is read only
            # in the branch that defines it.
            if gid:
                ctx.tally.gifs_sent += 1
            elif idx == 0 and outcome.media_landed:
                ctx.tally.image_attached += 1
            # Existing optimistic send path: persist each landed bubble
            # (Automation employee). The WS pump skips outbound, so this is the
            # only producer of the outbound `messages` rows.
            msg_id = result.get("id") if isinstance(result, dict) else None
            # OF's own stamp for the row we just put on the thread. Read out
            # here rather than inline in the attribution call because the
            # abort classification below needs the same number: "whose row is
            # newer, his reply or the bubble that landed on top of it".
            #
            # `provider_stamped` records whether that number is OF's or ours.
            # The attribution row still takes `utcnow()` as its fallback — a
            # row has to carry SOME timestamp — but the classification does
            # not pretend our clock is comparable to OF's. See `_Landed`.
            of_created = (ax._parse_iso(result.get("createdAt"))
                          if isinstance(result, dict) else None)
            landed_at = of_created or datetime.utcnow()
            last_landed = _Landed(landed_at, int(msg_id or 0),
                                  of_created is not None and msg_id is not None)
            if msg_id:
                await write_outbound_attribution(
                    account_id=ctx.account_id,
                    fan_id=int(fan_id),
                    message_id=int(msg_id),
                    sent_by_employee_id=None,  # → system Automation employee
                    automation_kind="welcome",  # matches grok_calls.purpose
                    # `part` is "" for the GIF bubble, which is exactly the
                    # body a GIF-only row wants: its content is the giphyId.
                    body=str(result.get("text") or part),
                    price_cents=0,
                    created_at=landed_at,
                    emit_live=True,  # WORKER→SSE bridge: surface the welcome live
                )
            # THE CLAIM, as early as it can honestly be made (§C5): the
            # greeting is on the wire and attributed, so this fan HAS been
            # welcomed — whatever happens to bubbles 2-4. Under either knob a
            # burst can now end early or run for minutes, and the one thing
            # that must never happen is a second greeting on the next tick.
            if idx == 0 and ctx.early_mark and not marked:
                await _mark_welcomed(ctx.account_id, fan_id, sub.get("username"))
                marked = True
        if landed == 0:
            return
        # Mark welcome_sent AFTER at least one bubble landed — a crash
        # mid-welcome re-welcomes (rare, safe) rather than marking with no send.
        # (Already claimed above under `early_mark`; this is the flags-off
        # placement, unchanged.)
        if not marked:
            await _mark_welcomed(ctx.account_id, fan_id, sub.get("username"))
        ctx.tally.sent += 1
        sent_ok = True

        # Follow back — the second touch, fired while the welcome is still
        # the newest thing in their inbox. Deliberately AFTER the welcome is
        # marked: the follow is a bonus notification, and a follow that
        # fails (or a paid profile we refuse to pay for) must never cost the
        # fan their welcome or leave the sweep re-welcoming them forever.
        # Errors are counted, logged, and swallowed for exactly that reason.
        #
        # PER-RUN BOUND, since it is set by a knob with a different name:
        # there is no follow_back cap. One follow per LANDED welcome, and the
        # welcomes are capped at `max_welcomes` (default 25), so the worst
        # case is 25 paying calls per tick. `welcome_sent` dedups across
        # ticks, so each fan is followed here at most once, ever.
        if ctx.follow_back and not ctx.dry_run:
            try:
                # `_classify`'s own vocabulary, not a rename of it: the labels
                # travel with the gate. The stat KEYS below keep their
                # `follow_back_*` names — they are this lane's run stats and the
                # operator reads them next to `welcomes_sent`, not next to
                # auto_follow's.
                fb_outcome = await gated_follow(
                    ctx.client, fan_id, unfollow_first=False,
                    money_gate=ctx.follow_back_gate)
                if fb_outcome in ("followed", "refollowed"):
                    ctx.tally.followed_back += 1
                    # ONE cooldown ledger across both follow subsystems. This
                    # is the notification auto_follow's own `stamp_ping`
                    # records for every follow IT fires; without the stamp, an
                    # auto_follow backfill tick would follow this fan a second
                    # time and notify him twice for one relationship.
                    #
                    # Its OWN swallow, not the outer one: the follow has
                    # already landed and is already counted, so a failed
                    # ledger write must not also book the fan as an error. The
                    # cost of losing it is one duplicate notification later,
                    # which is never worth costing him his welcome.
                    try:
                        await stamp_ping(ctx.account_id, fan_id)
                    except Exception:
                        log.warning("send_welcome follow-back ledger write "
                                    "failed account=%s fan=%s — the follow "
                                    "landed; auto_follow may notify him again",
                                    ctx.account_id, fan_id, exc_info=True)
                elif fb_outcome == "already_following":
                    ctx.tally.follow_back_already += 1
                elif fb_outcome == "paid_profile":
                    ctx.tally.follow_back_paid_skipped += 1
                else:
                    ctx.tally.follow_back_no_price += 1
            except Exception:
                ctx.tally.follow_back_errors += 1
                log.warning("send_welcome follow-back failed account=%s fan=%s "
                            "— welcome already landed", ctx.account_id, fan_id,
                            exc_info=True)
        # ── AFTER the follow-back, deliberately. The handoff job's `run_at`
        # is `utcnow + _WELCOME_REST_S + _HANDOFF_DELAY_PAD_S`, and the rest
        # it has to clear does not START until the `finally` below — which is
        # on the far side of one or two OF network calls. Stamped before
        # them, a slow follow-back spent the whole 15s pad and the job came
        # due INSIDE our own cooldown: both engines check `fan_on_cooldown`
        # in their send loops, so the job was consumed and dropped as
        # `skipped_cooldown` while `handoff_enqueued: 1` reported a rescue
        # that never happened. Stamping it here leaves only the cooldown
        # write itself between the two clocks.
        #
        # ── CHECKPOINT 4 (§C2, the TAIL). Every check above runs BEFORE a
        # send; not one of them runs after the LAST send of the burst. On the
        # shipped default shape that leaves the final bubble completely
        # uncovered: no `gif_id` ⇒ plan is [greeting, activity, question], he
        # answers while she is typing the question, the question lands on top
        # of his reply, the loop runs out — and `aborted` is still None, so
        # the classification below never runs at all. His `last_dir` is now
        # "out", both engines gate on him having spoken last, and nothing
        # answers him until he double-texts. That is precisely the blocker
        # §C3 exists to fix, reintroduced at the tail, and it is INVISIBLE:
        # `aborted_on_reply` stays 0 because the burst was never cut short.
        #
        # So ask once more, after the burst — but ONLY when the loop did not
        # already break on a reply. That reply has been classified already;
        # re-reading it here would hand one turn off twice.
        his_reply = aborted
        if check_reply and his_reply is None:
            his_reply = await _fan_replied_since(ctx.account_id, fan_id, anchor)

        # ── He spoke somewhere inside the burst (§C3). WHOSE TURN IS IT?
        # Compare his row against the last bubble of ours that landed:
        #
        #   his row NEWER  → the turn is HIS, and both engines will pick him
        #                    up on their own cadence once the rest expires.
        #                    Nothing to do — and this is the common case,
        #                    because the quiet-phase checkpoint catches most
        #                    replies before a bubble ever ships.
        #   OUR row newer  → a bubble she was already typing landed ON TOP of
        #                    his reply and took the turn with it. Nothing
        #                    would answer him until he double-texted, so the
        #                    turn is handed back explicitly.
        #
        # Failures here are logged and swallowed: he has his welcome, and a
        # handoff that could not be enqueued must not cost him the rest of it.
        if his_reply is not None:
            # `aborted_on_reply` counts bursts the FAN cut short — bubbles he
            # cost us. The tail check finds replies the burst simply finished
            # over; that is a different event and reporting it as an abort
            # would make the stat lie in the one direction that matters (it
            # is how the operator reads "is stop_on_reply working").
            if aborted is not None:
                ctx.tally.aborted_on_reply += 1
            # Only OF's own numbers may decide this. An unstamped bubble
            # (no `createdAt`, no id — OF answered 200 with a body we could
            # not read) is not comparable to his provider-stamped row, so we
            # do not guess: the turn is handed back. See `_Landed`.
            his_turn = last_landed is None or (
                last_landed.provider_stamped
                and his_reply > (last_landed.created_at,
                                 last_landed.message_id))
            log.info("send_welcome saw a reply mid-burst account=%s fan=%s "
                     "landed=%d stopped=%s turn=%s", ctx.account_id, fan_id,
                     landed, aborted is not None,
                     "his" if his_turn else "handoff")
            if not his_turn:
                try:
                    # THE BELT (§C3's skip_reasons row). `force_ids` bypasses
                    # ai_chatter's skip_reason gate, and this job carries it —
                    # near-vacuous, because he was deliverable seconds ago, but
                    # a restriction CAN land mid-burst (a scrape discovers he
                    # muted us) and `test_fan` reaches this path past the run's
                    # own hard-skip filter entirely. Cheap, and it is the one
                    # gate the handoff would otherwise open by accident.
                    if fan_id in await load_hard_skip_ids(ctx.account_id):
                        ctx.tally.handoff_skipped_restricted += 1
                        log.info("send_welcome skipped the turn handoff for a "
                                 "restricted fan account=%s fan=%s",
                                 ctx.account_id, fan_id)
                    else:
                        kind = await _enqueue_turn_handoff(ctx.account_id, fan_id)
                        if kind is None:
                            ctx.tally.handoff_no_engine += 1
                            log.warning("send_welcome ate a reply and this "
                                        "account runs no chat engine "
                                        "account=%s fan=%s", ctx.account_id, fan_id)
                        else:
                            ctx.tally.handoff_enqueued += 1
                            log.info("send_welcome handed the turn to %s "
                                     "account=%s fan=%s", kind, ctx.account_id,
                                     fan_id)
                except Exception:
                    log.warning("send_welcome turn handoff failed account=%s "
                                "fan=%s — his welcome already landed",
                                ctx.account_id, fan_id, exc_info=True)

    finally:
        # W3: rest the fan, then hand the lease straight back — do NOT sit on
        # it. Holding it meant the 15-minute lease TTL, not the rest we chose,
        # decided when he could be answered.
        #
        # ORDER IS LOAD-BEARING: the cooldown must be in force BEFORE the lease
        # drops. A tick landing in the gap would see a fan with neither brake
        # and could reply on top of the welcome — the exact double-message the
        # lease exists to stop. And if the cooldown write fails we KEEP the
        # lease, so a fan is never left with no brake at all; it expires on its
        # own and we are back to the old, slower-but-safe behaviour.
        rested = False
        if sent_ok:
            try:
                await ax.start_fan_cooldown(ctx.account_id, fan_id,
                                            cooldown_s=_WELCOME_REST_S)
                rested = True
            except Exception:
                log.warning("send_welcome cooldown set failed account=%s fan=%s "
                            "— keeping the lease as the fallback brake",
                            ctx.account_id, fan_id, exc_info=True)
        if rested or not sent_ok:
            await ax.release_fan_lease(ctx.account_id, fan_id)


# ── The automation ───────────────────────────────────────────────────

@register("send_welcome")
async def run(account_id: str, payload: dict, *, run_id: int) -> dict:
    payload = payload or {}
    _wv = (await load_voice_blocks(account_id)).voice
    dry_run = bool(payload.get("dry_run"))
    with_image = payload.get("with_image", True)
    limit = int(payload.get("limit") or _DEFAULT_NOTIF_LIMIT)
    max_welcomes = int(payload.get("max_welcomes") or _DEFAULT_MAX_WELCOMES)

    cfg = await _load_ai_config(account_id)
    strip_emoji_on = await load_strip_emojis(account_id)  # account-wide emoji strip
    model = await resolve_model(account_id, "welcome", payload.get("model"))
    typing_wpm = await load_typing_wpm(account_id)       # per-bubble "typing" pacing
    typing_on = await load_typing_indicator(account_id)  # live "...is typing" frames
    restyle = bool(payload.get("restyle", True))         # AI-restyle the activity bubble
    # Bubble 2 says only the day / time of day / location — no activity.
    #
    # ⚠️ ON by default for a NEW rule, OFF for an absent key. Not a
    # contradiction and not the same mechanism as `human_pace` below: the
    # default is stamped at rule CREATION (the catalog's `default: True`, and
    # BrainPanel's own `WELCOME_DEFAULTS`), never applied at this read.
    # Defaulting here would turn every rule ever written without the key —
    # including sixteen cases that assert the activity path — into a clock-only
    # welcome retroactively. A new account gets the new behaviour from its
    # schema default; existing rules were migrated by an explicit flip; an
    # absent key keeps meaning what it meant when it was written.
    #
    # (So this file now carries three different spellings of "on by default", on
    # purpose: `bool(payload.get(k))` = stamped at creation, `payload.get(k,
    # DEFAULT)` = a read-default, and `_on_unless_off(payload, k)` = a read
    # default that INVERTS under the test harness. Each read says which it is.)
    time_only = bool(payload.get("time_only"))
    # Drop bubble 2 ENTIRELY — greeting(+image), then the question, then the GIF.
    # OUTRANKS `time_only` and any pin: `time_only` only changes what bubble 2
    # SAYS, and every pin ever minted is an activity line, so a bubble that has
    # been removed cannot be re-filled by either. Skipping it also skips the
    # restyle LLM CALL (not just its output), so a bubble that will never ship
    # never burns a slot of the account's daily cap. Absent = off (V5): `=== true`
    # in the UI, `bool(payload.get(...))` here, catalog default False — an old rule
    # cannot acquire this behaviour by being re-saved.
    skip_time_bubble = bool(payload.get("skip_time_bubble"))
    # Bubble 3 (optional): an operator-written question appended VERBATIM — no
    # restyle, no LLM, the same exact text for every fan. Blank/absent = off.
    # The default ("what's yours?") is stamped at rule creation/save by the Brain
    # panel, NOT here — same pattern (and same reason) as time_only above: a read
    # default would retroactively append it to every rule saved before the knob
    # existed.
    question = str(payload.get("question") or "").strip()
    # Bubble 4 (optional): the giphy id the operator picked in the Brain panel,
    # sent as its own text-less bubble. Blank/absent = off, and stamped at save by
    # the panel — never defaulted here, for the same reason as `question` above.
    gif_id = str(payload.get("gif_id") or "").strip()
    # Follow back every new sub in the same tick as the welcome (see _follow_back).
    # Default ON; `follow_back_gate` (default OFF) adds the paid-profile check.
    follow_back = bool(payload.get("follow_back", _FOLLOW_BACK_DEFAULT))
    follow_back_gate = bool(payload.get("follow_back_gate", _FOLLOW_BACK_GATE_DEFAULT))
    # Pace the burst like a person, and run several fans' bursts at once.
    #
    # `test_fan` is EXEMPT: the UI's "send test" button points this sender at one
    # fan and waits for the result, and a paced burst would make the operator sit
    # through real quiet draws to see a message he asked for now. A test send is
    # not a fan's first impression, so it keeps today's flat holds.
    # ⚠️ ABSENT = ON (operator decision, 2026-09-06). This knob shipped
    # absent-means-off behind the add-on fence; the operator then turned it on by
    # default, so an existing welcome rule that has never been re-saved paces from
    # the next tick. The catalog default and BrainPanel's `!== false` seed match
    # this, so the checkbox never shows a state the sender disagrees with (V5's
    # mismatch, deliberately not repeated). Turning it OFF writes an explicit
    # `false`, which is why the UI always writes the key rather than omitting it.
    human_pace = _on_unless_off(payload, "human_pace")
    paced = human_pace and not payload.get("test_fan")
    # The two quiet bands, overridable per rule via RAW JSON (no typed editor —
    # `_validate_payload_for_kind` passes unknown keys through). Each end is
    # clamped in the sampler; a malformed value falls back to the default there.
    # If the operator wants ~30s opens that is `"pace_gap_quiet_s": [22, 38]` on
    # the rule — one edit, no deploy.
    pace_open_quiet = payload.get("pace_open_quiet_s")
    pace_gap_quiet = payload.get("pace_gap_quiet_s")
    # Stop the burst the moment he says something back (§C). ⚠️ ABSENT = ON
    # (operator decision, 2026-09-06 — same flip as `human_pace` above), and
    # INDEPENDENT of `human_pace`: with pacing off the checks still run at every
    # bubble boundary and before the GIF; with it on, the quiet phase becomes its
    # own interruptible hold so a reply that arrives before she started typing
    # costs him nothing.
    stop_on_reply = _on_unless_off(payload, "stop_on_reply")
    # §C5 — under EITHER knob the `welcome_sent` claim moves to the moment bubble
    # 0 lands, instead of the end of the burst. A burst that is now allowed to
    # stop early (or to be several minutes long) would otherwise leave an aborted
    # or crashed fan un-claimed, and the next tick would open with a SECOND
    # greeting: the loudest possible bot tell, delivered by the anti-bot-tell
    # feature. Consequence, accepted deliberately: later bubbles never resume
    # after a crash — a truncated burst is exactly today's bubble-1-failure
    # semantics, which already ship. Both knobs off ⇒ today's placement.
    early_mark = human_pace or stop_on_reply
    run_started = time.monotonic()

    client = await asyncio.to_thread(ax._make_client, account_id)

    # Time-of-day vault image. Deterministic per hour, identical for every fan this
    # tick, so resolve ONCE — not per fan. Skipped on dry runs. Prefer the account's
    # configured per-slot image id (time_images); fall back to the legacy folder
    # picker when no slot id is set.
    hour = _model_hour(_clock_hours(cfg))
    bot_media_id: int | None = None
    if with_image and not dry_run:
        bot_media_id = _slot_image_id(cfg, hour)
        if bot_media_id is None:
            bot_media_id = await asyncio.to_thread(_bot_folder_media_id, client, hour)

    # Source the candidate subscribers.
    test_fan = payload.get("test_fan")
    if test_fan:
        nm = payload.get("test_name") or ""
        subs = [{"id": int(test_fan), "username": nm or None, "name": nm or None}]
    else:
        # Scope the fetch to the subscribe feed. `type=subscribed` (past tense,
        # the value the OF web UI's /my/notifications/subscribed tab uses) is the
        # ONLY working filter — `subscribes`/`subscriptions` 400 (verified live
        # 2026-06). The untyped feed is unusable: a content-moderation event
        # (`deactivated_media`) flood can bury every subscribe past offset 1000+,
        # silently starving welcomes (this happened to Lexi 2026-06-10 → 4 days of
        # missed welcomes). _extract_new_subscribers still filters client-side as a
        # belt-and-braces guard.
        resp = await asyncio.to_thread(
            client.notifications, limit=limit, offset=0, type="subscribed",
        )
        subs = _extract_new_subscribers(resp)

    # Dedup against welcome_sent (survives restarts → exactly one welcome).
    welcomed = await _load_welcomed(account_id)
    new_subs = [s for s in subs if s["id"] not in welcomed]

    # Auto-discovery hygiene — NOTIFICATION path only (`test_fan` is an explicit
    # force, so the live drivers / UI "send test" bypass these). Two filters:
    #   • NOT-NEW: the `type=subscribed` feed includes renewals / re-subs, and
    #     `welcome_sent` has no row for fans who pre-date this automation — so
    #     welcoming a fan we ALREADY have a real conversation with would re-welcome
    #     an established fan (e.g. a $999 whale mid-funnel). A genuinely new sub may
    #     still have a LITTLE history first (a mass blast → 1-2 outbound; their own
    #     opening DMs → a handful inbound), so we only skip once a fan EXCEEDS the
    #     tolerances (default >2 outbound or >8 inbound; payload-overridable).
    #   • CONTACT GUARD: a fan another automation touched inside the window
    #     shouldn't ALSO get a welcome this tick (defense-in-depth — an actually-
    #     new sub has no prior outbound, so this never blocks a real first welcome).
    max_out = int(payload.get("new_max_outbound", 2))
    max_in = int(payload.get("new_max_inbound", 8))
    skipped_existing = 0
    skipped_guard = 0
    skipped_restricted = 0   # muted peer-creator / hand-restricted "no automations"
    if not test_fan and new_subs:
        known = await _established_fan_ids(
            account_id, [s["id"] for s in new_subs],
            max_outbound=max_out, max_inbound=max_in)
        if known:
            new_subs = [s for s in new_subs if s["id"] not in known]
            skipped_existing = len(known)
            log.info("send_welcome skipped %d established fan(s) account=%s",
                     skipped_existing, account_id)
        guard_h = resolve_window_hours(payload.get("guard_hours"), _GUARD_DEFAULT_H)
        if new_subs and guard_h > 0:
            guard_ids = await contact_guard_excludes(account_id, outbound_hours=guard_h)
            if guard_ids:
                before = len(new_subs)
                new_subs = [s for s in new_subs if s["id"] not in guard_ids]
                skipped_guard = before - len(new_subs)
        # Durably restricted (muted peer-creator / hand-restricted) never get welcomed.
        if new_subs:
            hard_skip = await load_hard_skip_ids(account_id)
            if hard_skip:
                before = len(new_subs)
                new_subs = [s for s in new_subs if s["id"] not in hard_skip]
                skipped_restricted = before - len(new_subs)

    # Include-only audience — ordered AFTER the auto-add fast-path, on purpose:
    # a provably-new sub is enrolled into the operator's folder first (pending
    # OF confirm) and counts as INSIDE, so enforce mode never eats the welcome
    # of the very fan it is about to admit. Fans the ledger refuses (returning
    # churner, pre-baseline) stay subject to the gate: shadow logs, enforce skips.
    skipped_audience = 0
    if new_subs:
        import audience_include as _audiences
        from . import audience_sync as _audience_sync
        _pol = await _audiences.automation_audience(account_id)
        if _pol.mode != "off":
            enrolled: set[int] = set()
            if _pol.auto_add:
                for _s in new_subs:
                    try:
                        if await _audience_sync.fast_path_enroll(
                                account_id, _s["id"], client=client):
                            enrolled.add(int(_s["id"]))
                    except Exception:  # noqa: BLE001 — the roster diff is the guarantee
                        log.warning("audience fast-path enroll failed account=%s fan=%s",
                                    account_id, _s["id"], exc_info=True)
            kept = set(await _audiences.filter_candidates(
                account_id, [s["id"] for s in new_subs], kind="send_welcome",
                policy=_pol, extra_allowed_ids=enrolled))
            before = len(new_subs)
            new_subs = [s for s in new_subs if int(s["id"]) in kept]
            skipped_audience = before - len(new_subs)

    new_total = len(new_subs)

    batch_capped = new_total > max_welcomes
    if batch_capped:
        log.warning(
            "send_welcome batch capped account=%s new=%d cap=%d (rest next tick)",
            account_id, new_total, max_welcomes,
        )
        new_subs = new_subs[:max_welcomes]

    # Every per-run counter, in one object. It used to be a wall of locals the
    # loop body closed over; that body is now `_welcome_one`, a module-level
    # coroutine several of which can be in flight at once, so the alternative
    # would be twenty return values. They are still only ever mutated on the
    # event loop (single-threaded asyncio), so no lock is involved.
    t = _Tally()

    # Fans who already texted us get a FRESH per-fan restyle (never the cached
    # slot line); everyone else shares one cached rewrite per slot.
    texted_ids: set[int] = set()
    if restyle and new_subs:
        texted_ids = await _fans_with_inbound(account_id, [s["id"] for s in new_subs])

    # The per-slot restyle is ONE paid call shared by every fan in the run — a
    # contract that "check the cache, else call the LLM" only keeps while the
    # fans are serial. Concurrently, six cache MISSES race and six calls are paid
    # for one line. This lock makes the miss path one-at-a-time so the first call
    # populates the cache and the rest read it, exactly as today. The per-fan
    # FRESH path (a fan who already texted us) deliberately does not take it —
    # it has nothing to share, so serialising it would only cost latency.
    restyle_lock = asyncio.Lock()

    # Everything above, frozen and handed to each fan's coroutine (see _RunCtx).
    ctx = _RunCtx(
        account_id=account_id, voice=_wv, cfg=cfg, hour=hour, client=client,
        bot_media_id=bot_media_id, tally=t, texted_ids=texted_ids,
        restyle_lock=restyle_lock,
        dry_run=dry_run, restyle=restyle, time_only=time_only,
        skip_time_bubble=skip_time_bubble, question=question, gif_id=gif_id,
        follow_back=follow_back, follow_back_gate=follow_back_gate,
        paced=paced, stop_on_reply=stop_on_reply, early_mark=early_mark,
        pace_open_quiet=pace_open_quiet, pace_gap_quiet=pace_gap_quiet,
        model=model, typing_wpm=typing_wpm, typing_on=typing_on,
        strip_emoji_on=strip_emoji_on)


    if not paced:
        # TODAY'S SHAPE, byte-identical: one fan at a time, no jitter, no budget,
        # no gather. The run pins its executor slot for the SUM of its bursts —
        # documented (V6/H1b), deliberately not repaired here, because repairing
        # it would move timing on every account with every flag off.
        for sub in new_subs:
            await _welcome_one(ctx, sub)
    else:
        # ── The concurrent shape (§A3). A paced burst is ~85-99s of mostly
        # SLEEPING, and a run holds one of the executor's 4 GLOBAL slots for its
        # WALL time — so serialising paced bursts would price a 25-fan backlog at
        # about an hour of pinned slot. Several fans at once turns that back into
        # minutes without adding a new concurrency class: OF/Fansly clients are
        # already shared across concurrent `to_thread` sends today (client_pool
        # pins one client per account, curl_cffi keeps a handle PER THREAD, and
        # of_client keeps every per-request value in locals for exactly this
        # reason).
        sem = asyncio.Semaphore(_PACE_CONCURRENCY)
        tasks: list[asyncio.Task] = []

        async def _start_jitter(fan_id: int) -> None:
            """A short wait before this fan's FIRST bubble, so N greetings do not
            fire in the same second. Its own seeded stream, so it cannot shift
            the burst's own draws.

            It is a plain wait, not a bubble hold — nothing is being typed and no
            bubble is waiting on it. It still goes through `hold_with_typing`
            (with the indicator off, which is what makes it a plain sleep)
            because that is the ONE seam the whole subsystem's tests patch: an
            `asyncio.sleep` here would sleep 0.5-4 real seconds per fan in every
            paced case, and two of those cases pin the semaphore by parking on
            exactly this call."""
            await hold_with_typing(
                account_id, fan_id,
                Random(f"wstart:{account_id}:{fan_id}").uniform(
                    *_PACE_START_JITTER_S),
                typing_indicator=False)

        async def _welcome_holding_the_slot(one: dict) -> None:
            """One fan's paced burst. THE CALLER HAS ALREADY ACQUIRED `sem`; this
            coroutine owns releasing it, on every path including cancellation.

            The split is not an accident and it is why this is not called
            anywhere else: acquiring is the ADMISSION point, and admission has to
            happen in the loop below, where the wall-budget clock is read after
            the wait rather than before it. Releasing has to happen where the
            work ends. One name says who owns which half."""
            try:
                await _start_jitter(one["id"])
                await _welcome_one(ctx, one)
            finally:
                sem.release()

        try:
            for i, sub in enumerate(new_subs):
                # ADMISSION CONTROL, in wall time. Acquiring the semaphore IS the
                # admission point: the first `_PACE_CONCURRENCY` fans start at once
                # and the next one waits here, which is where the budget is a real
                # elapsed measurement rather than zero. A fan is NEVER stopped
                # mid-burst — overshoot is bounded by one burst — and a deferred fan
                # has no `welcome_sent` claim, so the next tick re-serves him off the
                # same feed.
                await sem.acquire()
                if time.monotonic() - run_started > _RUN_WALL_BUDGET_S:
                    # The one place the loop releases a slot it acquired: this
                    # fan never starts, so the task that would have owned the
                    # release is never created.
                    sem.release()
                    t.deferred_budget = len(new_subs) - i
                    log.warning("send_welcome wall budget spent account=%s — deferring "
                                "%d fan(s) to the next tick", account_id,
                                t.deferred_budget)
                    break
                tasks.append(asyncio.create_task(_welcome_holding_the_slot(sub)))
        except asyncio.CancelledError:
            # We were cancelled while parked on sem.acquire(). The tasks already
            # spawned are detached and would otherwise run to completion AFTER
            # this function raises — sending real DMs and issuing real follows
            # for a run the executor has already parked as cancelled.
            #
            # What this actually guarantees: a task not yet inside a blocking
            # send STOPS — parked in a hold or between bubbles, it takes the
            # cancel at its next await and ships nothing more. A task already
            # inside `asyncio.to_thread` does NOT: the thread is not
            # interruptible, so that one send or follow completes regardless.
            # That is `to_thread`, not a defect here, and it is why the run must
            # still RAISE — the executor requeues, and the next tick re-serves
            # whoever has no `welcome_sent` claim.
            for task in tasks:
                task.cancel()
            # …and WAIT for them to actually unwind. `task.cancel()` only
            # schedules the CancelledError; each child's `finally` — the one that
            # starts the fan's rest and hands the LEASE back, in that order,
            # because "ORDER IS LOAD-BEARING" — runs on a later loop turn that a
            # shutdown may never take. Left un-awaited, `tasks` goes out of scope
            # here and the interpreter's "Task was destroyed but it is pending"
            # is the only trace, with up to six fans holding a 15-minute lease
            # nobody will release: they are un-messageable by every automation
            # until it expires. `return_exceptions=True` so one child's escape
            # cannot stop the others being collected, and the second `except`
            # covers being cancelled AGAIN while collecting — the original cancel
            # is what we re-raise either way.
            if tasks:
                try:
                    await asyncio.gather(*tasks, return_exceptions=True)
                except asyncio.CancelledError:
                    pass
            raise
        if tasks:
            # One fan's escape must never kill the others' bursts mid-flight.
            for res in await asyncio.gather(*tasks, return_exceptions=True):
                # CancelledError is NOT an Exception, so return_exceptions=True
                # hands it back here as an ordinary result. Counting it as an
                # error and returning a stats dict is how a shutdown mid-tick
                # finalized as `ok`: automation_executor only parks the run and
                # requeues the job when CancelledError propagates out of run().
                # Re-raising is what makes the half-executed batch get retried
                # instead of being recorded as a clean success.
                if isinstance(res, asyncio.CancelledError):
                    raise res
                if isinstance(res, BaseException):
                    # ⚠️ DELIBERATELY DIFFERENT FROM THE SERIAL PATH, and worth
                    # knowing about: there, a per-fan crash propagates out of
                    # run(), the executor records the run as `error` and requeues
                    # the job. Here it is counted and the run still finalises
                    # `ok`. That is §A3's rule — one fan's escape must never kill
                    # five other bursts mid-flight — and the requeue it gives up
                    # is worth little now that `welcome_sent` is claimed at bubble
                    # 0: a retried run re-serves the SAME feed and skips every fan
                    # it already welcomed, so the crashed fan is retried by the
                    # next scheduled tick either way. `errors` is the signal.
                    t.errors += 1
                    log.warning("send_welcome paced fan failed account=%s",
                                account_id, exc_info=res)

    return {
        "subscribers_seen": len(subs),
        "new_subscribers": new_total,
        "welcomes_sent": t.sent,
        "image_attached": t.image_attached,
        "restyled": t.restyled,
        "restyled_cached": t.restyled_cached,
        "pinned_used": t.pinned_used,
        "gifs_sent": t.gifs_sent,
        "followed_back": t.followed_back,
        "follow_back_already": t.follow_back_already,
        "follow_back_paid_skipped": t.follow_back_paid_skipped,
        "follow_back_no_price": t.follow_back_no_price,
        "follow_back_errors": t.follow_back_errors,
        "skipped_locked": t.skipped_locked,
        "skipped_cooldown": t.skipped_cooldown,
        "skipped_existing": skipped_existing,
        "skipped_guard": skipped_guard,
        "skipped_restricted": skipped_restricted,
        "skipped_audience": skipped_audience,
        "errors": t.errors,
        "cap_hit": t.cap_hit,
        "batch_capped": batch_capped,
        "dry_run": dry_run,
        # ── Pacing (§A3/§A5). Present on every run so a dashboard does not have
        # to know which rules have the flag on; all three are inert with it off.
        "human_pace": paced,
        "run_wall_s": round(time.monotonic() - run_started, 1),
        "deferred_budget": t.deferred_budget,
        # An UPPER BOUND on `_emit_typing_for`'s un-metered emit, not a
        # measurement of it: it also carries event-loop contention from the other
        # concurrent bursts. See `_HOLD_OVERRUN_WARN_S` before deciding anything
        # (H8's Fansly gate) from this number.
        "hold_overrun_max_s": round(t.hold_overrun_max_s, 2),
        # ── stop_on_reply (§C). Also always present, also inert with the knob
        # off. `aborted_on_reply` is the feature working; `handoff_enqueued` is
        # the narrower case where a finishing bubble ate his reply and another
        # engine was asked to answer him; `handoff_no_engine` is the same case on
        # an account that runs no chat engine at all.
        #
        # ⚠️ Both `human_pace` above and `stop_on_reply` here report the knob AS
        # THIS RUN RESOLVED IT at run level — `human_pace` is already past its
        # `test_fan` exemption (the one run-level modifier either knob has), and
        # `stop_on_reply` has none. What NEITHER can say at run level is whether
        # the guard actually ran for a given FAN, because a failed anchor read
        # switches it off for that fan alone. `reply_guard_off` is that number,
        # and it is the difference between "nobody replied" and "we never
        # looked".
        "stop_on_reply": stop_on_reply,
        "reply_guard_off": t.reply_guard_off,
        "aborted_on_reply": t.aborted_on_reply,
        "handoff_enqueued": t.handoff_enqueued,
        "handoff_no_engine": t.handoff_no_engine,
        "handoff_skipped_restricted": t.handoff_skipped_restricted,
    }
