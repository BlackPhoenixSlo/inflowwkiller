"""service/automations/pack_claim.py — the sentence he reads before he pays.

Split out of `pack_sender` on 2026-08-11. This module owns every word the fan
sees on a priced send and nothing else: no DB, no price, no wire. It is the
contract in the legal sense, and it is the artifact the whole pack lane exists
to keep honest.

## Why this is its own module

On 2026-07-31 two fans made their first-ever purchase and deleted their entire
OnlyFans accounts within hours. One asked three times *"only feet right?"*, paid
$3.25, received a bra/face selfie with no feet, and wrote *"Goodbye, you stupid
liar."* Every rule here is that message, turned into a predicate.

## The claim carries its own count

🚨 A clause is never a bare string. `Claim` pairs the text with the number it
promises, because the audit used to recover that number by summing every digit
in the rendered prose — so a fan whose own words carried a digit ("34DD tits",
"2 girls", "your top 3 sets") produced `counted=41` against 7 attached items and
was silently refused. Parsing your own output is not a check; it is a second
implementation of the thing you are checking, and it was the buggier one.

Both builders derive the count from the media list itself, so a caption cannot
disagree with what is attached even before the audit compares them.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# 🚨 Fan-facing rung phrases. Internal folder names are TRIAGE vocabulary and are
# wrong for a fan: `nude` reads as HER BODY, which is rung 3. The negative half
# is load-bearing — "bare feet" alone leaves him free to expect rung 3 at $59.
RUNG_PHRASES: dict[tuple[str, str], str] = {
    ("feet", "tease"): "feet, covered",
    ("feet", "nude"): "bare feet, no nudity",
    ("feet", "nude-body"): "bare feet, and me nude with them",
}

# The corpus word, per (category, rung-agnostic). Real asks are short and
# literal — median 35 characters — so the noun is his, not ours.
ASK_NOUN: dict[str, str] = {"feet": "feet pics"}

# Subjects that are the MEDIA, not a thing depicted in it. The clause already
# names the format, so repeating it produces "3 vids of videos".
_MEDIA_NOUNS = frozenset({
    "video", "videos", "vid", "vids", "clip", "clips",
    "pic", "pics", "picture", "pictures", "photo", "photos", "image", "images",
    "content", "stuff", "set", "sets", "media",
})

# A voice line may not carry a number, a price, or a content claim: the clause
# above it is the contract, and a second claim underneath can contradict it.
_VOICE_BAN = re.compile(r"[0-9$€£]|\bpics?\b|\bphotos?\b|\bvideos?\b|\bset\b", re.I)

_REPLY_MAX_CHARS = 600        # OF truncates the chat-list preview past this

# The kinds that are moving images. Defined HERE, in the dependency-free leaf,
# and imported by `pack_pricing` — a clip priced as video but captioned "pic" is
# the caption/payload disagreement this whole lane exists to prevent, and two
# copies of this tuple is how that happens.
MOVING_KINDS = ("video", "gif")

# Her VOICE. Named separately from stills because it is neither — see
# `_media_phrase`, where "not moving" used to mean "a picture".
AUDIO_KINDS = ("audio",)
# The authored noun's own media word, stripped when the phrase names the media
# itself: "feet pics" + a clip becomes "5 pics + 2 vids of bare feet".
_TRAILING_MEDIA_WORD = re.compile(r"\s*\b(pics?|photos?|videos?|vids?)\b\s*$")


@dataclass(frozen=True)
class Claim:
    """What the caption promises: the words, and the number of pieces.

    `n` is always derived from the media list the clause was built from — never
    passed in, never parsed back out.
    """
    text: str
    n: int


def _media_phrase(kinds: list[str]) -> tuple[str, int]:
    """`"6 pics + 1 vid"` and the count it claims.

    The ONE place a pack's media is counted and named. Both clause builders go
    through it, because two implementations of the contract line is exactly the
    drift that put a video inside a pack captioned "7 bare feet pics".

    🚨 AUDIO is its own word. It used to fall into the `pics` bucket by
    subtraction — anything not moving was a picture — so a voice note shipped as
    "3 pics". 699 audio rows on this roster are retrievable today, which makes
    that a caption that lies about what he is paying for, in the one line he
    reads before deciding. The same bug as the video-in-a-stills-pack above,
    caught before it reached a fan rather than after.
    """
    low = [str(k or "").lower() for k in kinds]
    vids = sum(1 for k in low if k in MOVING_KINDS)
    auds = sum(1 for k in low if k in AUDIO_KINDS)
    buckets = ((len(low) - vids - auds, "pic"), (vids, "vid"), (auds, "voice note"))
    parts = [f"{n} {word}{'s' * (n > 1)}" for n, word in buckets if n]
    # An empty pack is not a phrase, but it is not this function's error to
    # raise either — `audit_ask` rule 1 compares the count and refuses the send.
    return (" + ".join(parts) if parts else "0 pics"), len(low)


def render_clause(category: str, rung: str, kinds: list[str]) -> Claim:
    """The CURATED claim — an authored rung phrase, counted from the media.

    Grammar is `"{n} {rung-qualified ask noun}"`, the rung folded INTO the noun
    rather than trailing in a dash-clause, because OF truncates a long caption in
    the chat-list preview: a voice-first caption shows him the flirt and hides
    the subject at exactly the moment he decides whether to open it.

    🚨 The `kinds` list is REQUIRED. This once said "pics" about packs containing
    VIDEO — caught 2026-08-11 in a live dry-run, where a 7-item feet pack held 5
    photos and a 4-second clip and was captioned "7 bare feet pics". `ASK_NOUN`
    hardcodes the media word per category; the shelves were stills when it was
    written and the whole-vault work put clips on them. No audit rule caught it,
    because `audit_pack`'s rules 1-5 check MEMBERSHIP and never the noun.

    It used to take `n` and an OPTIONAL `kinds`, which let a caller state a count
    the media did not support. Deriving both from one list removes the
    disagreement rather than checking for it.
    """
    body, n = _media_phrase(kinds)
    noun = ASK_NOUN.get(category, f"{category} pics")
    if any(str(k or "").lower() in MOVING_KINDS for k in kinds):
        subject = _TRAILING_MEDIA_WORD.sub("", noun).strip()
        if not subject:
            return Claim(body, n)
        return Claim(f"{body} of {'bare ' if rung == 'nude' else ''}{subject}", n)
    # All stills: keep the authored wording, which reads better than the generic
    # "7 pics of bare feet" and is what the shelves have shipped since 08-01.
    if rung == "nude":
        return Claim(f"{n} bare {noun}", n)
    phrase = RUNG_PHRASES.get((category, rung))
    if phrase:
        return Claim(f"{n} {noun} — {phrase}", n)
    return Claim(f"{n} {noun}", n)


def _his_noun(subject: str | None) -> str:
    """His word for what he asked for, or "" when he named only the format.

    "3 vids of videos" — his subject IS the media word. Live dry-run against a
    fan whose whole ask is the format ("do you have vids for purchase").
    Naming it twice reads as broken, and the count already says it.
    """
    noun = " ".join(str(subject or "").split())[:40]
    return "" if noun.lower().strip() in _MEDIA_NOUNS else noun


def needs_action(subject: str | None, substitute: bool) -> bool:
    """Does this caption need the describe-derived "what she is doing in it"?

    The rule lives HERE because it is a fact about the words, not about the
    planner: a caption needs the phrase exactly when it would otherwise say
    nothing about the media.

      • a SUBSTITUTE always needs it — its whole subject clause is a hedge;
      • a FIT needs it only when his ask named no noun. "7 pics of ass" already
        says what it is, and paying a model to re-say it is how a caption that
        has shipped for weeks starts drifting.

    🚨 It gates an LLM CALL, so a False here is money as well as words.
    """
    return bool(substitute or not _his_noun(subject))


def ask_clause(kinds: list[str], subject: str | None,
               action: str = "") -> Claim:
    """The VAULT-WIDE claim: a count, the media word, and his own noun.

    It has to name the MEDIA. "6 booty" is not English and, worse, is not a
    promise he can hold her to — and OF truncates a long caption in the
    chat-list preview, so this is often the only thing he reads before deciding
    whether to unlock.

    No subject is a legitimate answer here ("show me" names nothing), and the
    clause used to simply drop the noun — which is how a $87 send went out
    captioned "3 vids" and nothing else (Lucas1, 2026-08-16; he bought it
    anyway). Operator ruling that day: when his ask named nothing, SAY WHAT IS
    IN IT, shortly. `action` is that phrase, from the vault's own describe pass
    via `content_resolver.action_phrase`, and it is used ONLY in the no-noun
    case — a caption that already names his subject is left byte-identical.
    """
    body, n = _media_phrase(kinds)
    noun = _his_noun(subject)
    if noun:
        return Claim(f"{body} of {noun}", n)
    act = clean_action(action)
    return Claim(f"{body}, {act}" if act else body, n)


# The lead of a SUBSTITUTE claim, and the thing `audit_ask` checks survived to
# the wire.
#
# It hedges and it does NOT apologise — operator ruling 2026-08-16, "make a
# girly wording, I'm not sure this is exactly it, here is the … , hope you will
# enjoy — reduced by 70-90%". That is the 2026-08-13 frame ("my quirky version
# of joi") moved one step toward honesty and three steps shorter: he is told up
# front that this is not the thing, in her voice, in four words, and then he is
# told what it IS. Sold as a flat shortfall ("i dont have that") a substitute
# invites him to decline; sold as a hedge plus a real act, the miss is a tease.
#
# 🚨 The EMOJI IS NOT PART OF THE CHECKED PREFIX. `is_substitute_claim` matches
# `_SUBSTITUTE_LEAD` alone, so any path that strips emoji from an outgoing body
# cannot make the audit refuse a caption that framed itself correctly.
_SUBSTITUTE_LEAD = "not exactly"
_SUBSTITUTE_EMOJI = "🙈"

# The action phrase is written by a model and lands in the contract, so it is
# bounded like one. `_VOICE_BAN` is reused deliberately: a phrase carrying a
# digit, a price or a media word would state a second claim under the one the
# audit checks, which is the exact failure `compose_caption` already refuses a
# voice line for.
_ACTION_MAX_CHARS = 48


def clean_action(text: str | None) -> str:
    """A model's "what she is doing in it" phrase, made safe for the contract.

    Returns "" for anything it cannot vouch for — an empty action is a caption
    that reads exactly as it did before this existed, which is why every check
    here drops the phrase rather than repairing it. A repaired phrase is still a
    phrase nobody wrote.
    """
    line = " ".join(str(text or "").split()).strip().strip('"').strip()
    line = line.rstrip(".!").strip().lower()
    if not line or _VOICE_BAN.search(line):
        return ""
    if len(line) > _ACTION_MAX_CHARS:
        line = line[:_ACTION_MAX_CHARS].rsplit(" ", 1)[0].strip()
    return line if len(line) >= 3 else ""


def substitute_clause(kinds: list[str], subject: str | None,
                      action: str = "") -> Claim:
    """The claim for media that is NOT what he asked for.

    🚨 The one clause that must never promise its subject. `ask_clause` renders
    "4 vids of joi", and `audit_ask` checks count, liveness and company — never
    whether the media matches the noun. So routing substitutes through the
    normal builder ships a lie that passes every existing gate, which is worse
    than the silence it replaces. The difference is which side of "but" his noun
    falls on: "not exactly joi but …" is a hedge, "4 vids of joi" is a promise.

    `action` is what she is DOING in the media he is about to receive, derived
    from the vault's own descriptions (`content_resolver.action_phrase`) and
    sanitised by `clean_action`. It is the "but I have THIS" half — without it a
    substitute names only what it ISN'T, which is a refusal wearing a price. It
    is optional on purpose: a capped or erroring model must degrade to the old
    caption, never to silence.

    Same `_media_phrase` as every other clause, so a mixed send says "3 pics +
    1 vid" and the count the audit checks is the count he receives.
    """
    body, n = _media_phrase(kinds)
    hedge = f"{_SUBSTITUTE_LEAD} {_his_noun(subject) or 'that'} but {_SUBSTITUTE_EMOJI}"
    return Claim(f"{hedge} {clean_action(action) or 'my own take on it'} — {body}", n)


def is_substitute_claim(text: str) -> bool:
    """Did the substitute frame survive to the wire? `audit_ask`'s rule 4.

    Checked at the START of the string on purpose: `compose_caption` leads with
    the clause because OF truncates the chat-list preview, so a frame that has
    drifted into the tail is a frame he will not read before deciding.
    """
    return str(text or "").lstrip().lower().startswith(_SUBSTITUTE_LEAD)


def product_description(category: str, rung: str) -> str:
    """The blurb stored on the rung's `CatalogItem`, for the model to quote.

    🚨 COUNT-FREE. It is stored once and serves fans receiving 3 to 12 items, so
    a stored "11 bare feet pics" is a lie to the fan who got 7 — and this is the
    text `_pending_block` answers "its feet right?" from. The number lives only
    in the rendered claim.
    """
    phrase = RUNG_PHRASES.get((category, rung), f"{category}, {rung}")
    return (f"{phrase}. Sold as a set of photos from her {category} collection; "
            f"the number of photos depends on the price.")


def compose_caption(claim: Claim, voice_line: str | None) -> str:
    """Claim clause first, then her reply to the thread.

    ⚠️ The clause LEADS because OF truncates a long caption in the chat-list
    preview: a voice-first caption shows him the flirt and hides the subject at
    exactly the moment he decides whether to open it.

    The voice line is rejected — not trimmed — when it carries a digit, a
    currency symbol or a content-claim word. The clause above it is the contract
    and a second claim underneath can only contradict it.
    """
    line = " ".join(str(voice_line or "").split())
    if not line or _VOICE_BAN.search(line):
        return claim.text
    budget = _REPLY_MAX_CHARS - len(claim.text) - 2
    if budget <= 0:
        return claim.text
    return f"{claim.text}\n\n{line[:budget]}"
