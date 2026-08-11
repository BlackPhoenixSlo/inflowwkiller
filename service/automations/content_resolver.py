"""service/automations/content_resolver.py — send him what he actually asked for.

ONE resolver, three callers (`tip_reward`, `make_right`, the on-ask PPV). It
answers a single question: *given this thread, which media may we attach?*

## Why this exists

Every path that sends a fan media used to choose it by MONEY. `tip_reward` picks
a tier from the tip size; `make_right` picks by the over-charge basis;
`ai_chatter` picks the cheapest offerable item. Meanwhile `gen_info` writes
`fetishes` for every fan and **nothing reads it** (verified 2026-08-10 — the only
consumers are a REST serializer and tests). So what he liked shaped what she
SAID and never what she SENT.

`tip_context.pick_context_media` was the first fix and it had the right idea, but
in production it produced **one match in 430 runs** (98 ok, 133 of them the
image-reply freebie, 6 bundles, 1 non-empty match). Nothing to learn from; the
mechanism was never exercised. This generalises it and hardens it.

## The three calls, and why three

The operator's budget is up to THREE LLM calls per send, and the third is the
one that matters:

    1. READ    thread tail          -> the CONTRACT (what he asked for)
    2. MATCH   contract + shelf     -> candidate media ids
    3. VERIFY  each pick + contract -> keep only what provably satisfies it

Two calls would be a matcher. The third makes it a gate. It exists because the
signal underneath is measured at **22% precision for subject matter**: the vault
tag/description generator yielded 307 candidates for 68 keepers, and
`body_focus` lists "feet" whenever feet are VISIBLE rather than the subject — a
lingerie photo with her feet in frame matches. VERIFY is prompted adversarially
and defaults to REJECT, so an item has to earn its place twice.

## Contract, not preference

🚨 The selector is **what this transaction promised**, never a stored profile.
The fan whose account deletion drives this asked *"only feet right?"* three
times: there was nothing to infer, and a perfect profile would not have saved
him. A profile fact is advisory; a thing he just said is binding. `fetishes` is
deliberately NOT read here.

## Failure contract — the caller decides, not this module

`resolve()` NEVER raises and never silently substitutes. It returns a
`Resolution` carrying either media ids or a typed `refusal`. That split is
load-bearing because the callers legitimately disagree:

  * `tip_reward` must never block a reward -> on refusal it falls back to its
    folder pull (a generic reward was never promised to be personal).
  * a PRICED send must fail CLOSED -> on refusal it sends nothing, because for a
    strict promise a generic send is worse than no send: it turns a recoverable
    delay into a second deception.

Hermetic in tests: no described vault rows ⇒ no candidates ⇒ zero LLM calls.
"""
from __future__ import annotations

import json
import logging
import re
import time
from collections import Counter
from dataclasses import dataclass, field

from sqlalchemy import func, or_, select

import llm_client                       # module import so tests can patch .chat
import vault_pack_picker
import vault_scripts                    # is_solo — one definition of "she is alone"
from db.engine import get_session
from jsonsafe import load_json
from db.models import Fan, Message, VaultFolder, VaultFolderItem, VaultItem

from ._common import load_voice_blocks, resolve_model

log = logging.getLogger("of-relay.automation.content_resolver")

# Prompt-size guardrails, inherited from tip_context (which they served well).
_CANDIDATES_MAX = 150
_DESC_LEN = 180
_VERIFY_MAX = 24          # never verify more than this many in one call

# ── Typed refusals ──────────────────────────────────────────────────
# A refusal is a STATE, never silence. The caller turns it into a truthful
# message, a fallback, or an operator escalation — but it always knows why.
NO_ASK = "no_ask"                        # nothing was asked for in the thread
UNSUPPORTED_SUBJECT = "unsupported_subject"   # asked for something nobody curated
EMPTY_SHELF = "empty_shelf"              # the curated shelf exists but is empty
EXHAUSTED_FOR_FAN = "exhausted_for_fan"  # he already owns/has seen all of it
INSUFFICIENT_QUANTITY = "insufficient_quantity"  # fewer than asked for
NO_MATCH = "no_match"                    # the matcher found nothing that fits
CUSTOM_REQUEST = "custom_request"        # he wants something that would be FILMED
VERIFY_REJECTED = "verify_rejected"      # every pick failed the adversarial check
LLM_UNAVAILABLE = "llm_unavailable"      # cap hit / provider error

MEDIA_KINDS = ("photo", "video")

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
    media_kind: str | None = None       # "photo" | "video" | None (no preference)
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


# ── Thread reading (shared with tip_context's proven shape) ─────────

async def _thread_lines(account_id: str, fan_id: int, n_msgs: int,
                        voice: str = "her") -> list[str]:
    """The last `n_msgs` live messages as FAN:/HER: lines, oldest first.

    PPV and tip markers ride along, because "what was promised" and "what was
    paid for" are both part of the contract.
    """
    async with get_session() as s:
        rows = (await s.execute(
            select(Message.direction, Message.body, Message.price_cents, Message.is_tip)
            .where(Message.account_id == str(account_id),
                   Message.fan_id == int(fan_id),
                   Message.is_unsent.is_(False))
            .order_by(Message.created_at.desc())
            .limit(max(1, int(n_msgs)))
        )).all()
    me = "HIM" if str(voice or "").strip().lower() == "him" else "HER"
    lines: list[str] = []
    for direction, body, price_cents, is_tip in reversed(rows):
        who = "FAN" if direction == "in" else me
        tag = " [tip]" if is_tip else (
            f" [PPV ${int(price_cents or 0) / 100:.0f}]" if int(price_cents or 0) > 0 else "")
        body = " ".join(str(body or "").split())[:300]
        if body or tag:
            lines.append(f"{who}{tag}: {body}")
    return lines


# ── Candidate pools ─────────────────────────────────────────────────

async def _curated_pool(account_id: str, category: str, rung: str | None,
                        seen: set[int]) -> list[tuple[int, str]]:
    """Unseen items from the OPERATOR-CURATED shelf for a category.

    🚨 This is the pool a priced send should draw from. Membership here is a
    human verdict (`vault_pack_picker`), not a tag match, which is the only
    trustworthy answer to "is this item actually about the thing he asked for".
    """
    cat = vault_pack_picker.CATEGORIES.get(category)
    if cat is None:
        return []
    names = [cat.folder_name(rung)] if rung in cat.rungs else cat.folder_names
    async with get_session() as s:
        folder_ids = [
            f.id for f in (await s.execute(
                select(VaultFolder).where(
                    VaultFolder.account_id == str(account_id),
                    VaultFolder.name.in_(names),
                    VaultFolder.deleted_at.is_(None),
                )
            )).scalars().all()
        ]
        if not folder_ids:
            return []
        media_ids = [
            int(m) for m in (await s.execute(
                select(VaultFolderItem.media_id).where(
                    VaultFolderItem.account_id == str(account_id),
                    VaultFolderItem.folder_id.in_(folder_ids),
                ).order_by(
                    VaultFolderItem.manual_order.is_(None),
                    VaultFolderItem.manual_order,
                    VaultFolderItem.media_id,
                )
            )).scalars().all()
        ]
        fresh = [m for m in media_ids if m not in seen]
        if not fresh:
            return []
        rows = (await s.execute(
            select(VaultItem.media_id, VaultItem.kind, VaultItem.search_text,
                   VaultItem.description, VaultItem.video_description)
            .where(VaultItem.account_id == str(account_id),
                   VaultItem.media_id.in_(fresh))
        )).all()
    by_id = {int(r[0]): r for r in rows}
    out: list[tuple[int, str]] = []
    for mid in fresh:                       # preserve the operator's rank order
        row = by_id.get(mid)
        if row is None:
            continue
        text = " ".join(str(row[2] or row[3] or row[4] or "").split())
        out.append((mid, text[:_DESC_LEN] or "(no description)"))
    return out


async def _described_pool(account_id: str, seen: set[int],
                          media_kind: str | None,
                          terms: list[str] | None = None) -> list[tuple[int, str]]:
    """Unseen DESCRIBED vault items matching the contract's TERMS.

    🚨 This used to take the newest 150 items and let the matcher hunt. Measured
    on a real account 2026-08-11: of **280** items whose text mentions feet, only
    **26** are in the newest 150 — the window hid **91%** of the shelf. Recency
    is not relevance, and no amount of LLM cleverness recovers a candidate it was
    never shown.

    So retrieval is lexical and runs over the WHOLE described vault. The terms
    come from the model (it produced "soles", "toes", "barefoot", "arches" from
    the fan's word "feet"), which is strictly wider than a regex a human would
    maintain — and the scan itself costs no LLM call, only SQL.

    ⚠️ Still the 22%-precision pool: a description mentioning feet is usually a
    photo that merely HAS feet in it. Retrieval buys RECALL; VERIFY is what buys
    precision. Nothing may ship from here without passing it.
    """
    terms = [t for t in (terms or []) if t]
    async with get_session() as s:
        q = select(VaultItem.media_id, VaultItem.kind, VaultItem.search_text,
                   VaultItem.description, VaultItem.video_description).where(
            VaultItem.account_id == str(account_id),
            VaultItem.removed_at.is_(None),
        )
        if media_kind in MEDIA_KINDS:
            q = q.where(VaultItem.kind == media_kind)
        if terms:
            blob = func.lower(
                func.coalesce(VaultItem.search_text, "")
                + " " + func.coalesce(VaultItem.description, "")
                + " " + func.coalesce(VaultItem.video_description, "")
                + " " + func.coalesce(VaultItem.ai_fields_json, ""))
            q = q.where(or_(*[blob.like(f"%{t}%") for t in terms]))
        # Recency is the TIE-BREAK now, not the filter.
        rows = (await s.execute(
            q.order_by(VaultItem.created_at.desc()).limit(_CANDIDATES_MAX * 4)
        )).all()
        if not rows and terms:
            # The scan found nothing. That is a vocabulary miss, NOT "he has seen
            # everything" — degrade to a broad pool and let the matcher judge,
            # rather than refusing a fan who genuinely asked for something.
            log.info("retrieval miss account=%s terms=%s — broad fallback",
                     account_id, terms[:6])
            broad = select(VaultItem.media_id, VaultItem.kind, VaultItem.search_text,
                           VaultItem.description, VaultItem.video_description).where(
                VaultItem.account_id == str(account_id),
                VaultItem.removed_at.is_(None))
            if media_kind in MEDIA_KINDS:
                broad = broad.where(VaultItem.kind == media_kind)
            rows = (await s.execute(
                broad.order_by(VaultItem.created_at.desc()).limit(_CANDIDATES_MAX * 2)
            )).all()

    scored: list[tuple[int, int, str]] = []
    for mid, _kind, search_text, desc, vdesc in rows:
        if int(mid) in seen:
            continue
        text = " ".join(str(search_text or desc or vdesc or "").split())
        if not text:
            continue
        # More matching terms = a stronger candidate. A cheap ordering that puts
        # "bare feet, soles up" above "…and her feet are in frame" before the
        # matcher ever sees them, without pretending to be a relevance model.
        low = text.lower()
        hits = sum(1 for t in terms if t in low) if terms else 0
        scored.append((hits, int(mid), text[:_DESC_LEN]))
    scored.sort(key=lambda r: -r[0])
    return [(mid, text) for _h, mid, text in scored[:_CANDIDATES_MAX]]


# ── The vault's own vocabulary ──────────────────────────────────────
#
# 🚨 Measured on the pilot 2026-08-11, and it is the whole reason this exists:
#
#     the descriptions say  breasts (979 items),  vulva (627)
#     fans ask for          tits    (10 fans),    pussy
#
# `read_contract` is asked to expand a fan's word into search terms and it does
# it BLIND — the model has never seen this vault. So it produces the words a
# reasonable person would use, the lexical scan finds nothing, and a man asking
# for the most-photographed part of the vault gets `no_match`. No amount of
# cleverness in the matcher recovers a candidate retrieval never returned.
#
# Feeding the account's real description vocabulary into the prompt closes it:
# the model sees `breasts` in the list and emits `breasts` for "tits".
#
# It goes in the SYSTEM prompt and is static per account, which is deliberate —
# static text inside the window is nearly free against the provider's prefix
# cache, while the same words appended per-message would not be.
_LEXICON_TTL_S = 6 * 3600
_LEXICON_MAX = 200
# Photographic scaffolding: frequent, and never what a fan asks for. Kept short
# on purpose — the model is better at spotting a non-content word than a list is,
# and an over-eager stoplist would delete the anatomy this is for. ("none" is a
# JSON null that leaked into the description text.)
_LEXICON_JUNK = frozenset({
    "none", "null", "stills", "handheld", "selfie", "image", "images", "photo",
    "photos", "picture", "pictures", "video", "videos", "clip", "clips", "frame",
    "frames", "camera", "shot", "shots", "view", "angle", "background",
    "foreground", "lighting", "visible", "appears", "shown", "showing",
    "unknown", "unclear", "description", "left", "right", "toward", "towards",
})
_lexicon_cache: dict[str, tuple[float, Counter, int]] = {}


async def vault_lexicon(account_id: str) -> list[str]:
    """The words THIS account's descriptions actually use, most common first.

    Document frequency, not raw count: a word in 900 items beats one repeated
    nine times in the same item. Deliberately NOT frequency-capped — `breasts`
    is in 61% of this vault and is exactly the word the expander needs, so any
    "too common to be useful" rule would delete the payload.
    """
    freq, _total = await _term_frequencies(account_id)
    return [w for w, _n in freq.most_common(_LEXICON_MAX)
            if w not in _LEXICON_JUNK]


async def _term_frequencies(account_id: str) -> tuple[Counter, int]:
    """`(word -> items containing it, total items)`. One scan, cached.

    DOCUMENT frequency: a word in 900 items beats one repeated nine times in
    the same item. The FULL counter is kept, not just the head — the broadness
    guard has to score terms the lexicon never showed the model ("soles").

    ⚠️ Approximate by construction: retrieval matches `%term%` as a substring
    while this counts whole words, so "ass" scores lower here than it selects.
    It is a guard, not an index — a term has to be wildly common to trip it.
    """
    now = time.monotonic()
    hit = _lexicon_cache.get(str(account_id))
    if hit and now - hit[0] < _LEXICON_TTL_S:
        return hit[1], hit[2]
    async with get_session() as s:
        rows = (await s.execute(
            select(VaultItem.search_text, VaultItem.description,
                   VaultItem.video_description).where(
                VaultItem.account_id == str(account_id),
                VaultItem.removed_at.is_(None))
        )).all()
    freq: Counter = Counter()
    for row in rows:
        freq.update({
            w for w in re.split(r"[^a-z]+",
                                " ".join(str(x or "") for x in row).lower())
            if 3 <= len(w) <= 20 and w not in _STOPWORDS
        })
    _lexicon_cache[str(account_id)] = (now, freq, len(rows))
    return freq, len(rows)


# A term matching this much of the vault carries no information: OR-ed with the
# others it drags the whole library into the pool and the ranking stops meaning
# anything. Measured 2026-08-11 — with the lexicon in the prompt but no guard,
# every ask retrieved ~1,520 of 1,601 items, feet included.
_TERM_MAX_DF = 0.35
_TERM_KEEP_MIN = 2        # never strip a query down to nothing


async def drop_useless_terms(account_id: str, terms: list[str]) -> list[str]:
    """Remove terms so common they select the whole vault.

    A prompt asking the model not to add generic words is necessary and not
    sufficient — it was already told, and still emitted "nude" for a feet ask.
    This is the deterministic half: measure each term against the real corpus
    and drop the ones that discriminate nothing.

    Keeps the RAREST when everything is common, because a fan who asks about a
    vault that is 90% nudes still deserves the narrowest cut available rather
    than a refusal.
    """
    terms = [t for t in (terms or []) if t]
    if len(terms) <= _TERM_KEEP_MIN:
        return terms
    freq, total = await _term_frequencies(account_id)
    if not total:
        return terms
    df = {t: freq.get(t, 0) for t in terms}
    keep = [t for t in terms if df[t] / total <= _TERM_MAX_DF]
    if len(keep) >= _TERM_KEEP_MIN:
        dropped = [t for t in terms if t not in keep]
        if dropped:
            log.info("terms too broad, dropped account=%s %s", account_id, dropped)
        return keep
    # Everything is common. Take the narrowest few rather than refusing.
    return sorted(terms, key=lambda t: df[t])[:_TERM_KEEP_MIN + 1]


async def solo_only(account_id: str, media_ids: list[int]) -> list[int]:
    """Drop anything with someone else in it.

    🚨 Operator ruling 2026-08-11, marked "very important": unless he asked for
    company, she is ALONE in what he gets. A fan who asked for her and was sent
    a threesome did not get a bonus, he got the wrong thing — and on this
    account that has cost an unsubscribe before.

    Reuses `vault_scripts.is_solo` rather than reading `people_count` here. That
    function already knows the way this goes wrong: the model returns
    `people_count: 1` for a clip it has just described as a blowjob, because it
    counts the person the clip is ABOUT. So the test is a disjunction over
    `partner_visible`, the count, the acts and the penetration field.

    🚨 THREE-WAY, not a veto. `is_solo` is deliberately false for a row nobody
    ever asked — correct for building an operator-facing folder, fatal here: on
    a vault without the V2 describe pass EVERY row is unknown, and a binary
    filter turns the whole lane silent. That is the same mistake `_rank_by_tier`
    documents, and it cost this file 8 red tests an hour apart.

      * confirmed company  → DROPPED. This is the rule, and it is absolute.
      * confirmed solo     → first.
      * never asked        → kept, behind the confirmed. Composition consumes
        this order, so an unknown is only reached once the certain ones are out.

    On the pilot vault that is 1,590 of 1,623 answered and 60 with company, so
    the uncertain tail is 2% — small, and behind everything that is sure.
    """
    if not media_ids:
        return []
    async with get_session() as s:
        rows = (await s.execute(
            select(VaultItem.media_id, VaultItem.ai_fields_json).where(
                VaultItem.account_id == str(account_id),
                VaultItem.media_id.in_(media_ids))
        )).all()
    fields = {int(m): (load_json(blob, {}) or {}) for m, blob in rows}
    solo, unknown = [], []
    for mid in media_ids:
        f = fields.get(mid, {})
        if vault_scripts.is_solo(f):
            solo.append(mid)
        elif not vault_scripts.solo_known(f):
            unknown.append(mid)        # nobody asked — not evidence of company
    return solo + unknown


async def profile_terms(account_id: str, fan_id: int) -> list[str]:
    """What HE likes, from his profile — not from this conversation.

    ⚠️ Deliberately never used to fill an ask. A thing he just said is binding
    and a profile fact is advisory, and letting `fetishes` answer "send me feet"
    is how a fan gets charged for a guess. This exists for the opposite moment:
    the answer to his ask is NO, and the alternative she offers instead should
    be something he has actually shown interest in rather than whatever happened
    to sort first.
    """
    async with get_session() as s:
        fan = await s.get(Fan, (str(account_id), int(fan_id)))
    if fan is None:
        return []
    raw = [str(fan.fetishes or ""), str(fan.self_description or "")]
    if fan.likes_ass:
        raw.append("ass booty butt")
    if fan.likes_boobs:
        raw.append("tits boobs cleavage")
    terms: list[str] = []
    for chunk in raw:
        for word in re.split(r"[^a-z0-9]+", chunk.lower()):
            if 3 <= len(word) <= 24 and word not in _STOPWORDS and word not in terms:
                terms.append(word)
    return terms[:12]


async def _alternatives(account_id: str, fan_id: int, pool: list[tuple[int, str]],
                        seen: set[int], count: int) -> list[int]:
    """What she CAN show when the answer to his ask is no.

    Operator ruling 2026-08-11: on "show me" with nothing that fits, say *"i
    don't have that, but check this — you might like it"* and take the
    alternative from what is known about HIM. A refusal that offers nothing is
    silence, and silence is the failure shape this map keeps hitting.

    Falls back to the head of the pool, which is still ranked by his own words —
    a fan with an empty profile gets a worse alternative, not no alternative.
    """
    terms = await profile_terms(account_id, fan_id)
    if terms:
        liked = await _described_pool(account_id, seen, None, terms)
        # A consolation offer is still a send, so the solo rule holds here too.
        solo = await solo_only(account_id, [mid for mid, _ in liked])
        if solo:
            return solo[:count]
    return [mid for mid, _ in pool[:count]]


async def _kind_of(account_id: str, media_ids: list[int]) -> dict[int, str]:
    if not media_ids:
        return {}
    async with get_session() as s:
        rows = (await s.execute(
            select(VaultItem.media_id, VaultItem.kind).where(
                VaultItem.account_id == str(account_id),
                VaultItem.media_id.in_(media_ids))
        )).all()
    return {int(m): str(k or "") for m, k in rows}


# ── The three calls ─────────────────────────────────────────────────

async def read_contract(account_id: str, fan_id: int, *, n_msgs: int = 20,
                        model: str | None = None) -> Contract:
    """CALL 1 — what did he ask for, in his own words?

    Deliberately conservative: no ask is a perfectly good answer, and inventing
    one is how a fan gets sent something he never wanted. `strict` is set only
    for exclusive language ("only", "just", "nothing but") — the exact shape of
    the message that preceded a real account deletion.
    """
    lines = await _thread_lines(
        account_id, fan_id, n_msgs,
        (await load_voice_blocks(account_id)).voice)
    if not lines:
        return Contract()
    known = ", ".join(sorted(vault_pack_picker.CATEGORIES)) or "(none)"
    system = (
        "You read an OnlyFans chat and decide TWO separate things.\n\n"
        "FIRST — is_ask: is he asking to SEE or RECEIVE content from her?\n"
        "  YES: \"send me that set\", \"can i see\", \"show me\", \"come show me\", "
        "\"i wanna see your feet\", \"got any videos\", \"unlock it for me\", or him "
        "naming content he wants to watch.\n"
        "  NO: small talk, his job, logistics, plans, an address, or wanting to "
        "SEE something that is not her content — \"i wanna see them go home to "
        "their families\" is NOT an ask, even though it says \"wanna see\".\n"
        "  A bare \"show me\" IS an ask.\n"
        "  NOT an ask: he is REPORTING a purchase, not requesting one — "
        "\"unlocking now babe\", \"just bought it\", \"said unlock for $10 but it "
        "was already unlocked\". He is paying. Pitching him here bills a man mid-"
        "payment for what he is already paying for.\n"
        "  NOT an ask, and important: he is TESTING whether she is a bot or "
        "demanding proof she is real (\"until u show me proof of life\", \"are "
        "you a bot\", \"send a pic with today's date\"). Set is_ask false and "
        "bot_test true — another part of the system owns that moment.\n\n"
        "CUSTOM REQUEST — he is describing a scene, a prop or a camera setup that "
        "would have to be FILMED for him (\"put the camera on a stand and throw "
        "the ball to you\"). Set is_ask true AND custom_request true: he wants "
        "content, but not content that exists.\n\n"
        "SECOND — subject: WHAT he asked for. This may be null even when is_ask "
        "is true (a bare \"show me\" names nothing).\n"
        "  Take the subject from HIS OWN LATEST MESSAGE FIRST. If he names a "
        "thing — leather, feet, ass, a video — that is the subject, and it "
        "OUTRANKS anything said earlier in the chat.\n"
        "  Only if his message names nothing, resolve a referring expression "
        "(\"that set\", \"it\", \"them\") against what SHE offered earlier.\n"
        "  Never carry a subject over from an old message when his latest one is "
        "small talk. If is_ask is false, subject MUST be null.\n\n"
        "COMPANY — did he ask for SOMEONE ELSE to be in it? Only true when he "
        "names another person: \"you and your friend\", \"a threesome\", \"with "
        "a guy\", \"two girls\". Him being in his own fantasy (\"me fucking "
        "you\") is NOT company — he is not in her vault. Default false.\n\n"
        "Reply as JSON:\n"
        '{"is_ask": true|false,\n'
        ' "company": true|false,\n'
        ' "subject": "<short noun, or null>",\n'
        f' "category": "<one of: {known}, or null if none fit>",\n'
        ' "media_kind": "photo" | "video" | null,\n'
        ' "strict": true if he used exclusive words like "only"/"just"/"nothing '
        'but", else false,\n'
        ' "quote": "<his exact words, <=120 chars>",\n'
        ' "exclusions": ["<things he said he does NOT want>"],\n'
        ' "terms": ["REQUIRED when subject is set: 8-15 SINGLE lowercase words '
        'that would appear in a written description of the media he wants. '
        'Decompose his phrasing and add synonyms; NEVER return his sentence. '
        'Be careful with words that also name FURNITURE or a SETTING — for '
        '\'leather\' he means what she is WEARING, so use: leather, boots, '
        'latex, jacket, corset, harness, gloves, thigh-high — not couch or sofa. '
        'Examples — \'feet\': feet, foot, soles, toes, barefoot, arches, ankles. '
        '\'back shots\': ass, behind, doggy, bent, arched, rear."]}'
    )
    # 🚨 The vault's OWN words, appended to the system prompt.
    #
    # Without this the expansion is a guess about a vault the model has never
    # seen. Measured on the pilot: the descriptions say `breasts` in 979 items
    # and `vulva` in 627; fans ask for "tits". Retrieval is a LIKE over that
    # description text, so an expansion into words the descriptions do not use
    # returns NOTHING — and a man asking for the most-photographed thing in the
    # vault gets `no_match`.
    #
    # Static per account and appended to the SYSTEM block on purpose: it is the
    # same bytes on every call for this account, which is what a provider prefix
    # cache rewards. Carried per-message it would be paid for on every turn.
    lexicon = await vault_lexicon(account_id)
    if lexicon:
        system += (
            "\n\nTHE VAULT'S OWN WORDS — the vocabulary these descriptions "
            "actually use, most common first:\n" + ", ".join(lexicon) + "\n"
            "Add a word from this list ONLY when it is what THIS VAULT calls "
            "the thing he asked for — \"tits\" -> \"breasts\". Do NOT add a "
            "word just because it is common: \"nude\", \"posing\" and "
            "\"bedroom\" describe most of the vault and would match everything. "
            "And NEVER DROP one of your own terms because it is missing here — "
            "this is the 200 most common words, not the whole vocabulary."
        )
    try:
        res = await llm_client.chat(
            model=model or await resolve_model(account_id, "content_resolver"),
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": "CHAT (oldest first):\n" + "\n".join(lines)}],
            purpose="content_resolver_contract",
            account_id=str(account_id), fan_id=int(fan_id),
            response_format={"type": "json_object"}, temperature=0.1,
        )
        p = res.parsed if isinstance(res.parsed, dict) else {}
    except Exception:
        log.warning("contract read failed account=%s fan=%s", account_id, fan_id,
                    exc_info=True)
        return Contract()

    is_ask = bool(p.get("is_ask")) and not bool(p.get("bot_test"))
    subject = (str(p.get("subject") or "").strip() or None)
    if not is_ask:
        subject = None            # an ask and a subject stand or fall together
    category = (str(p.get("category") or "").strip().lower() or None)
    if category not in vault_pack_picker.CATEGORIES:
        category = None
    kind = (str(p.get("media_kind") or "").strip().lower() or None)
    if kind not in MEDIA_KINDS:
        kind = None
    excl = [str(x).strip() for x in (p.get("exclusions") or []) if str(x).strip()]
    # Split anything multi-word and drop stop-words: a term is a LIKE needle, and
    # "booty shake on my face" matches nothing while "booty" matches plenty. The
    # first live review set came back with terms == [the whole sentence] and an
    # empty pool on every thread, so this is a safety net under the prompt.
    raw = list(p.get("terms") or [])
    if subject:
        raw.append(subject)
    terms: list[str] = []
    for chunk in raw:
        for word in re.split(r"[^a-z0-9]+", str(chunk).strip().lower()):
            if 3 <= len(word) <= 24 and word not in _STOPWORDS and word not in terms:
                terms.append(word)
    terms = await drop_useless_terms(account_id, terms[:15])
    return Contract(
        is_ask=is_ask, custom_request=bool(p.get("custom_request")) and is_ask,
        subject=subject, category=category, rung=None, media_kind=kind,
        company=bool(p.get("company")) and is_ask,
        strict=bool(p.get("strict")), quote=str(p.get("quote") or "")[:120],
        exclusions=excl[:6], terms=terms,
    )


async def _match(account_id: str, fan_id: int, contract: Contract,
                 pool: list[tuple[int, str]], limit: int,
                 model: str) -> list[int]:
    """CALL 2 — pick ids from the pool that fit the contract."""
    want = contract.subject or contract.category or "what he asked for"
    system = (
        "You match vault media to what a fan asked for. Pick AT MOST "
        f"{int(limit)} ids from the catalog whose descriptions clearly show: "
        f"{want}. "
        + (f"He does NOT want: {', '.join(contract.exclusions)}. "
           if contract.exclusions else "")
        + "Only clear matches — an empty list is the right answer when nothing "
        'fits. Reply as JSON: {"media_ids": [..]}.'
    )
    user = "CATALOG (id: description):\n" + "\n".join(
        f"{mid}: {text}" for mid, text in pool)
    res = await llm_client.chat(
        model=model,
        messages=[{"role": "system", "content": system},
                  {"role": "user", "content": user}],
        purpose="content_resolver_match",
        account_id=str(account_id), fan_id=int(fan_id),
        response_format={"type": "json_object"}, temperature=0.2,
    )
    p = res.parsed if isinstance(res.parsed, dict) else {}
    valid = {mid for mid, _ in pool}
    picked: list[int] = []
    for x in (p.get("media_ids") or []):
        try:
            mid = int(x)
        except (TypeError, ValueError):
            continue
        if mid in valid and mid not in picked:
            picked.append(mid)
        if len(picked) >= limit:
            break
    return picked


async def _verify(account_id: str, fan_id: int, contract: Contract,
                  picks: list[tuple[int, str]], model: str) -> list[int]:
    """CALL 3 — the gate. Keep only what PROVABLY satisfies the contract.

    🚨 Prompted to REJECT on doubt, on purpose. The pool underneath is measured
    at 22% precision for subject matter: a photo whose description mentions feet
    is usually a photo that merely HAS feet in it. The matcher is optimistic by
    construction (it is asked to find things); this call is the pessimist that
    has to agree before anything is attached.
    """
    want = contract.subject or contract.category or "what he asked for"
    system = (
        "You are checking whether media matches a promise, before a fan is "
        f"charged for it. The promise is: {want}. "
        + (f"He explicitly said ONLY this — anything else is a broken promise. "
           if contract.strict else "")
        + (f"He does NOT want: {', '.join(contract.exclusions)}. "
           if contract.exclusions else "")
        + "For each id, answer true ONLY if the description shows the promised "
        "thing as the SUBJECT of the media, not merely present in the frame. "
        "If the description is vague, or the thing is only incidentally "
        "visible, answer false. DEFAULT TO FALSE when uncertain — sending the "
        "wrong thing costs far more than sending nothing. "
        'Reply as JSON: {"verdicts": {"<id>": true|false}}.'
    )
    user = "ITEMS (id: description):\n" + "\n".join(
        f"{mid}: {text}" for mid, text in picks)
    res = await llm_client.chat(
        model=model,
        messages=[{"role": "system", "content": system},
                  {"role": "user", "content": user}],
        purpose="content_resolver_verify",
        account_id=str(account_id), fan_id=int(fan_id),
        response_format={"type": "json_object"}, temperature=0.0,
    )
    p = res.parsed if isinstance(res.parsed, dict) else {}
    verdicts = p.get("verdicts") if isinstance(p.get("verdicts"), dict) else {}
    kept: list[int] = []
    for mid, _text in picks:
        v = verdicts.get(str(mid), verdicts.get(mid))
        if v is True:                    # anything but an explicit true is a reject
            kept.append(mid)
    return kept


# ── The resolver ────────────────────────────────────────────────────

async def resolve(account_id: str, fan_id: int, *, count: int,
                  seen: set[int] | None = None,
                  contract: Contract | None = None,
                  require_curated: bool = False,
                  verify: bool = True,
                  n_msgs: int = 20) -> Resolution:
    """Media for this fan that satisfies what he asked for, or a typed refusal.

    `require_curated` — draw ONLY from operator-curated shelves. A priced send
    should set this: it is the difference between "a human filed this under
    feet" and "an LLM thought this description sounded like feet".

    `verify` — run CALL 3. Leave it on for anything a fan pays for. It may be
    turned off for a free, non-promised bundle where the cost of a loose match
    is a slightly-off freebie rather than a broken contract.

    `contract` — pass one to SKIP call 1 (the caller already knows what was
    promised, e.g. `make_right` repairing a specific broken promise).
    """
    seen = set(seen or ())
    if count <= 0:
        return _empty(contract or Contract(), INSUFFICIENT_QUANTITY)

    if contract is None:
        contract = await read_contract(account_id, fan_id, n_msgs=n_msgs)
    if not contract.asked:
        return _empty(contract, NO_ASK)
    # He asked but named nothing — "come show me", "Show me". Operator ruling
    # 2026-08-11: that IS a PPV ask. There is no subject to search on, so the
    # pool is the vault at large and the matcher picks something worth sending.
    generic = contract.is_ask and not (contract.subject or contract.category)

    # ── The pool is the CURATED SHELF ∪ THE WHOLE VAULT ────────────
    #
    # Operator ruling 2026-08-11: "we are searching the whole vault for videos a
    # fan might want + these folders, as they are absolutely correct as I picked
    # them." So the two are a union, not a fallback chain:
    #
    #   • curated items are TRUSTED — a human looked at the pixels and filed
    #     them. They rank first and they skip VERIFY, because re-judging a
    #     human's eyes against a description written by a different model is how
    #     the verifier came to reject 14 of the operator's own 14 keeps.
    #   • retrieved items are UNTRUSTED — found by lexical scan over everything,
    #     which is the only way a VIDEO ever gets picked (the feet shelves are
    #     stills). These are what VERIFY exists to gate.
    trusted: list[tuple[int, str]] = []
    if contract.category:
        trusted = await _curated_pool(account_id, contract.category, contract.rung, seen)
        if not trusted and require_curated:
            return _empty(contract, EMPTY_SHELF)
    if require_curated:
        pool = trusted
        if not pool:
            return _empty(contract, UNSUPPORTED_SUBJECT)
    else:
        have = {mid for mid, _ in trusted}
        found = [(m, t) for m, t in
                 await _described_pool(account_id, seen, contract.media_kind,
                                       contract.terms)
                 if m not in have]
        pool = trusted + found
    trusted_ids = {mid for mid, _ in trusted}
    if not pool:
        return _empty(contract, EXHAUSTED_FOR_FAN)

    # Media kind is a promise too: "send me a video" is not satisfied by a photo,
    # and this is why the whole vault is in the pool — the curated feet shelves
    # are stills, so a video can only ever come from the retrieved side.
    if contract.media_kind in MEDIA_KINDS:
        kinds = await _kind_of(account_id, [mid for mid, _ in pool])
        pool = [(mid, t) for mid, t in pool
                if kinds.get(mid, contract.media_kind) == contract.media_kind]
        trusted_ids &= {mid for mid, _ in pool}
        if not pool:
            return _empty(contract, EXHAUSTED_FOR_FAN)

    # SOLO unless he asked otherwise. Applied to the whole pool — including the
    # curated shelves, which a human filed by SUBJECT and never by who else was
    # in the frame, so "a human picked it" is not evidence about company.
    if not contract.company:
        keep = set(await solo_only(account_id, [mid for mid, _ in pool]))
        pool = [(mid, t) for mid, t in pool if mid in keep]
        trusted_ids &= keep
        if not pool:
            return _empty(contract, EXHAUSTED_FOR_FAN)

    considered = len(pool)
    model = await resolve_model(account_id, "content_resolver")

    if contract.custom_request:
        # He described a shoot. Refuse the ASK but hand the caller the nearest
        # real things, so she says "i don't have anything like that, but check
        # this" instead of guessing or going quiet.
        return _empty(contract, CUSTOM_REQUEST, considered=considered,
                      alternatives=await _alternatives(account_id, fan_id, pool,
                                                       seen, count))
    try:
        picked = await _match(account_id, fan_id, contract, pool, count, model)
    except Exception:
        log.warning("match failed account=%s fan=%s", account_id, fan_id, exc_info=True)
        return _empty(contract, LLM_UNAVAILABLE, considered=considered)
    if not picked:
        alts = await _alternatives(account_id, fan_id, pool, seen, count)
        # 🚨 A GENERIC ask can never fail to match. Operator ruling 2026-08-11:
        # "show me — always find something." He named nothing, so there is no
        # promise for a pick to break; refusing here is the bug, not the safety.
        # Three of the operator's 22 silences in round 3 were exactly this
        # ("come show me", "Show me", "Show me tell me once and I learn it") and
        # every one of them was a live buyer mid-conversation.
        if generic and alts:
            return Resolution(alts[:count], contract, considered=considered)
        return _empty(contract, NO_MATCH, considered=considered,
                      alternatives=alts)

    # Nothing was promised, so there is no promise to check. Running the
    # verifier against "what he asked for" when he asked for nothing in
    # particular is how a bare "show me" ends in VERIFY_REJECTED.
    if not verify or generic:
        return Resolution(picked, contract, considered=considered)

    by_id = dict(pool)
    # A curated pick is already verified — by a human, against the pixels. Only
    # what the machine found needs the gate.
    machine = [mid for mid in picked if mid not in trusted_ids][:_VERIFY_MAX]
    human = [mid for mid in picked if mid in trusted_ids]
    if not machine:
        return Resolution(picked, contract, considered=considered)
    try:
        kept = human + await _verify(
            account_id, fan_id, contract,
            [(mid, by_id.get(mid, "")) for mid in machine], model)
        kept = [mid for mid in picked if mid in set(kept)]   # keep rank order
    except Exception:
        # 🚨 A verifier that cannot run is not a pass. The whole point of this
        # call is that nothing ships unchecked.
        log.warning("verify failed account=%s fan=%s — refusing",
                    account_id, fan_id, exc_info=True)
        return _empty(contract, LLM_UNAVAILABLE, considered=considered)

    rejected = len(picked) - len(kept)
    if not kept:
        return _empty(contract, VERIFY_REJECTED, considered=considered,
                      rejected_by_verify=rejected)
    # A STRICT promise is all-or-nothing: if the verifier threw any of it out,
    # what is left is a partial delivery of an exclusive claim. Refuse instead.
    if contract.strict and rejected:
        return _empty(contract, VERIFY_REJECTED, considered=considered,
                      rejected_by_verify=rejected)
    return Resolution(kept, contract, considered=considered,
                      rejected_by_verify=rejected)


def explain(res: Resolution) -> str:
    """One line for a log or an operator readout. Never shown to a fan."""
    if res.ok:
        return (f"{len(res.media_ids)} item(s) for {res.contract.subject or '?'}"
                f"{' [strict]' if res.contract.strict else ''}"
                f" — {res.considered} considered, {res.rejected_by_verify} rejected")
    return (f"REFUSED {res.refusal} — asked={res.contract.subject or 'nothing'}"
            f" considered={res.considered} rejected={res.rejected_by_verify}")
