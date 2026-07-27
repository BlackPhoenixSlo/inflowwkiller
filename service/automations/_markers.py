"""service/automations/_markers.py — how a model-emitted protocol marker is found.

Both chat protocols hide an instruction in the reply text: `STICKER: <tag>` for
the cat pack, `>>OFFER <id>` for the 1:1 offer engine. Each says "end your reply
with a line that is exactly this", each is stripped before the fan sees it, and
each used to be matched with its own line-anchored regex.

A prod audit (07-27, every outbound body that ever contained either keyword)
found the model does not comply, and that BOTH protocols had leaked to fans:

    *sticker: flirty*                                    dressed as an emote
    ...best place u been to in ktown? STICKER: flirty     inline after real text
    i dare ya 😏, OFFER 20                                arrows dropped

None of those START a line, so neither engine's regex caught any of them. This
module is the one place that shape is written down. A pure leaf — `re` and
nothing else — so the leaf modules that need it stay leaves.
"""
from __future__ import annotations

import re

# Markdown/roleplay dressing the model wraps a marker in: *sticker: flirty*,
# **STICKER: shy**, [sticker: sleepy].
_WRAP = r"[*_~`\"'()\[\]{}]*"


def protocol_marker_re(keyword: str) -> re.Pattern[str]:
    """Matcher for a `<keyword><payload>` marker, wherever the model put it.
    Group 1 is the payload tail, left for the caller to resolve.

    No line anchor, wrapper punctuation eaten, and a lookbehind rather than `\\b`
    so `_sticker:` matches while a word merely ENDING in the keyword
    (`unsticker:`) does not.

    Text BEFORE the marker survives; the marker itself runs to end of line. That
    split is straight off the audit: both leaks carried real fan-facing text
    AHEAD of the marker — a question to the fan, a dare — so the old whole-line
    strips would have deleted the message along with the protocol. Nothing has
    ever followed a marker on its line, because the protocol puts it last, so
    anything there is residue and goes.

    `keyword` carries the discrimination, and that is the CALLER's job: ordinary
    prose must not match it. Stickers lean on the colon ("you design stickers
    that make people obey" is a real fan-facing body). Offers need the arrows or
    a trailing id, because "well i usually offer 1k per custom vid" is also a
    real body — 636 outbound messages say "offer", and exactly one was a marker.
    """
    return re.compile(rf"{_WRAP}(?<![A-Za-z])(?:{keyword})([^\n]*)",
                      re.IGNORECASE | re.MULTILINE)
