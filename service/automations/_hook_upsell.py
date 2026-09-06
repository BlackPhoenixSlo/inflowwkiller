"""service/automations/_hook_upsell.py — "sell him the thing he is doing", pure parts.

Make It Right checks in 1-3 messages after a sale that everything went smoothly.
This is the selling twin of that idea: after he UNLOCKS a PPV, a few of his
messages later she reads the thread for a reason to sell him one more set — he
asks for something, he says he is off to shower or stuck in the car, or the
things he already bought were photos-only and the vault holds the video. If
there is one, the ordinary reply goes out unchanged and a priced pack follows it,
captioned in her voice.

Everything here is PURE (no DB, no I/O, no LLM) so it unit-tests without a
harness: the per-fan slot shape, the cadence windows, the bridge-line check and
the reasoning-effort pick. `ai_chatter` owns the gate (which turn may carry it),
`content_prompts.read_hook` owns the read, `pack_sender` owns the pack, and the
stamp happens on CONFIRMED send.

The cadence has NO random draw, unlike `_life_tip`. The operator's words are
"at his 5th, 6th and 7th reply after a purchase, check whether there is an
option to upsell him", so the BAND IS THE CHECK WINDOW: every one of his
messages inside it is read until one fires or the resolver refuses, and the
whole decision is explainable from the slot alone.

⚠️ This module is an ADD-ON. Nothing here changes how the post-buy rung, the HOT
ladder, the pack-on-ask lane, make_right or the life-tip behave; with
`hook_upsell_enabled` off none of it is reached at all.

Leaf module: imports nothing from ai_chatter or _common — the same rule as
`_life_tip.py` and `fan_state.py`, so a second engine can pick it up without the
8k-line import. `pack_claim` is the one exception and it is a leaf itself (re +
dataclasses): the bridge line is checked against the SAME ban the caption
composer drops lines by, and a second copy of that regex is a second answer.
"""
from __future__ import annotations

import re
from datetime import datetime

from .pack_claim import voice_line_ok

#: fans.custom_fields slot. Its value:
#:
#:     {"anchor": "<iso of the unlock this cycle counts from>",
#:      "sent": [{"w": 1, "n": 5, "msg": <offer_message_id>, "px": 2700}, ...],
#:      "spent": [1],                 # windows closed by a resolver refusal
#:      "checked_mid": <the last inbound message id the read ran on>}
#:
#: `sent` holds 0-2 entries with the early-ask switch off, 0-3 with it on. A new
#: unlock replaces the whole slot — see `cycle`.
KEY = "_hook_upsell"

# The windows, in HIS messages since the unlock, inclusive on both ends.
#: W0 — asks ONLY, and only when `hook_upsell_early_ask` is on. Off by default:
#: an ask this soon after a buy belongs to the ordinary ask lane and its caps.
EARLY = (1, 4)
#: W1 — the operator's "his 5th, 6th and 7th reply after a purchase".
FIRST = (5, 7)
#: W2 — "check again after another 5-7, or at 14-20 from the start". From the
#: SAME anchor, so a fan who went quiet does not get a second box in a minute.
SECOND = (14, 20)

#: An unlock older than this opens no cycle. A man whose last purchase was three
#: weeks ago is not "just after a buy" by any reading of the operator's ask.
MAX_AGE_DAYS = 14
#: Thread lines the read sees — `content_prompts.read_contract`'s own number.
N_MSGS = 20

#: The config vocabulary for `hook_upsell_effort`. "auto" is the shipped value
#: and means the one-step-up rule in `effort_for`.
EFFORT_PICKS = ("auto", "low", "medium", "high", "max")

# 🚨 ON TOP OF `pack_claim._VOICE_BAN`, not instead of it. That ban stops a voice
# line carrying a digit, a price, "pics", "photos", "videos" or "set" — but
# "clip" and "vid" both PASS it, and this lane's bridge sits directly above a
# claim that has already counted the media ("3 pics + 1 vid of me in the
# shower"). A bridge saying "sending u a lil clip" is a second, unbacked count
# under the contract, so the two media words the shared ban misses are banned
# here.
BRIDGE_BAN = re.compile(r"\b(clips?|vids?)\b", re.I)


def _parse(raw) -> datetime | None:
    """An iso stamp out of the slot; None when absent or corrupt."""
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return None


def cycle(state: dict | None, paid_at: datetime | None) -> dict:
    """The slot for the CURRENT purchase — the caller's state, or a fresh one.

    A new unlock starts a new cycle: two sends per purchase means the budget is
    counted from the anchor, so the old `sent`/`spent`/`checked_mid` must not
    survive it. No anchor at all (he has never unlocked anything) is `{}`, and
    `due` refuses on that.

    Compared at SECOND precision. The anchor we write and the
    `coalesce(purchased_at, created_at)` we read back are formatted by the same
    process, but a microsecond of drift between them would restart the cycle on
    every tick — which is the whole budget silently turning itself off.
    """
    if paid_at is None:
        return {}
    have = _parse((state or {}).get("anchor"))
    if have is not None and have.replace(microsecond=0) == paid_at.replace(microsecond=0):
        return dict(state or {})
    return {"anchor": paid_at.isoformat()}


def _closed(state: dict | None) -> set[int]:
    """Windows this cycle can no longer use — one send or one refusal each."""
    s = state or {}
    done = {int(e.get("w")) for e in (s.get("sent") or []) if e.get("w") is not None}
    done |= {int(w) for w in (s.get("spent") or [])}
    return done


def window(state: dict | None, n_since: int, *, early: bool = False) -> int | None:
    """Which window his `n_since`-th message falls in, or None.

    `early` is the caller's `hook_upsell_early_ask AND he is asking` — W0 is the
    asks-only window, so a caller that has no ask never opens it.
    """
    n = int(n_since)
    done = _closed(state)
    if early and EARLY[0] <= n <= EARLY[1] and 0 not in done:
        return 0
    if FIRST[0] <= n <= FIRST[1] and 1 not in done:
        return 1
    if SECOND[0] <= n <= SECOND[1] and 2 not in done:
        return 2
    return None


def due(state: dict | None, paid_at: datetime | None, *, n_since: int,
        now: datetime, message_id: int | None, early: bool = False) -> int | None:
    """Is a hook due on THIS inbound, and in which window?

    Three questions, cheapest first: is the unlock recent enough to count as
    "after a buy"; have we already read this exact message (the read costs an
    LLM call and the same message cannot produce a different answer); and does
    his count land in an open window.
    """
    if paid_at is None:
        return None
    if (now - paid_at).total_seconds() > MAX_AGE_DAYS * 86400:
        return None
    seen = (state or {}).get("checked_mid")
    if seen is not None and message_id is not None and int(seen) == int(message_id):
        return None
    return window(state, n_since, early=early)


def stamp_checked(state: dict | None, message_id: int | None) -> dict:
    """"The read ran on this inbound and found nothing." The window STAYS OPEN —
    his next message inside it is read again."""
    out = dict(state or {})
    if message_id is not None:
        out["checked_mid"] = int(message_id)
    return out


def stamp_spent(state: dict | None, w: int) -> dict:
    """"The resolver refused inside window `w`." The vault could not serve this
    hook, and re-reading on his next two messages would buy the same refusal
    three times — so the window closes and the next one still gets its chance."""
    out = dict(state or {})
    out["spent"] = sorted({*(int(x) for x in (out.get("spent") or [])), int(w)})
    return out


def stamp_sent(state: dict | None, w: int, n: int, msg_id: int | None,
               px: int | None) -> dict:
    """The CONFIRMED send. `msg_id` is the offer's own message id, which is what
    lets `own_open_offer` tell our unpaid box from anyone else's."""
    out = dict(state or {})
    out["sent"] = [*(out.get("sent") or []),
                   {"w": int(w), "n": int(n),
                    "msg": (int(msg_id) if msg_id is not None else None),
                    "px": (int(px) if px is not None else None)}]
    return out


def own_open_offer(state: dict | None, offer_message_id: int | None) -> int | None:
    """Which window sent this open offer, or None if it is not one of ours.

    The caller uses it for the supersede rule: an unpaid box from an EARLIER
    window of the same cycle is expired and unsent so the later window can send
    a fresher one (W1 over W0, W2 over W0/W1). Anything else that is open — a
    post-buy rung, an ask-lane pack, a chatter's own PPV — is not ours to
    expire, and the window simply waits.
    """
    if offer_message_id is None:
        return None
    hit = [int(e["w"]) for e in ((state or {}).get("sent") or [])
           if e.get("msg") is not None and int(e["msg"]) == int(offer_message_id)]
    return max(hit) if hit else None


def bridge_ok(text: str | None) -> bool:
    """Would her one-line bridge survive `compose_caption`, AND say nothing about
    the media? Both halves — see `BRIDGE_BAN` for why one is not enough."""
    return voice_line_ok(text) and not BRIDGE_BAN.search(str(text or ""))


def effort_for(choices, saved: str | None, pick: str = "auto") -> str:
    """The `reasoning_effort` for the hook read. `""` means "send no such field".

    Two rules, and the operator's pick wins. `pick` is `hook_upsell_effort`: a
    value this model actually offers is used as-is, because the switch exists to
    be tested in production and the operator may deliberately go DOWN.

    Otherwise AUTO — the read is one short prompt with a real judgement in it, so
    it runs ONE STEP above the model's weakest setting, and never below what the
    operator saved for the account. A model with no effort control at all (an
    empty list, or a single choice that is therefore not a choice) gets `""`.

    `choices` is `llm_client.effort_options(model)`, weakest first:
    glm-5.3-flash (low, high, max) → "high"; deepseek-v4-pro (low, medium, high)
    → "medium"; deepseek-v4-flash () → "". A pick the model does not offer is
    NOT passed through — `llm_client._resolve_effort` raises on one — so it falls
    back to auto and the caller logs that it did.
    """
    opts = [str(c) for c in (choices or [])]
    want = str(pick or "auto").strip().lower()
    if want and want != "auto" and want in opts:
        return want
    if len(opts) < 2:
        return ""
    have = str(saved or "").strip().lower()
    at = opts.index(have) if have in opts else 0
    return opts[max(1, at)]
