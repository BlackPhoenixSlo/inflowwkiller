"""
_openers.py — which gen_info opener to use next, and what has been used already.

`gen_info` mines each fan's chat into three deep questions (`fan_profiles.q1..q3`)
and three flirty teases (`tease1..tease3`). Today `deep_convo` is the only thing
that reads the questions, and it has sent 60 messages in its life. The plan
(library/CHAT_LANE_CONSOLIDATION.md) retires it and makes those openers a PHASE of
the ordinary chat engines instead:

    gather  → the existing bio-gap interrogation (unchanged)
    deepen  → one profile opener per ordinary reply turn, questions before teases
    done    → plain banter

Both engines are on it: `of_ai_chat` runs the deepen phase with `one_pass=True` and
graduates the fan when it returns None; `ai_chatter` runs it open-ended and just
falls back to banter, because it calls `_maybe_refresh_profile` after every reply
regardless — a spent pool restocks itself with no trigger of its own (see `next_for`).

WHAT THIS DELIBERATELY DOES NOT DO
----------------------------------
The first cut of this module nulled each slot as it was used, which is the obvious
reading of "use it, then it's gone". Nulling turned out to buy a whole apparatus:
a `>>PHASE_GOAL` protocol so the model could declare what it used, a lexical
heuristic to check it really had, an exact-text conditional UPDATE so a stale
worker couldn't destroy a regenerated line — and, worst, an unbounded-regeneration
hazard, because an empty class trips `gen_info.profile_is_stale`'s `lines_empty`
exemption, ~7% of generations come back empty, and `_PROFILE_KEEP_IF_EMPTY` then
preserves the NULL (gen_info's own comment warns about exactly this loop).

None of that was buying anything a used-marker doesn't. Recording WHICH slots were
used, and leaving the text alone, gets the same fan-visible behaviour — an opener
is offered once — with none of the machinery:

  • nothing is destroyed, so nothing has to be proven before destroying it;
  • the pool never empties, so `lines_empty` never fires and the regen loop
    cannot happen. gen_info needs no change at all;
  • a used line is still on the row, so "why did she ask that?" stays answerable.

The used-set is scoped to the profile GENERATION it was read from. When gen_info
regenerates, `last_generated_at` moves, the old set no longer applies and every
slot is fresh again — so a refill resets the cycle with no explicit clearing step
and no failure-baseline bookkeeping.

WHERE THE STATE LIVES
---------------------
`fans.custom_fields["_openers"]`, via `fan_state` — the canonical accessor for that
shared blob. Every writer has to read the whole blob and write it back, so
hand-rolling the cycle clobbers a sibling automation's key. `record_used` is the
only thing here that touches it, and it is the only reason engines never have to.

THE DAILY RATION
----------------
`should_offer`'s dice ration WHICH TURNS may carry an opener; they do not ration
how many a fan gets. At the house rate (0.50 on every configured account) three
independent wins inside four minutes is ordinary, and it reads as a questionnaire:
prod fired q1, q2 and q3 between 16:01 and 16:05 and the fan asked "Why all these
questions all of sudden?". A rate is not a pace.

So a class is also capped per fan per LOCAL day (`DAILY_CAP`). The day is the
CREATOR's, derived from the account's tz offset by `local_day`, because "one a day"
has to mean one of her days — a UTC boundary would double-fire mid-evening for a
Colombian account. The counter is stamped with the day it belongs to and simply
stops applying when the date rolls, so nothing has to reset it.

ONE CHANNEL (within these two engines)
--------------------------------------
`need_block` is the only way a q/tease line reaches the model from `ai_chatter` or
`of_ai_chat`. Both prompt builders used to also read `profile.tease1..3` straight
off the row and hand all three over as "lines the team wrote for him — reword them,
don't paste them verbatim": a menu, offered on every turn, outside the ration and —
the part that actually bit — outside `record_used`, so a line the model lifted from
it was never marked delivered and could be handed back days later as a deliberate
opener. Prod sent tease1 three times in one evening with all six slots already
marked used.

⚠️ The pool has other senders that this module does NOT meter, so "used" means
"used by the chat engines", not "never sent to this fan":

  • `reengage_buyers.compose_opener` picks a tease with `rng.choice` for the
    re-engagement bubble (reengage_buyers.py ~90);
  • `deep_convo` sends its tease verbatim as a message body (deep_convo.py ~765).

Neither reads or writes the used-set, so a line either one sends can still arrive
later as a `need_block` opener. Closing that means routing them through
`record_used` too — worth doing, and deliberately not done here: both have their
own send paths, and deep_convo is being retired (library/CHAT_LANE_CONSOLIDATION.md).

The menu is gone rather than filtered. Filtering it (only unused lines, capped per
day) would have kept two paths that must agree about one pool, and two paths that
must agree is the shape of the original bug. Everything the model is invited to say
now goes through the same metered door, so the used-set is a fact rather than a
suggestion. The cost is real and accepted: on turns `should_offer` declines, the
prompt carries the fan's bio and notes but no tease material.

THE API
-------
The engines use nothing else — not `fan_state`, not `STATE_KEY`, not the resolver:

    next_for(fan, profile, *, one_pass=False, today="") -> Opener | None
    need_block(opener) -> str          # the goal line for the reply prompt
    local_day(tz_offset_minutes, now) -> str    # the creator-local date stamp
    await record_used(account_id, fan_id, fan, profile, slot, *, today="")

`next_for` returning None means "no opener to offer" — it does not say what to do
about it, because the two engines differ: of_ai_chat graduates the fan, ai_chatter
falls back to banter. `one_pass` is the one thing that cannot be derived from the
pool, so it is a parameter rather than a guess (see `next_for`).

`mark_used` is the pure state transform inside `record_used`, public only so the
generation-scoping rules can be tested without a database. Engines call
`record_used`.

There is no stored PHASE. An earlier cut persisted gather/deepen/done, which is
redundant: "still gathering" is `_questions_still_needed(...)` being non-empty and
"finished" is this module returning None, both derived from state that is already
durable. A persisted copy could only drift from them. The caller asks for an opener
once its own gaps are closed, and that IS the phase.
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta
from typing import NamedTuple

from .fan_state import fan_state, set_fan_state

STATE_KEY = "_openers"           # the fans.custom_fields slot (see fan_state.py)

# How many openers of ONE class a fan may receive per creator-local day. One: the
# pool is three deep per class, so at one a day a class lasts three days — long
# enough that gen_info's ordinary 2-day cadence restocks it before it runs dry,
# and slow enough that consecutive turns can never read as a questionnaire.
DAILY_CAP = 1

# Questions before teases: a question invites an answer and keeps the thread going,
# a tease is a flourish on top of one.
CLASSES = ("q", "tease")
SLOTS = {"q": ("q1", "q2", "q3"), "tease": ("tease1", "tease2", "tease3")}
ALL_SLOTS = tuple(s for cls in CLASSES for s in SLOTS[cls])
_SLOT_CLASS = {s: cls for cls in CLASSES for s in SLOTS[cls]}


class Opener(NamedTuple):
    cls: str            # "q" | "tease"
    slot: str           # the fan_profiles column, e.g. "q2"
    text: str           # the stored line, stripped
    instruction: str    # the need_block line handed to the model


_Q_INSTRUCTION = (
    "- Ask him this, in your own words — keep the specific thing it refers to: {line}")
# gen_info writes teases as openers "to send after a silence", but both engines are
# REACTIVE — he just messaged. Never describe one to the model as an opener or the
# reply reads like it ignored him.
_T_INSTRUCTION = (
    "- Work this line in as a grounded flirty riff, reworded in your own voice — he "
    "just messaged you, so it is a reply, NOT an opener: {line}")


def _generation(profile) -> str:
    """A stamp identifying which gen_info run produced the openers now on the row.
    The used-set is scoped to this, so a regeneration restocks the pool by making
    the old set stop applying. `""` when unknown — then the set simply always
    applies, which errs towards NOT re-offering a line."""
    at = getattr(profile, "last_generated_at", None)
    return at.isoformat() if at is not None else ""


def _used(state, profile) -> set[str]:
    """The used slots that still apply to the CURRENT generation."""
    if not isinstance(state, dict) or state.get("gen") != _generation(profile):
        return set()
    used = state.get("used")
    return {u for u in used if u in ALL_SLOTS} if isinstance(used, list) else set()


def local_day(tz_offset_minutes: int | None, now: datetime | None = None) -> str:
    """The CREATOR's calendar date as `YYYY-MM-DD` — the stamp THE DAILY RATION runs
    on. `tz_offset_minutes` is the value both engines already hold for the prompt
    clock (`rhythm.tz_offset_for`); None (no timezone configured) gives `""`, which
    every caller reads as "the ration does not apply".

    Engines resolve this ONCE per run and reuse it for every fan, so a sweep that
    straddles her midnight stamps the whole batch with the day it started in. That
    is deliberate: a fan is worth at most one extra opener, and re-deriving it per
    fan would let two fans in the same sweep disagree about what day it is."""
    if tz_offset_minutes is None:
        return ""
    base = now or datetime.utcnow()
    return (base + timedelta(minutes=int(tz_offset_minutes))).strftime("%Y-%m-%d")


def _day_sent(state, today: str) -> dict[str, int]:
    """Openers delivered per class on `today`. A stamp from any other day does not
    apply, so the ration resets at the creator's midnight with nothing to clear."""
    if not today or not isinstance(state, dict) or state.get("day") != today:
        return {}
    sent = state.get("sent")
    if not isinstance(sent, dict):
        return {}
    return {k: int(v) for k, v in sent.items()
            if k in CLASSES and isinstance(v, (int, float))}


def stocked(profile, cls: str) -> list[tuple[str, str]]:
    """`(slot, text)` for every slot of `cls` that gen_info actually filled — used or
    not. Takes anything with the slot attributes: a `FanProfile`, or the Row of a
    narrow column select.

    THE definition of "this class has lines", and public because three separate
    questions are asked of it and they MUST agree:

      • the resolver — which stocked line is still free? (`_next_opener`)
      • the refill trigger — are all the stocked lines gone? (`class_spent`)
      • gen_info's gate — is the class empty, i.e. `not stocked(...)`? (`lines_empty`)

    The last two are complements, and `class_spent` is only a one-shot while they
    stay complements: if "empty" and "spent" could ever both be true of one class,
    every empty regeneration would re-arm the trigger and the unbounded loop is back.
    That invariant is worth a shared function rather than three inline walks — it was
    three inline walks, and two engines disagreeing about one pool is the bug this
    whole module was rewritten to fix."""
    if profile is None:
        return []
    out: list[tuple[str, str]] = []
    for slot in SLOTS[cls]:
        value = getattr(profile, slot, None)
        if isinstance(value, str) and value.strip():
            out.append((slot, value.strip()))
    return out


def _next_opener(profile, state, today: str = "") -> Opener | None:
    """The pure resolver behind `next_for`: questions first, then teases; within a
    class, the first slot that is neither blank, already used, nor over today's
    ration. A class at its cap is SKIPPED rather than ending the search — teases
    still ride a day whose question is spent."""
    used = _used(state, profile)
    sent = _day_sent(state, today)
    for cls in CLASSES:
        if today and sent.get(cls, 0) >= DAILY_CAP:
            continue
        for slot, text in stocked(profile, cls):
            if slot in used:
                continue
            tmpl = _Q_INSTRUCTION if cls == "q" else _T_INSTRUCTION
            return Opener(cls, slot, text, tmpl.format(line=text))
    return None


def class_spent(fan, profile) -> bool:
    """Has a WHOLE class — every question, or every tease — been delivered out of the
    current generation? The signal that this fan has run out of openers and gen_info
    should mine him fresh ones.

    Per class, not per pool, matching `gen_info`'s own `lines_empty`: a fan with three
    spent questions and two unused teases is out of questions, and waiting for the
    teases to go too would leave a whole class dead for days.

    ⚠️ A class that gen_info never stocked is NOT spent — it is empty, which is a
    different thing and the distinction is load-bearing. ~7% of generations come back
    with no lines at all; if "no teases" counted as "teases spent", the refill trigger
    would re-arm the instant it fired and every inbound message would buy another LLM
    call. That is precisely the unbounded loop that killed the null-the-slot design
    (see WHAT THIS DELIBERATELY DOES NOT DO, and gen_info's `_PROFILE_KEEP_IF_EMPTY`).
    Requiring `stocked` to be non-empty is what keeps this trigger a one-shot.

    Generation-scoped through `_used`, so a refill disarms it with no reset step."""
    used = _used(fan_state(fan, STATE_KEY), profile)
    for cls in CLASSES:
        lines = stocked(profile, cls)
        if lines and all(slot in used for slot, _ in lines):
            return True
    return False


def mark_used(state, profile, slot: str, *, today: str = "") -> dict:
    """`state` with `slot` recorded as used, stamped to the current generation.

    Stamping on WRITE is what makes a refill self-resetting: the next generation
    carries a different stamp, so `used` stops applying and every slot is fresh
    again without anyone clearing anything.

    `classes_done` is the opposite — it is NOT generation-scoped, so it survives
    every refill. It is what lets a caller ask "has this fan had his question and
    his tease *at all*", which `used` cannot answer once gen_info restocks.

    `today` (a `local_day` stamp) spends one of the day's ration; `""` records none.
    The count is stamped with the day it belongs to, so the reset at her midnight is
    a consequence of the stamp rather than a step someone could miss."""
    if slot not in ALL_SLOTS:
        raise ValueError(f"unknown opener slot {slot!r}")
    out = dict(state) if isinstance(state, dict) else {}
    out["gen"] = _generation(profile)
    out["used"] = sorted(_used(state, profile) | {slot})
    done = out.get("classes_done")
    done = set(done) if isinstance(done, list) else set()
    out["classes_done"] = sorted(done | {_SLOT_CLASS[slot]})
    if today:
        sent = _day_sent(state, today)          # already a fresh dict
        cls = _SLOT_CLASS[slot]
        sent[cls] = sent.get(cls, 0) + 1
        out["day"] = today
        out["sent"] = sent
    return out


def _pass_complete(state) -> bool:
    """Has this fan had one of EACH class delivered, ever? Generation-independent
    on purpose — see `one_pass` below."""
    done = state.get("classes_done") if isinstance(state, dict) else None
    return set(done or ()) >= set(CLASSES)


# How often the deepen phase may ride an ordinary reply, when the operator has not
# said otherwise. NOT 1.0, and that is the whole point: `next_for` only answers
# "which opener is next", never "does this turn want one at all". Without a ration
# a fan whose bio gaps are closed carries "ask him this" on EVERY reply from then
# on — a permanent interrogation cadence, and the exact bot tell the rest of this
# work exists to remove. Most turns just want to be a reply.
DEFAULT_RATE = 0.30


def should_offer(*, enabled: bool, rate: float, seed: str) -> bool:
    """Does THIS turn get an opener? Policy only — the pool is `next_for`'s business.

    Seeded per (account, fan, his message) rather than unseeded, so a retry of the
    same turn makes the same decision instead of re-rolling until it wins. Same
    reason the humanizer takes its rng from the caller.

    `rate` is clamped, so a config of 2 means "always" and -1 means "never" rather
    than raising into a send path."""
    if not enabled:
        return False
    r = min(1.0, max(0.0, rate))
    if r >= 1.0:
        return True
    if r <= 0.0:
        return False
    return random.Random(seed).random() < r


def next_for(fan, profile, *, one_pass: bool = False,
             today: str = "") -> Opener | None:
    """The opener to work into this turn's reply for an already-loaded `Fan`, or
    None. THE entry point — engines never read the blob themselves, so which slot
    of `custom_fields` this lives in stays this module's business.

    Call it only once your own bio-gap list is empty. That is what "the fan has
    graduated to openers" means, and it is why there is no phase to store.

    `one_pass` is the difference between the two engines, and it cannot be derived
    from the pool:

      • OFF (ai_chatter) — keep offering as long as there is anything unused. It
        has no graduation cutoffs; cycling through each fresh gen_info batch is
        exactly what it wants.
      • ON (of_ai_chat) — stop once one of EACH class has been delivered, ever.
        The starter lane has to hand the fan on, and "stop when the pool is dry"
        cannot do it: `_maybe_refresh_profile` runs after every successful reply,
        so gen_info restocks continuously and the pool never empties. Caught live —
        a regenerated profile landed three seconds after a send and re-armed it.

    `today` is a `local_day` stamp and applies THE DAILY RATION; `""` skips it.

    None means nothing to offer. What to do about that is the caller's policy:
    of_ai_chat graduates the fan, ai_chatter carries on with plain banter.

    The two arguments do not compose, so `one_pass` DROPS `today` rather than
    documenting that they must not be combined. A ration None is temporary — there
    is one again at her midnight — and a one-pass caller cannot tell it apart from a
    spent pool, so it would graduate the fan, one-way, for a reason that expires in
    a few hours. `one_pass` already bounds its lane at one opener per class for the
    fan's life, so it has no burst left to ration and loses nothing. Enforcing it
    here and not in prose is the same lesson as ONE CHANNEL above: an instruction
    is not a guard."""
    state = fan_state(fan, STATE_KEY)
    if one_pass:
        today = ""
        if _pass_complete(state):
            return None
    return _next_opener(profile, state, today)


def need_block(opener: Opener) -> str:
    """The goal line for the reply prompt. Lives here, not in the engines: both
    build the same block, and the framing is this module's business — an engine
    should not have to know that a tease must not be sold to the model as an
    opener (see `_T_INSTRUCTION`)."""
    return "YOUR GOAL THIS MESSAGE:\n" + opener.instruction


async def record_used(account_id: str, fan_id: int, fan, profile,
                      slot: str, *, today: str = "") -> None:
    """Record that `slot` was delivered, and spend one of today's ration.

    Both engines had this as four nested calls assembled at the call site; one
    function is one place to get the read-modify-write of the shared blob right.
    Nothing is deleted — the line stays on the profile and we simply stop offering
    it — so a crash between the send and this call costs one repeated question,
    never content.

    Call ONLY after a bubble has landed: a ration spent on a send that never went
    out would silence the fan for the rest of her day for nothing."""
    await set_fan_state(account_id, fan_id, STATE_KEY,
                        mark_used(fan_state(fan, STATE_KEY), profile, slot,
                                  today=today))
