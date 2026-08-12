"""Quote-reply context for the chat prompt — OF lets a fan answer ONE specific
bubble, and this module is everything the prompt knows about that pointer.

Two calls, in this order, and nothing else crosses the boundary:

    ref   = await _quotes.resolve(account_id, fan_id)   # one DB read, per reply
    quote = _quotes.render(ref, lines)                  # marks + closing sentence

`render` returns an `Annotation` whose `.marks` the transcript join reads and whose
`.tail` closes the block. With no quote-reply it hands back empty marks and the
sentence the block always ended with, so the ~93% of turns with no quote build a
byte-identical prompt and the caller never grows an `if` (same contract as
`_customs.prompt_block`).

Split out of ai_chatter.py (2026-08-12) where it was ten loose symbols and two locals
inside a 677-line prompt builder. It has one consumer and touches nothing else — the
shape `_pins` / `_customs` / `_language` / `_voice` / `_markers` / `_objection`
already use.
"""
from __future__ import annotations

import json
import re
from collections.abc import Sequence
from typing import NamedTuple

from sqlalchemy import select

import of_shapes                   # pure readers for OF's wire shapes (quote-reply)
from db.engine import get_session
from db.models import Message

# One transcript line as the prompt builder holds it: (direction, body, message_id).
Line = tuple[str, str, int]

# How many of his recent inbounds to check for a quote-reply. He can quote a bubble
# and then send "lol" on top of it, so "the newest row" alone misses it; three rows
# covers a normal unanswered run at three tiny reads.
_SCAN = 3
_PREVIEW_CLIP = 120              # enough of the quoted bubble to identify it in prose

_TAG_RE = re.compile(r"<[^>]+>")

# What the transcript block has always ended with. Public because it is the module's
# contract for "no quote-reply here" — every path that must not change today's prompt
# returns it verbatim.
REPLY_NOW = "Reply to his last message now, in the STYLE FOR THIS MESSAGE above."

# `[A]` marks the bubble he answered, `[replying to A]` marks his answer. ASCII beats
# a glyph the model has to resolve first. ONE label, so the two strings cannot drift
# apart (they were built by slicing `"[A]"`, which quietly assumed the closing
# bracket). The pair does NOT carry its own legend — that claim lived here for three
# weeks and `_tail` is the incident that disproved it.
_LABEL = "A"


def _strip_html(s: str | None) -> str:
    """Local copy — house pattern (of_ai_chat, gen_info, transaction_ingest, …)."""
    if not s:
        return ""
    if "<" not in s:
        return s.strip()
    return _TAG_RE.sub("", s).strip()


class QuoteRef(NamedTuple):
    """One resolved quote-reply: which of his messages answered which bubble.

    Named rather than a 5-tuple because two of the fields are only legible with their
    name attached — `locked_price_cents` is 0 for a FREE bubble *and* for one he has
    already unlocked, and `quoted_is_his` is the difference between "your earlier
    message" and a sentence that puts his words in her mouth."""
    his_id: int
    quoted_id: int
    preview: str
    locked_price_cents: int
    quoted_is_his: bool


class Annotation(NamedTuple):
    """What his quote-reply adds to the prompt.

    `marks` is {message_id: line suffix} for the transcript join; `tail` is the
    sentence that closes the block. Both come from ONE `render` call because both
    answer the same two questions about the same pointer, and deriving them apart is
    how `[A]` ended up pointing at a line the closing sentence contradicted."""
    marks: dict[int, str]
    tail: str


async def resolve(account_id: str, fan_id: int) -> QuoteRef | None:
    """His most recent inbound that QUOTE-REPLIED a bubble, else None.

    Read per reply, and NOT in `_gather`: that is one query over the whole account's
    messages and the quote lives in `raw_json`, 64KB a row (see _gather's docstring).
    Here it is three rows on ix_messages_account_fan_time, only for a fan we are
    already about to answer.

    It returns the id of the message that CARRIED the quote — not "his last message".
    He can quote-reply and then send "lol", and a prompt line claiming his LAST
    message answered her bubble is a lie the model has no way to check. Annotating
    the exact line stays true whatever he sent after it, which is also why there is no
    time window here: the result describes a line of the transcript, not the state of
    the turn. (An `AND created_at > her last outbound` window looked right and was
    wrong — `last_out_at` moves on broadcasts too, so a PPV blast landing on top of
    his quote would have hidden it, the same way it used to steal the turn.) `render`
    is where that distinction is turned back into a claim about the CURRENT turn.

    `locked_price_cents` is ZERO for an already-unlocked item: the price is only worth
    surfacing while it is still an open offer, and calling a paid item "locked" is a
    falsehood she would repeat to the man who bought it."""
    async with get_session() as s:
        rows = (await s.execute(
            select(Message.message_id, Message.raw_json)
            .where(Message.account_id == str(account_id),
                   Message.fan_id == int(fan_id),
                   Message.direction == "in",
                   Message.raw_json.is_not(None))
            .order_by(Message.created_at.desc(), Message.message_id.desc())
            .limit(_SCAN)
        )).all()
    for mid, raw in rows:
        try:
            quoted = of_shapes.quoted_reply(json.loads(raw or "{}"))
        except (TypeError, ValueError):
            continue          # truncated payload → no context, never a broken reply
        if quoted is None or not quoted.get("id"):
            continue
        try:
            price = float(quoted.get("price") or 0)
        except (TypeError, ValueError):
            price = 0.0
        quoted_from = (quoted.get("fromUser") or {}).get("id")
        return QuoteRef(
            his_id=int(mid), quoted_id=int(quoted["id"]),
            preview=_strip_html(quoted.get("text"))[:_PREVIEW_CLIP],
            locked_price_cents=0 if quoted.get("isOpened") else int(round(price * 100)),
            quoted_is_his=int(quoted_from or 0) == int(fan_id))
    return None


def render(ref: QuoteRef | None, lines: Sequence[Line]) -> Annotation:
    """The ONE entry point. `lines` is the rendered transcript, and it decides
    everything below — which is why both facts are derived here, once, and named.

    `on_screen`: did the quoted bubble actually become a transcript LINE? The convo
    join drops empty bodies, so a caption-less PPV or a bare photo send is in
    `msg_ids` yet has no line; marking it would put `[A]` nowhere while
    `[replying to A]` pointed at it. Off-screen ⇒ name it in prose on HIS line, which
    is also the aged-out-of-the-tail path.

    `carrier_is_newest`: is the quote HIS NEWEST word, or does it sit behind messages
    he sent after it? Only the first is a claim about THIS TURN — see `_tail`. Newest
    INBOUND, not newest line: a broadcast landing on top of his quote does not take
    the turn from him, the same reason `resolve` refuses a `last_out_at` window.

    His own line missing (a media-only quote-reply, or it aged out) ⇒ nothing at all:
    there is no line in the prompt to annotate, and a tail that referred to one would
    point at empty air."""
    rendered = {mid for _, _, mid in lines}
    if ref is None or ref.his_id not in rendered:
        return Annotation({}, REPLY_NOW)
    on_screen = ref.quoted_id in rendered
    newest_in = next((mid for d, _, mid in reversed(lines) if d == "in"), None)
    return Annotation(_marks(ref, on_screen),
                      _tail(ref, on_screen,
                            carrier_is_newest=newest_in == ref.his_id))


def _locked(price_cents: int) -> str:
    """`$25.20 LOCKED` — one formatter for both the marker and the prose."""
    return f"${price_cents / 100:.2f} LOCKED"


def _desc(ref: QuoteRef) -> str:
    """The quoted bubble named in prose — for when it is NOT one of the lines in the
    prompt, so there is nothing to point `[A]` at.

    `whose` is load-bearing: he can quote-reply HIS OWN message, and "your earlier
    message: …" would then hand her his words as something she said. A prompt block
    whose only job is grounding must not invent a line she never wrote."""
    whose = "his own" if ref.quoted_is_his else "your"
    if ref.locked_price_cents > 0:
        return f"{whose} {_locked(ref.locked_price_cents)} message"
    return (f'{whose} earlier message: "{ref.preview}"' if ref.preview
            else f"a photo {'he' if ref.quoted_is_his else 'you'} sent")


def _marks(ref: QuoteRef, on_screen: bool) -> dict[int, str]:
    """{message_id: line suffix} for the ONE quote-reply in the tail."""
    if not on_screen:
        return {ref.his_id: f" [replying to {_desc(ref)}]"}
    # The price rides the TARGET's mark: the transcript carries her caption but never
    # what it cost, and "he is asking about the $25 he hasn't unlocked" is the whole
    # reason this feature earns its ~4/day.
    tag = (_LABEL if ref.locked_price_cents <= 0
           else f"{_LABEL} · {_locked(ref.locked_price_cents)}")
    return {ref.quoted_id: f" [{tag}]", ref.his_id: f" [replying to {_LABEL}]"}


def _tail(ref: QuoteRef, on_screen: bool, *, carrier_is_newest: bool) -> str:
    """What the markers MEAN, and — when his newest line is the quote — a closing
    instruction that names what he is actually answering.

    The markers alone were not enough. Live receipt, 2026-08-12 04:46→05:01: she
    asked "hiw many u got?" 66 seconds after he had already answered it, so he
    quote-replied HIS OWN "I have 11 i want morw tho" with a bare ".". The stored
    prompt shows both marks reached the model:

        FAN: I have 11 i want morw tho [A]
        YOU: hiw many u got?
        FAN: Hy
        FAN: . [replying to A]

        Reply to his last message now, …

    She answered "u sent just \"hy\" lol u ok out there". `[A]` was defined NOWHERE in
    the prompt — the pair was assumed to carry its own legend — and the one sentence
    that WAS defined pointed at ".", the pointer rather than the thing pointed at. So
    the legend is now stated, and on a quote turn the closing line stops naming "his
    last message", because on this turn that is not the message.

    A self-quote is RARE and it is a BUYING signal. All 5 in the 7 days to 08-12 (of
    366 quote-replies — the other 361 quote HER) are the same act: a near-empty
    carrier re-surfacing something substantive she skipped. Three were money —
    "How about sext session with unlocked content for 1 hour" (carrier "Tell me"),
    "Send me a picture bundle. Or a video of you twerking ass." ("One request. One or
    the other bae"), "So you do soles vids?" ("Soo"). Every one got answered as if the
    carrier were the message: "Soo" drew "sooo what? / u conna make me work for it or
    just hint all night", and the sext-session proposal drew a second deflection and
    then went cold. This is the highest-intent message in the thread arriving as two
    words, and it was being read as two words.

    Measured on the stored prod prompt at prod's own model and temperature, 12
    samples an arm: as-shipped 3/12 engaged what he pointed at, legend-only 4/12,
    this tail 11/12. That is repeatability on ONE case, not general accuracy.

    What this deliberately does NOT say (Codex, adversarial pass):

    * NOT "ignore the words on his pointing line". Those five carriers were disposable
      but the general field is not — "*12 now", "which one do u wanna see" and a bare
      "😂" are all comment-on-the-quote, and binning them loses the message. The quote
      is the REFERENT, his new line is his comment on it, and she answers both.
    * NOT "he already told you, own it". A self-quote does not prove she erred, and a
      forced "my bad" reads defensive or lands on a turn that never needed it.
    * NOT a blanket "don't re-ask" — that suppresses the good follow-up ("11 total or
      11 folding?"). Only the narrow, checkable form: don't ask for what the quoted
      message already gives."""
    if on_screen:
        legend = (f"In the transcript [{_LABEL}] marks a message he QUOTE-REPLIED, "
                  f"and [replying to {_LABEL}] marks the line he sent pointing back "
                  "at it.")
    else:
        legend = ("In the transcript, the line marked [replying to …] is him "
                  "QUOTE-REPLYING the message named right there on it.")
    if not carrier_is_newest or not ref.quoted_is_his:
        return f"{legend}\n\n{REPLY_NOW}"
    # Name his words inline rather than leaving the model to resolve `[A]`: the one
    # time we know the indirection failed, it failed on exactly this turn. Falls back
    # to the marker (or to prose) when the quote has no text — a photo he re-surfaced
    # has nothing to inline.
    if ref.preview:
        quoted = f'his own earlier message ("{ref.preview}")'
    elif on_screen:
        quoted = f"his own earlier message, the line marked [{_LABEL}]"
    else:
        # Nothing to inline and no line to point at. `_desc` already names it AND
        # already says "his own", so prefixing it again says it twice.
        quoted = _desc(ref)
    return (
        f"{legend} He quote-replied {quoted} — THAT message is what he is talking "
        "about, and the line he just sent is his comment on it. Read the two as ONE "
        "message and answer that. Don't ask him for anything the quoted message "
        "already tells you (a new, specific follow-up is fine).\n\n"
        "Reply to that now, in the STYLE FOR THIS MESSAGE above.")
