"""service/vault_solo_fill.py — fill CONTENT into catalog singles that have
text but no media.

`ai_chatter` sells from `catalog_items`, and each row's `description_for_ai` is
the only thing the model may promise (`_manifest_block`: describe a piece using
ONLY its description). On production **52 of 153 items are enabled with zero
media** — a written promise with nothing behind it. The engine never offers
those, so they are not a live bug; they are dead inventory, and every one of
them is a caption an operator already wrote and meant.

This proposes media for exactly those rows. It is the SOLO lane — standalone
pieces (`script_id IS NULL`), the flat pool the chatter picks from when a fan
asks for something. Script-sequence bundles are a separate producer.

The spec is derived from the ITEM'S OWN TEXT, not from a table of known labels:
an operator writes "bra rate" or "boob bounce naked" as often as they use a
shipped default, and a filler that only understands the 12 defaults would leave
every custom row empty. Keywords in `label` + `description_for_ai` become the
same hard constraints a hand-built spec would carry.

Three rules carried over from what production got wrong:

  1. **Empty beats wrong.** A caption is a promise; "playing with my toy" over a
     clip with no toy is a refund. No honest match → the row stays empty.
  2. **Never re-bind media another item already sells** — including script
     items. On prod one AriaFree media id sits in three rungs at three prices.
  3. **Believe the prose over the taxonomy.** Measured on AriaFree: 3 items are
     tagged `fully_nude` while their own description says "wearing black lace
     bodysuit", and the only dildo clip carries an empty `toys` list.

Read-only. `suggest_singles` proposes; the caller (and the operator) decide.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from sqlalchemy import select

import vault_ai_brief
import vault_catalog_seed
from db.engine import get_session
from db.models import CatalogItem, VaultItem
from vault_catalog_seed import (
    _GARMENT_RE, _TOY_RE, Rung, in_band, item_band, preview_want_for, rung_for,
    sane_acts)

log = logging.getLogger("of-relay.vault_solo_fill")

# How many media a row gets, by its declared kind. A set wants enough to feel
# like a set; a single image is one piece.
WANT_BY_KIND = {"image": 1, "image_set": 8, "video": 1}

# The fewest media that still make the word honest. "Set" is plural: AriaFree's
# "White halter tease" came out of the filler holding ONE photo, which is not a
# set at a set's price — it is a single image with a bundle's caption.
MIN_BY_KIND = {"image": 1, "image_set": 3, "video": 1}

# How many stills one row may borrow from a single DEARER row as padding. A
# taste of a dear set makes a cheap bundle look worth buying; a copy of it means
# the fan never needs the dear one.
_MAX_BORROW_PER_ROW = 2

# OF's own bundle ceiling, mirrored from `ppv_library_config_api._MAX_MEDIA`.
_MAX_WANT = 50

# A caption that names a COUNT is making a promise about it. "20+ pics of me"
# shipped with the 8 that `WANT_BY_KIND` allows is the same defect as a toy
# caption over a clip with no toy — the fan is told a number and gets a third of
# it. Any number sitting next to a media word raises the row's size.
_COUNT_RE = re.compile(
    r"(\d{1,2})\s*\+?\s*(?:of me\b|pics?\b|pictures?\b|images?\b|photos?\b|"
    r"clips?\b|videos?\b)", re.I)


def want_from_text(label: str, description: str, kind: str) -> int:
    """How many media the row should hold.

    Order of authority:
      1. The SHIPPED rung's own `want`, when the row is one of the defaults.
         Those sizes are deliberate — a "quick peek" is 6 stills, "the full
         thing" is 3 clips — and they are what the operator reviewed in the
         report. `WANT_BY_KIND` alone collapsed every video row to 1, so
         pressing Fill content produced a visibly smaller bundle than the plan
         they had already approved.
      2. A count the caption itself promises ("20+ pics") — an operator-written
         row that names a number has to honour it.
      3. The kind's default.
    """
    r = rung_for(label, description)
    if r is not None:
        return min(r.want, _MAX_WANT)
    base = WANT_BY_KIND.get(kind, 1)
    found = [int(m) for m in _COUNT_RE.findall(f"{label} {description}")]
    return min(max([base, *found]), _MAX_WANT)


def min_from_text(label: str, description: str, kind: str) -> int:
    """How few media make the row a LIE rather than merely a small one.

    Same authority order as `want_from_text`. The count case is the one that
    bites: "a big mixed set — 10+ of me, all different 😇" shipped with 8, which
    is the toy-caption-over-a-toyless-clip defect wearing a number. A caption
    that names a count has to clear it or stay empty — an item with no media is
    never offered, so empty is safe and short is a refund.
    """
    r = rung_for(label, description)
    if r is not None:
        return max(1, r.min_media)
    base = MIN_BY_KIND.get(kind, 1)
    found = [int(m) for m in _COUNT_RE.findall(f"{label} {description}")]
    return min(max([base, *found]), _MAX_WANT)


def _kw(*words: str) -> re.Pattern[str]:
    return re.compile(r"\b(" + "|".join(words) + r")\b", re.I)


# Text → constraint. Order matters only for `want`; every rule that fires
# contributes its constraints. These are the phrasings that actually appear in
# the shipped defaults and in operator-written rows on production ("bra rate",
# "boob bounce naked", "ass shaking strip dance", "Pussy Backshot").
_NUDE = _kw("nude", "naked", "nothing left on", "all of me", "bare")
_LINGERIE = _kw("lingerie", "bra", "panties", "bodysuit", "underwear", "thong", "outfit")
_TOY = _kw("toy", "toys", "dildo", "vibrator", "wand")
_PARTNER = _kw("collab", "partner", "him", "he", "b/g", "bg", "boy", "guy", "couple")
_SHOWER = _kw("shower", "bath", "wet")
_STRIP = _kw("strip", "strips", "stripping", "undress", "undressing", "take off", "come off")
_TALK = _kw("joi", "dirty talk", "talk", "talking", "tell you", "instructions")
_FINISH = _kw("finish", "finishes", "cum", "cumming", "orgasm", "squirt", "the full thing")
_FEET = _kw("feet", "foot", "toes", "soles")
_ASS = _kw("ass", "booty", "twerk", "backshot", "cheeks")
_BOOBS = _kw("boob", "boobs", "tits", "breast", "breasts", "nipples")
_TEASE = _kw("tease", "teasing", "dance", "dancing", "flash", "peek")
_TAME = _kw("selfie", "rate", "cute", "no makeup", "sfw", "casual", "bikini", "swimsuit")

# A caption that NAMES an act is making a checkable promise, and unlike the soft
# `acts_any` hints above it does not get to degrade into a ranking preference.
# Measured on AriaFree: "black lace deep fingering" ($100) went out over a clip
# whose only acts are `groping_own_breasts` and `posing` — there is no fingering
# in it. `acts` under-reports, so a required act can cost a row its content; that
# is the correct trade, because the alternative is charging $100 for a promise
# the vision layer cannot see in the media.
_NAMED_ACTS: tuple[tuple[re.Pattern[str], set[str], bool], ...] = (
    (_kw("fingering", "fingers", "fingered", "fingerfuck"), {"fingering"}, True),
    (_kw("rubbing", "rubs", "clit"), {"rubbing_clit", "fingering"}, True),
    (_kw("squirt", "squirts", "squirting"), {"squirt"}, True),
    (_kw("blowjob", "sucking", "bj"), {"blowjob"}, True),
    (_kw("spreading", "spread"), {"spreading"}, False),
)


class Spec:
    """The constraints one catalog row's text implies."""

    def __init__(self) -> None:
        self.tiers: set[str] = set()
        self.acts_any: set[str] = set()
        self.acts_required: set[str] = set()
        self.acts_none: set[str] = set()
        self.clothing_any: set[str] = set()
        self.clothing_none: set[str] = set()
        self.payoff: bool | None = None
        self.needs_toy = False
        self.partner: bool | None = None
        self.bare_only = False
        self.media_kind: str | None = None
        self.price_cents = 0
        self.want = 1                     # bundle size — volume credit on the ceiling
        self.rung: Rung | None = None     # the shipped rung this row IS, if any
        self.matched: list[str] = []      # which keywords fired — shown to the operator

    def _has_hard_signal(self) -> bool:
        """Whether anything RELIABLE constrains this row.

        `payoff` is excluded on purpose: `stands_alone` is computed FROM `acts`,
        so it inherits exactly the unreliability this test exists to route
        around.
        """
        return bool(self.clothing_any or self.needs_toy or self.tiers
                    or self.partner is True)

    def eligible(self, row: dict[str, Any], *, ignore_price: bool = False) -> bool:
        """`acts` gates only when it is the ONLY signal the text gave us.

        `acts` is the least trustworthy field in the describe output — the menu
        echo, the alias drift, and an empty list on the vault's one dildo clip
        are all measured. So when a row already carries a reliable constraint
        (clothing, toy, partner, tier), acts drops to a RANKING signal. AND-ing
        them starved two real rungs: "wore this thinking of you… wanna see it
        come off?" demanded a strip act on top of lingerie and matched nothing,
        and the toy rung rejected the only dildo clip because its `acts` is [].
        """
        f, s, text = row["fields"], row["sell"], row["text"]
        # The PRICE is a promise too, and it is the one an operator reads first.
        # The band gates BOTH ways: a 26-second lingerie tease cannot go out as
        # "one of one, nobody else ever sees this" at $200, and a payoff still
        # cannot hide inside a $25 tease set either. Content outside the band is
        # not a cheaper or dearer version of the row — it is the wrong content
        # for it, and repricing the row around it would quietly rewrite what the
        # caption promised.
        if not ignore_price and not in_band(row, self.price_cents, self.want):
            return False
        if self.acts_required and not (self.acts_required & row["acts"]):
            return False
        if self.acts_none & row["acts"]:
            return False
        if self.media_kind and row["kind"] != self.media_kind:
            return False
        if self.tiers and s["tier"] not in self.tiers:
            return False
        # ASYMMETRIC on purpose. `payoff=False` ("keep closers out of the $8
        # rung") stays HARD — it is a safety guard, and acts OVER-reporting is
        # already handled by the menu-echo filter. `payoff=True` ("this row
        # wants a closer") relaxes when a reliable signal is present, because
        # acts UNDER-report: the vault's one dildo clip has `acts: []`, so a
        # hard payoff test rejected it from the toy rung it was written for.
        if self.payoff is False and s["stands_alone"]:
            return False
        if (self.payoff is True and not s["stands_alone"]
                and not self._has_hard_signal()):
            return False
        if self.needs_toy and not (
                f.get("toys")
                or ({"toy_insertion", "toy_on_clit", "riding_toy"} & row["acts"])
                or _TOY_RE.search(text)):
            return False
        # Rule 3 — her own words say she is still wearing something.
        if self.bare_only and _GARMENT_RE.search(text):
            return False
        if self.partner is True and f.get("partner_visible") is not True and not (
                {"sex_missionary", "sex_doggy", "sex_riding", "blowjob", "handjob",
                 "cumshot"} & row["acts"]):
            return False
        if self.partner is False and f.get("partner_visible") is True:
            return False
        if (self.acts_any and not self._has_hard_signal()
                and not (self.acts_any & row["acts"])):
            return False
        if self.clothing_any and str(f.get("clothing_state") or "").lower() \
                not in self.clothing_any:
            return False
        if str(f.get("clothing_state") or "").lower() in self.clothing_none:
            return False
        return True

    def affinity(self, row: dict[str, Any]) -> tuple:
        named = len(self.acts_any & row["acts"]) if self.acts_any else 0
        return (named, row["duration"] or 0, -row["media_id"])


def spec_from_text(label: str, description: str, kind: str,
                   price_cents: int = 0) -> Spec:
    """Derive the match spec from what the row already says it is — and from
    what it charges."""
    text = f"{label or ''} {description or ''}"
    sp = Spec()
    sp.price_cents = max(0, int(price_cents or 0))
    sp.want = want_from_text(label, description, kind)
    sp.rung = rung_for(label, description)
    if sp.rung is not None:
        # The rung REPLACES the keyword pass, it does not layer on top of it.
        # Both read the same words, and where they disagree the keyword table is
        # simply wrong: "Body rate video" ($90, explicit-only by spec) tripped
        # the `_TAME` rule on the word "rate", which intersected the rung's
        # explicit tier with sfw/suggestive down to nothing and left the rung
        # unfillable against a vault that has the content.
        #
        # Its constraints are COPIED into this spec rather than delegated to
        # `Rung.eligible`, so there is still exactly one place a row's gates can
        # be read off — and so the two deliberate softenings this class carries
        # keep applying. The `payoff=True` relaxation is the one that matters:
        # the vault's only dildo clip has `acts: []`, and a hard payoff test
        # rejects it from the toy rung it was written for.
        r = sp.rung
        sp.matched.append(f"rung:{r.label}")
        sp.tiers = set(r.tiers)
        sp.acts_required = set(r.acts_any)   # a rung's acts gate hard, as shipped
        sp.acts_none = set(r.acts_none)
        sp.clothing_any = set(r.clothing_any)
        sp.clothing_none = set(r.clothing_none)
        sp.payoff, sp.media_kind = r.payoff, r.media_kind
        sp.partner, sp.needs_toy = r.partner, r.needs_toy
        sp.bare_only = r.bare_only
        return sp

    def hit(name: str, rx: re.Pattern[str]) -> bool:
        if rx.search(text):
            sp.matched.append(name)
            return True
        return False

    for rx, acts, is_payoff in _NAMED_ACTS:
        if rx.search(text):
            sp.acts_required |= acts
            sp.matched.append(f"act:{sorted(acts)[0]}")
            if is_payoff:
                sp.payoff = True

    # Most specific first; a row can carry several.
    if hit("toy", _TOY):
        sp.needs_toy = True
    if hit("partner", _PARTNER):
        sp.partner = True
    else:
        sp.partner = False       # "solo" is the default for this lane
    if hit("finish", _FINISH):
        sp.acts_any |= {"masturbation_orgasm", "squirt", "cumshot", "fingering",
                        "rubbing_clit"}
        sp.payoff = True
    if hit("shower", _SHOWER):
        sp.acts_any |= {"shower", "bath"}
    if hit("strip", _STRIP):
        sp.acts_any |= {"strip", "undress", "tease"}
    if hit("talk", _TALK):
        sp.acts_any |= {"talking_to_camera"}
    if hit("feet", _FEET):
        sp.acts_any |= {"posing"}
    if hit("ass", _ASS):
        sp.acts_any |= {"ass_shaking", "twerk", "spreading", "posing"}
    if hit("boobs", _BOOBS):
        sp.acts_any |= {"groping_own_breasts", "posing"}
    if hit("tease", _TEASE):
        sp.acts_any |= {"tease", "posing", "ass_shaking", "twerk"}

    # Clothing is a separate axis and the two nude/lingerie words conflict, so
    # nude wins when both appear ("boob bounce naked").
    if hit("nude", _NUDE):
        sp.clothing_any = {"fully_nude"}
        sp.bare_only = True
    elif hit("lingerie", _LINGERIE):
        sp.clothing_any = {"lingerie_on", "pulled_aside", "pulled_down",
                           "partially_off"}

    if hit("tame", _TAME) and sp.payoff is None:
        sp.tiers = {"sfw", "suggestive"}
        sp.payoff = False

    if kind == "video":
        sp.media_kind = "video"
    elif kind in ("image", "image_set"):
        sp.media_kind = "photo"
    return sp


def _suggest_price(price_cents: int, blocked: list[dict[str, Any]],
                   count: int = 1) -> int:
    """The nearest price that would let this row's own content in.

    A row emptied by the price band is the one failure the operator can fix with
    a number instead of a camera, so saying "it doesn't match" and stopping
    there wastes the most actionable finding in the report. Suggests the band of
    the STRONGEST blocked candidate, clamped toward the price already set, so a
    $90 rung over teases is told $50 rather than $8.
    """
    if not price_cents or not blocked:
        return 0
    floor, ceiling = item_band(max(blocked, key=lambda r: item_band(r)[0]))
    if ceiling is not None:
        ceiling = int(ceiling * vault_catalog_seed.volume_factor(count))
    sug = max(floor, price_cents if ceiling is None else min(price_cents, ceiling))
    return sug if sug != price_cents else 0


async def _load_vault(account_id: str) -> list[dict[str, Any]]:
    async with get_session() as s:
        recs = (await s.execute(
            select(VaultItem.media_id, VaultItem.kind, VaultItem.duration_seconds,
                   VaultItem.ai_fields_json)
            .where(VaultItem.account_id == account_id,
                   VaultItem.removed_at.is_(None))
            .order_by(VaultItem.created_at.asc())
        )).all()
    rows: list[dict[str, Any]] = []
    for mid, kind, dur, fj in recs:
        fields = vault_ai_brief.load_fields(fj)
        if not fields:
            continue
        acts = sane_acts(fields)
        fields = {**fields, "acts": sorted(acts)}
        rows.append({
            "media_id": int(mid), "kind": kind, "duration": dur,
            "fields": fields, "acts": acts,
            "text": str(fields.get("description") or ""),
            "sell": vault_ai_brief.sellability(fields, duration_seconds=dur),
        })
    return rows


async def suggest_singles(account_id: str, *, only_empty: bool = True,
                          drafts: list[dict[str, Any]] | None = None,
                          allow_reuse: bool = False) -> dict[str, Any]:
    """Propose media for this account's catalog singles. Read-only.

    `only_empty` (the default, and the point) restricts the fill to rows that
    have text but NO content — an operator's hand-picked media is never
    second-guessed.

    `drafts` are UNSAVED rows sitting in the editor — the ones "Suggest text
    sets" just added, which have no row id yet. Without them "Fill content" was
    a no-op on exactly the rows it was meant to serve: the filler reads targets
    from the DB, the new rows are not in the DB, and the UI matches proposals by
    row id. Each draft needs `label` / `description_for_ai` / `kind`; the spec is
    derived from its own text, same as a saved row. Drafts own no media yet, so
    they reserve nothing and are simply extra targets competing for the pool.
    """
    async with get_session() as s:
        items = (await s.execute(
            select(CatalogItem)
            .where(CatalogItem.account_id == account_id)
            .order_by(CatalogItem.price_cents.asc(), CatalogItem.id.asc())
        )).scalars().all()

    import json as _json

    def _ids(it: CatalogItem) -> list[int]:
        try:
            return [int(x) for x in _json.loads(it.media_ids or "[]")]
        except (TypeError, ValueError):
            return []

    singles = [it for it in items if it.script_id is None]

    class _T:
        """A fill target — a saved row or an unsaved draft, so both walk the
        same allocation instead of a second code path."""
        __slots__ = ("key", "id", "label", "description_for_ai", "kind", "media",
                     "price")

        def __init__(self, key, id, label, desc, kind, media, price):
            self.key, self.id = key, id
            self.label, self.description_for_ai = label, desc
            self.kind, self.media, self.price = kind, media, int(price or 0)

    targets = [_T(f"db:{it.id}", it.id, it.label, it.description_for_ai,
                  it.kind, _ids(it), it.price_cents)
               for it in singles if not only_empty or not _ids(it)]
    for i, d in enumerate(drafts or []):
        # A draft carries its price from the suggestion that proposed it. It is
        # not cosmetic: the price gate is the only thing standing between a $200
        # caption and a lingerie tease, and a draft posted without one arrives
        # at price 0 — ungated on exactly the rows the button just invented.
        targets.append(_T(f"draft:{i}", None, str(d.get("label") or ""),
                          str(d.get("description_for_ai") or ""),
                          str(d.get("kind") or "image"), [],
                          d.get("price_cents") or 0))
    target_ids = {t.id for t in targets if t.id is not None}

    # Rule 2 — everything ANOTHER catalog row already sells is off the table.
    # A target's own current media is NOT reserved: when re-proposing over a
    # filled row, reserving its own ids would starve the row that owns them.
    #
    # `allow_reuse` turns that off. It exists because exclusivity has a real
    # cost once the catalog outgrows the vault: on AriaFree, 18 rows over 101
    # items meant a row could match 69 photos and still fill with none, because
    # every one was spoken for. The trade-off it reinstates is the production
    # defect it was written against — a fan climbing the ladder can be re-sold
    # media he already unlocked lower down — so it is the operator's call, per
    # fill, not a default.
    #
    # Rows filled in the SAME pass still spread out either way: without that a
    # single press would hand every row the identical top-N.
    taken: set[int] = set()
    if not allow_reuse:
        for it in items:
            if it.id not in target_ids:
                taken.update(_ids(it))
    # (drafts own no media, so they reserve nothing)

    vault = await _load_vault(account_id)
    specs = {t.key: spec_from_text(t.label or "", t.description_for_ai or "",
                                   t.kind, t.price) for t in targets}
    # `matches` is what the vault HOLDS for this text; `pools` is what is still
    # for sale. Keeping them apart is what lets an empty row say WHICH problem
    # it has — "you don't own anything like this" and "your other rows already
    # sell all of it" call for opposite actions (shoot content vs. rebalance
    # rows), and reporting the first when the second is true sends the operator
    # off to film something they already have.
    matches = {t.key: [r for r in vault if specs[t.key].eligible(r)]
               for t in targets}
    pools = {t.key: [r for r in matches[t.key] if r["media_id"] not in taken]
             for t in targets}
    # Media the CAPTION fits but the PRICE does not. A third reason for an empty
    # row, and the only one whose fix is a number rather than a camera: "you own
    # 4 clips for this line, but they're teases and you're asking $90".
    wrong_price = {t.key: [r for r in vault
                           if specs[t.key].eligible(r, ignore_price=True)
                           and not specs[t.key].eligible(r)
                           and r["media_id"] not in taken]
                   for t in targets}

    # Scarcest row first: a row with one candidate must pick before a row with
    # sixty, or the broad rows strip the shelf.
    order = sorted(targets, key=lambda t: (len(pools[t.key]), t.key))

    by_id = {r["media_id"]: r for r in vault}
    out: dict[str, dict[str, Any]] = {}

    def _cap_dearer(sellers: list[tuple[int, list[int]]], price: int) -> set[int]:
        """Media a row at `price` may NOT duplicate, because a dearer row sells
        it and would be undercut by its own content. Half a row at most, and
        never more than two — the half matters at the small end, where a dear
        row holding one still would otherwise be handed over whole."""
        blocked: set[int] = set()
        for px, ids in sellers:
            if px <= price or not ids:
                continue
            allow = min(_MAX_BORROW_PER_ROW, len(ids) // 2)
            spare = sorted(ids, key=lambda m: vault_catalog_seed._tameness(by_id[m])
                           if m in by_id else (9,))
            blocked.update(spare[allow:])
        return blocked

    saved_sellers = [(int(it.price_cents or 0), _ids(it))
                     for it in items if it.id not in target_ids]

    def dup_blocked(price: int) -> set[int]:
        return _cap_dearer(
            saved_sellers + [(t.price, out[t.key]["locked_media_ids"])
                             for t in targets if t.key in out], price)

    for t in order:
        sp = specs[t.key]
        label, desc = t.label or "", t.description_for_ai or ""
        need = min_from_text(label, desc, t.kind)
        avail = [r for r in pools[t.key] if r["media_id"] not in taken]
        avail.sort(key=sp.affinity, reverse=True)
        want = want_from_text(label, desc, t.kind)
        picked = avail[:want]
        # Too few UNSOLD matches to reach the minimum — top up with material
        # other rows already sell rather than leaving the row empty. "20+ pics
        # of me — my whole set, everything i've got" over a vault of 70 matching
        # photos read as "you don't have enough", which was false: what it did
        # not have was 18 photos nobody else was selling, and that caption
        # promises volume, not exclusivity.
        #
        # This only ever reaches the MINIMUM — everything above it still comes
        # from unsold material, so exclusivity governs the bulk of every row.
        # Whether to go further is the operator's, through the ♻️ allow reuse
        # checkbox, which is why this does not try to judge from the caption
        # which rows "deserve" duplication.
        dup: list[dict[str, Any]] = []
        if len(picked) < need:
            have = {r["media_id"] for r in picked}
            for r in matches[t.key]:
                if len(picked) + len(dup) >= need:
                    break
                if r["media_id"] not in have and r["media_id"] not in dup_blocked(t.price):
                    dup.append(r)
            picked = picked + dup
        short = len(picked) < need
        if short:
            picked = []            # rule 1 — empty beats a caption that undercounts
        taken.update(r["media_id"] for r in picked)
        blocked = wrong_price[t.key]
        sug = _suggest_price(t.price, blocked, want_from_text(label, desc, t.kind))
        out[t.key] = {
            "id": t.id, "key": t.key, "label": t.label, "kind": t.kind,
            "description_for_ai": t.description_for_ai,
            "price_cents": t.price,
            "suggested_price_cents": sug if (sug and not picked) else 0,
            "needs": need,
            "matched": sp.matched,
            "candidates": len(matches[t.key]),      # what EXISTS, not what's free
            "available": len(pools[t.key]),         # what was still unsold
            "wrong_price": len(blocked),
            "runtime_seconds": vault_catalog_seed.total_seconds(picked),
            "runtime_wanted": vault_catalog_seed.min_video_seconds(t.price),
            "images_wanted": vault_catalog_seed.min_images(t.price, t.kind),
            "padding_media_ids": [], "shape_note": "",
            "dup_media_ids": [r["media_id"] for r in dup],
            "media_ids": [r["media_id"] for r in picked],
            # What the row SELLS, before any teaser is attached to it. Kept
            # apart from `media_ids` because a borrowed still lands in both.
            "locked_media_ids": [r["media_id"] for r in picked],
            "preview_media_ids": [],
            "items": picked,
            "previews": [], "preview_shared": False,
            "empty_reason": (
                "" if picked else
                # Ordered by which fix the operator should reach for. "Short" is
                # only the honest answer when something WAS free: with nothing
                # free at all the shortfall is a symptom, and the cause is
                # either exclusivity or the price band below.
                (f"only {len(avail)} free match(es), and this caption promises "
                 f"{need} — shipping fewer is a false count, so it stays empty")
                if short and avail else
                (f"{len(blocked)} match(es) fit the words but not the price — "
                 f"${t.price / 100:.0f} is outside what that content can carry"
                 + (f"; ${sug / 100:.0f} would fill it" if sug else ""))
                if blocked else
                "nothing in the vault matches this text" if not matches[t.key] else
                (f"all {len(matches[t.key])} match(es) are already sold by another "
                 "row — free one up or widen this caption")),
        }

    # Previews run only once every row has taken what it SELLS: a still can
    # become a free teaser only after no row wanted to charge for it. Borrowed
    # stills are appended to `media_ids` because OF unlocks a slice of the
    # attachment rather than a separate file — `scripts_api` enforces
    # previews ⊆ media_ids — which is also what makes the teaser double as the
    # after-purchase extra.
    # Runtime top-up, as its OWN pass and DEAREST ROW FIRST. A bar is met by
    # taking MORE clips — "$100 wants five minutes" is a statement about the
    # bundle, and the rungs' `want` values were written before there was one.
    # Doing it inline while each row picked let a $100 row chasing five minutes
    # take four clips before the $200 closer had picked at all, and the top rung
    # came out holding one: the runtime rule was quietly outranking scarcity.
    # Every row now has its core content before any row takes a second helping,
    # and the dearest ask gets the leftovers, because that is where runtime is
    # worth the most.
    for t in sorted(targets, key=lambda t: -t.price):
        entry = out[t.key]
        bar = entry["runtime_wanted"]
        if not bar or not any(r["kind"] == "video" for r in entry["items"]):
            continue
        for r in pools[t.key]:
            if vault_catalog_seed.total_seconds(entry["items"]) >= bar:
                break
            if (r["kind"] == "video" and r["media_id"] not in taken
                    and len(entry["items"]) < _MAX_WANT):
                entry["items"].append(r)
                entry["media_ids"].append(r["media_id"])
                entry["locked_media_ids"].append(r["media_id"])
                taken.add(r["media_id"])
        entry["runtime_seconds"] = vault_catalog_seed.total_seconds(entry["items"])

    # What every piece of media is already being SOLD for, across the saved
    # catalog and this pass. A shared teaser may only ever be something sold
    # CHEAPER elsewhere: giving away a $8 still to move a $50 set costs $8, but
    # unlocking a $50 still to move a $24 set gives away the dearer product.
    owner_price: dict[int, int] = {}
    owners: list[tuple[int, list[int]]] = []      # (price, media) per selling row
    for it in items:
        if it.id not in target_ids:
            ids = _ids(it)
            owners.append((int(it.price_cents or 0), ids))
            for m in ids:
                owner_price[m] = max(owner_price.get(m, 0), int(it.price_cents or 0))
    for t in targets:
        ids = out[t.key]["locked_media_ids"]
        owners.append((t.price, ids))
        for m in ids:
            owner_price[m] = max(owner_price.get(m, 0), t.price)

    def dearer_owners(price: int) -> list[list[int]]:
        return [ids for px, ids in owners if px > price and ids]

    # Bulk each bundle out to its per-$20 still count BEFORE previews, so the
    # teaser is chosen against the finished set. This is the one place
    # exclusivity is deliberately off — padding is not what the caption sells,
    # so a dup costs the ladder nothing, and on a vault where every good still
    # is spoken for the alternative is a $200 row holding one clip.
    #
    # Borrowing UPWARD is capped rather than banned. Banning it outright starved
    # the bottom of the ladder — at $8 every other row is dearer, so the peek
    # rung came out with two stills again — but letting a $8 row copy a $25 set
    # wholesale means a fan buys the cheap rung and has most of the dear one.
    # A couple of stills reads as a taste; the whole set replaces it.
    for t in targets:
        entry = out[t.key]
        if not entry["items"]:
            continue
        gap = entry["images_wanted"] - vault_catalog_seed.count_images(entry["items"])
        if gap <= 0:
            continue
        capped = _cap_dearer(owners, t.price)
        pad = vault_catalog_seed.pick_padding(
            entry["items"], vault, gap, t.price,
            exclude=set(entry["media_ids"]) | capped)
        entry["items"] = entry["items"] + pad
        entry["padding_media_ids"] = [r["media_id"] for r in pad]
        entry["media_ids"] += entry["padding_media_ids"]

    # Each borrowed still is used ONCE across the pass, so twenty locked rows do
    # not all tease with the same photo.
    used_previews: set[int] = set()
    for t in targets:
        entry = out[t.key]
        # Keyed on what the row ENDED UP holding, not on its declared kind: a
        # "quick peek" padded out to ten stills is a set, and a set with no free
        # frame is a price tag with nothing attached. Only a genuine single
        # previews nothing — there, unlocking the one image IS the sale.
        want = 0 if len(entry["items"]) <= 1 else preview_want_for("image_set")
        if not entry["items"] or not want:
            continue
        prevs = vault_catalog_seed.pick_previews(
            t.kind, want, entry["items"], vault, taken | used_previews)
        # Nothing unsold left that is safe to give away. Measured here: 43 of
        # AriaFree's 102 items clear `preview_ok`, and after 21 rows have taken
        # what they sell, FOUR are still free — because the $25 tease sets sell
        # exactly the dressed stills a teaser is made of. Leaving the locked
        # rows with no free frame is the worse trade: a preview is the shop
        # window, not inventory. It is capped at suggestive and it is the
        # cheapest material on the shelf, so re-showing it costs a rung nothing
        # — unlike the defect rule 2 exists for, which is re-SELLING a fan
        # content he already unlocked further down the ladder.
        borrowed_sold = False
        if not prevs:
            # Same capped rule as padding, not a blanket ban on dearer material:
            # at $8 every other row is dearer, so a hard exclusion left the
            # bottom rung with no free frame at all.
            prevs = vault_catalog_seed.pick_previews(
                t.kind, want, entry["items"], vault,
                used_previews | _cap_dearer(owners, t.price)
                | set(entry["media_ids"]))
            borrowed_sold = bool(prevs)
        taken.update(r["media_id"] for r in prevs)
        used_previews.update(r["media_id"] for r in prevs)
        entry["previews"] = prevs
        entry["preview_media_ids"] = [r["media_id"] for r in prevs]
        entry["preview_shared"] = borrowed_sold
        entry["media_ids"] += [r["media_id"] for r in prevs
                               if r["media_id"] not in entry["media_ids"]]

    # Shape shortfalls are a WARNING, never an emptying. An undersized bundle is
    # a weak offer, not a false one — the caption promises nothing about runtime
    # — so the operator prices that call. Where it cannot be met at all, the
    # honest move is usually a cheaper rung, not a longer shoot.
    for t in targets:
        entry = out[t.key]
        if not entry["items"]:
            continue
        notes = []
        secs, bar = entry["runtime_seconds"], entry["runtime_wanted"]
        if bar and secs and secs < bar:
            notes.append(f"{secs}s of video — ${t.price / 100:.0f} wants "
                         f"{bar // 60} min")
        imgs, want_imgs = (vault_catalog_seed.count_images(entry["items"]),
                           entry["images_wanted"])
        if want_imgs and imgs < want_imgs:
            notes.append(f"{imgs} still(s) — one per $20 would be {want_imgs}")
        entry["shape_note"] = "; ".join(notes)

    proposals = [out[t.key] for t in targets]   # back into display order
    filled = sum(1 for p in proposals if p["media_ids"])
    return {
        "account_id": account_id,
        "singles": len(singles),
        "targets": len(targets),
        "drafts": len(drafts or []),
        "allow_reuse": bool(allow_reuse),
        "proposals": proposals,
        "summary": {
            "targets": len(targets), "filled": filled,
            "still_empty": len(targets) - filled,
            "media_bound": sum(len(p["media_ids"]) for p in proposals),
            "previews": sum(len(p["preview_media_ids"]) for p in proposals),
            "padding": sum(len(p["padding_media_ids"]) for p in proposals),
            "undersized": sum(1 for p in proposals if p["shape_note"]),
            "needs_reprice": sum(1 for p in proposals if p["suggested_price_cents"]),
            "vault_described": len(vault),
        },
    }
