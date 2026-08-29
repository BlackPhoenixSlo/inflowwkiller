"""service/ppv_captions.py — caption BOX SETS for a PPV Library entry.

One PPV, one press, a rotating set of caption boxes: a hook per style written
from the media's own vision description, each composed with one of that PPV
lane's house lines.

WHY THIS IS A SET AND NOT A CAPTION
-----------------------------------
`ppv_send._pick_caption` picks ONE of a PPV's `caption_texts` boxes uniformly at
random per send. That single fact is the whole design:

  • rotation is what a SET is — consecutive sends of one PPV draw different
    boxes, so the fan never sees the same line twice running;
  • EVERY box in that set has to be a whole message, because the pick is
    UNIFORM. There is no probability to roll, nothing to seed, and NOTHING
    ADDED TO THE SEND PATH — which also means there is no send-path check
    left to catch a box that is only half a caption.

WHY THERE IS NO "HOW MANY GET THE POOL LINE" KNOB ANY MORE
----------------------------------------------------------
There was one — `framed`, defaulting to 8 of 10 — and the two boxes it left
bare are the whole of the 2026-08-29 incident: a live $3.99 PPV whose entire
message body was

    glad i finally had an excuse to wear this bra. tomorrow i lose it.

No unlock ask, no price. The knob read like a style mix and was really a 20%
chance of shipping half a caption, because NONE of the four hook styles asks
the model for a call to action — deliberately, since the pool line underneath
was assumed to carry it. That assumption was simply absent from the tail of
the set. So a bare box is legal in exactly one case now: the lane has no house
lines at all (no `caption_pool_key`, or a key that resolves to nothing), and
there the hook is all there is to send.

So this module produces boxes and stops. It writes no config: the operator reads
the set, keeps what they want, and presses Save. A caption reaching a fan has
been read by a human first — that is deliberate, because this copy goes out
verbatim, at a price, to hundreds of people at once, and a model that invents an
act the media does not show is a refund, not a typo.

WHY THE FRAME IS THE LANE'S OWN LINE
------------------------------------
Only two of the eight pools in live use (`winback_dormant`, `bundle_long`) carry
the {off}/{was}/{now} price tokens at all. `vip_whale` sells exclusivity,
`intimate_reveal` sells nerve, `teaser_free` sells the rest of it. Appending a
fixed "% off" under a whale drop would invent a pitch that lane has never made,
so the frame comes from the PPV's own pool or there is no frame.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from pydantic import BaseModel

from automations import _language, _voice
from automations import describe_media as dm
from automations.ppv_send import (
    PPV_CAPTION_POOLS,
    PPV_CAPTION_POOLS_BY_LANG,
    PPV_CAPTION_POOLS_HIM,
)

log = logging.getLogger("of-relay.ppv_captions")

# Ceilings on the composer's knobs. The UI exposes every one of them; these are
# what it cannot talk us past. BOXES_MAX is the save-time list cap, because a set
# longer than that would be silently truncated on Save.
BOXES_MAX = 20
CALLS_MAX = 6           # distinct styled hooks — and paid calls — per press
TEXT_MAX = 1500         # one caption box. THE per-caption cap: the config API
                        # imports this for its own save-time clamp, so a box
                        # that leaves here can never be truncated on Save.


class ComposeSpec(BaseModel):
    """The box-set knobs. Every one is a UI control; `clamped` below holds the
    only opinions the server keeps."""

    # Which lane's house lines to compose with — the PPV's own caption_pool_key.
    # Empty (or a pool with no lines) composes bare hooks and says so.
    caption_pool_key: str = ""
    boxes: int = 10
    # `framed` ("how many boxes carry a house line") was RETIRED 2026-08-29 —
    # its default left two of ten boxes with no ask on them (the module note).
    # There is no field for it: pydantic ignores unknown keys here, so a stale
    # client still posting `framed` composes a fully framed set with no 422.

    # How many boxes put the house line ABOVE the hook. NONE by default: the
    # pool lines are written as closing asks ("unlock it for me"), so a line on
    # top makes the ask the opener and the tease the afterthought.
    frame_top: int = 0
    # Empty → all styles, in `describe_media.CAPTION_STYLES` order.
    styles: list[str] = []

    def clamped(self) -> tuple[int, int, list[str]]:
        """(boxes, frame_top, styles), each bounded by the one before it.

        Ordered, because the knobs nest: you cannot put the line on top of more
        boxes than exist. Clamping rather than rejecting is deliberate — this is
        reached from a number input an operator is typing into, and a 422
        mid-keystroke is worse than a clamped preview.
        """
        boxes = max(1, min(int(self.boxes or 1), BOXES_MAX))
        frame_top = max(0, min(int(self.frame_top or 0), boxes))
        styles = [s for s in (self.styles or []) if s in dm.CAPTION_STYLES]
        return boxes, frame_top, (styles or list(dm.CAPTION_STYLES))[:CALLS_MAX]


def lane_frames(pool_key: str, voice: str, lang: str) -> list[str]:
    """The house lines for ONE lane, resolved exactly the way the sender resolves
    them (`ppv_send._pick_caption`): his pool for a male account — never her
    localized one, because every translated line is female-voiced by construction
    — otherwise the language pool with an English per-key fallback.

    Resolving it differently here would compose a caption in a register the same
    account's own pool sends would never use.
    """
    key = str(pool_key or "").strip()
    if not key:
        return []
    if _voice.norm_voice(voice) == _voice.VOICE_HIM:
        return list(PPV_CAPTION_POOLS_HIM.get(key) or [])
    return list(_language.localized(PPV_CAPTION_POOLS_BY_LANG, lang, key)
                or PPV_CAPTION_POOLS.get(key) or [])


def compose_boxes(hooks: list[tuple[str, str]], frames: list[str], *,
                  boxes: int, frame_top: int = 0) -> list[dict[str, Any]]:
    """Rotate `hooks` across `boxes` caption boxes, giving EVERY one of them one
    of the lane's own `frames`, `frame_top` of those with the frame ABOVE the
    hook. Pure and deterministic — the same knobs always build the same set, so
    what the operator reviews is exactly what will send.

    EVERY box, with no knob to say otherwise: the sender picks one uniformly and
    ships it whole, so a box without the lane's ask under it is a mood line over
    a paywall. The only bare box left is the one with nothing to frame — an
    empty `frames`, meaning the PPV has no `caption_pool_key` or a key that
    resolves to no lines. See the module note for what the old `framed` knob
    cost live.

    Framed boxes advance the frame index until the (hook, frame) PAIR is one we
    have not emitted. Plain `i % len(frames)` looks right and silently halves the
    rotation whenever the pool length shares a factor with the hook count — four
    hooks against a four-line pool re-emits box 0 verbatim as box 4 — and a
    duplicate box is not a second option, it is one option weighted DOUBLE in the
    sender's random pick. Skipping to an unused pair keeps every framed box
    distinct for any pool length, which no amount of after-the-fact filtering can
    do: that only ever hands back fewer boxes than were asked for.

    Bare boxes are still deduped, and there the shortfall is real and correct —
    two styles can only make two distinct bare boxes, however many the knobs ask
    for. The caller reports what it actually built.
    """
    out: list[dict[str, Any]] = []
    if not hooks:
        return out
    top_at: set[int] = set()
    if frames and frame_top > 0:
        step = boxes / frame_top
        top_at = {int(i * step + step / 2) for i in range(frame_top)}
    seen: set[str] = set()
    pairs: set[tuple[int, int]] = set()
    fi = 0
    for i in range(boxes):
        hi = i % len(hooks)
        style, hook = hooks[hi]
        if frames:
            for _ in range(len(frames)):
                if (hi, fi % len(frames)) not in pairs:
                    break
                fi += 1
            pairs.add((hi, fi % len(frames)))
            frame = frames[fi % len(frames)]
            fi += 1
            pos = "top" if i in top_at else "bottom"
            text = f"{frame}\n\n{hook}" if pos == "top" else f"{hook}\n\n{frame}"
        else:
            pos, text = None, hook
        text = text[:TEXT_MAX]
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append({"text": text, "style": style, "frame": pos})
    return out


def _result(account_id: str, *, boxes: list[dict[str, Any]],
            skipped: list[dict[str, Any]], generated: int, cost_mc: int,
            pool_lines: int = 0) -> dict[str, Any]:
    """The one response envelope, so an early return can never drift from the
    happy path's shape. `captions` is the field the editor appends."""
    return {
        "account_id": account_id,
        "captions": [b["text"] for b in boxes],
        "boxes": boxes,
        "skipped": skipped,
        "generated": generated,
        "cost_millicents": cost_mc,
        "pool_lines": pool_lines,
        # Advice about the SEND path's `ai_caption_at_send` flag, which this
        # module knows nothing about. Always present so a client reads it
        # unconditionally; the route is the one writer of a non-empty value.
        "warning": "",
    }


async def build_box_set(account_id: str, media_ids: list[int], spec: ComposeSpec,
                        *, voice: str, lang: str) -> dict[str, Any]:
    """Write one hook per style, then compose them with the lane's frames.

    `voice` and `lang` are passed in rather than re-read here: the caller already
    holds the account's config row, and a second opinion about which voice lane
    an account is in is exactly how the male pool gets missed.
    """
    boxes, frame_top, styles = spec.clamped()

    # The Vault-AI master used to stand in front of this press; the 2026-08-23
    # ruling took it out. That switch runs the SWEEP — it is what WRITES the
    # descriptions — so refusing to compose a box from a description already on file
    # because the sweep is off is a refusal with nothing behind it, and it is what
    # made this button do nothing on the accounts that never ran one. What stops a
    # box now is the media itself: `dm.caption_hook` comes back `no_description`
    # per item, before any spend, and the operator reads that reason per box.
    model = await dm.caption_model(account_id)

    # One hook per style, each from a DIFFERENT media item where the PPV has
    # them, so the set is grounded in the whole photoset rather than four
    # readings of one photo. Concurrent — independent calls, operator waiting.
    picks = [(st, media_ids[i % len(media_ids)]) for i, st in enumerate(styles)]
    written = await asyncio.gather(*[
        dm.caption_hook(account_id, mid, st, model=model) for st, mid in picks
    ])

    hooks: list[tuple[str, str]] = []
    skipped: list[dict[str, Any]] = []
    cost_mc = 0
    for (st, mid), res in zip(picks, written):
        cost_mc += int(res.get("cost_millicents") or 0)
        if res.get("ok"):
            hooks.append((st, str(res.get("caption") or "")))
        else:
            skipped.append({"media_id": mid, "style": st,
                            "reason": str(res.get("status") or "error")})
    if not hooks:
        return _result(account_id, boxes=[], skipped=skipped, generated=0,
                       cost_mc=cost_mc)

    frames = lane_frames(spec.caption_pool_key, voice, lang)
    if not frames and spec.caption_pool_key:
        # A lane was NAMED and came back empty — with every box framed, that is
        # the difference between a full set and a set of bare hooks, and the
        # operator has to read the reason before they press Add. An empty key is
        # not reported: bare hooks are exactly what a pool-less PPV asked for.
        skipped.append({"reason": "no_pool_lines",
                        "caption_pool_key": spec.caption_pool_key})
    built = compose_boxes(hooks, frames, boxes=boxes, frame_top=frame_top)

    log.info("caption_box_set account=%s media=%d styles=%s boxes=%d framed=%d "
             "generated=%d cost_mc=%d", account_id, len(media_ids),
             ",".join(styles), len(built),
             sum(1 for b in built if b["frame"]), len(hooks), cost_mc)
    return _result(account_id, boxes=built, skipped=skipped, generated=len(hooks),
                   cost_mc=cost_mc, pool_lines=len(frames))
