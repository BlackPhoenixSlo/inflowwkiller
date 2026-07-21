"""service/vault_catalog_seed.py — seed the 12-rung default catalog and bind
vault media to each rung from the stored V2 describe fields.

The catalog is what `ai_chatter` may actually sell: each `catalog_items` row is
one rung of a price ladder, and its `description_for_ai` is the ONLY thing the
model may promise ("_manifest_block": describe a piece using ONLY its
description). Today those rows are hand-made, and on production that shows —
34% of items are enabled with zero media, and the same media id is re-sold at
three different prices up the same ladder.

This module does the binding from data instead. The 12 default rungs below are
the ones seeded on production verbatim (label / price / caption); what is new is
that each carries a MATCH SPEC, so the content under a caption is chosen by what
the vision layer actually found in the media rather than by hand.

Three rules, each of which exists because of an observed production defect:

  1. **A rung stays EMPTY rather than take wrong content.** The caption is a
     promise — "me and him, the whole scene 🔥" on a solo clip is a refund, and
     worse than silence. An item with no media is never offered (verified on
     prod), so an empty rung is safe; a mis-bound one is not. the graded vault has no
     partner content at all, so her collab rung is seeded empty on purpose.
  2. **No media id appears in two rungs.** On prod the graded vault has 26 ids bound to
     more than one rung — "Long solo finish" ($200) shares 6 of its 9 with
     cheaper rungs, so a fan who climbs the ladder is re-sold what he owns.
     Allocation here is exclusive.
  3. **Scarcest rung first.** Payoff content is rare (19 of the graded vault's 103 items
     can close a sale at all). Filling the $8 rung first would eat the material
     the $200 rung needs, so rungs are allocated in order of how few candidates
     they have, not in price order.

Read-only until `apply_seed` is called. Never touches OF.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Any, Iterable

from sqlalchemy import delete, select

import vault_ai_brief
import vault_scripts
from db.engine import get_session
from db.models import CatalogItem, VaultItem
from vault_scripts import _ACT_ALIASES

log = logging.getLogger("of-relay.vault_catalog_seed")


# An `acts` array this long is the model echoing the ACTS *menu* back instead of
# choosing from it. Measured over both test accounts: real rows carry 0-4 acts
# (99/68/22/6/2 at counts 0-4) and exactly ONE row carried 25 — the entire
# vocabulary verbatim, `none` included. That row is a lingerie tease clip, and
# the phantom `sex_missionary`/`toy_insertion` in it were enough to win the $200
# "Collab scene" rung. A caption is a promise, so a row we cannot trust must
# contribute NO acts rather than its most flattering one.
_MENU_ECHO_MIN_ACTS = 8

# Where the taxonomy and the prose disagree, believe the PROSE. The V2 prompt
# asks for 2-4 literal sentences and gets them right far more often than it gets
# the closed fields right — measured on the graded vault: 3 items are tagged
# `clothing_state=fully_nude` while their own description says "wearing black
# lace bodysuit", and the vault's ONE dildo clip ("using a pink dildo on
# herself") carries an EMPTY `toys` list. Trusting the field alone put a
# bodysuit under "all of me, nothing left on 🤭" and left the toy rung empty
# while its content sat in another rung.
_GARMENT_RE = re.compile(
    r"\b(bodysuit|lingerie|bra|panties|underwear|thong|dress|skirt|bikini|"
    r"tank ?top|shirt|stockings|fishnets?)\b", re.I)
_TOY_RE = re.compile(r"\b(dildo|vibrator|toy|wand|plug)\b", re.I)

# ── Previews ────────────────────────────────────────────────────────
#
# OF's `previews` is the UNLOCKED SLICE OF THE ATTACHMENT, not a separate
# teaser: `of_client.send_message` sends 3 mediaFiles + price + previews=[id1]
# and the fan sees 1 free thumb and 2 locked, and `scripts_api` enforces
# `previews ⊆ media_ids`. So a preview photo has to be ADDED to the item, which
# is also why it doubles as the after-they-buy bonus — the buyer receives it too.
#
# A video rung therefore needs a companion STILL. Batches are kind-pure (photos
# and videos upload in separate bursts) so `script_id` cannot link them, and on
# the graded vault the photo batches (06-30 → 07-01) barely overlap the video batches
# (07-01 → 07-02) — so "same shoot" is NOT provable and is not claimed here.
# What is claimed is OUTFIT COHERENCE: the fan sees a black-lace still and
# unlocks a black-lace clip, which reads consistent however far apart they were
# filmed. Colour+material is the join (`setting` is useless — 73 of 76 photos
# and 18 of 20 videos are "bedroom"), and because black alone covers 33 of 80
# photos, token overlap and upload proximity break the tie.
_COLOURS = ("black", "pink", "blue", "red", "white", "green", "purple")


def _garment_tokens(row: dict[str, Any]) -> tuple[set[str], set[str]]:
    """(colours, all tokens) describing the outfit. Colour is its identity —
    same colour and material matches far more reliably than any single garment
    word, since a bra in the stills becomes 'underwear' in the clip.

    Falls back to the DESCRIPTION when `clothing_items` is empty but the prose
    names a garment: the vault's toy clip reads "in black lace lingerie" with an
    empty list, and without this it would match on nothing and take whatever
    upload sat nearest. Colours are only read from prose alongside a garment
    word, so "white sheets" never becomes an outfit.
    """
    toks: set[str] = set()
    for g in row["fields"].get("clothing_items") or []:
        toks |= {w for w in re.split(r"\W+", str(g).lower()) if w}
    if not toks:
        text = row.get("text") or ""
        if _GARMENT_RE.search(text):
            toks = {w for w in re.split(r"\W+", text.lower()) if w}
    return {c for c in _COLOURS if c in toks}, toks


# A preview is given away FREE, so it is capped at DRESSED or SUGGESTIVE — never
# explicit, never fully nude, whatever the locked content is. An earlier rule
# only required the preview to be no more explicit than the piece it teased,
# which let "Full nude set" ($50, "all of me, nothing left on 🤭") unlock an
# explicit fully-nude still: the product itself, for nothing. The tease has to
# leave something unspent.
_PREVIEW_MAX_TIER = "suggestive"
_PREVIEW_BANNED_CLOTHING = ("fully_nude", "pulled_aside", "pulled_down")


def preview_ok(row: dict[str, Any]) -> bool:
    """May this piece be unlocked for free?"""
    s = row["sell"]
    if s["stands_alone"]:
        return False               # a piece that can close a sale IS the sale
    ceiling = vault_ai_brief.EXPLICITNESS_LADDER.index(_PREVIEW_MAX_TIER)
    if s["rung"] is None or s["rung"] > ceiling:
        return False               # explicit / hardcore / untiered → never free
    if str(row["fields"].get("clothing_state") or "").lower() in _PREVIEW_BANNED_CLOTHING:
        return False               # nothing left to promise
    if _GARMENT_RE.search(row["text"]) is None and not row["fields"].get("clothing_items"):
        return False               # no garment named anywhere → can't call it dressed
    return True


def _tameness(row: dict[str, Any]) -> tuple:
    """Sort key, lowest = safest to give away free. A preview must never be the
    payoff: an item that can close a sale on its own is worth money, and
    unlocking it hands over the thing being sold."""
    s = row["sell"]
    return (bool(s["stands_alone"]), s["rung"] if s["rung"] is not None else 9,
            -(row["duration"] or 0), row["media_id"])


def sane_acts(fields: dict[str, Any]) -> set[str]:
    """Normalised, trustworthy act set.

    Two corrections, both from real data: the model returns menu-adjacent
    spellings (`stripping` for `strip`), so the alias table is reused rather
    than letting a rung silently miss its content; and a menu-echo row is
    discarded wholesale (see `_MENU_ECHO_MIN_ACTS`).
    """
    raw = fields.get("acts") or []
    if not isinstance(raw, list):
        return set()
    if len(raw) >= _MENU_ECHO_MIN_ACTS:
        return set()
    out: set[str] = set()
    for a in raw:
        s = str(a).strip().lower()
        if s in ("", "none", "unclear"):
            continue
        out.add(_ACT_ALIASES.get(s, s))
    return out


class Rung:
    """One catalog rung + the spec that decides what may sit under its caption.

    `want` is how many media the rung takes at most. `min_media` is what it
    needs before it is worth creating at all — below that the rung is seeded
    EMPTY on purpose (rule 1).
    """

    def __init__(self, label: str, kind: str, price_cents: int, caption: str, *,
                 want: int, min_media: int = 1,
                 tiers: Iterable[str] | None = None,
                 acts_any: Iterable[str] | None = None,
                 acts_none: Iterable[str] | None = None,
                 clothing_any: Iterable[str] | None = None,
                 clothing_none: Iterable[str] | None = None,
                 payoff: bool | None = None,
                 media_kind: str | None = None,
                 partner: bool | None = None,
                 needs_toy: bool = False,
                 bare_only: bool = False,
                 preview_want: int = 1,
                 bundle: bool = False) -> None:
        self.label, self.kind, self.price_cents = label, kind, price_cents
        self.caption, self.want, self.min_media = caption, want, min_media
        self.tiers = set(tiers or ())
        self.acts_any = set(acts_any or ())
        self.acts_none = set(acts_none or ())
        self.clothing_any = set(clothing_any or ())
        self.clothing_none = set(clothing_none or ())
        self.payoff, self.media_kind = payoff, media_kind
        self.partner, self.needs_toy = partner, needs_toy
        self.bare_only = bare_only
        self.preview_want = preview_want
        self.bundle = bundle

    def eligible(self, row: dict[str, Any]) -> bool:
        """HARD gate. Everything here is a promise the caption makes; failing
        any of it means the media cannot honestly sit under that text."""
        f, s = row["fields"], row["sell"]
        # The rung's own price is a promise as binding as its caption, and the
        # only one the tier/act filters cannot express: nothing in "Custom /
        # exclusive" ($200) said the content had to be worth $200, so a
        # 26-second lingerie tease cleared every other gate it had.
        if not in_band(row, self.price_cents, self.want):
            return False
        if self.media_kind and row["kind"] != self.media_kind:
            return False
        if self.tiers and s["tier"] not in self.tiers:
            return False
        if self.payoff is not None and bool(s["stands_alone"]) is not self.payoff:
            return False
        if self.needs_toy and not (
                f.get("toys")
                or ({"toy_insertion", "toy_on_clit", "riding_toy"} & row["acts"])
                or _TOY_RE.search(row["text"])):
            return False
        if self.bare_only and _GARMENT_RE.search(row["text"]):
            return False    # her own description says she is still wearing something
        if self.partner is True and f.get("partner_visible") is not True and not (
                {"sex_missionary", "sex_doggy", "sex_riding", "blowjob", "handjob",
                 "cumshot"} & row["acts"]):
            return False
        if self.partner is False and f.get("partner_visible") is True:
            return False
        if self.acts_none & row["acts"]:
            return False
        if self.acts_any and not (self.acts_any & row["acts"]):
            return False
        if self.clothing_any and str(f.get("clothing_state") or "").lower() \
                not in self.clothing_any:
            return False
        if str(f.get("clothing_state") or "").lower() in self.clothing_none:
            return False
        return True

    def affinity(self, row: dict[str, Any]) -> tuple:
        """Rank among eligible rows — higher is a better fit. Prefers an act the
        caption names, then longer clips (a $200 'full thing' should not be the
        shortest clip on the shelf), then a stable id so a re-run is identical."""
        named = len(self.acts_any & row["acts"]) if self.acts_any else 0
        return (named, row["duration"] or 0, -row["media_id"])


# ── Price floors ────────────────────────────────────────────────────
#
# Operator floors (2026-07-21), by what the media actually IS rather than by
# which caption it landed under. A rung's seeded price is a starting point; the
# floor is what the content is worth, so a rung that ends up holding a toy clip
# can never go out at the tease price even if the allocator surprised us.
#
#   toys or a payoff act   $100   she is doing something, not showing something
#   explicit video          $50   motion is worth more than a still
#   explicit / nude image   $25
#
# Floors only ever RAISE a price — a rung priced above its floor keeps its
# price, because the ladder's upper rungs are positioned, not costed.
_FLOOR_ACTS_OR_TOYS = 10000
_FLOOR_EXPLICIT_VIDEO = 5000
_FLOOR_NUDE_IMAGE = 2500
# The $10 tease tier from `vault_scripts` — breasts out, still covered below.
_FLOOR_PARTIAL_REVEAL = 1000

# A floor only answers "is this row too CHEAP", and it cannot catch the defect
# an operator actually sees, because every piece of content clears a floor of
# $0. Measured on the graded vault: "Custom / exclusive" ($200 — "one of one, nobody
# else ever sees this") went out over a 26-second lingerie tease, and "Body rate
# video" ($90) over two suggestive clips she is fully dressed in. Both cleared
# their floor.
#
# So a content class is a BAND, not a minimum — the most it can honestly be
# asked for as well as the least:
#
#   payoff act or toy    $100 → ∞     she is DOING something, not showing it
#   explicit video        $50 → $100  motion, but it does not finish
#   explicit/nude image   $25 → $50
#   video, below explicit  $8 → $50   a 91s strip is worth more than a still
#   dressed / suggestive   $8 → $25   there is a reason this rung is cheap
#
# The band is the gate, in BOTH directions, and that is what makes the ladder
# hold together: too-tame content cannot be sold at $200, and — the half a floor
# can never express — a payoff still cannot hide inside a $25 tease set either.
# Under a floor-only rule that set was simply repriced to $100, which silently
# turned "wore this thinking of you" into a closer.
_CEILING_TAME = 2500
_CEILING_TAME_VIDEO = 5000
_CEILING_NUDE_IMAGE = 5000
_CEILING_EXPLICIT_VIDEO = 10000


# The flags pass is stills-only and opt-in per account (`POST /flags-all`), so
# most vaults do not have it yet. Its readers treat a MISSING flag as "not
# covered" — right for a lane, wrong for a price floor, where it would read
# every item with breasts in `body_focus` as topless and lift its floor to $10.
# A price rule may only use a flag that was actually answered.
def _has_flags(fields: dict[str, Any]) -> bool:
    """Fully answered, not partially. A price rule reading a half-flagged row
    would fall back to `clothing_state` for the missing half without saying so,
    which is the unreliable signal these flags exist to replace."""
    return vault_ai_brief.flags_known(fields)


def item_floor(row: dict[str, Any]) -> int:
    """The LEAST one piece of media may be sold for, in cents."""
    return item_band(row)[0]


def item_ceiling(row: dict[str, Any]) -> int | None:
    """The MOST a row holding this piece may ask. None = uncapped."""
    return item_band(row)[1]


def item_band(row: dict[str, Any]) -> tuple[int, int | None]:
    """(floor, ceiling) — the honest price range for ONE piece of media."""
    s, f = row["sell"], row["fields"]
    explicit = (s["rung"] or 0) >= vault_ai_brief.EXPLICITNESS_LADDER.index("explicit")
    has_toy = bool(f.get("toys")) or bool(_TOY_RE.search(row["text"])) or bool(
        {"toy_insertion", "toy_on_clit", "riding_toy"} & row["acts"])
    if has_toy or s["stands_alone"]:
        return (_FLOOR_ACTS_OR_TOYS, None)
    if row["kind"] == "video":
        return ((_FLOOR_EXPLICIT_VIDEO, _CEILING_EXPLICIT_VIDEO) if explicit
                else (0, _CEILING_TAME_VIDEO))
    # Stills are priced off the FLAGS pass when it has run, not off
    # `clothing_state`. The enum was measured wrong on ~1 in 3 of the items this
    # boundary is built from — it reported `fully_nude` over panties still on —
    # and these flags are a direct yes/no the model had to answer by looking.
    # `vault_scripts` owns the reading of them; a second copy drifts on the
    # first tune.
    # ...and only inside the PAID tiers, which is the gate every lane in
    # `vault_scripts` reads these flags behind. `_is_nude` is literally
    # `not underwear_visible`, so a street selfie in a leather jacket — no
    # underwear anywhere in frame — came back NUDE and was priced $25-$50.
    # Three of them then padded a $100 row as "explicit". The flags answer how
    # much is on show in material already judged explicit; they are not a nudity
    # detector for a coffee-shop photo.
    if _has_flags(f) and explicit:
        if vault_scripts._is_nude(f) or vault_scripts._shows_genitals(f):
            return (_FLOOR_NUDE_IMAGE, _CEILING_NUDE_IMAGE)
        # The middle tier the old binary could not express: breasts out, but
        # still covered below. On the graded vault the two flags disagree on 31 of 81
        # stills, and this is where those 31 live — a real reveal, worth more
        # than a peek and less than the full one.
        if vault_scripts._shows_breasts_bare(f):
            return (_FLOOR_PARTIAL_REVEAL, _CEILING_NUDE_IMAGE)
    if explicit or str(f.get("clothing_state") or "").lower() == "fully_nude":
        return (_FLOOR_NUDE_IMAGE, _CEILING_NUDE_IMAGE)
    return (0, _CEILING_TAME)


# Volume is the other half of a bundle's price, and a per-item ceiling cannot
# see it: "20+ pics of me — my whole set, everything i've got" is not worth what
# one still is worth, yet a flat $50 ceiling made the shipped $60 rung
# permanently unfillable. So a bundle may ASK more than a single piece —
#
#   NEVER the reverse. Volume does not make a piece worth LESS, so the floor is
#   untouched: putting twelve nude stills in an $8 rung is still giving them
#   away, and no amount of counting fixes that.
_VOLUME_STEPS = ((16, 3.0), (8, 2.0), (3, 1.5))


def volume_factor(count: int) -> float:
    """How much more a bundle of `count` may ask than one piece of it."""
    for at_least, mult in _VOLUME_STEPS:
        if count >= at_least:
            return mult
    return 1.0


def in_band(row: dict[str, Any], price_cents: int, count: int = 1) -> bool:
    """May this piece sit in a row asking `price_cents` for `count` media?"""
    if not price_cents:
        return True                     # unpriced row (a draft) — nothing to check
    floor, ceiling = item_band(row)
    if price_cents < floor:
        return False
    return ceiling is None or price_cents <= ceiling * volume_factor(count)


# ── What a price buys, as SHAPE ─────────────────────────────────────
#
# The band says a piece is explicit enough for the ask. It says nothing about
# how MUCH there is, and at the top of the ladder that is most of the value: a
# $200 row over one 26-second clip is not a lie about the content, it is a bad
# deal, and a fan who buys once and feels short-changed is worse than one who
# never buys.
#
# Two operator rules, both about volume rather than heat:
#   * runtime — $100+ wants 5 minutes of video in total, $50+ wants 3.
#   * a still per $20 — a $200 bundle carries at least 10 images.
#
# Neither can refuse content the way the band does. Runtime is met by taking
# MORE clips, and the images may be dups of material other rows already sell:
# they are there to make the bundle feel like one, and a photo the fan has seen
# is still a photo he now owns. Where they cannot be met, the row is FLAGGED,
# not emptied — an undersized bundle is a weak offer, not a false one, and the
# operator prices that call, not this module.
_VIDEO_SECONDS_FOR_PRICE = ((_FLOOR_ACTS_OR_TOYS, 300), (_FLOOR_EXPLICIT_VIDEO, 180))
_CENTS_PER_IMAGE = 2000
_MIN_IMAGES_PER_SET = 10


def min_video_seconds(price_cents: int) -> int:
    """Total runtime a video row at this price should carry."""
    for at_least, seconds in _VIDEO_SECONDS_FOR_PRICE:
        if price_cents >= at_least:
            return seconds
    return 0


def min_images(price_cents: int, kind: str = "") -> int:
    """Stills a row at this price should carry.

    One per $20 — plus a flat floor for anything sold AS photos. A photo set is
    a volume product before it is a heat product: "a quick lil peek" at $8 came
    out holding two stills, and two stills is not a peek, it is an accident. The
    per-$20 rule only starts to bind above $200, which is the wrong end of the
    ladder to be the only rule.

    A video row is exempt from the flat floor — its stills ride along with the
    clip rather than being the product.
    """
    if price_cents <= 0:
        return 0
    per_dollar = -(-int(price_cents) // _CENTS_PER_IMAGE)      # ceil
    if kind in ("image", "image_set"):
        return max(_MIN_IMAGES_PER_SET, per_dollar)
    return per_dollar


def total_seconds(items: list[dict[str, Any]]) -> int:
    return sum(int(r["duration"] or 0) for r in items if r["kind"] == "video")


def count_images(items: list[dict[str, Any]]) -> int:
    return sum(1 for r in items if r["kind"] == "photo")


def pick_padding(items: list[dict[str, Any]], rows: list[dict[str, Any]],
                 want: int, price_cents: int, *, exclude: set[int]
                 ) -> list[dict[str, Any]]:
    """Stills that bulk a bundle out to its per-$20 count.

    DUPS ARE THE POINT — this is the one place exclusivity is deliberately off.
    Padding is not what the caption sells, so re-showing a still another row
    also carries costs the ladder nothing; refusing to would leave a $200 row
    holding one clip on a vault where every good still is already spoken for.

    The band does NOT apply. Asking each filler still to justify the row's price
    rejected exactly the cheap material padding is made of (every $24 tease
    still thrown out of a $100 row for not being worth $100); asking it to stay
    UNDER the price rejected the good ones, which is what an operator notices —
    a $200 bundle padded with ten dressed stills is not a $200 bundle.

    One line survives instead: **a piece that can close a sale is never
    padding.** Payoff acts and toys are the top rung's whole inventory, and
    handing one over inside an $8 peek collapses the ladder it sits on. Above
    the payoff price the row already sells that tier, so the bar lifts.

    Ordered by the row's OWN level, not by heat alone. Material the row could
    already sell outright comes first, hottest of it first — that is what puts
    real explicit stills in the expensive bundles. Only once that runs out does
    it reach ABOVE the ask, and there it takes the LEAST revealing first, so an
    $8 peek climbs one rung into topless rather than opening on the vault's most
    explicit nude. Same outfit outranks both, so the bundle reads as one shoot.
    """
    if want <= 0:
        return []
    want_colours: set[str] = set()
    for row in items:
        want_colours |= _garment_tokens(row)[0]
    cands = []
    for row in rows:
        if row["kind"] != "photo" or row["media_id"] in exclude:
            continue
        floor = item_floor(row)
        if floor >= _FLOOR_ACTS_OR_TOYS and price_cents < _FLOOR_ACTS_OR_TOYS:
            continue
        colours, _ = _garment_tokens(row)
        affordable = floor <= price_cents
        # HEAT outranks outfit coherence. With outfit first, a $50 set padded
        # itself with the tamest stills that happened to share its colour while
        # explicit ones sat unused — the operator's note was "we want more
        # explicit images as addons", twice. Outfit is now only a tie-break
        # between equally hot candidates.
        cands.append(((0 if affordable else 1,
                       -floor if affordable else floor,
                       0 if (colours & want_colours) else 1,
                       *_tameness(row)), row))
    cands.sort(key=lambda t: t[0])
    return [row for _, row in cands[:want]]


def price_floor(items: list[dict[str, Any]]) -> int:
    """The least this bundle may be sold for, in cents. 0 when nothing is bound
    (an empty rung is never offered, so its price is moot)."""
    return max((item_floor(row) for row in items), default=0)


def price_ceiling(items: list[dict[str, Any]]) -> int | None:
    """The most this bundle may honestly be asked for. None = uncapped."""
    ceilings = [item_ceiling(row) for row in items]
    if not ceilings or any(c is None for c in ceilings):
        return None
    return max(c for c in ceilings if c is not None)


# The rungs seeded on production, captions verbatim. Prices are the prod
# defaults, raised where `price_floor` says the content is worth more.
DEFAULT_RUNGS: list[Rung] = [
    Rung("Quick rate", "image", 800,
         "rate me babe? 🙈 a quick lil peek just for you",
         want=6, media_kind="photo", tiers=("sfw", "suggestive"), payoff=False,
         clothing_none=("fully_nude",), preview_want=0),
    Rung("Selfie set", "image_set", 1200,
         "no makeup, just me being cute for you 😘",
         want=4, media_kind="photo", tiers=("sfw", "suggestive"), payoff=False,
         clothing_any=("dressed", "lingerie_on")),  # already excludes nude
    Rung("Lingerie set", "image_set", 2400,
         "wore this thinking of you… wanna see it come off?",
         # "…wanna see it come off?" is a caption about the garment COMING OFF,
         # so the mid-strip states are its subject, not a violation of it.
         # `lingerie_on` alone rejected 6 of the 8 stills this rung was already
         # selling — every one of them a bra pulled down or aside.
         want=8, media_kind="photo", tiers=("suggestive", "explicit"),
         clothing_any=("lingerie_on", "pulled_aside", "pulled_down",
                       "partially_off"), payoff=False),
    Rung("Shower strip", "video", 3600,
         "watch me get undressed and step into the shower 🚿",
         want=2, media_kind="video", acts_any=("shower", "bath", "strip", "undress")),
    Rung("Full nude set", "image_set", 5000,
         "all of me, nothing left on 🤭",
         want=8, media_kind="photo", clothing_any=("fully_nude",), payoff=False,
         bare_only=True),
    Rung("Bed tease", "video", 6000,
         "dancing and teasing on my bed just for you baby",
         want=2, media_kind="video",
         acts_any=("tease", "ass_shaking", "twerk", "posing"), payoff=False),
    Rung("Body rate video", "video", 9000,
         "close up… tell me exactly what you'd do to me 🥵",
         want=2, media_kind="video", tiers=("explicit", "hardcore"), payoff=False),
    Rung("Dirty talk / JOI", "video", 11000,
         "let me talk you through it and tell you exactly what to do",
         want=2, media_kind="video", acts_any=("talking_to_camera",)),
    Rung("Toy play", "video", 15000,
         "playing with my toy until i finish for you 😈",
         want=2, media_kind="video", needs_toy=True),
    Rung("Long solo finish", "video", 20000,
         "the full thing, start to finish, no cuts",
         want=3, media_kind="video", partner=False,
         acts_any=("masturbation_orgasm", "squirt", "fingering", "rubbing_clit")),
    Rung("Collab scene", "video", 20000,
         "me and him, the whole scene 🔥",
         want=2, media_kind="video", partner=True),
    Rung("Custom / exclusive", "video", 20000,
         "one of one, made just for you — nobody else ever sees this",
         want=2, media_kind="video", tiers=("explicit", "hardcore"), payoff=True),
    # Bundles. Deliberately the most permissive specs in the list, so scarcity
    # order allocates them LAST and they are built from what no themed rung
    # wanted — a bundle is leftovers sold as volume, never material taken off a
    # rung that could have sold it alone. `min_media` is high on purpose: "10+
    # images" that ships 4 is a worse promise than no bundle at all.
    Rung("Mixed set", "image_set", 3500,
         "a big mixed set — 10+ of me, all different 😇",
         want=12, min_media=10, media_kind="photo", payoff=False, preview_want=1,
         bundle=True),
    Rung("Mega mixed set", "image_set", 6000,
         "20+ pics of me — my whole set, everything i've got 😇",
         want=22, min_media=18, media_kind="photo", payoff=False, preview_want=1,
         bundle=True),
]


def rung_for(label: str, description: str = "") -> Rung | None:
    """The shipped rung a catalog row IS, matched on label or on caption.

    A row that carries a default caption was reviewed as that rung — with its
    `clothing_none`, its `tiers`, its `min_media`. Re-deriving a spec from the
    same words throws all of that away and keeps only what the keyword table
    happens to cover, which is how "rate me babe? 🙈 a quick lil peek just for
    you" ($8) ended up over two fully-nude stills: the rung forbids nude, the
    keyword "rate" only says sfw-or-suggestive, and the vision layer had tagged
    those two nudes `suggestive`.

    The CAPTION is what identifies it, not the label. An operator who keeps the
    name "Lingerie set" but rewrites the caption to something about her feet has
    written a different row, and handing it the shipped spec would override the
    words they actually typed. A label match only counts while the row has no
    caption of its own yet.
    """
    lab = (label or "").strip().lower()
    cap = (description or "").strip().lower()
    for r in DEFAULT_RUNGS:
        if cap:
            if cap == r.caption.strip().lower():
                return r
        elif lab and lab == r.label.strip().lower():
            return r
    return None


async def _load_rows(account_id: str) -> list[dict[str, Any]]:
    async with get_session() as s:
        recs = (await s.execute(
            select(VaultItem.media_id, VaultItem.kind, VaultItem.duration_seconds,
                   VaultItem.ai_fields_json, VaultItem.script_id, VaultItem.script_seq,
                   VaultItem.created_at)
            .where(VaultItem.account_id == account_id,
                   VaultItem.removed_at.is_(None))
            .order_by(VaultItem.created_at.asc())
        )).all()
    rows: list[dict[str, Any]] = []
    for mid, kind, dur, fj, sid, seq, created in recs:
        fields = vault_ai_brief.load_fields(fj)
        if not fields:
            continue          # never described → nothing to match on
        acts = sane_acts(fields)
        # Sanitise BEFORE sellability() reads it: `stands_alone` is derived from
        # `acts`, so a menu-echo row would otherwise be scored as a payoff piece
        # by the very field we just decided we cannot trust.
        fields = {**fields, "acts": sorted(acts)}
        rows.append({
            "media_id": int(mid), "kind": kind, "duration": dur,
            "fields": fields, "acts": acts,
            "text": str(fields.get("description") or ""),
            "sell": vault_ai_brief.sellability(fields, duration_seconds=dur),
            "script_id": sid, "script_seq": seq, "created": created,
        })
    return rows


def allocate(rows: list[dict[str, Any]], rungs: list[Rung] | None = None
             ) -> list[dict[str, Any]]:
    """Exclusive assignment of media to rungs, scarcest rung first.

    Returns one entry per rung (including the ones that end up empty) so the
    caller can see WHY a rung is unfilled rather than just missing.
    """
    rungs = rungs if rungs is not None else DEFAULT_RUNGS
    pools = {r.label: [row for row in rows if r.eligible(row)] for r in rungs}

    # Rule 3 — scarcity order. A rung with 1 candidate must pick before a rung
    # with 60, or the common rungs strip the shelf bare. Rule 4 — BUNDLES LAST,
    # after even the previews: a bundle is leftovers sold as volume, and the
    # most permissive spec in the list would otherwise sweep up both the themed
    # rungs' material and every still reserved to tease them.
    themed = [r for r in rungs if not r.bundle]
    bundles = [r for r in rungs if r.bundle]
    order = sorted(themed, key=lambda r: (len(pools[r.label]), r.label))

    taken: set[int] = set()
    out: dict[str, dict[str, Any]] = {}
    for r in order:
        avail = [row for row in pools[r.label] if row["media_id"] not in taken]
        avail.sort(key=r.affinity, reverse=True)
        picked = avail[: r.want]
        if len(picked) < r.min_media:
            picked = []                      # rule 1 — empty beats wrong
        taken.update(row["media_id"] for row in picked)
        out[r.label] = {
            "rung": r, "items": picked,
            "price_cents": max(r.price_cents, price_floor(picked)),
            "floor_cents": price_floor(picked),
            "candidates": len(pools[r.label]),
            "available": len(avail),
            "empty_reason": (
                "" if picked else
                ("no media in this vault matches the caption"
                 if not pools[r.label] else
                 "every match was already claimed by a scarcer rung")),
        }

    # Previews for the themed rungs run once all of them are filled — a still
    # can only become a free teaser after no themed rung wanted to SELL it.
    for r in themed:
        entry = out[r.label]
        entry["previews"] = _pick_previews(r, entry["items"], rows, taken)
        taken.update(row["media_id"] for row in entry["previews"])
        entry["price_cents"] = max(r.price_cents, price_floor(entry["items"]))

    # Only now do the bundles take what is genuinely left over.
    for r in sorted(bundles, key=lambda r: (len(pools[r.label]), r.label)):
        avail = [row for row in pools[r.label] if row["media_id"] not in taken]
        avail.sort(key=r.affinity, reverse=True)
        picked = avail[: r.want]
        if len(picked) < r.min_media:
            picked = []
        taken.update(row["media_id"] for row in picked)
        out[r.label] = {
            "rung": r, "items": picked,
            "price_cents": max(r.price_cents, price_floor(picked)),
            "floor_cents": price_floor(picked),
            "candidates": len(pools[r.label]), "available": len(avail),
            "empty_reason": ("" if picked else
                             f"fewer than {r.min_media} unclaimed images left over"),
        }
        out[r.label]["previews"] = _pick_previews(r, picked, rows, taken)
        taken.update(row["media_id"] for row in out[r.label]["previews"])

    return [out[r.label] for r in rungs]     # back into price order for display


def _pick_previews(rung: Rung, items: list[dict[str, Any]],
                   rows: list[dict[str, Any]], taken: set[int]
                   ) -> list[dict[str, Any]]:
    return pick_previews(rung.kind, rung.preview_want, items, rows, taken)


def preview_want_for(kind: str) -> int:
    """How many free stills a row of this kind should tease with.

    A single image previews NOTHING — unlocking the only image IS the sale.
    Everything else gets one, including video: a locked clip with no free frame
    is a price tag with nothing attached to it.
    """
    return 0 if kind == "image" else 1


def pick_previews(kind: str, preview_want: int, items: list[dict[str, Any]],
                  rows: list[dict[str, Any]], taken: set[int]
                  ) -> list[dict[str, Any]]:
    """The unlocked slice for one row.

    An image_set previews the tamest image it ALREADY holds, but ONLY if that
    image clears `preview_ok` — on a set where every piece is nude ("Full nude
    set") the tamest own image is still explicit, and unlocking it hands over
    the product. Such a set falls through to BORROWING a dressed still, exactly
    like a video does. The borrowed still is attached to the item as well, which
    is what makes it both the teaser and the after-purchase extra.

    A single-image rung previews nothing: unlocking the only image IS the sale.
    """
    if not items or preview_want <= 0:
        return []

    if kind == "image_set" and len(items) > preview_want:
        own = [r for r in sorted(items, key=_tameness) if preview_ok(r)]
        if own:
            return own[: preview_want]
        # every image in the set is too revealing to give away → borrow below

    want_colours: set[str] = set()
    want_toks: set[str] = set()
    for row in items:
        c, t = _garment_tokens(row)
        want_colours |= c
        want_toks |= t
    # A nude set names NO garments, so there is no colour to match on — and that
    # is precisely the rung most in need of a dressed teaser. Fall back to
    # upload proximity: within one creator's photos a neighbouring upload is the
    # same sitting, which is the honest claim anyway (see the note above — this
    # was never "same shoot" for photo↔video, but photo↔photo it holds).
    require_colour = bool(want_colours)

    cands: list[tuple[tuple, dict[str, Any]]] = []
    for row in rows:
        if row["media_id"] in taken or row["kind"] != "photo":
            continue
        if not preview_ok(row):
            continue
        colours, toks = _garment_tokens(row)
        if require_colour and not (colours & want_colours):
            continue                   # different outfit = different look
        overlap = len(toks & want_toks)
        # Upload proximity as a tie-break: among equally-matching black-lace
        # stills, the batch nearest the clip's own upload is the likeliest to
        # be the same session.
        gap = min((abs((row["created"] - it["created"]).total_seconds())
                   for it in items
                   if row.get("created") and it.get("created")),
                  default=0.0)
        cands.append(((-overlap, gap, *_tameness(row)), row))
    cands.sort(key=lambda t: t[0])
    return [row for _, row in cands[: preview_want]]


async def plan_seed(account_id: str) -> dict[str, Any]:
    """Read-only proposal: what each rung would get, and why a rung is empty."""
    rows = await _load_rows(account_id)
    alloc = allocate(rows)
    bound = sum(len(a["items"]) for a in alloc)
    return {
        "account_id": account_id,
        "described_items": len(rows),
        "allocation": alloc,
        "summary": {
            "rungs": len(alloc),
            "filled": sum(1 for a in alloc if a["items"]),
            "empty": sum(1 for a in alloc if not a["items"]),
            "media_bound": bound,
            "media_unused": len(rows) - bound,
        },
    }


async def apply_seed(account_id: str, plan: dict[str, Any], *,
                     replace: bool = False) -> dict[str, int]:
    """Write the rungs to `catalog_items`.

    `replace` wipes this account's existing catalog first — off by default so a
    hand-curated catalog is never destroyed by a re-run.
    """
    now = datetime.utcnow()
    async with get_session() as s:
        if replace:
            await s.execute(delete(CatalogItem).where(
                CatalogItem.account_id == account_id))
        for entry in plan["allocation"]:
            r: Rung = entry["rung"]
            px = int(entry.get("price_cents") or r.price_cents)
            ids = [row["media_id"] for row in entry["items"]]
            # previews MUST be a subset of media_ids (scripts_api enforces it,
            # and OF only unlocks ids that were attached), so a borrowed still
            # is attached to the item as well — teaser now, bonus after buying.
            prev = [row["media_id"] for row in entry.get("previews") or []]
            ids += [m for m in prev if m not in ids]
            s.add(CatalogItem(
                account_id=account_id, script_id=None, position=None,
                kind=r.kind, label=r.label, description_for_ai=r.caption,
                media_ids=json.dumps(ids),
                preview_media_ids=json.dumps(prev),
                duration_sec=(max((row["duration"] or 0) for row in entry["items"])
                              if entry["items"] else None),
                price_cents=px, tip_unlock_cents=px,
                is_free_teaser=False, tags="[]", enabled=True,
                created_at=now, updated_at=now))
        await s.commit()
    log.info("catalog seeded account=%s rungs=%d media=%d",
             account_id, len(plan["allocation"]), plan["summary"]["media_bound"])
    return {"rungs": len(plan["allocation"]),
            "media_bound": plan["summary"]["media_bound"]}


# ── Text-set suggestions (the operator's first button) ──────────────
#
# `vault_solo_fill` fills CONTENT into a row that already has TEXT. Nothing
# produced the text, so an empty catalog stayed empty — the operator had to
# invent 14 captions before the filler had anything to work on.
#
# This proposes the TEXT ROWS: caption, price, kind, no media. They flow
# straight into the existing "Fill content" → "Save singles" path, so the
# suggestion is never authoritative and nothing is written here.
#
# A suggestion is only worth making if the vault can honestly FILL it, so each
# proposal is checked against the unbound pool first. A caption the account has
# no content for is returned separately as a shot-list rather than as a row that
# would sit empty forever.


async def _bound_media(account_id: str) -> set[int]:
    """Every media id an existing catalog row already promises.

    Shared with `vault_catalog_copy` so a suggestion never counts SOLD media as
    free capacity — otherwise every proposal looks fillable off the same photos.
    """
    async with get_session() as s:
        blobs = (await s.execute(
            select(CatalogItem.media_ids)
            .where(CatalogItem.account_id == account_id))).scalars().all()
    out: set[int] = set()
    for blob in blobs:
        try:
            out.update(int(m) for m in json.loads(blob or "[]"))
        except (TypeError, ValueError):
            continue
    return out


async def _existing_labels(account_id: str) -> set[str]:
    async with get_session() as s:
        rows = (await s.execute(
            select(CatalogItem.label, CatalogItem.description_for_ai)
            .where(CatalogItem.account_id == account_id))).all()
    out: set[str] = set()
    for label, desc in rows:
        for v in (label, desc):
            if v and str(v).strip():
                out.add(str(v).strip().lower())
    return out


# A row only carries the closed taxonomy if it was described with the V2
# prompt. V1 rows have prose and free tags but NO `clothing_state`/`acts`, so
# every clothing- or act-gated rung matches nothing — on a 1,361-item vault
# that produced a shot-list telling the operator to film content she already
# owns. Coverage is reported so the answer is "describe this vault", never a
# false "you don't have this".
# NOT `acts` — `_load_rows` injects a sanitised `acts` key on every row, so it
# is present even on V1 data and would make this check pass for everything.
_V2_MARKERS = ("clothing_state", "penetration", "beats", "clothing_items")


def _is_v2(fields: dict[str, Any]) -> bool:
    return any(k in fields for k in _V2_MARKERS)


async def suggest_texts(account_id: str) -> dict[str, Any]:
    """Caption rows to add, grounded in what this vault can actually fill.

    Read-only, no LLM, no OF. Returns `proposals` (fillable now) and `shoot`
    (a caption worth having, with nothing in the vault behind it yet).

    Requires a V2-described vault: without the closed taxonomy every themed
    rung is unmatchable and the honest answer is "run Describe", not a
    shot-list. `blocked` says so rather than inventing either.
    """
    rows = await _load_rows(account_id)
    v2 = [r for r in rows if _is_v2(r["fields"])]
    if rows and not v2:
        return {
            "account_id": account_id, "proposals": [], "shoot": [],
            "blocked": ("this vault has no V2 describe fields yet — every themed "
                        "caption needs clothing_state/acts to match on. Run "
                        "Describe (V2) first; nothing here is a content gap."),
            "summary": {"proposed": 0, "shoot": 0, "already_present": 0,
                        "unbound_media": 0, "described": len(rows),
                        "v2_described": 0},
        }
    # Coverage decides whether to PROCEED — it must not filter the pool. A row
    # described by V2 whose fields all came back empty (the vault's only dildo
    # clip has no clothing_state, no clothing_items and no acts) is still real
    # content, and dropping it reported its rung as unfillable while the seeder
    # was filling it.
    v2_count = len(v2)
    have = await _existing_labels(account_id)

    bound = await _bound_media(account_id)
    free = [r for r in rows if r["media_id"] not in bound]

    # Reuse the REAL allocator on the unbound pool rather than a parallel greedy
    # — a second implementation drifted immediately (it reported "Toy play" as
    # unfillable while `allocate` was happily filling it), and a shot-list that
    # contradicts the filler is worse than none.
    alloc = allocate(free)
    have_lower = have

    proposals: list[dict[str, Any]] = []
    shoot: list[dict[str, Any]] = []
    for entry in alloc:
        r: Rung = entry["rung"]
        if (r.label.strip().lower() in have_lower
                or r.caption.strip().lower() in have_lower):
            continue                       # never propose a caption twice
        row = {
            "label": r.label, "description_for_ai": r.caption,
            "kind": r.kind, "price_cents": int(entry["price_cents"]),
            "fillable": len(entry["items"]), "needs": r.min_media,
            "floor_cents": int(entry["floor_cents"]),
            "previews": len(entry.get("previews") or []),
        }
        if entry["items"]:
            proposals.append(row)
        else:
            row["price_cents"] = r.price_cents
            row["why"] = entry["empty_reason"] or "nothing in this vault matches it"
            shoot.append(row)

    proposals.sort(key=lambda p: p["price_cents"])
    return {
        "account_id": account_id,
        "proposals": proposals,
        "shoot": shoot,
        "blocked": "",
        "summary": {
            "proposed": len(proposals),
            "shoot": len(shoot),
            "v2_described": v2_count,
            "already_present": sum(
                1 for r in DEFAULT_RUNGS
                if r.label.strip().lower() in have
                or r.caption.strip().lower() in have),
            "unbound_media": len(free),
        },
    }
