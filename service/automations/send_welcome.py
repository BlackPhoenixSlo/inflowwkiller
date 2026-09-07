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
    fan their welcome. ⚠️ MONEY: the paid-profile gate is ON by default
    (2026-09-07, reversing the one-day-old OFF), so a new sub who is themself a
    PRICED creator is SKIPPED rather than paid for — same posture as
    auto_follow. It costs +1 read per welcomed sub. `follow_back_gate: false`
    follows blind and saves that read, at the price of buying subscriptions.

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
import logging
import time
from collections import Counter
from collections.abc import Mapping
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import datetime
from random import Random
from types import MappingProxyType
from typing import Any, NamedTuple

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

import automation_executor as ax  # _make_client / _parse_iso / fan-lease seams
from attribution import write_outbound_attribution
from audiences import contact_guard_excludes, resolve_window_hours
from automation_registry import register
from ._common import (bool_knob, hold_with_typing,
                      load_hard_skip_ids, load_strip_emojis,
                      load_typing_indicator, load_typing_wpm, load_voice_blocks,
                      resolve_model, send_dropping_bad_media,
                      skip_unreachable_fan, test_mode, typing_delay_seconds)
from ._run_pool import run_paced
from .pacing import ROLE_GIF as _ROLE_GIF, welcome_burst_pace  # the burst's own sampler (§A1)
# The follow-notification cooldown ledger AND the follow itself, both shared with
# auto_follow so the two follow subsystems cannot double-notify one fan and
# cannot disagree about which profiles cost money. auto_follow imports nothing
# from here, so this direction is safe.
from .auto_follow import gated_follow, stamp_ping
# What the welcome SAYS — the clock, the greeting, the activity line, the restyle
# and the composer shared with the Brain panel's preview.
from .welcome_compose import (_ADJS, _ADJS_HIM, _RESTYLE_CACHE_MAX,
                              _bot_folder_media_id,
                              _clock_hours, _compose_bubbles, _fans_with_inbound,
                              _greet_token, _load_ai_config, _local_greeting,
                              _model_hour, _resolve_welcome_name,
                              _restyle_activity, _restyle_cache, _slot_image_id)
# `_ADJS`/`_ADJS_HIM` are re-exported, not used here: the compose half owns them
# now, but `test_voice_lane` asserts the two lanes stay distinct by reading them
# off THIS module, as does anything else that knew where they lived. Splitting a
# module is only behaviour-preserving if its name still answers the same
# questions.
# Whose turn it is after the burst (`stop_on_reply`), and the one reply
# predicate. Referenced through the module so the suite has ONE seam to patch
# for every read of the predicate — the anchor here and the checkpoints there.
from . import welcome_turn
from .welcome_turn import WELCOME_REST_S as _WELCOME_REST_S, Landed as _Landed
from db.engine import get_session
from db.models import WelcomeSent
from llm_client import LLMCapExceeded


log = logging.getLogger("of-relay.automation.send_welcome")

_DEFAULT_NOTIF_LIMIT = 50      # how many subscribe-notifications to pull per tick
_DEFAULT_MAX_WELCOMES = 25     # batch cap per run (logged when it bites)
_GUARD_DEFAULT_H = 12.0        # cross-automation contact-guard window (payload override)

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



@dataclass(slots=True)
class _Tally:
    """Every per-run counter, in one object.

    The per-fan body is its own coroutine and several can be in flight at once,
    so the counters live here rather than in `nonlocal`s. They are mutated only
    on the event loop — single-threaded asyncio — so nothing here needs a lock."""

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
    # follows + re-arms); `follow_back` books every OTHER `gated_follow` outcome
    # under `_classify`'s own label, so a label this lane has never seen lands
    # in the counter instead of being booked as one of the named ones.
    followed_back: int = 0
    follow_back: Counter = field(default_factory=Counter)
    follow_back_errors: int = 0
    deferred_budget: int = 0   # fans not admitted before the wall budget ran out
    # stop_on_reply (§C): bursts cut short by the fan speaking, and what
    # `hand_back_turn` decided about the turn afterwards, by its verdict.
    # `turn["handoff"]` only ever moves when a bubble of OURS landed after his
    # reply — the (b) half of the mix.
    aborted_on_reply: int = 0
    turn: Counter = field(default_factory=Counter)
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
# Default ON (2026-09-07), matching auto_follow, which has always gated: the
# operator's rule is "we always skip those". This reverses the one-day-old OFF
# of 2026-09-06 — the read it saved was never worth an unasked-for purchase,
# and no live rule had written the key, so the flip reached all of them.
# `follow_back_gate: false` opts one rule back into the blind, paying follow.
_FOLLOW_BACK_GATE_DEFAULT = True


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
# ⚠️ MONEY, unchanged by the de-duplication: `follow_back_gate` decides whether
# this lane spends a get_user before each follow. ON is the default (2026-09-07)
# and a priced sub is skipped. Turning it OFF passes `money_gate=False`, which
# calls follow_user with NO profile read — so a new sub who is themself a paid
# creator CHARGES this account their price. That was briefly the default; it is
# not any more.


def _on_unless_off(payload: dict, key: str) -> bool:
    """An ON-BY-DEFAULT payload knob: absent ⇒ on, explicit `false` ⇒ off.

    `human_pace` and `stop_on_reply` were flipped on by default by the operator
    (2026-09-06), so an existing welcome rule that has never been re-saved gets
    both. BrainPanel reads `!== false` and the catalog declares `default: True`,
    so all three sides agree and the checkbox never shows a state the sender
    disagrees with.

    ⚠️ EXCEPT under CHATTERLY_TEST_MODE, where absent ⇒ OFF. Both knobs move real
    wall-clock time: `human_pace` draws quiet seconds that are NOT wpm-derived, so
    `load_typing_wpm`'s test-mode zero does not reach them, and 84 pre-existing
    cases call `run_once` with no payload at all. A test that wants either
    behaviour passes it explicitly. `test_mode` is the same parse `load_typing_wpm`
    and `load_typing_indicator` use, so `CHATTERLY_TEST_MODE=0` means "no" here
    and there alike.

    The body is `bool_knob` with a COMPUTED default and nothing else — there is
    one boolean-knob reader in this codebase and this is a caller of it."""
    return bool_knob(payload, key, not test_mode())


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


# ── One fan's welcome, and everything a run decided before it ─────────

@dataclass(frozen=True, slots=True)
class _RunCtx:
    """Everything `run()` resolved ONCE, handed to each fan's coroutine.

    Frozen because none of it is per-fan: it must read the same to all six
    concurrent bursts. The two members every burst WRITES — `tally` and
    `restyle_lock` — are shared on purpose and only ever touched on the event
    loop; everything else is immutable by type."""

    account_id: str
    voice: str                 # the creator's lane ("her" / "him")
    cfg: Mapping[str, Any]     # the merged brain (persona / activities / images)
    hour: int                  # the creator's local hour → the time-of-day slot
    client: Any                # the account's OF/Fansly client (shared)
    bot_media_id: int | None   # the slot's vault image, resolved once per run
    tally: _Tally              # ⚠️ WRITTEN by every burst
    texted_ids: frozenset[int] # fans who already messaged us → fresh restyle
    restyle_lock: asyncio.Lock # ⚠️ contended by every burst

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


# One bubble of the burst: (text, giphy id, role). The GIF bubble is
# ("", gif_id, ROLE_GIF) — OF carries a GIF as a top-level `giphyId` beside
# EMPTY text.
_Bubble = tuple[str, str | None, str]


class _Burst(NamedTuple):
    """What one burst left behind, for the phases after it."""
    landed: int                             # bubbles that reached the wire
    last_landed: _Landed | None             # the last of them, as OF stamped it
    aborted: tuple[datetime, int] | None    # his reply, when one cut it short
    marked: bool                            # `welcome_sent` already claimed


async def _reply_anchor(ctx: _RunCtx, fan_id: int
                        ) -> tuple[tuple[datetime, int] | None, bool]:
    """THE ANCHOR (§C1): his newest WORDED inbound as of right now, and whether
    the reply guard is on for this fan.

    Taken at the earliest point per-fan state exists, so every check in the
    burst asks the one honest question: "is there a newer one than there was
    when I started?" A pre-existing inbound IS the anchor, which is what keeps
    a sub who messaged us before his welcome getting the whole burst.

    If the SNAPSHOT itself fails the fan runs with the knob OFF rather than
    against a None anchor: None means "he had said nothing", and any
    pre-existing message would then read as a brand-new reply and abort the
    burst on the strength of a failed query."""
    if not ctx.stop_on_reply:
        return None, False
    try:
        return await welcome_turn.newest_worded_inbound(ctx.account_id, fan_id), True
    except Exception:
        ctx.tally.reply_guard_off += 1
        log.warning("send_welcome reply anchor failed account=%s fan=%s — burst "
                    "runs unguarded", ctx.account_id, fan_id, exc_info=True)
        return None, False


async def _cached_restyle(ctx: _RunCtx, fan_id: int, line: str) -> str:
    """The live run's restyle: ONE cached rewrite per slot line, shared by every
    fan (the rewrites are near-identical paraphrases — no point paying per fan);
    a fan who already texted us gets a fresh per-fan call instead. Best-effort —
    any failure, cap included, falls back to the verbatim template line, because
    a restyle hiccup must never cost a fan their welcome. Once the daily cap
    trips we stop attempting restyles for the rest of the run."""
    if not ctx.restyle or ctx.tally.restyle_capped:
        return line
    ck = (str(ctx.account_id), line)
    fresh = fan_id in ctx.texted_ids
    # Only the CACHEABLE path is serialised (see `restyle_lock`): a fan who
    # already texted us pays a fresh per-fan call that shares nothing, so making
    # him queue would buy latency and nothing else.
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
            # Cache even a verbatim echo — a model that refuses to rewrite
            # shouldn't be re-asked for every fan this slot.
            _restyle_cache[ck] = styled
            while len(_restyle_cache) > _RESTYLE_CACHE_MAX:
                _restyle_cache.pop(next(iter(_restyle_cache)))
        if styled != line:
            ctx.tally.restyled += 1
            return styled
        return line


async def _compose_plan(ctx: _RunCtx, sub: dict) -> list[_Bubble] | None:
    """The whole burst for one fan — every text bubble, then the GIF — or None
    when it could not be composed (already counted as an error).

    Only the greeting TOKEN varies per fan — a resolvable name, else a token
    derived from the raw handle — with the same deterministic stutter either way
    and no LLM, so the daily cap can never cost a nameless fan their welcome.
    The bubble shape and the precedence between the three knobs that decide the
    middle one are `_compose_bubbles`', shared with the preview; each bubble's
    ROLE travels beside it because its POSITION is not stable.

    The GIF joins HERE and not in `bubbles`: the composition would have
    word-restricted it, emoji-stripped it, then dropped it as blank. Riding the
    same plan is what keeps ONE burst policy for four bubbles — sending it after
    the loop fired it even when the burst had been abandoned. `bubbles` is
    non-empty by the time it is appended, so a GIF cannot ship as a welcome on
    its own."""
    fan_id = sub["id"]
    try:
        name = await _resolve_welcome_name(ctx.account_id, fan_id, sub)
        greeting = _local_greeting(name or _greet_token(sub, ctx.voice), ctx.voice)
    except Exception:
        ctx.tally.errors += 1
        log.warning("send_welcome generate failed account=%s fan=%s",
                    ctx.account_id, fan_id, exc_info=True)
        return None
    bubbles, roles, was_pinned = await _compose_bubbles(
        greeting=greeting, cfg=ctx.cfg, hour=ctx.hour,
        skip_time_bubble=ctx.skip_time_bubble, time_only=ctx.time_only,
        ignore_pin=False, question=ctx.question,
        strip_emoji_on=ctx.strip_emoji_on,
        restyle_fn=lambda line: _cached_restyle(ctx, fan_id, line))
    if was_pinned:
        ctx.tally.pinned_used += 1
    if not bubbles:
        ctx.tally.errors += 1
        return None
    plan: list[_Bubble] = [(b, None, r) for b, r in zip(bubbles, roles)]
    if ctx.gif_id:
        plan.append(("", ctx.gif_id, _ROLE_GIF))
    return plan


async def _send_burst(ctx: _RunCtx, sub: dict, plan: list[_Bubble],
                      anchor: tuple[datetime, int] | None,
                      check_reply: bool) -> _Burst:
    """Ship the plan: image on bubble 1 only, each bubble held for its typing
    time (or its paced hold) first, checked for his reply at every boundary it
    can honestly be checked at when `check_reply` is on.

    ONE burst policy: bubble 0 failing is fatal, any later failure stops the
    burst, and every landed send is persisted the same way."""
    fan_id = sub["id"]
    media_ids = [ctx.bot_media_id] if ctx.bot_media_id is not None else []
    landed = 0
    marked = False
    last_landed: _Landed | None = None
    aborted: tuple[datetime, int] | None = None
    for idx, (part, gid, role) in enumerate(plan):
        # Bubble 0 is never interruptible — it IS the welcome, and there is
        # nothing yet for him to have replied to.
        checking = check_reply and idx >= 1
        # ── CHECKPOINT 1 (§C2.1): the top of every TEXT bubble after the
        # greeting. The GIF is excluded: its whole hold is interruptible, so the
        # check after it is strictly stronger than this one.
        if checking and not gid:
            aborted = await welcome_turn.fan_replied_since(ctx.account_id, fan_id, anchor)
            if aborted is not None:
                break
        # A GIF is PICKED, not typed: no typing time and no "...is typing" frame.
        typing_s = 0.0 if gid else typing_delay_seconds(part, ctx.typing_wpm)
        # `Pace.total_s` is the ONLY number that moves a clock — the phase
        # arguments just decide when the indicator is on inside that same total
        # (pacing.py's INVARIANT). Seeded per (account, fan, bubble) so a burst
        # replays. Unpaced ⇒ no sampler call at all, so nothing is drawn.
        pace = welcome_burst_pace(
            role=role, has_image=bool(media_ids),
            typing_s=typing_s, text=part,
            rng=Random(f"welcome_pace:{ctx.account_id}:{fan_id}:{idx}"),
            open_quiet=ctx.pace_open_quiet,
            gap_quiet=ctx.pace_gap_quiet) if ctx.paced else None
        t0 = time.monotonic()
        # ── THE HOLD, and CHECKPOINTS 2 (§C2.2, the quiet/typing boundary) and
        # 3 (§C2.3, after the GIF's beat): `_hold_segments` is the one place
        # that decides which parts of a bubble the fan may interrupt.
        for seg in _hold_segments(pace=pace, typing_s=typing_s,
                                  is_gif=bool(gid), typing_on=ctx.typing_on,
                                  interruptible=checking):
            await hold_with_typing(
                ctx.account_id, fan_id, seg.seconds,
                typing_indicator=seg.indicator, quiet_s=seg.quiet_s,
                think_at_s=seg.think_at_s, think_for_s=seg.think_for_s)
            if seg.check_after:
                aborted = await welcome_turn.fan_replied_since(
                    ctx.account_id, fan_id, anchor)
                if aborted is not None:
                    break
        if aborted is not None:
            break
        if pace is not None:
            # INSTRUMENTATION ONLY (§A5): elapsed = requested + the un-metered
            # emit overhead (`_emit_typing_for` does not subtract the awaited
            # emit from its budget). See `_HOLD_OVERRUN_WARN_S`.
            over = (time.monotonic() - t0) - pace.total_s
            if over > ctx.tally.hold_overrun_max_s:
                ctx.tally.hold_overrun_max_s = over
            if over > _HOLD_OVERRUN_WARN_S:
                log.warning("send_welcome hold overran by %.1fs account=%s "
                            "fan=%s bubble=%d (requested %.1fs)",
                            over, ctx.account_id, fan_id, idx, pace.total_s)
        try:
            if gid:
                result = await asyncio.to_thread(
                    lambda g=gid: ctx.client.send_message(fan_id, "", giphy_id=g))
            else:
                # A refused ATTACHMENT degrades to text-only rather than losing
                # the greeting. Otherwise one dead vault id blocks EVERY welcome
                # on the account: a failed bubble-0 leaves the welcome_sent
                # claim unwritten, so the next sweep regenerates and re-fails.
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
                # deliver to. Transient errors leave the claim unwritten.
                if await skip_unreachable_fan(ctx.account_id, fan_id, e, log=log):
                    await _mark_welcomed(ctx.account_id, fan_id, sub.get("username"))
                log.warning("send_welcome send failed account=%s fan=%s",
                            ctx.account_id, fan_id, exc_info=True)
            else:
                # The greeting already landed → the fan IS welcomed; losing the
                # follow-up bubble is cosmetic. Stop the burst, still mark.
                log.warning("send_welcome bubble %d failed account=%s fan=%s",
                            idx + 1, ctx.account_id, fan_id, exc_info=True)
            break
        landed += 1
        # Only a text bubble can carry the time-of-day image, so `outcome` is
        # read only in the branch that defines it.
        if gid:
            ctx.tally.gifs_sent += 1
        elif idx == 0 and outcome.media_landed:
            ctx.tally.image_attached += 1
        # Persist each landed bubble (Automation employee). The WS pump skips
        # outbound, so this is the only producer of the outbound `messages` rows.
        msg_id = result.get("id") if isinstance(result, dict) else None
        # OF's own stamp for the row we just put on the thread; the turn
        # classification needs the same number. `provider_stamped` records
        # whether it is OF's or ours — the attribution row still takes
        # `utcnow()` as its fallback, but the classification does not pretend
        # our clock is comparable to OF's. See `Landed`.
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
                # `part` is "" for the GIF bubble, which is exactly the body a
                # GIF-only row wants: its content is the giphyId.
                body=str(result.get("text") or part),
                price_cents=0,
                created_at=landed_at,
                emit_live=True,  # WORKER→SSE bridge: surface the welcome live
            )
        # THE CLAIM, as early as it can honestly be made (§C5): the greeting is
        # on the wire and attributed, so this fan HAS been welcomed — whatever
        # happens to bubbles 2-4. A burst can end early or run for minutes, and
        # the one thing that must never happen is a second greeting next tick.
        if idx == 0 and ctx.early_mark and not marked:
            await _mark_welcomed(ctx.account_id, fan_id, sub.get("username"))
            marked = True
    return _Burst(landed, last_landed, aborted, marked)


async def _follow_back_one(ctx: _RunCtx, fan_id: int) -> None:
    """Follow the fan back — the second touch, fired while the welcome is still
    the newest thing in his inbox. AFTER the welcome is marked: a follow that
    fails (or a paid profile we refuse to pay for) must never cost the fan his
    welcome or leave the sweep re-welcoming him forever, so errors are counted,
    logged, and swallowed.

    PER-RUN BOUND: one follow per LANDED welcome, capped by `max_welcomes`;
    `welcome_sent` dedups across ticks, so each fan is followed here at most
    once, ever. The gate is `gated_follow`'s own — see `auto_follow`."""
    try:
        fb_outcome = await gated_follow(
            ctx.client, fan_id, account_id=ctx.account_id, unfollow_first=False,
            money_gate=ctx.follow_back_gate)
        if fb_outcome in ("followed", "refollowed"):
            ctx.tally.followed_back += 1
            # ONE cooldown ledger across both follow subsystems: without the
            # stamp an auto_follow backfill tick would follow this fan again and
            # notify him twice. Its OWN swallow — the follow has already landed
            # and is counted, so a failed ledger write must not also book the
            # fan as an error; the cost is one duplicate notification later.
            try:
                await stamp_ping(ctx.account_id, fan_id)
            except Exception:
                log.warning("send_welcome follow-back ledger write failed "
                            "account=%s fan=%s — the follow landed; auto_follow "
                            "may notify him again", ctx.account_id, fan_id,
                            exc_info=True)
        else:
            ctx.tally.follow_back[fb_outcome] += 1
    except Exception:
        ctx.tally.follow_back_errors += 1
        log.warning("send_welcome follow-back failed account=%s fan=%s — "
                    "welcome already landed", ctx.account_id, fan_id,
                    exc_info=True)


async def _welcome_one(ctx: _RunCtx, sub: dict) -> None:
    """One fan's whole welcome, as the phase list: cooldown + lease, the reply
    anchor, compose, the burst, follow-back, the turn, then rest + lease back.

    Everything the RUN decided arrives in `ctx`; `sub` is the one thing that is
    per-fan. With `human_pace` on, six of these are in flight at once (§A3);
    with it off `run()` awaits them one at a time.

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
        anchor, check_reply = await _reply_anchor(ctx, fan_id)
        plan = await _compose_plan(ctx, sub)
        if plan is None:
            return
        if ctx.dry_run:
            ctx.tally.sent += 1  # would-send; do NOT mark welcome_sent on a dry run
            return
        burst = await _send_burst(ctx, sub, plan, anchor, check_reply)
        if burst.landed == 0:
            return
        # Mark welcome_sent AFTER at least one bubble landed — a crash
        # mid-welcome re-welcomes (rare, safe) rather than marking with no send.
        # (Already claimed at bubble 0 under `early_mark`.)
        if not burst.marked:
            await _mark_welcomed(ctx.account_id, fan_id, sub.get("username"))
        ctx.tally.sent += 1
        sent_ok = True
        if ctx.follow_back:
            await _follow_back_one(ctx, fan_id)
        # The turn goes LAST: the handoff job's `run_at` is measured from now
        # and the rest it must clear starts in the `finally`, so the follow's
        # network calls are already behind it (see `hand_back_turn`).
        if burst.aborted is not None:
            # Bursts the FAN cut short — bubbles he cost us. The tail check
            # finds replies the burst simply finished over; that is a different
            # event, and it is how the operator reads "is stop_on_reply working".
            ctx.tally.aborted_on_reply += 1
        verdict = await welcome_turn.hand_back_turn(
            ctx.account_id, fan_id, anchor=anchor, check_reply=check_reply,
            aborted=burst.aborted, last_landed=burst.last_landed,
            landed=burst.landed)
        ctx.tally.turn[verdict] += 1
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


class _Candidates(NamedTuple):
    """Who gets welcomed this tick, and who was filtered out on the way."""
    seen: int                  # notifications that parsed as a subscriber
    new_subs: list[dict]
    skipped_existing: int
    skipped_guard: int
    skipped_restricted: int    # muted peer-creator / hand-restricted "no automations"
    skipped_audience: int


async def _candidates(account_id: str, client, payload: dict, *,
                      limit: int) -> _Candidates:
    """Source this tick's subscribers and run the pre-send filters, in order:
    `welcome_sent` dedup, the NOT-NEW gate, the contact guard, the hard-skip
    list, then the include-only audience. `test_fan` is an explicit force and
    bypasses everything but the audience."""
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
        # silently starving welcomes (this happened to Ava 2026-06-10 → 4 days of
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

    return _Candidates(len(subs), new_subs, skipped_existing, skipped_guard,
                       skipped_restricted, skipped_audience)


# ── The automation ───────────────────────────────────────────────────

@register("send_welcome")
async def run(account_id: str, payload: dict, *, run_id: int) -> dict:
    payload = payload or {}
    _wv = (await load_voice_blocks(account_id)).voice
    dry_run = bool_knob(payload, "dry_run", False)
    with_image = bool_knob(payload, "with_image", True)
    limit = int(payload.get("limit") or _DEFAULT_NOTIF_LIMIT)
    max_welcomes = int(payload.get("max_welcomes") or _DEFAULT_MAX_WELCOMES)

    cfg = await _load_ai_config(account_id)
    strip_emoji_on = await load_strip_emojis(account_id)  # account-wide emoji strip
    model = await resolve_model(account_id, "welcome", payload.get("model"))
    typing_wpm = await load_typing_wpm(account_id)       # per-bubble "typing" pacing
    typing_on = await load_typing_indicator(account_id)  # live "...is typing" frames
    restyle = bool_knob(payload, "restyle", True)         # AI-restyle the activity bubble
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
    # (Every boolean in this file is read through `bool_knob`; what differs is
    # the DEFAULT each read hands it. `False` = the default is stamped at rule
    # creation, not applied here. A literal `True` = a read-default. And
    # `_on_unless_off` = a read-default that inverts under the test harness.
    # Each read says which it is by the default it passes.)
    #
    # TODO (C-N7, 2026-09-07): `time_only` is a THREE-WAY mismatch and is left
    # that way ON PURPOSE — catalog `default: True`, BrainPanel absent-as-ON,
    # this read absent-as-OFF. The migration hazard above is the reason. But it
    # is not free. TWO surfaces write this key on every save:
    #   • `BrainPanel.saveWelcome`'s update branch — open the welcome card on a
    #     legacy rule, change the greeting, and the rule becomes clock-only.
    #   • `RuleEditor.buildFromFields` — worse, because it writes EVERY
    #     catalogued bool on every save from the typed editor, off the CATALOG's
    #     default (True), so a save that touched an unrelated knob stamps
    #     `time_only: true` onto a rule that had never carried the key.
    # (`AutoFollowTab` was named here in the round-2 note and contains zero
    # `time_only` references — it edits `auto_follow` rules. Corrected.)
    # Fixing it means
    # choosing between (a) backfilling `time_only: false` onto every rule
    # written before the knob existed and then defaulting this read to True, or
    # (b) dropping the catalog default to False. Both are one-way; neither is a
    # drive-by. Do NOT "tidy" this into `bool_knob(payload, "time_only", True)`.
    time_only = bool_knob(payload, "time_only", False)
    # Drop bubble 2 ENTIRELY — greeting(+image), then the question, then the GIF.
    # OUTRANKS `time_only` and any pin: `time_only` only changes what bubble 2
    # SAYS, and every pin ever minted is an activity line, so a bubble that has
    # been removed cannot be re-filled by either. Skipping it also skips the
    # restyle LLM CALL (not just its output), so a bubble that will never ship
    # never burns a slot of the account's daily cap. Absent = off (V5): `=== true`
    # in the UI, a `False` read-default here, catalog default False — an old rule
    # cannot acquire this behaviour by being re-saved.
    skip_time_bubble = bool_knob(payload, "skip_time_bubble", False)
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
    # Both default ON; `follow_back_gate` is the paid-profile check that makes a
    # priced sub a skip instead of a purchase.
    #
    # ⚠️ `bool_knob`, not `bool(payload.get(k, DEFAULT))`. The two differ on
    # exactly one stored value: `null`. `_validate_payload_for_kind` used to skip
    # None, so a `null` reached storage, and `bool(None)` is False — the rule ran
    # with the knob OFF while BrainPanel's `!== false` read showed it TICKED, and
    # the catalog declared it True. Three sides, two answers, on the pair of keys
    # where the wrong answer is either a silently dead lane or a blind PAID
    # follow. The boundary now POPS a null for a catalogued knob, so no NEW rule
    # can store one; `bool_knob` here defends rules written before that line, and
    # stored nulls do not expire. A key that is present-but-null says nothing, so
    # it means the same as absent.
    follow_back = bool_knob(payload, "follow_back", _FOLLOW_BACK_DEFAULT)
    follow_back_gate = bool_knob(payload, "follow_back_gate", _FOLLOW_BACK_GATE_DEFAULT)
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

    cands = await _candidates(account_id, client, payload, limit=limit)
    new_subs = cands.new_subs
    new_total = len(new_subs)

    batch_capped = new_total > max_welcomes
    if batch_capped:
        log.warning(
            "send_welcome batch capped account=%s new=%d cap=%d (rest next tick)",
            account_id, new_total, max_welcomes,
        )
        new_subs = new_subs[:max_welcomes]

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
        account_id=account_id, voice=_wv, cfg=MappingProxyType(cfg), hour=hour,
        client=client, bot_media_id=bot_media_id, tally=t,
        texted_ids=frozenset(texted_ids), restyle_lock=restyle_lock,
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
        # deliberately not repaired here, because repairing it would move timing
        # on every account with every flag off.
        for sub in new_subs:
            await _welcome_one(ctx, sub)
    else:
        # ── The concurrent shape (§A3). Several fans at once turns an hour of
        # pinned slot back into minutes without adding a new concurrency class:
        # OF/Fansly clients are already shared across concurrent `to_thread`
        # sends today (client_pool pins one client per account, curl_cffi keeps
        # a handle PER THREAD, and of_client keeps every per-request value in
        # locals for exactly this reason). The pool is `_run_pool.run_paced`;
        # what is welcome-specific is the worker, the start jitter (its own
        # seeded stream, so it cannot shift the burst's own draws) and what a
        # deferred fan means — no `welcome_sent` claim, so the next tick
        # re-serves him off the same feed.
        pool = await run_paced(
            new_subs, lambda sub: _welcome_one(ctx, sub),
            concurrency=_PACE_CONCURRENCY, wall_budget_s=_RUN_WALL_BUDGET_S,
            started_at=run_started, clock=time.monotonic,
            jitter_for=lambda sub: Random(
                f"wstart:{account_id}:{sub['id']}").uniform(*_PACE_START_JITTER_S))
        t.deferred_budget = pool.deferred
        if pool.deferred:
            log.warning("send_welcome wall budget spent account=%s — deferring "
                        "%d fan(s) to the next tick", account_id, pool.deferred)
        for exc in pool.errors:
            # ⚠️ DELIBERATELY DIFFERENT FROM THE SERIAL PATH: there, a per-fan
            # crash propagates out of run() and the executor requeues the job.
            # Here it is counted and the run still finalises `ok` — one fan's
            # escape must never kill five other bursts mid-flight (§A3), and the
            # requeue is worth little now that `welcome_sent` is claimed at
            # bubble 0: the next scheduled tick re-serves the crashed fan either
            # way. `errors` is the signal.
            t.errors += 1
            log.warning("send_welcome paced fan failed account=%s",
                        account_id, exc_info=exc)

    return {
        "subscribers_seen": cands.seen,
        "new_subscribers": new_total,
        "welcomes_sent": t.sent,
        "image_attached": t.image_attached,
        "restyled": t.restyled,
        "restyled_cached": t.restyled_cached,
        "pinned_used": t.pinned_used,
        "gifs_sent": t.gifs_sent,
        # ── Follow-back lane. The counters, and — like `human_pace` and
        # `stop_on_reply` below — the two KNOBS AS THIS RUN RESOLVED THEM,
        # because the counters alone cannot be read: `followed_back: 0` means
        # "switched off", "everyone was already followed" or "silently did
        # nothing", and `follow_back` separates the first from the other two.
        #
        # ⚠️ `follow_back_gate` is MONEY and it is the reason this is not
        # optional: with it off, every number in `followed_back` is a follow
        # fired without reading the fan's price first, and some of them BOUGHT
        # a subscription. Nothing else in this bag can say that.
        "follow_back": follow_back,
        "follow_back_gate": follow_back_gate,
        "followed_back": t.followed_back,
        # `pinged` is reachable only with `unfollow_first=True`, which this lane
        # never passes; booked beside `already_following` for totality.
        "follow_back_already": (t.follow_back["already_following"]
                                + t.follow_back["pinged"]),
        "follow_back_paid_skipped": t.follow_back["paid_profile"],
        "follow_back_no_price": t.follow_back["no_price"],
        # Every non-follow outcome by `_classify`'s own label — so a label this
        # lane has never named is visible here instead of booked as one it has.
        "follow_back_outcomes": dict(t.follow_back),
        "follow_back_errors": t.follow_back_errors,
        "skipped_locked": t.skipped_locked,
        "skipped_cooldown": t.skipped_cooldown,
        "skipped_existing": cands.skipped_existing,
        "skipped_guard": cands.skipped_guard,
        "skipped_restricted": cands.skipped_restricted,
        "skipped_audience": cands.skipped_audience,
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
        # measurement of it: it also carries event-loop contention from the
        # other concurrent bursts. See `_HOLD_OVERRUN_WARN_S` before deciding
        # anything from this number.
        "hold_overrun_max_s": round(t.hold_overrun_max_s, 2),
        # ── stop_on_reply (§C). Also always present, also inert with the knob
        # off. `aborted_on_reply` is the feature working; `handoff_enqueued` is
        # the narrower case where a finishing bubble ate his reply and another
        # engine was asked to answer him; `handoff_no_engine` is the same case
        # on an account that runs no chat engine at all.
        #
        # ⚠️ Both `human_pace` above and `stop_on_reply` here report the knob AS
        # THIS RUN RESOLVED IT at run level. What NEITHER can say is whether the
        # guard actually ran for a given FAN, because a failed anchor read
        # switches it off for that fan alone. `reply_guard_off` is that number,
        # and it is the difference between "nobody replied" and "we never
        # looked".
        "stop_on_reply": stop_on_reply,
        "reply_guard_off": t.reply_guard_off,
        "aborted_on_reply": t.aborted_on_reply,
        "handoff_enqueued": t.turn["handoff"],
        "handoff_no_engine": t.turn["no_engine"],
        "handoff_skipped_restricted": t.turn["restricted"],
    }
