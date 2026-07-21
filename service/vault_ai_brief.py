"""service/vault_ai_brief.py — turn the describe taxonomy into a SELLING brief.

The V2 describe pass stores ~21 structured fields per item (`ai_fields_json`):
`beats`, `acts`, `clothing_state`, `penetration`, `position`, `setting`,
`camera`, `toys`, `people_count`, `partner_visible`, `face_visible`,
`explicitness`, `primary_folder`, `sellable`, `confidence`, …

Until now the copy writer got only the prose sentence, so every one of those
facts was thrown away at exactly the moment it was worth money. This module is
the bridge: `item_facts()` renders the taxonomy as a compact fact block, and
`SELLING_BRIEF` is the instruction text that tells a copy/seller LLM what each
field is FOR — how to find the sellable moment, when a piece stands alone, and
how to run the beats in reverse when the payoff is the better hook.

Consumed by `automations/describe_media._copy_one` (caption + script), and
exported for the 1:1 seller / upseller path via `vault_ai_to_chatter`.

Everything here is derived from fields the describe pass actually writes — no
invented schema. `sellability()` is deliberately conservative: it reports what
the data supports and says `unknown` rather than guessing, because a confident
wrong call about what's in a clip is how a fan gets sold something that isn't
there and asks for a refund.
"""
from __future__ import annotations

import json
import re
from typing import Any

# Ordered low → high. This IS the escalation ladder: a night's sends walk it
# upward, a re-sell to someone who already owns the top walks it back down.
EXPLICITNESS_LADDER = ("sfw", "suggestive", "explicit", "hardcore")

# Clothing states ranked by how much REVEAL is still unspent. The "almost"
# states outsell full nudity on a first ask because there is still something
# left to promise.
_REVEAL_TENSION = {
    "dressed": "everything still to come",
    "lingerie_on": "everything still to come",
    "pulled_aside": "the almost-moment — covered, but not covering anything",
    "pulled_down": "mid-strip, caught halfway",
    "partially_off": "mid-strip, caught halfway",
    "fully_nude": "nothing left to reveal — sell the ACT, not the strip",
}

# Acts that constitute a payoff rather than a build-up. An item with one of
# these can close on its own; an item without one is a rung, not a finish.
_PAYOFF_ACTS = {
    "fingering", "toy_insertion", "toy_on_clit", "riding_toy", "rubbing_clit",
    "masturbation_orgasm", "squirt", "blowjob", "handjob", "cumshot",
    "sex_missionary", "sex_doggy", "sex_riding",
}


# ── The flags pass contract ─────────────────────────────────────────
#
# Canonical HERE because three modules depend on it and must not drift:
# `vault_ai_api` writes it, `vault_scripts` gates lanes on it, and
# `vault_catalog_seed` picks media by it.
#
# WHY AN ENUM AND NOT BOOLEANS. The v1 shape asked `pussy_covered` /
# `breasts_covered`, and measurement killed it:
#
#   * The two asked OPPOSITE questions. `pussy_covered` was about FABRIC ("out
#     of frame → false"); `breasts_covered` was about VISIBILITY ("not visible
#     → true"). So `false` meant "no fabric on it" in one and "actually on
#     show" in the other, and no caller could treat them uniformly.
#   * Because "not in frame" and "not covered" both collapsed to `false`, an
#     item could qualify for the mass-safe lane by having NO EVIDENCE rather
#     than by being safe. That is vacuous truth, and it put an anal-penetration
#     clip and a dildo clip in the folder whose entire job is to be safe. It
#     was patched twice; both patches were crutches on `body_focus`, a field
#     that is frequently absent and was never an exposure signal.
#   * `breasts_covered` came back `false` on 94 of 103 AriaFree items,
#     including 14 tagged `dressed` or `sfw`. A flag that is ~91% constant and
#     contradicts the describe pass on clothed stills carries no information.
#     The double-negative phrasing ("are her bare breasts KEPT FROM VIEW")
#     fought the model's natural polarity.
#
# The enum fixes the class of bug rather than the instances: `not_in_frame` is
# a STATED answer, so absence of evidence stops being expressible as safety,
# and every region answers the same question in the same direction.
#
# WHY THE TOP STATE IS "bare" AND NOT "visible". It was `visible` for one
# measured round and the model answered it 74 times out of 81 stills, including
# on a white tank top and a black halter top — it read the LABEL ("is her chest
# visible in this photo") and skipped the definition underneath it ("and bare").
# The old boolean failed the mirror-image way for the same reason: `covered` as
# a label pulled toward "not covered" on anything racy. A one-word label is what
# the model actually classifies against, so the label has to be the question.
# `bare` cannot be satisfied by cleavage.
VIS_STATES: tuple[str, ...] = ("not_in_frame", "covered", "bare")

# Ordered by how much is on show. Folding several frames of a clip takes the
# MAX — one exposed frame exposes the clip.
_VIS_RANK = {s: i for i, s in enumerate(VIS_STATES)}

# The regions asked about. `anus` is separate from the rest of her rear
# deliberately: a bare backside is fine in a mass send (that is the whole point
# of the "turned away" lane) while an exposed anus is not, and the clip that
# proved it listed `body_focus: ['ass','anus','fingers']`.
VIS_REGIONS: tuple[str, ...] = ("vulva_vis", "breasts_vis", "anus_vis")

# `underwear_visible` survives the rewrite untouched. It already asks a
# positive, single-direction question ("is ANY garment visible"), it measured a
# healthy 58/24 split rather than pinning to one answer, and it is what
# rescued the $50 tier from the `fully_nude` mislabels.
FLAG_KEYS: tuple[str, ...] = ("underwear_visible", *VIS_REGIONS)

# What the model NAMED over each region ("white tank top", "her right hand"),
# absent when there was nothing to name. The flags pass asks for these before
# it asks for a state, because naming a garment is a far easier question than
# judging exposure — and it is answered correctly on photos where the state
# question is not. It then overrules the state: name something, and the region
# is covered. Optional by nature, so NOT part of `flags_known`.
OVER_KEYS: dict[str, str] = {"breasts_vis": "over_breasts",
                             "vulva_vis": "over_vulva",
                             "anus_vis": "over_anus"}


# Garments that cannot cover a given region. The model sometimes fills every
# `over_` slot with the one garment it can see — "pink thong" over her breasts
# on a still whose own description reads "fully nude except for pink underwear",
# i.e. topless. A bodysuit legitimately covers both; a thong does not, and that
# copy put a topless still in the mass-safe folder. Checked per region rather
# than by trying to detect duplication, because duplication is correct for
# one-piece garments.
_IMPOSSIBLE_OVER: dict[str, frozenset[str]] = {
    "breasts_vis": frozenset({"thong", "panties", "panty", "knickers", "briefs",
                              "g-string", "gstring", "shorts", "skirt",
                              "stockings", "socks", "boots", "heels"}),
    "vulva_vis": frozenset({"bra", "bralette", "top", "tank", "t-shirt",
                            "tshirt", "blouse", "shirt", "necklace", "choker"}),
}


def garment_over(fields: dict[str, Any] | None, region: str) -> str:
    """What is over this region, in the model's words, or "".

    Returns "" when the named garment could not physically be there, so the
    caller falls back to "covered by something unnamed" — which every lane
    already treats as not knowing. A wrong name is worse than no name: it is
    what marks an exposed region decently covered.
    """
    if not isinstance(fields, dict):
        return ""
    v = fields.get(OVER_KEYS.get(region, ""))
    if not isinstance(v, str):
        return ""
    name = v.strip()
    impossible = _IMPOSSIBLE_OVER.get(region, frozenset())
    words = {w for w in re.split(r"[\s,./-]+", name.lower()) if w}
    return "" if words & impossible else name


# Things that cover without being clothing. She is still NUDE behind her own
# hand, and a hand-bra nude is a nude at full price — but she is not nude behind
# a thong. `covered` treats the two alike, because for "what is on show" they
# are alike; nudity must not, because for "what is she wearing" they are
# opposites. This is the whole reason the named string is kept rather than
# collapsed into a boolean at write time.
_NOT_CLOTHING = frozenset({
    "hand", "hands", "finger", "fingers", "palm", "fist", "arm", "arms",
    "forearm", "elbow", "wrist", "thigh", "thighs", "leg", "legs", "knee",
    "knees", "hair", "shadow", "shadows", "angle", "pose", "body", "back",
    "sheet", "sheets", "bedsheet", "bedsheets", "bedding", "blanket", "duvet",
    "pillow", "pillows", "cushion", "mattress", "bed",
})

_FILLER = frozenset({"her", "his", "the", "a", "an", "own", "of", "and", "is",
                     "over", "on", "across", "left", "right", "both", "one"})


def is_clothing(name: str) -> bool:
    """Is the named thing a GARMENT, as opposed to a body part or the bedding?

    Decided on the content words: "her right hand" and "the bedsheet" reduce to
    nothing but body/bedding words, while "black lace thong" keeps `thong`. An
    unrecognised word counts AS clothing, so an unusual garment is not silently
    treated as a hand.
    """
    words = [w for w in re.split(r"[\s,./-]+", (name or "").lower()) if w]
    content = [w for w in words if w not in _FILLER]
    if not content:
        return False
    return not all(w in _NOT_CLOTHING or w.rstrip("s") in _NOT_CLOTHING
                   for w in content)


def wearing_something(fields: dict[str, Any] | None) -> bool:
    """A GARMENT was named over her chest or her crotch.

    Distinct from `underwear_visible`, which asks only about UNDERWEAR and so
    answers False for a woman in a tank top and a skirt — technically right,
    and the reason a fully-dressed street selfie read as nude.
    """
    return any(is_clothing(garment_over(fields, r)) for r in OVER_KEYS)


def flags_known(fields: dict[str, Any] | None) -> bool:
    """Has this item actually been looked at by the flags pass?

    The difference between "we checked and nothing is on show" and "we never
    checked" is the whole safety of any lane that claims something is safe to
    mass-send. Absence of evidence is not evidence of absence — so a row that
    is missing a region, or carries a value outside `VIS_STATES`, is unknown
    and must be excluded rather than assumed safe.
    """
    if not isinstance(fields, dict):
        return False
    if not isinstance(fields.get("underwear_visible"), bool):
        return False
    return all(fields.get(r) in _VIS_RANK for r in VIS_REGIONS)


def vis(fields: dict[str, Any] | None, region: str) -> str:
    """The stored state for one region, or `""` if it was never answered.

    Never guesses a default: an unanswered region reads as unknown at every
    call site, which is the only reading that cannot manufacture safety.
    """
    if not isinstance(fields, dict):
        return ""
    v = fields.get(region)
    return v if v in _VIS_RANK else ""


def is_bare(fields: dict[str, Any] | None, region: str) -> bool:
    """That part of her is in frame AND has nothing over it. Unknown reads as
    False, so this is only ever safe where NOT-bare is the permissive answer —
    pair it with `flags_known` anywhere a lane grants access on its strength.
    """
    return vis(fields, region) == "bare"


def any_bare(fields: dict[str, Any] | None,
             regions: tuple[str, ...] = VIS_REGIONS) -> bool:
    return any(is_bare(fields, r) for r in regions)


def fold_vis(states: list[str]) -> str:
    """Several frames of one clip → one verdict, by MAX exposure.

    `bare` in any frame exposes the clip; `not_in_frame` everywhere means it
    was never in shot. Taking the max is the conservative direction for a
    safety gate, and — unlike the old `all()` over booleans — a frame that
    simply could not see the region no longer reads as an exposed one. That
    conflation is why 20 of 21 AriaFree clips folded to "uncovered".
    """
    ranked = [_VIS_RANK[s] for s in states if s in _VIS_RANK]
    return VIS_STATES[max(ranked)] if ranked else ""


# ── Free quality measurement ────────────────────────────────────────
#
# There is no labelled ground truth for this vault and hand-labelling it is the
# one cost we cannot make trivial. But the row self-checks: the describe pass
# and the flags pass look at the same image and answer overlapping questions,
# so where they disagree, one of them is provably wrong — no human needed.
#
# That turns "does the new prompt work better" from a matter of taste into a
# number that can be run before and after any change. The v1 flags scored 14
# contradictions on 103 AriaFree items (13.6%); anything that replaces them has
# to beat that.
CONTRADICTIONS: dict[str, str] = {
    "dressed_but_bare": "clothing_state=dressed, yet a bare region is visible",
    "sfw_but_bare": "explicitness=sfw, yet a bare region is visible",
    "nude_but_clothed": "clothing_state=fully_nude, yet a garment is visible",
    "lingerie_but_nothing_on": "clothing_state=lingerie_on, yet no garment is visible",
    "penetration_offscreen": "penetration recorded, yet neither vulva nor anus is in frame",
}


def contradictions(fields: dict[str, Any] | None) -> list[str]:
    """Which stored facts about this item cannot all be true at once.

    Only fires where BOTH sides of the check were actually answered, so an
    unflagged or half-described row scores clean rather than noisy — this
    measures disagreement, not coverage.
    """
    if not isinstance(fields, dict) or not flags_known(fields):
        return []
    cl = _scalar(fields, "clothing_state").lower()
    tier = _scalar(fields, "explicitness").lower()
    garment = fields.get("underwear_visible")
    bare = any_bare(fields)
    out: list[str] = []

    if cl == "dressed" and bare:
        out.append("dressed_but_bare")
    if tier == "sfw" and bare:
        out.append("sfw_but_bare")
    if cl == "fully_nude" and garment is True:
        out.append("nude_but_clothed")
    if cl == "lingerie_on" and garment is False:
        out.append("lingerie_but_nothing_on")

    acts = {a.lower() for a in _clean(fields.get("acts"))}
    penetrating = bool(_scalar(fields, "penetration")) or bool(acts & {
        "fingering", "toy_insertion", "riding_toy", "sex_missionary",
        "sex_doggy", "sex_riding",
    })
    if penetrating and vis(fields, "vulva_vis") == "not_in_frame" \
            and vis(fields, "anus_vis") == "not_in_frame":
        out.append("penetration_offscreen")
    return out


def load_fields(raw: str | dict | None) -> dict[str, Any]:
    """`ai_fields_json` → dict. Tolerates the column being empty or malformed."""
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        out = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return out if isinstance(out, dict) else {}


def _clean(v: Any) -> list[str]:
    """Drop the model's null-ish placeholders so they never reach the brief as
    if they were findings."""
    if isinstance(v, str):
        v = [v]
    if not isinstance(v, list):
        return []
    return [str(x).strip() for x in v
            if str(x).strip() and str(x).strip().lower() not in ("none", "unclear")]


def _scalar(fields: dict, key: str) -> str:
    v = fields.get(key)
    s = str(v).strip() if v is not None else ""
    return "" if s.lower() in ("", "none", "unclear") else s


def sellability(fields: dict[str, Any], *, duration_seconds: int | None = None
                ) -> dict[str, Any]:
    """What the stored facts support, as a decision aid for the seller.

    `stands_alone` — has a payoff act, so it can close a sale by itself rather
    than only teeing up the next rung.
    `rung` — its index on EXPLICITNESS_LADDER, i.e. where it sits in a night's
    escalation (and where it lands on the way back down for a re-sell).
    `solo` — one person, no partner visible. A solo piece can be sold as
    "just me, thinking about you"; partner content CANNOT, and mis-framing it
    is a refund.
    """
    acts = {a.lower() for a in _clean(fields.get("acts"))}
    tier = _scalar(fields, "explicitness").lower()
    people = fields.get("people_count")
    partner = fields.get("partner_visible")
    payoff = sorted(acts & _PAYOFF_ACTS)
    solo: bool | None
    if partner is True:
        solo = False
    elif isinstance(people, int):
        solo = people <= 1 and partner is not True
    else:
        solo = None
    return {
        "stands_alone": bool(payoff),
        "payoff_acts": payoff,
        "rung": EXPLICITNESS_LADDER.index(tier) if tier in EXPLICITNESS_LADDER else None,
        "tier": tier or "unknown",
        "solo": solo,
        "penetration": _scalar(fields, "penetration"),
        "reveal": _REVEAL_TENSION.get(_scalar(fields, "clothing_state").lower(), ""),
        "seconds": int(duration_seconds) if duration_seconds else None,
        "sellable_flag": fields.get("sellable"),
    }


def item_facts(fields: dict[str, Any], *, description: str = "",
               duration_seconds: int | None = None, kind: str = "") -> str:
    """The compact fact block handed to the copy/seller LLM.

    Ordered by selling value, not by schema order: what happens (in sequence)
    first, then the reveal state, then the concrete props that make copy sound
    like a memory instead of a template.
    """
    s = sellability(fields, duration_seconds=duration_seconds)
    lines: list[str] = []

    if description.strip():
        lines.append(f"WHAT IT IS: {description.strip()}")

    beats = _clean(fields.get("beats"))
    if beats:
        lines.append("SEQUENCE (in order — beat 1 is how it opens, the last beat "
                     "is the payoff):")
        lines += [f"  {i}. {b}" for i, b in enumerate(beats, 1)]

    def add(label: str, value: str) -> None:
        if value:
            lines.append(f"{label}: {value}")

    add("ACTS", ", ".join(_clean(fields.get("acts"))))
    add("PENETRATION", s["penetration"] + (
        f" ({_scalar(fields, 'insertion_depth')})" if _scalar(fields, "insertion_depth") else ""))
    cl = _scalar(fields, "clothing_state")
    if cl:
        add("CLOTHING", f"{cl}" + (f" — {s['reveal']}" if s["reveal"] else ""))
    add("WEARING", ", ".join(_clean(fields.get("clothing_items"))))
    add("TOYS", ", ".join(_clean(fields.get("toys"))))
    add("POSITION", _scalar(fields, "position"))
    add("SETTING", _scalar(fields, "setting"))
    add("SHOT", _scalar(fields, "camera"))

    who = []
    if s["solo"] is True:
        who.append("SOLO — she is alone")
    elif s["solo"] is False:
        who.append("PARTNER PRESENT — do NOT write this as solo")
    if fields.get("face_visible") is True:
        who.append("her face is visible (you may promise eye contact)")
    elif fields.get("face_visible") is False:
        who.append("her face is NOT visible (do not promise eye contact)")
    if who:
        lines.append("WHO: " + "; ".join(who))

    tier = s["tier"]
    rung = f" (rung {s['rung'] + 1} of {len(EXPLICITNESS_LADDER)})" if s["rung"] is not None else ""
    lines.append(f"HEAT: {tier}{rung}; "
                 + ("CLOSES ON ITS OWN — payoff: " + ", ".join(s["payoff_acts"])
                    if s["stands_alone"] else
                    "BUILD-UP ONLY — no payoff act, so it teases the next send"))
    if s["seconds"]:
        lines.append(f"LENGTH: {s['seconds']}s")
    if kind:
        lines.append(f"TYPE: {kind}")

    conf = fields.get("confidence")
    if isinstance(conf, int) and conf < 70:
        lines.append(f"⚠ LOW CONFIDENCE ({conf}) — the vision pass was unsure. "
                     "Stay general; do not promise specific details.")
    return "\n".join(lines)


# ── The instruction block ───────────────────────────────────────────
#
# This is the "what is all this FOR" briefing. It is deliberately about HOW TO
# USE the facts, not about tone — the caller's own voice/style rules still own
# tone. Every field named here is one the describe pass really writes.
SELLING_BRIEF = """\
HOW TO USE THE FACTS ABOVE

You are selling ONE piece of a creator's own content to a fan in DMs. The facts
block is what a vision model actually saw in the media — it is ground truth.
Everything you write must be true against it.

THE HARD RULE
  Never promise something the facts don't list. If PENETRATION is absent, she is
  not penetrated. If it says BUILD-UP ONLY, do not imply she finishes. If PARTNER
  PRESENT appears, never frame it as solo. Selling a thing that isn't in the
  media is what produces refunds and chargebacks — a vaguer line that is TRUE
  outsells a specific line that is false.

FINDING THE SELLABLE MOMENT
  SEQUENCE is the clip's actual progression. The sellable moment is almost never
  beat 1 — it is the beat where the state CHANGES (she pulls something aside, the
  toy goes in, she stops posing and starts doing). Find that beat and build the
  copy around it. The beats are also the honest source for "what happens next",
  which is the only tease that survives the fan actually watching.

  REVERSE ORDER — read the beats BACKWARD when the payoff is the better hook.
  Lead with the last beat's result, then withhold how she got there:
  "…by the end my [X] was [state]. want to see how I got there?" This works when
  the final beat is stronger than the opening one. Keep the forward order when
  the build-up is the appeal (a slow strip sells the anticipation, not the end).
  Either way the FACTS stay the same — you are choosing a narration order, not
  inventing a different clip.

CLOTHING is the tension axis
  pulled_aside / pulled_down are the highest-value states: still covered, not
  covering anything. Name the garment (WEARING) — the specific detail is what
  makes it read as a real memory instead of a template. When CLOTHING is
  fully_nude there is no reveal left to promise, so sell the ACT instead.

STANDS ALONE vs BUILD-UP
  "CLOSES ON ITS OWN" = this can be the ask by itself; write copy that lands a
  purchase now. "BUILD-UP ONLY" = this is a rung; write copy that makes the NEXT
  send inevitable, and do not over-promise a finish it does not contain.

HEAT is the ladder position
  sfw → suggestive → explicit → hardcore. Going UP across a night is escalation;
  each send should read one notch hotter than the last. Going DOWN is also a
  real move: for a fan who already bought the top rung, a lower-rung piece is
  sold as intimacy ("just me, no show") rather than as more.

CONCRETENESS THAT SELLS
  POSITION, SETTING, SHOT and TOYS are what stop copy sounding generic. A
  closeup promises detail; a mirror or pov shot promises "you're there"; a named
  toy is proof it's real. Use one or two, not all of them — a list is inventory,
  not seduction.

SOLO PIECES
  When WHO says SOLO, the strongest frame is that it was made alone and
  privately — "I was thinking about you" is available to you, and it is the
  highest-converting solo frame precisely because it is about HIM, not the media.
  That frame is a lie for partner content, so it is off the table there.
"""


def copy_brief(fields: dict[str, Any], *, description: str = "",
               duration_seconds: int | None = None, kind: str = "") -> str:
    """Facts + instructions — the full user-message body for a copy call."""
    facts = item_facts(fields, description=description,
                       duration_seconds=duration_seconds, kind=kind)
    return f"{facts}\n\n{SELLING_BRIEF}"
