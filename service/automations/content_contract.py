"""service/automations/content_contract.py — what an ask PROMISED, and how it fails.

The dependency-free leaf of the content lane, in the same role `pack_claim` plays
for the pack lane: the two shapes (`Contract`, `Resolution`) and the typed
refusals that both the PROMPTS layer and the RESOLVER need to name.

It lives apart from either of them because both import it and neither may import
the other — `content_prompts` builds a `Contract` from a thread, `content_resolver`
turns one into media. Everything here is data and vocabulary; nothing here does
I/O, calls a model, or knows what a vault is.

🚨 A refusal is a STATE, never silence. The caller turns it into a truthful
message, a fallback, or an operator escalation — but it always knows why.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# ── Typed refusals ──────────────────────────────────────────────────
NO_ASK = "no_ask"                        # nothing was asked for in the thread
UNSUPPORTED_SUBJECT = "unsupported_subject"   # asked for something nobody curated
EMPTY_SHELF = "empty_shelf"              # the curated shelf exists but is empty
EXHAUSTED_FOR_FAN = "exhausted_for_fan"  # he already owns/has seen all of it
# 🚨 The two refusals that used to hide inside EXHAUSTED_FOR_FAN, which claims he
# has seen everything. Both are SHELF facts, not fan facts, and an operator can
# only act on them once they are named: `no_media_of_kind` means describe (or
# shoot) some videos, `no_solo_media` means every match has company in frame.
NO_MEDIA_OF_KIND = "no_media_of_kind"    # he named photo/video; none of that kind fits
NO_SOLO_MEDIA = "no_solo_media"          # everything that fits has company in frame
INSUFFICIENT_QUANTITY = "insufficient_quantity"  # fewer than asked for
NO_MATCH = "no_match"                    # the matcher found nothing that fits
CUSTOM_REQUEST = "custom_request"        # he wants something that would be FILMED
VERIFY_REJECTED = "verify_rejected"      # every pick failed the adversarial check
LLM_UNAVAILABLE = "llm_unavailable"      # cap hit / provider error

# The kinds a fan can NAME in an ask, and the kinds the pool can be filtered to.
# `audio` is here so "send me a voice note" is answered honestly: every audio row
# on this roster is an OF placeholder with no prose, so the pool comes back empty
# and the refusal is NO_MEDIA_OF_KIND — which tells an operator to describe that
# shelf, instead of the ask quietly resolving to stills.
MEDIA_KINDS = ("photo", "video", "audio")

# Words that would match half the vault and tell the matcher nothing.
_STOPWORDS = frozenset({
    "the", "and", "for", "you", "your", "with", "some", "that", "this", "have",
    "want", "wanna", "send", "show", "see", "pic", "pics", "photo", "photos",
    "video", "videos", "vid", "vids", "please", "more", "one", "get", "got",
    "can", "could", "would", "like", "love", "really", "just", "them", "they",
    "her", "his", "him", "she", "from", "out", "off", "any", "all", "new",
})


@dataclass(frozen=True)
class Contract:
    """What THIS transaction promised, read from the thread.

    `strict` is the difference between "he likes feet" and "only feet, right?".
    A strict contract may never be satisfied by a substitute — the caller must
    refuse rather than send something adjacent.
    """
    # 🚨 ASK and SUBJECT are two different facts, and conflating them was a bug.
    # "come show me" and "Show me" ARE asks for content (operator, 2026-08-11)
    # with no subject in them at all; "I wanna see them go home to their
    # families" carries the words "wanna see" and is not an ask for anything.
    # So the trigger is `is_ask`, and the subject may legitimately be null.
    is_ask: bool = False
    # He is describing a shoot that would have to be MADE — a scene, a prop, a
    # setup. Operator ruling 2026-08-11: the answer is "i don't have anything
    # like that, but check this kind of video", never a wrong send and never
    # silence. So it is an ask that refuses WITH an alternative.
    custom_request: bool = False
    subject: str | None = None          # the noun as he said it ("feet")
    category: str | None = None         # a curated category, when the subject maps
    rung: str | None = None             # a rung inside it, when he was specific
    media_kind: str | None = None       # one of MEDIA_KINDS, or None (no preference)
    # 🚨 He NAMED someone else — "you and your friend", "a threesome", "with a
    # guy". Default False, and False means SOLO ONLY: operator ruling
    # 2026-08-11, "if not asked fill only with images where only she is on if
    # not specified, that is important very." Company is opt-in, never a bonus.
    company: bool = False
    strict: bool = False
    quote: str = ""                     # his own words, for the audit trail
    exclusions: list[str] = field(default_factory=list)
    # Query terms for RETRIEVAL — his own words plus the LLM's expansion of them.
    # A fan says "feet"; the vault descriptions say "soles", "toes", "barefoot",
    # "arches". A lexical scan on his word alone finds a fraction of the shelf,
    # and the model is far better at producing the vocabulary than a regex is.
    terms: list[str] = field(default_factory=list)

    @property
    def asked(self) -> bool:
        return bool(self.is_ask or self.subject or self.category)


@dataclass(frozen=True)
class Resolution:
    media_ids: list[int]
    contract: Contract
    refusal: str | None = None
    considered: int = 0                 # candidates the matcher saw
    rejected_by_verify: int = 0
    # What she CAN send when the answer to his ask is no. A refusal that offers
    # nothing is silence, and silence is the failure shape this map keeps hitting.
    alternatives: list[int] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.refusal is None and bool(self.media_ids)


def _empty(contract: Contract, refusal: str, **kw) -> Resolution:
    return Resolution(media_ids=[], contract=contract, refusal=refusal, **kw)
