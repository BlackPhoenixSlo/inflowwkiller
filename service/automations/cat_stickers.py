"""service/automations/cat_stickers.py — the cat-sticker reaction pack.

A reply from the AI chatter can end with a cat reaction gif — occasionally the
gif IS the whole reply (he says "good morning" → just a waking-up kitten), the
way a real girl texts. The protocol mirrors `>>OFFER`: the model ends its reply
with a line that is exactly `STICKER: <tag>`; the marker is ALWAYS stripped
before anything reaches the fan — wherever it lands and however it is dressed
up, see `_MARKER_RE` — and the tag maps here to a giphy id sent via
`of_client.send_message(fan_id, "", giphy_id=...)` (top-level `giphyId`,
GIF-only empty-text sends verified live 2026-05-23).

Rate control is CODE-SIDE, not trust: measured on real convos (07-23),
DeepSeek attaches a sticker to ~48% of replies whenever the block is visible —
double what the prompt asks for. `roll_mode` can hide the block ("skip") to
thin that out, and a per-fan minute floor can space sends. All three knobs
(skip %, solo %, gap minutes) are per-account in the Styles card; the house
defaults below run wide open (skip 0, gap 0).
"solo" injects a sticker-ONLY nudge (measured ~46% pure-sticker obedience).

Catalog: 89 real-cat giphy gifs across 22 emotion tags, hand-picked in
`cat_stickers/picker.html` (repo root) — `picks.json` there is the source of
truth; regenerate this literal via `cat_stickers/finalize_catalog.py`.

The MALE lane has its own pack (`_CATALOG_HIM`, 38 dog/wolf gifs across 13
tags) — same protocol, same rate knobs, different pictures and a shorter tag
list. `harvest_male.py` → contact sheets → `finalize_male.py` is its pipeline.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta
from typing import NamedTuple

from ._common import (
    CAT_STICKER_GAP_MIN_KEY, CAT_STICKER_SKIP_PCT_KEY, CAT_STICKER_SOLO_PCT_KEY,
    CAT_STICKERS_KEY, _STYLE_FORCE_OFF_ENV, _load_style_json,
)

log = logging.getLogger("of-relay.automation.cat_stickers")

# ── Account config (style_config_json) ────────────────────────────────
# The storage KEYS stay in `_common`, which is where `style_config_api` and
# `test_style_config` already read them from and which this module imports
# anyway — a second copy here was three string literals that had to agree with
# the validator forever. The READER lives here, next to the DEFAULT_* knobs and
# the pack it configures.


class StickerConfig(NamedTuple):
    """One style_config_json read's worth of sticker knobs. A NamedTuple so
    existing 4-way unpacking and slicing keep working unchanged."""
    enabled: bool
    skip_w: float
    solo_w: float
    gap_min: float


async def load_cat_sticker_config(account_id: str) -> StickerConfig:
    """The whole cat-sticker config in ONE style_config_json read.

    enabled: account-wide toggle for the reaction pack (ai_chatter may end a
    reply with a cat gif, occasionally sending JUST the gif). Reads the
    'cat_stickers' key. DEFAULT ON (absent/NULL/parse-error → True); an
    explicit stored False opts the account out. STYLE_FORCE_OFF kills it with
    the rest of the realism stack.

    Rate knobs: the cat_sticker_*_pct/_gap_min keys; absent/NULL/parse-error/
    non-numeric → the house defaults (wide open: skip 0, solo 5%, gap 0)."""
    stored = await _load_style_json(account_id)
    if os.environ.get(_STYLE_FORCE_OFF_ENV):
        enabled = False
    else:
        val = stored.get(CAT_STICKERS_KEY)
        enabled = True if val is None else bool(val)

    def _num(key: str, default: float, scale: float, hi: float) -> float:
        v = stored.get(key)
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return default
        return min(max(float(v) * scale, 0.0), hi)

    return StickerConfig(
        enabled,
        _num(CAT_STICKER_SKIP_PCT_KEY, DEFAULT_SKIP, 0.01, 1.0),
        _num(CAT_STICKER_SOLO_PCT_KEY, DEFAULT_SOLO, 0.01, 1.0),
        _num(CAT_STICKER_GAP_MIN_KEY, DEFAULT_GAP_MIN, 1.0, 7 * 24 * 60))


from ._markers import bare_star_span, protocol_marker_re

log = logging.getLogger("of-relay.automation.cat_stickers")

# tag -> (when-to-use line for the prompt, giphy ids to rotate among)
_CATALOG: dict[str, tuple[str, tuple[str, ...]]] = {
    'laugh': ('genuinely funny',
              ('2g6sCTsSoVuSfSxK4W', 'M8lbkkn8BFFLO', '96suQaNpBRyE')),
    'love': ('he said something sweet',
             ('a9VAlh8bgb0SA', 'LVXJQat47MwQU', 'ohiWzVWGkAOCA', '5QXd9CLYmU944', 'zGJegUgNbPOtW')),
    'kiss': ('goodnight/goodbye kiss',
             ('W1hd3uXRIbddu', 'xI1pK446iNJeg')),
    'flirty': ('playful teasing',
               ('KI5JqBqOKCPjG', 'HB9nUzmw6L74HHWpqN')),
    'shy': ('play coy after a compliment',
            ('xFOc3rYIGE3aE', 'Iwt30C4BH94ty', '33JJnbUuorikA5zWgB', 'dxqOkrl29R8ac')),
    'celebrate': ('he bought/tipped or good news',
                  ('TbRkubcqlgBksEqMv4', 'A0Zt7yuDULiy4ofmVD', 'NfzERYyiWcXU4', 'Ga0gHvT84m3yTE8lIp')),
    'miss_you': ('he was away',
                 ('vxsoC27UotxBK', 'vFjAksOeerSdW', 'MDJ9IbxxvDUQM', '1FkCqpyObTuo0', 'd9eL06htb5Vks', 'SgZtvjwcfq0ww')),
    'eyeroll': ('sassy, something silly',
                ('1wqK6DFqm5FW8ZCdJ9', 'NxwRDSUNzXshW', 'nazt106Gb1Ga4')),
    'shocked': ('wild thing he said',
                ('Cdkk6wFFqisTe', '135QXxqZ9d0QGk', 'BBNYBoYa5VwtO', '113YkW9oWdtFlu')),
    'sleepy': ('goodnight, cozy in bed',
               ('12cYyFxlbIgXeg', 'GxQABXQhII7cY', 'Tfr91anUahoME', 'lFpvlU0or3SHC', 'DRNsbfCHNznxe')),
    'good_morning': ('morning greeting',
                     ('8B0TmFIOZ9iJJgJu8T', 'OWQzUnzgX7bS8', '10zHDq77BLwcy4', 'cwxKYaFLOBd1S', 'AC8n0wdJvnA6A', 'papAALBn286ty')),
    'waiting': ('he went quiet',
                ('ZT0YXuyEN2ZdNLmAq8', 'cbLcSvHw50bJ8UuImn', 'm22Lj3VfcwDNvqc2Rd', 'tTImgMAq1DDuVnPrbX')),
    'money': ('money talk, he spoils you',
              ('XQKBuQmfjt1xm', '10RTemEe5yjo0U', 'cLLgfNJiKppgA', 'ND6xkVPaj8tHO')),
}

TAGS = frozenset(_CATALOG)

# Per-reply roll knobs — house defaults, overridable PER ACCOUNT via
# style_config_json (cat_sticker_skip_pct / cat_sticker_solo_pct /
# cat_sticker_gap_min — see load_cat_sticker_config above); the resolved
# values arrive here as call args. "skip" hides the protocol entirely,
# "allow" shows it and lets the model judge, "solo" nudges a sticker-ONLY
# reply (the gif replaces the text). Wide open since 07-23: skip=0 and no
# per-fan gap — volume is tuned live from the Styles card.
DEFAULT_SKIP = 0.0     # chance a reply never sees the sticker block
DEFAULT_SOLO = 0.05    # chance of the gif-replaces-the-text nudge
DEFAULT_GAP_MIN = 0.0  # per-fan minutes between stickers (0 = no floor)

# Per-fan floor state — in-memory (restart = clean slate).
_last_sent: dict[tuple[str, int], datetime] = {}
# Per-fan LAST (tag, giphy_id) — the consecutive-repeat guard. Also in-memory, and
# deliberately so: the tag exists only in the prompt protocol (never persisted) and
# the giphy id lives in `messages.raw_json`, which _gather does NOT select — pulling
# raw_json into that whole-account scan to save one duplicate gif after a relay
# restart is the wrong trade. Worst case on restart is a single repeat.
_last_pick: dict[tuple[str, int], tuple[str, str]] = {}

# Six of the audited leaks were stickers, in three shapes — `*sticker: flirty*`
# (5 of the 6), the marker appended inline after a real question, and a
# one-letter typo. The COLON is this protocol's discriminator, and it is what
# makes anchorless matching safe here: the same audit turned up fan-facing lines
# like "you design stickers that make people obey" (he designs stickers for a
# living), and no colon means untouched. See `_markers` for the rest.
_MARKER_RE = protocol_marker_re(r"STICKERS?[ \t]*:")

# ── prose about the sticker ──
#
# The model sometimes writes ABOUT its pick instead of just emitting the marker:
# narrating it ("sticker works for this"), or reciting the solo directive
# outright. On 07-31 a fan got two bubbles of our own instructions, split across
# two sends by the bubble splitter. `_MARKER_RE` cannot see any of it — this
# protocol's discriminator is the COLON, and "the STICKER line" has none.
#
# One rule covers every leak, and it needs no vocabulary list. Read back off
# `grok_calls`, all four distinct leaked responses have the SAME shape — a CORRECT
# marker with prose about the sticker sitting above it:
#
#     a cat sticker says it all this time - if ANY sticker fits, reply with ...
#                                                        + STICKER: flirty
#     no text - just the sticker says it all             + STICKER: sleepy
#     a cat sticker feels right here                     + STICKER: eyeroll
#     ...this is cute. sticker works for this.           + STICKER: shy
#
# So: the marker already sends the gif, and text that still NAMES a sticker is the
# model describing its own protocol. The text goes; the reaction still lands, so
# the fan gets a reply either way.
#
# Free on the real corpus, which is why it needs no cleverness: across 520k
# outbound bodies exactly ONE genuine chat-engine reply says "sticker" — "you're
# the one that bought the stickers" (07-26, he collects them) — and it carried no
# marker, so this rule never sees it. `prompt_block` asks the model for the same
# restraint; this is the floor under it, because asking has not been enough.
_STICKER_WORD_RE = re.compile(r"(?<![A-Za-z])stickers?(?![A-Za-z])", re.IGNORECASE)


def _tag_for(words: str) -> str | None:
    """The catalog tag a marker asked for — None when the pack holds no such
    reaction, which is a silent no-gif and never a leak.

    Tolerant on purpose: a tag is two words as often as one (`miss you`), the
    model varies the separator, and an unambiguous prefix resolves so that
    `eyerol` — live, one letter short of `eyeroll` — still buys its gif."""
    parts = [p for p in re.split(r"[^A-Za-z]+", (words or "").lower()) if p]
    for take in (2, 1):
        key = "_".join(parts[:take])
        if not key:
            return None
        if key in _CATALOG:
            return key
        near = [t for t in _CATALOG if t.startswith(key)]
        if len(near) == 1:
            return near[0]
    return None


def parse_marker(raw: str) -> tuple[str, str | None]:
    """Strip EVERY sticker marker from the reply — the fan must never see the
    protocol, malformed included — and return the tag it asked for."""
    m = _MARKER_RE.search(raw or "")
    clean = _MARKER_RE.sub("", raw or "").strip()
    if m:
        # The marker is already gone, so this reads only what the fan would have
        # seen. See _STICKER_WORD_RE: prose about the sticker rides above a
        # correct marker, and the gif is the message.
        if clean and _STICKER_WORD_RE.search(clean):
            # Logged, not silent: this is the only path that deletes a whole
            # draft the fan would otherwise have read, so it has to be countable
            # — both to catch a new leak wording and to notice it over-firing.
            log.info("cat_stickers suppressed prose about the sticker: %r", clean[:120])
            clean = ""
        return clean, _tag_for(m.group(1))
    # A bare emote that NAMES a reaction we own is the same request as the
    # protocol, just without it: prod sent `*love*` as its whole reply, and
    # `love` is a catalog tag with five gifs behind it. Answering with the gif
    # beats the alternative, which is the narration strip emptying the draft and
    # the fan getting no reply at all that turn.
    #
    # EXACT key, not `_tag_for` — that resolves prefixes, and prefix-matching a
    # bare emote is how `*go on*` turns into a good_morning gif at a man
    # mid-conversation. Silence is better than the wrong reaction.
    #
    # Only the two chat engines call this, so only they turn a bare `*love*` into
    # a gif. In autoreply the same body reaches
    # `_markers.strip_narration` unparsed and is simply dropped. That asymmetry is
    # deliberate — that lane has no sticker send path to reach.
    if bare := bare_star_span(clean):
        tag = bare.lower().replace(" ", "_")
        if tag in _CATALOG:
            return "", tag
    return clean, None


def roll_mode(rng, on_cooldown: bool,
              skip_w: float = DEFAULT_SKIP,
              solo_w: float = DEFAULT_SOLO) -> str:
    """Per-reply dice: 'skip' | 'allow' | 'solo'. A cooldown forces 'skip' so
    the prompt never even offers a sticker the gate would drop. Weights are
    0..1; solo is carved out first, allow fills the rest up to 1-skip."""
    if on_cooldown:
        return "skip"
    skip_w = min(max(float(skip_w), 0.0), 1.0)
    solo_w = min(max(float(solo_w), 0.0), 1.0 - skip_w)
    r = rng.random()
    if r < solo_w:
        return "solo"
    if r < 1.0 - skip_w:
        return "allow"
    return "skip"


def cooldown_active(account_id: str, fan_id: int,
                    now: datetime | None = None,
                    gap_min: float = DEFAULT_GAP_MIN) -> bool:
    if gap_min <= 0:
        return False
    last = _last_sent.get((str(account_id), int(fan_id)))
    return (last is not None
            and (now or datetime.utcnow()) - last < timedelta(minutes=gap_min))


def mark_sent(account_id: str, fan_id: int,
              now: datetime | None = None,
              tag: str | None = None, gif_id: str | None = None) -> None:
    _last_sent[(str(account_id), int(fan_id))] = now or datetime.utcnow()
    if tag is not None or gif_id is not None:
        _last_pick[(str(account_id), int(fan_id))] = (tag or "", gif_id or "")


def keep_tag(account_id: str, fan_id: int, tag: str | None,
             *, has_text: bool) -> str | None:
    """The tag to actually send — None when it would be the same reaction twice in
    a row to this fan. Both chat engines call this; it is the one home for the rule.

    Observed on a live thread: he complimented her three turns running, the model
    answered `shy` all three times, and two of the three drew the SAME clip about
    twenty minutes apart. One `shy` is a girl being coy; three is a macro. Consecutive
    only — she may reuse the tag once something else has gone out.

    `has_text=False` (a solo roll, where the marker IS the whole reply) always keeps
    the tag: dropping it there would send an empty turn — nothing at all. A repeat
    solo still goes out, and `pick_gif` guarantees it is at least a different clip."""
    if tag is None or not has_text:
        return tag
    prev = _last_pick.get((str(account_id), int(fan_id)))
    return None if (prev and prev[0] == str(tag)) else tag


def open_with_gif(mode: str, *, turn_index: int, his_words: str) -> str:
    """Upgrade an `allow` roll to `solo` on his very first message.

    Operator call, off a live thread that opened gif-first and converted inside the
    first two hours. A NUDGE, not a mandate: `solo` tells the model a reaction may be
    the whole reply, and it writes text anyway when no sticker fits.

    Two guards, both load-bearing. A QUESTION is exempt — a gif in place of an answer
    is the one thing that reads as nobody being home. And only `allow` is upgraded:
    promoting a `skip` would put this above the account's own rate knob
    (`cat_sticker_skip_pct`) and above the cooldown, so an operator dialling gifs DOWN
    would still get one on every new fan.

    `his_words` must be his OWN text — not the history line, which carries the
    `[he sent: …]` vision tag and would let our describer's prose decide this."""
    if mode != "allow" or turn_index > 1 or "?" in (his_words or ""):
        return mode
    return "solo"


def pick_gif(tag: str, rng, account_id: str, fan_id: int,
             voice: str = "her") -> str | None:
    """The giphy id to send for `tag` — random among the tag's hand-picked gifs
    so the same reaction doesn't always land the same clip.

    The fan's PREVIOUS clip is excluded, which is the only guard that actually stops
    a visible repeat: the pools are 2-6 clips wide, so an independent draw per turn
    repeats ~25% of the time on a 4-clip tag, and the seed (fan + his text) does not
    help because his text differs every turn. Excluding is skipped when it would empty
    the pool, so a 1-clip tag still sends rather than going silent.

    `voice` selects the pack. There is deliberately NO fallback to the female
    pool on a male account: a tag `prompt_block` never offered him can still
    arrive (the model invents one, or an old marker survives a config flip), and
    sending a kitten from a dom is the failure this pack was built to remove.
    None is the right answer — the reply goes out as text."""
    if _is_him(voice):
        gifs = _CATALOG_HIM.get(tag, ())
    else:
        gifs = _CATALOG.get(tag, ("", ()))[1]
    if not gifs:
        return None
    prev = _last_pick.get((str(account_id), int(fan_id)))
    if prev and prev[1]:
        fresh = tuple(g for g in gifs if g != prev[1])
        if fresh:
            gifs = fresh
    return rng.choice(gifs)


# Tags a MALE creator may not send. The EMOTION each tag names carries a
# register: `shy`, `pout`, `beg` and `miss_you` are submissive-appealing;
# `kiss`, `love`, `dance`, `celebrate` and `excited` are eager-affectionate.
# Every one reads as performing for approval, which is the exact register the
# male lane exists to invert (see _voice._HUMANIZER_PUSHBACK). Kept as a named
# frozenset rather than folded into the catalog below because it states the
# INTENT — `test_cat_stickers` asserts the male catalog contains none of them,
# so re-adding one has to be deliberate rather than a slipped curation pick.
_TAGS_NOT_FOR_HIM = frozenset({
    "shy", "pout", "beg", "miss_you", "kiss", "love", "dance", "celebrate",
    "excited",
})

# The MALE pack — dogs and wolves, hand-picked 2026-08-02 via
# `cat_stickers/harvest_male.py` → contact sheets → `finalize_male.py`;
# pruned 2026-08-15 alongside the female catalog (dropped tags live in git).
#
# Only the gif ids differ: a tag means the same thing in both lanes, so the
# when-to-use lines are read from `_CATALOG` above and are never duplicated
# here. That is what keeps `prompt_block` honest — one set of descriptions, one
# place to edit, and a male tag list that is exactly this dict's keys.
#
# ⚠️ WHY NOT GYM AND COMBAT SPORTS, WHICH WERE ASKED FOR
# Both were harvested (26 candidates across `shocked`/`money`, 15 for
# `thumbs_up`) and both yielded ~nothing, for a structural reason worth keeping
# written down so nobody re-runs it: giphy's supply for those terms is
# RECOGNISABLE PEOPLE. "boxer shocked" is Joe Rogan and Dana White cageside;
# "fighter cash money" is Mayweather and McGregor; "gym thumbs up" is stock
# personal-trainer footage. A gif of an identifiable man, sent from the
# creator's own account, is a worse failure than the tonal problem this lane
# was opened to fix — the fan reads it as a photo of "him" (it is not, and a
# reverse image search says so) or as a forwarded celebrity reaction, which no
# real person sends as a selfie. The cat pack works precisely because a cat is
# NOBODY. So the male pack is animals too.
_CATALOG_HIM: dict[str, tuple[str, ...]] = {
    'laugh': ('FLKUJnRt6cGBG102B0', 'Ut0KxC3gnIwcEpmTZW', 'XN8YOV0H6YfVFFGxth'),
    'flirty': ('sWBzg2D15WwQjHcxbt', 'eKP4xPPkYm7WyMyDR2', 'zZbkdtXpqqkARUomtQ'),
    'eyeroll': ('Wwn5NKv4At2CIc8XQa', '11nQ2iZnQpPkgo', 'TZC932cYxsgr87gowA'),
    'shocked': ('ZK92FCOPbY8JaFhiMM', '1ZNI35FsYGMtko7m9k', 'hoictzHHdRbZr0XrqE'),
    'sleepy': ('d26dt3KtXoPeft6Lm6', '13MqteASr9UOGs', 'NFnR57Oj2LiWcJUb01'),
    'good_morning': ('b9NywKMAEpmPm', 'ROjjp6hqqgACs', 'OssGz3OQzVqb6'),
    'waiting': ('hgT3tIssMXLTc7ZwwT', '1xV85u4aYIXfpVh8xW', 'UNkS45j4Rcam9TyOQ2'),
    'money': ('RoS4JYcw0RvK8', 'VRwGkD5zYcbW8', '12pJ8OxSWwO86Y'),
}

# The pack's own name reaches the model, and "cat" is a fact about the pictures
# rather than a style knob: a male account whose prompt says "you have a pack of
# cat reaction gifs" gets a tag list of dogs and wolves described as cats, and
# the one leak class no regex catches is the model narrating the sticker (see
# prompt_block's last rule). Naming it correctly is cheaper than any strip.
_PACK_NOUN = {"her": "cat", "him": "dog & wolf"}


def _is_him(voice: str) -> bool:
    return str(voice or "").strip().lower() == "him"


def prompt_block(mode: str, voice: str = "her") -> str:
    """The prompt section for 'allow'/'solo' rolls ('' otherwise). The tag
    protocol is the wording validated on real convos (0 invalid tags across 90
    samples); the last rule was added later, unvalidated — see below."""
    if mode not in ("allow", "solo"):
        return ""
    male = _is_him(voice)
    # The male tag list IS the male catalog's keys — a tag offered with no gif
    # behind it is a turn where the model emits a marker and `pick_gif` returns
    # None, i.e. a silently dropped reaction.
    items = [(t, w) for t, (w, _) in _CATALOG.items()
             if not male or t in _CATALOG_HIM]
    tag_lines = "\n".join(f"- {t}: {when}" for t, when in items)
    # "the kind real girls spam in texts" is the framing, and it is the sentence
    # that tells the model WHO is sending. Left as-is on a male account it asks a
    # dom to text like a girl, in the same prompt that just told him not to.
    frame = ("the kind you fire off when a word would be too much"
             if male else "the kind real girls spam in texts")
    noun = _PACK_NOUN["him" if male else "her"]
    block = (
        f"{noun.upper()} STICKERS — {noun} reaction gifs, "
        f"{frame}. Tags:\n" + tag_lines + "\n"
        "Rules: MOST replies need NO sticker, max ONE. to attach, end your "
        "reply with a line that is exactly: STICKER: <tag> — it's stripped, "
        "the fan only sees the gif. it can BE the whole reply (ONLY the "
        "STICKER line, no text). "
        # The only leak class a regex cannot touch. Three fans got prose instead
        # of a gif ("a cat sticker feels right here", "sticker works for this"):
        # the model narrating the reaction rather than emitting the marker. No
        # strip can catch that without also eating the fan who genuinely talks
        # about stickers, so it has to be prevented upstream, here.
        "NEVER mention the sticker in your text."
    )
    if mode == "solo":
        block += (
            "\n- THIS message: if ANY sticker fits, reply with ONLY the "
            "STICKER line, no text.")
    return block
