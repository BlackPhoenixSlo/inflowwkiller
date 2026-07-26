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
graduates the fan when it returns None; `ai_chatter` runs it open-ended and queues a
gen_info refill instead (see `next_for`).

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

THE API
-------
Three functions, and the engines use nothing else — not `fan_state`, not
`STATE_KEY`, not the resolver:

    next_for(fan, profile, *, one_pass=False) -> Opener | None
    need_block(opener) -> str          # the goal line for the reply prompt
    await record_used(account_id, fan_id, fan, profile, slot)

`next_for` returning None means "no opener to offer" — it does not say what to do
about it, because the two engines differ: of_ai_chat graduates the fan, ai_chatter
queues a refill and carries on. `one_pass` is the one thing that cannot be derived
from the pool, so it is a parameter rather than a guess (see `next_for`).

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
from typing import NamedTuple

from .fan_state import fan_state, set_fan_state

STATE_KEY = "_openers"           # the fans.custom_fields slot (see fan_state.py)

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


def _next_opener(profile, state) -> Opener | None:
    """The pure resolver behind `next_for`: questions first, then teases; within a
    class, the first slot that is neither blank nor already used."""
    used = _used(state, profile)
    for cls in CLASSES:
        for slot in SLOTS[cls]:
            value = getattr(profile, slot, None) if profile is not None else None
            if slot not in used and isinstance(value, str) and value.strip():
                tmpl = _Q_INSTRUCTION if cls == "q" else _T_INSTRUCTION
                text = value.strip()
                return Opener(cls, slot, text, tmpl.format(line=text))
    return None


def mark_used(state, profile, slot: str) -> dict:
    """`state` with `slot` recorded as used, stamped to the current generation.

    Stamping on WRITE is what makes a refill self-resetting: the next generation
    carries a different stamp, so `used` stops applying and every slot is fresh
    again without anyone clearing anything.

    `classes_done` is the opposite — it is NOT generation-scoped, so it survives
    every refill. It is what lets a caller ask "has this fan had his question and
    his tease *at all*", which `used` cannot answer once gen_info restocks."""
    if slot not in ALL_SLOTS:
        raise ValueError(f"unknown opener slot {slot!r}")
    out = dict(state) if isinstance(state, dict) else {}
    out["gen"] = _generation(profile)
    out["used"] = sorted(_used(state, profile) | {slot})
    done = out.get("classes_done")
    done = set(done) if isinstance(done, list) else set()
    out["classes_done"] = sorted(done | {_SLOT_CLASS[slot]})
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


def next_for(fan, profile, *, one_pass: bool = False) -> Opener | None:
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

    None means nothing to offer. What to do about that is the caller's policy:
    of_ai_chat graduates the fan, ai_chatter queues a refill and carries on."""
    state = fan_state(fan, STATE_KEY)
    if one_pass and _pass_complete(state):
        return None
    return _next_opener(profile, state)


def need_block(opener: Opener) -> str:
    """The goal line for the reply prompt. Lives here, not in the engines: both
    build the same block, and the framing is this module's business — an engine
    should not have to know that a tease must not be sold to the model as an
    opener (see `_T_INSTRUCTION`)."""
    return "YOUR GOAL THIS MESSAGE:\n" + opener.instruction


async def record_used(account_id: str, fan_id: int, fan, profile,
                      slot: str) -> None:
    """Record that `slot` was delivered. Call ONLY after a bubble has landed.

    Both engines had this as four nested calls assembled at the call site; one
    function is one place to get the read-modify-write of the shared blob right.
    Nothing is deleted — the line stays on the profile and we simply stop offering
    it — so a crash between the send and this call costs one repeated question,
    never content."""
    await set_fan_state(account_id, fan_id, STATE_KEY,
                        mark_used(fan_state(fan, STATE_KEY), profile, slot))
