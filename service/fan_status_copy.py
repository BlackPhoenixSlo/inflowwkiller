"""What the FanDrawer badge SAYS — the sentences, kept out of the endpoint.

`get_fan_ai_status` evaluates a fan against every gate the engine runs. It was also
writing the prose for each verdict inline, which is how a 324-line function got that
way: policy evaluation and copywriting interleaved, so neither could be read alone.

The split is the same one `automations/_leash.py` makes one layer down. There, a gate
returns a typed verdict instead of a decision buried in a sweep. Here, each builder
turns one of those verdicts into a `Badge` — the endpoint reads "evaluate, then
describe", and the copy lives somewhere a person can edit it without touching a gate.

Every builder returns `None` when it has nothing to say, so the caller's rule stays
the one it always was: FIRST HIT WINS, and a later gate never overwrites an earlier
one's badge.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, NamedTuple

from automations import _leash


class Badge(NamedTuple):
    """One line of status as the drawer shows it."""
    state: str                  # active | off | blocked | paused | companion
    label: str                  # the chip
    detail: str                 # the sentence under it
    until: str | None = None    # ISO wake time, when the state has one


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() + "Z" if dt is not None else None


ACTIVE = Badge("active", "Active", "She'll answer his next message")


def standing_badge(*, engine_on: bool, blacklisted: bool, skip_reason: str | None,
                   rhythm_wake_at: datetime | None,
                   paused_until: datetime | None,
                   companion_until: datetime | None,
                   post_buy_until: datetime | None) -> Badge:
    """The durable reasons she is not answering, in the engine's own precedence —
    master switch, blacklist, skip_list, then the three timed states. First hit wins,
    and every branch here is a fact about stored state rather than a live gate."""
    if not engine_on:
        return Badge("off", "AI Upseller off",
                     "The engine's master switch is off for this account.")
    if blacklisted:
        return Badge("blocked", "Blacklisted", "This fan is on the blacklist.")
    if skip_reason is not None:
        return Badge("blocked", f"Skipped ({skip_reason})",
                     "A skip_list row closes the thread to the AI.")
    if rhythm_wake_at is not None:
        return Badge("paused", "On a break",
                     "Human Rhythm stepped her away to look human. She wakes on her "
                     "own — a live ask or a recent purchase makes a thread "
                     "break-proof.", _iso(rhythm_wake_at))
    if paused_until is not None:
        return Badge("paused", "Cooling down", "Per-fan cooldown after a send.",
                     _iso(paused_until))
    if companion_until is not None:
        return Badge("companion", "Talking, not selling",
                     "He said he's broke or asked to just talk. The conversation "
                     "stays on; no price goes in front of him. (A bot accusation no "
                     "longer lands him here — it's counted, but he stays sellable.)",
                     _iso(companion_until))
    if post_buy_until is not None:
        return Badge("companion", "Talking, not selling",
                     "Post-purchase ease-off — he just bought; she isn't charging "
                     "again yet.", _iso(post_buy_until))
    return ACTIVE


def burst_payload(*, tier: str, used: int, cap: int, stopped: bool, gap_min: int,
                  last_message_at: datetime | None) -> dict[str, Any]:
    """The burst numbers as the drawer reads them. `cap`/`left` are None when she is
    UNCAPPED (e.g. the post-purchase window) — the drawer shows a dash, not a zero,
    and 0 would read as "no replies allowed", the exact opposite of the truth."""
    return {
        "enabled": True,
        "tier": tier,                 # baseline | buying_signal | no_signal | …
        "used": used,                 # REPLIES this burst (3-5 bubbles = 1)
        "cap": cap or None,
        "left": max(cap - used, 0) if cap else None,
        "stopped": bool(stopped),
        # The burst resets after this much silence — that's what "continues back"
        # means for a CAP (a break, by contrast, has a wake time).
        "resets_after_minutes": gap_min,
        "last_message_at": _iso(last_message_at),
    }


def quota_free_at(q: Any, her_last_at: datetime | None) -> datetime | None:
    """The instant the backoff lifts: her last reply plus the rung being served.

    `her_last_at` is HER OWN last reply — `_OUR_KINDS` only. A human's manual message
    and a mass/PPV row are both excluded from it by the engine, so neither pushes this
    time out. That surprises people (it surprised me), and it is the whole reason this
    is computed from the engine's own anchor rather than from "the last outbound row",
    which would answer a different question and be wrong by days.

    The rung alone, deliberately un-capped by the quota day's own expiry: a day that
    turns over mid-backoff hands his ALLOWANCE back, not his wait, so the rung really
    is the whole answer to "when does she speak to him again"."""
    if her_last_at is None or not q.hold or not q.wait_h:
        return None
    return her_last_at + timedelta(hours=float(q.wait_h))


def daily_payload(q: Any, *, enforced: bool, cad: dict[str, Any] | None = None,
                  free_at: datetime | None = None) -> dict[str, Any]:
    """The daily-ceiling numbers, straight off the engine's own `_Quota`. Same
    None-not-zero rule as above for an uncapped fan (a whale, or one still inside his
    runway). `enforced` False means these are shadow figures — recorded, not served.

    `reason` is the gate's OWN verdict name, forwarded rather than re-derived. The
    client used to infer the state from the numbers — `quota == null` plus a positive
    runway meant "still being courted", a null quota without one meant "whale" — which
    is an invariant of `_quota_gate`'s exit order living in a React component. The
    engine already named every state (`QUOTA_*`); sending the name means the UI
    dispatches on it the way the gate copy dispatches on the offer gate's `why`.

    `runway_left` NOT `left`: `left` was `quota - used`, which the client can do
    itself, while the runway — the number that says how long a fan has before the
    ceiling starts applying at all — was computed by the engine and then dropped here.
    Nearly every fan on a roster is inside his runway, so that was the one figure the
    common case needed and the only one the payload refused to carry."""
    ladder = _leash.quota_ladder(cad or {})
    rung = _leash.quota_rung(q.dry_h, ladder) if ladder else -1
    return {
        "reason": q.reason,
        "used": q.used,
        "quota": q.quota or None,
        "runway_left": q.runway_left or None,
        "held": bool(q.hold),
        "enforced": bool(enforced),
        "backoff_hours": q.wait_h or None,
        "dry_days": round(q.dry_h / 24.0, 1) if q.dry_h else None,
        # The ladder and where he stands on it. Sent as the RUNGS THEMSELVES rather
        # than "rung 4 of 4", because the shape is the thing that was misread: the
        # bands are as wide as their own rungs, so a fan is usually found on the
        # longest one and it looks like the gate jumped straight there. Seeing
        # 4 · 12 · 24 · [72] answers that without a paragraph.
        "ladder_hours": [round(h, 1) for h in ladder] or None,
        "rung": rung if rung >= 0 else None,
        # When the hold lifts, as an instant — NOT "in 65h". A duration is only true
        # at the moment it is computed, and this payload is cached and re-rendered.
        "free_at": _iso(free_at) if (free_at and q.hold) else None,
    }


def burst_badge(*, stopped: bool, tier: str, used: int, cap: int,
                gap_min: int) -> Badge | None:
    """Item 21 — the burst cap. A CAP has no wake time (that's what makes it not a
    break): it lifts on a buying signal or on silence, so the copy says so instead of
    promising a clock."""
    if not stopped:
        return None
    return Badge("paused", f"Reply cap reached ({used}/{cap})",
                 f"She's made {used} replies in this burst ({tier} tier). She resumes "
                 f"on a real buying signal, or after {gap_min} min of silence starts "
                 f"a fresh burst.")


def daily_quota_badge(q: Any, *, enforced: bool, cad: dict[str, Any] | None = None,
                      free_at: datetime | None = None) -> Badge | None:
    """Item 21c — the daily ceiling. `q` is the `_Quota` the engine itself returned.

    Silent unless the hold is actually being SERVED: in shadow mode the verdict is
    computed and recorded but the reply still goes out, and a badge claiming she is
    paused when she is visibly still replying would be a lie the operator can see."""
    if not (q.hold and enforced):
        return None
    ladder = _leash.quota_ladder(cad or {})
    rung = _leash.quota_rung(q.dry_h, ladder) if ladder else -1
    # "rung 4/4" in the chip, because the number alone reads as a punishment that
    # escalates forever. It does not: the ladder is cyclic, and naming the position
    # is what makes the NEXT step legible — 4 of 4 is followed by 1 of 4, not by more.
    where = f" · rung {rung + 1}/{len(ladder)}" if rung >= 0 else ""
    # `:g` alone printed SIX significant figures of a jittered float — the rung is
    # multiplied by a random ±25%, so a live thread read "She goes quiet for 3.58587h".
    # Round first, then `:g`, so 24.0 still prints as "24" and not "24.0".
    step = ""
    if rung >= 0:
        nxt = ladder[(rung + 1) % len(ladder)]
        step = (f" The ladder is {' → '.join(f'{round(h, 1):g}h' for h in ladder)} "
                f"and then back to the first — he is on "
                f"{round(ladder[rung], 1):g}h (jittered to {round(q.wait_h, 1):g}h), "
                f"and the step after this one is {round(nxt, 1):g}h.")
    return Badge("paused", f"Daily quota reached ({q.used}/{q.quota}){where}",
                 f"She's made {q.used} replies to him in the last 24h, and he hasn't "
                 f"paid in {q.dry_h / 24:.1f} days. She goes quiet for "
                 f"{round(q.wait_h, 1):g}h from her OWN last reply — a manual message "
                 f"or a mass send does not move it — then talks to him again."
                 f"{step} This slows her down, it never stops her. "
                 f"A purchase or a content ask resumes her immediately.",
                 _iso(free_at))
