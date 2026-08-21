"""service/automations/_funnel_routing.py — branching-storyline routing for
reply_mass_funnel.

The PURE step-graph logic split out of the walker: strip a reply to plain text,
map step NUMBERS→list indices, choose the next step from the fan's reply facts,
and the loop-guarded hop that (re)schedules a FunnelState. No DB, no OF, no LLM —
the walker (reply_mass_funnel) owns all I/O and calls these to decide WHERE to go.

Kept separate so reply_mass_funnel stays a readable size and the routing rules
(precedence, loop guard, +1 fallback) can be read and tested in isolation.

Branching contract (funnels_api validates the shape + target existence)::
    {"next": {"bought": 5, "ignored": 2, "keyword": {"more": 3}, "default": 3}}
Targets are step NUMBERS (the `step` field); resolve_next maps them to LIST
INDICES via `step_number_index`. ABSENT `next` → the classic current_step + 1
advance, so every existing (linear) funnel behaves exactly as before.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

_TAG_RE = re.compile(r"<[^>]+>")


def strip_html(s: str | None) -> str:
    if not s:
        return ""
    if "<" not in s:
        return s.strip()
    return _TAG_RE.sub("", s).strip()


def step_number_index(steps: list[dict]) -> dict[int, int]:
    """Map each step's declared `step` NUMBER → its INDEX in the list. A funnel
    numbers steps from 1; the walker drives by index, so `next` targets (step
    numbers) resolve through this."""
    out: dict[int, int] = {}
    for i, st in enumerate(steps):
        try:
            out[int(st.get("step"))] = i
        except (TypeError, ValueError):
            continue
    return out


def _keyword_hit(word: str, text_cf: str) -> bool:
    """Case-insensitive WORD match of `word` inside already-casefolded `text_cf`
    (a whole-word boundary so 'no' doesn't fire on 'now'). Blank word → no hit."""
    w = str(word).strip().casefold()
    if not w:
        return False
    return re.search(r"\b" + re.escape(w) + r"\b", text_cf) is not None


def resolve_next(step: dict, facts: dict, num_to_idx: dict[int, int], cur_idx: int) -> int:
    """The NEXT step INDEX for `step`, chosen from the fan's `facts`
    ({replied, bought, substantive, reply_text}). Precedence (spec):
        keyword match > bought > ignored(didn't reply) > default > (+1 fallback)
    A missing/blank `next`, or a target naming an unknown step, yields the linear
    cur_idx + 1 — so a funnel with no `next` walks exactly as it did before."""
    nxt = step.get("next")
    if not isinstance(nxt, dict):
        return cur_idx + 1

    def _idx(target) -> int:
        try:
            return num_to_idx.get(int(target), cur_idx + 1)
        except (TypeError, ValueError):
            return cur_idx + 1

    # 1) keyword — first matching key wins. Only a SUBSTANTIVE reply (real text,
    #    not a lone reaction — is_substantive_msg) can trip a keyword branch.
    kw = nxt.get("keyword")
    if isinstance(kw, dict) and facts.get("substantive"):
        text_cf = strip_html(facts.get("reply_text")).casefold()
        for word, target in kw.items():
            if _keyword_hit(word, text_cf):
                return _idx(target)
    # 2) bought — the fan unlocked a PPV since the baseline (#30 signal).
    if facts.get("bought") and nxt.get("bought") is not None:
        return _idx(nxt.get("bought"))
    # 3) ignored — no reply in the window.
    if not facts.get("replied") and nxt.get("ignored") is not None:
        return _idx(nxt.get("ignored"))
    # 4) default.
    if nxt.get("default") is not None:
        return _idx(nxt.get("default"))
    # 5) linear fallback.
    return cur_idx + 1


def _bump_hops(cs, max_hops: int) -> bool:
    """Increment the branching loop-guard counter. Returns True (and marks the
    state done + 'loop_guard') once it reaches `max_hops` — a backward `next` cycle
    can't spin forever. `max_hops` is passed by the walker (which owns the tunable
    constant) so the guard has no hidden module global."""
    cs.hops = (cs.hops or 0) + 1
    if cs.hops >= max_hops:
        cs.status = "done"
        cs.last_error = "loop_guard"
        return True
    return False


def route(cs, tgt: int, steps: list[dict], now: datetime,
           *, wait_min: int, guard: bool, max_hops: int) -> bool:
    """Move `cs` to step INDEX `tgt` and (re)schedule in `wait_min` minutes. When
    `guard` (the step carried a `next` map), first bump the loop-guard hop counter
    and HALT past `max_hops` instead of routing. Returns True iff the state ended
    up `done` (guard halt OR the hop ran off the end of the funnel), so the caller
    can count the completion."""
    if guard and _bump_hops(cs, max_hops):
        return True
    cs.current_step = tgt
    if tgt >= len(steps):
        cs.status = "done"
        return True
    cs.check_count = 0
    cs.next_check_at = now + timedelta(minutes=wait_min)
    return False
