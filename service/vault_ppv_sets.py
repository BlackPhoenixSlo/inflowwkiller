"""service/vault_ppv_sets.py — build a WEEK of PPV Library sets from the vault.

The PPV Library fans one premade bundle out to every spend×recency segment at a
per-segment price, on a weekly cadence. Its levers are the SIZE of the bundle,
the free PREVIEW slice, the PRICE, and the caption POOL — and all four are set
by hand today. On production that shows: 8 of 36 PPVs have no preview at all, 6
hold fewer than 10 media, and 30 of 36 use just three of the thirteen available
caption pools.

This proposes a whole week instead of one bundle at a time. The lineup below is
the shape of a good sending week — a cheap wide top of funnel, two priced photo
tiers, the clips, one payoff drop for whales, and a discount sampler for
dormant fans — and each slot names the caption pool written for that job.

It reads two collectors in `vault_scripts`, both owned elsewhere:
  * `collect_purpose_folders` — heat lanes. `tease_10` (breasts out or nude with
    her pussy out of frame) and `tease_50` (pussy visible or full-body nude) are
    already guaranteed DISJOINT there, because an item cannot carry two prices.
  * `collect_outfit_scripts` — outfit-coherent shoots, for the themed bundle.

Four rules, each from the live config or OF's own behaviour:

  1. **Big bundles.** A fan opens a set, not a single. `MIN_SET` is the floor a
     slot must clear; under it the slot is returned flagged `thin` rather than
     silently shipped. OF caps a bundle at 50.
  2. **The preview is the hook, and it must be TAME.** OF's `previews` are the
     unlocked slice OF THE ATTACHED MEDIA (`previews ⊆ media_ids`, enforced in
     `ppv_library_config_api._validate_ppv`), so a preview is free content.
     Previewing a payoff item hands over the thing being sold.
  3. **Never zero previews — with one honest exception.** A PPV with no preview
     is a locked box with no picture, the state 8 live PPVs are in. The
     exception is a bundle where nothing may LEGALLY be given away (all payoff,
     or all explicit) and no tame frame was available to attach: it ships
     hookless and flagged `preview_unsafe`, because previewing the product is
     worse than previewing nothing, and a silent fallback would leave the
     operator nothing to act on.
  4. **Distinct slots, but a full bundle wins.** Allocation is exclusive by
     default, so a fan who buys the whole week is not re-sold what he already
     unlocked. A slot that CANNOT reach `MIN_SET` from unclaimed material may
     then top up — first from a named compatible lane, then by repeating a
     frame — because the operator's call is that 10+ media is what makes a fan
     open it. Reuse only ever tops UP to the floor, and every repeated id is
     reported in `reused`.

Read-only. Proposes only: nothing writes `ppv_library_config_json`, and nothing
touches OF.
"""
from __future__ import annotations

import logging
from typing import Any

import vault_ai_brief
import vault_scripts

log = logging.getLogger("of-relay.vault_ppv_sets")

# Bundle size the operator asked for — a fan is far likelier to open a set than
# a single. Slots below this are still returned, marked `thin`, because knowing
# the vault cannot fill a slot is the useful answer.
MIN_SET = 10

# `ppv_library_config_api._MAX_MEDIA` / `_MAX_PREVIEWS`, and ppv_send's price
# floor/ceiling. Mirrored, not imported, so this module stays importable without
# the FastAPI app.
MAX_MEDIA = 50
MAX_PREVIEWS = 10
PRICE_FLOOR_CENTS = 300
PRICE_CEIL_CENTS = 20_000

PREVIEWS_PER_SET = 3

# Hard ceiling on what may be handed over FREE as a preview. A preview is the
# unlocked slice of the attachment, seen by everyone the PPV reaches — so an
# explicit frame in it IS the bundle, given away. Enforced on every set, not
# just the cheap ones: the $50 full reveal is 14/14 explicit and previewed three
# explicit images until this existed.
PREVIEW_MAX_TIER = "suggestive"
_PREVIEW_TIER_CAP = vault_ai_brief.EXPLICITNESS_LADDER.index(PREVIEW_MAX_TIER)


class Slot:
    """One PPV in the weekly lineup.

    `source` names where its media comes from:
      lane:<slug>  a `collect_purpose_folders` heat lane
      videos       every clip, build-up only
      closers      every payoff piece (what a whale is actually buying)
      outfit       the biggest outfit-coherent shoot, for a themed bundle
      leftovers    whatever no earlier slot claimed
    """

    def __init__(self, key: str, name: str, source: str, pool: str,
                 price_cents: int, sends_per_week: int, want: int, *,
                 why: str = "", media_kind: str | None = None,
                 no_closers: bool = False,
                 max_tier: str | None = None, fill_from: str | None = None,
                 max_copies: int = 1) -> None:
        self.key, self.name, self.source = key, name, source
        self.pool, self.price_cents = pool, price_cents
        self.sends_per_week, self.want = sends_per_week, want
        self.why, self.media_kind = why, media_kind
        # A payoff piece is the most valuable thing on the shelf. The cheap wide
        # slots must never carry one: the $3 teaser goes to EVERY subscriber, so
        # one closer in it hands the whole list what the $60 drop is selling.
        # Observed on AriaFree — the `mass` lane is suggestive-only, but a
        # suggestive item can still carry `rubbing_clit`.
        self.no_closers = no_closers
        # Hard ceiling on the explicitness ladder. The free-ish teaser goes to
        # the WHOLE subscriber list, so "nothing explicit in a teaser" has to be
        # a guarantee rather than a property the source lane happens to have.
        self.max_tier = max_tier
        # Which heat lane may TOP UP a slot that cannot reach `MIN_SET` from its
        # own material. Deliberately a named lane, not "anything": widening the
        # $10 tease into tease_50 material would put pussy-visible content in
        # the cheap tier, and those two lanes are disjoint precisely because an
        # item cannot carry two prices.
        self.fill_from = fill_from
        # How many bundles of this KIND to build when the vault has the material
        # for more than one. Extra copies are built from fresh media only, so a
        # deep lane (AriaFree's `mass` holds 53) yields several genuinely
        # different sets instead of one and a lot of unused shelf.
        self.max_copies = max_copies


# A sending week. Ordered SCARCEST-FIRST, which is also the allocation order:
# the $50 reveal and the whale drop draw on the rarest material, and filling the
# cheap wide slots first would strip the shelf before they got a look.
WEEKLY_LINEUP: tuple[Slot, ...] = (
    Slot("whale", "Whale drop · the finish", "closers", "vip_whale",
         6000, 1, 12, max_copies=2,
         why="every payoff piece — what a whale is actually paying for"),
    Slot("reveal50", "$50 full reveal", "lane:tease_50", "intimate_reveal",
         5000, 2, 14, max_copies=2,
         why="pussy visible or a full-body nude — the top photo tier"),
    Slot("video", "Video drop · mixed", "videos", "video_ppv", 2500, 2, 14,
         fill_from="mass",
         why="the clips, build-up only (closers are held for the whale drop), "
             "topped up with suggestive stills to make a full set"),
    Slot("tease10", "$10 photo tease · mixed", "lane:tease_10",
         "photoset_striptease", 1000, 3, 16, no_closers=True, fill_from="mass",
         why="breasts out, or nude with her pussy out of frame, topped up with "
             "suggestive stills — never with tease_50 material"),
    Slot("bundle", "Big mixed bundle", "mixed", "bundle_long", 3500, 1, 20,
         no_closers=True, max_copies=3,
         why="a big drop — prefers one shoot, but draws across shoots to fill it"),
    Slot("teaser", "Free-ish teaser · wide", "lane:mass", "teaser_free",
         PRICE_FLOOR_CENTS, 2, 14, no_closers=True, max_tier="suggestive",
         max_copies=3,
         why="suggestive only — the widest thing that is safe to send everyone"),
    Slot("winback", "Win-back · discount", "leftovers", "winback_dormant",
         900, 2, 12, no_closers=True, max_copies=2,
         why="a sampler of whatever the week did not use, for dormant fans"),
)


def _tameness(item: dict[str, Any]) -> tuple:
    """Sort key, lowest = safest to give away free. A preview must never be the
    payoff: an item that can close a sale on its own is the product. Reads
    `vault_ai_brief` rather than re-deriving the ladder, so the selling taxonomy
    cannot drift into a second copy."""
    s = vault_ai_brief.sellability(item.get("fields") or {})
    return (bool(s["stands_alone"]),
            s["rung"] if s["rung"] is not None else 9,
            item.get("media_id") or 0)


def _closes(item: dict[str, Any]) -> bool:
    return bool(vault_ai_brief.sellability(item.get("fields") or {})["stands_alone"])


def preview_ok(item: dict[str, Any]) -> bool:
    """May this piece be handed over FREE as the hook?

    Two independent bars, and a preview has to clear both:
      * not a payoff piece — that is the product the bundle sells;
      * at or below `PREVIEW_MAX_TIER` — a preview is free content shown to
        everyone the PPV reaches, so an explicit frame in it is the bundle given
        away. The $50 full reveal is 14/14 explicit, so with only the payoff bar
        it previewed three explicit images for nothing.
    """
    s = vault_ai_brief.sellability(item.get("fields") or {})
    if s["stands_alone"]:
        return False
    rung = s["rung"]
    return rung is not None and rung <= _PREVIEW_TIER_CAP


def _pick_previews(items: list[dict[str, Any]], n: int = PREVIEWS_PER_SET
                   ) -> list[int]:
    """The n tamest ELIGIBLE media in the set, as ids.

    Photos are preferred — a still reads as a thumbnail in the DM where a video
    shows a poster frame — but only as a PREFERENCE, applied AFTER `preview_ok`
    has filtered. Ordering photos first outright once unlocked a whale bundle's
    only photo, which happened to be its closer.

    Returns EMPTY when nothing in the set may legally be given away. That looks
    like it contradicts "never zero previews", and it is a deliberate choice
    between two bad outcomes: a bundle with no hook is weak, but a bundle that
    unlocks an explicit frame for free has given away the thing it sells. The
    caller flags the empty case (`preview_unsafe`) so the operator can attach a
    tame frame — silently previewing the product would give them nothing to act
    on. `_set_from` attaches one first, so this should only fire on a vault with
    no tame material at all.
    """
    want = min(n, MAX_PREVIEWS, len(items))
    pool = [i for i in items if preview_ok(i)]
    photos = sorted((i for i in pool if i.get("kind") == "photo"), key=_tameness)
    rest = sorted((i for i in pool if i.get("kind") != "photo"), key=_tameness)
    return [i["media_id"] for i in (photos + rest)[:want]]


def _dedupe(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """First occurrence wins, order preserved. Reuse is a cross-slot decision;
    a bundle that shows the same photo twice just reads as padding."""
    seen: set[int] = set()
    out: list[dict[str, Any]] = []
    for i in items:
        mid = i["media_id"]
        if mid not in seen:
            seen.add(mid)
            out.append(i)
    return out


def _set_from(slot: Slot, items: list[dict[str, Any]], *, note: str = "",
              copy: int = 1,
              pad_pool: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    chosen = _dedupe(items)[:min(slot.want, MAX_MEDIA)]
    # Attach a tame hook whenever the bundle cannot supply one itself. OF
    # previews must be PART of the attachment, so a bundle with nothing
    # previewable has to have one ADDED — it cannot borrow. This fires for the
    # all-payoff whale drop AND for the all-explicit $50 reveal, which are the
    # same problem: the only frames they hold are the ones being sold.
    if not any(preview_ok(i) for i in chosen):
        held = {i["media_id"] for i in chosen}
        safe = sorted((i for i in (pad_pool or [])
                       if preview_ok(i) and i["media_id"] not in held),
                      key=_tameness)
        room = MAX_MEDIA - len(chosen)
        chosen = chosen + safe[:min(PREVIEWS_PER_SET, room)]
    kinds: dict[str, int] = {}
    tiers: dict[str, int] = {}
    for i in chosen:
        kinds[i["kind"]] = kinds.get(i["kind"], 0) + 1
        t = vault_ai_brief.sellability(i.get("fields") or {})["tier"]
        tiers[t] = tiers.get(t, 0) + 1
    previews = _pick_previews(chosen)
    return {
        "key": slot.key if copy == 1 else f"{slot.key}_{copy}",
        "name": slot.name if copy == 1 else f"{slot.name} · {copy}",
        "why": slot.why,
        "caption_pool_key": slot.pool,
        "base_price_cents": max(PRICE_FLOOR_CENTS,
                                min(slot.price_cents, PRICE_CEIL_CENTS)),
        "sends_per_week": slot.sends_per_week,
        "media_ids": [i["media_id"] for i in chosen],
        "preview_options": previews,
        # No frame in this bundle may legally be given away, and no tame one was
        # available to attach. Shipping it hookless is the lesser harm; the
        # operator has to add a tame frame (the ⭐/✕ controls in the UI).
        "preview_unsafe": not previews,
        "items": chosen,
        "kinds": kinds, "tiers": tiers,
        "size": len(chosen),
        "photos": kinds.get("photo", 0),
        "videos": kinds.get("video", 0),
        "closers": sum(1 for i in chosen if _closes(i)),
        "thin": len(chosen) < MIN_SET,
        "note": note,
    }


async def propose_week(account_id: str, *, min_set: int = MIN_SET
                       ) -> dict[str, Any]:
    """A whole week of PPV bundles. Read-only.

    Allocation walks `WEEKLY_LINEUP` in order — scarcest slot first — and every
    media id is claimed at most once (rule 4), so a fan who buys the entire week
    never pays twice for the same frame.
    """
    lanes = {l["lane"]: l for l in
             await vault_scripts.collect_purpose_folders(account_id)}
    shoots = await vault_scripts.collect_outfit_scripts(account_id)

    # One flat pool of every described item, from the lanes we were given.
    everything: dict[int, dict[str, Any]] = {}
    for lane in lanes.values():
        for it in lane["items"]:
            everything.setdefault(it["media_id"], it)
    for sc in shoots:
        for it in sc["items"]:
            everything.setdefault(it["media_id"], it)

    taken: set[int] = set()
    out: list[dict[str, Any]] = []

    def pool_for(slot: Slot) -> tuple[list[dict[str, Any]], str]:
        """Every item this slot may draw on, deduped, plus a display note.
        Slot-level content rules are applied HERE so they hold for the reuse
        top-up and for extra copies too."""
        note = ""
        if slot.source.startswith("lane:"):
            lane = lanes.get(slot.source.split(":", 1)[1])
            pool = list(lane["items"]) if lane else []
            if not lane:
                note = "that heat lane is empty in this vault"
        elif slot.source == "videos":
            pool = [i for i in everything.values()
                    if i["kind"] == "video" and not _closes(i)]
        elif slot.source == "closers":
            pool = [i for i in everything.values() if _closes(i)]
        elif slot.source == "mixed":
            # A themed bundle prefers ONE shoot, but a shoot that cannot reach
            # `min_set` is not worth shipping. Coherence is a nice-to-have; a
            # full bundle is the point, so shoots are concatenated biggest-first.
            pool = [i for sc in shoots for i in sc["items"]]
            biggest = shoots[0] if shoots else None
            note = (f"largest shoot: {biggest['outfit']} ({biggest['size']}), "
                    "topped up across shoots" if biggest else "no shoots")
        else:                                            # leftovers
            pool = list(everything.values())

        if slot.media_kind:
            pool = [i for i in pool if i["kind"] == slot.media_kind]
        if slot.no_closers:
            pool = [i for i in pool if not _closes(i)]
        if slot.max_tier:
            cap = vault_ai_brief.EXPLICITNESS_LADDER.index(slot.max_tier)
            pool = [i for i in pool
                    if (vault_ai_brief.sellability(i.get("fields") or {})["rung"]
                        or 0) <= cap]
        return _dedupe(pool), note

    pools = {slot.key: pool_for(slot) for slot in WEEKLY_LINEUP}

    # PASS 1 — one bundle of every KIND first. Copies come later: letting a deep
    # lane take its second bundle before `winback` had its first starved the
    # leftovers slot down to 3 media on AriaFree. Every slot type earns a bundle
    # before any slot earns a second.
    for slot in WEEKLY_LINEUP:
        uniq, note = pools[slot.key]
        fresh = [i for i in uniq if i["media_id"] not in taken]
        fresh.sort(key=_tameness, reverse=True)
        avail = fresh

        # REUSE top-up. A bundle under `min_set` is worth less than a bundle that
        # repeats a frame: 10+ media is what makes a fan open it, and a set may
        # draw across shoots to get there. Only ever tops UP to the floor.
        reused: list[int] = []
        if len(avail) < min_set:
            have = {i["media_id"] for i in uniq}
            widen: list[dict[str, Any]] = [i for i in uniq if i["media_id"] in taken]
            if slot.fill_from:
                lane = lanes.get(slot.fill_from)
                for i in (lane["items"] if lane else []):
                    if i["media_id"] not in have:
                        have.add(i["media_id"])
                        widen.append(i)
            if slot.no_closers:
                widen = [i for i in widen if not _closes(i)]
            if slot.max_tier:
                cap = vault_ai_brief.EXPLICITNESS_LADDER.index(slot.max_tier)
                widen = [i for i in widen
                         if (vault_ai_brief.sellability(i.get("fields") or {})["rung"]
                             or 0) <= cap]
            widen.sort(key=lambda i: (i["media_id"] in taken, _tameness(i)))
            picked = widen[:min_set - len(avail)]
            reused = [i["media_id"] for i in picked if i["media_id"] in taken]
            avail = avail + picked

        pad_pool = [i for i in everything.values() if i["media_id"] not in taken]
        st = _set_from(slot, avail, note=note, pad_pool=pad_pool)
        st["reused"] = [m for m in st["media_ids"] if m in reused]
        taken.update(st["media_ids"])
        out.append(st)

    # PASS 2 — extra bundles of the SAME kind, only where the vault still has
    # `min_set` untouched items for that slot. Built from FRESH material only:
    # a copy assembled out of the reuse top-up would just be a near-duplicate.
    for copy_n in range(2, max((s.max_copies for s in WEEKLY_LINEUP), default=1) + 1):
        for slot in WEEKLY_LINEUP:
            if slot.max_copies < copy_n:
                continue
            uniq, note = pools[slot.key]
            spare = [i for i in uniq if i["media_id"] not in taken]
            if len(spare) < min_set:
                continue
            spare.sort(key=_tameness, reverse=True)
            extra = _set_from(slot, spare, note=note, copy=copy_n,
                              pad_pool=[i for i in everything.values()
                                        if i["media_id"] not in taken])
            extra["reused"] = []
            taken.update(extra["media_ids"])
            out.append(extra)

    ready = [s for s in out if not s["thin"]]
    return {
        "account_id": account_id,
        "lanes": {k: v["size"] for k, v in lanes.items()},
        "shoots": len(shoots),
        "sets": out,
        "summary": {
            "slots": len(out),
            "ready": len(ready),
            "thin": sum(1 for s in out if s["thin"]),
            "media_bound": sum(s["size"] for s in out),
            "media_pool": len(everything),
            "sends_per_week": sum(s["sends_per_week"] for s in ready),
            "pools_used": len({s["caption_pool_key"] for s in ready}),
            "reused_media": sum(len(s.get("reused") or []) for s in out),
            "preview_unsafe": sum(1 for s in out if s.get("preview_unsafe")),
        },
    }


def to_config_ppvs(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """The week, in `ppv_library_config_json` shape — ready for the UI to append.

    Three fields the proposal does not otherwise carry, each one load-bearing:

      `id`      REQUIRED by `_validate_ppv` (422 without it), and
                `ppv_library_config_api._sync_rules` matches each PPV to its
                AutomationRule BY this id. So it is derived from the slot key
                and is STABLE across re-proposals: re-running updates a row
                instead of orphaning its rule and creating a duplicate.
      `enabled` **must be False.** It defaults to TRUE in `_validate_ppv`, so
                saving seven proposals as-is would arm ~13 sends/week the moment
                the operator pressed Save. A proposal is a draft; arming it is a
                separate, deliberate click.
      `feed_enabled` likewise defaults TRUE — a draft must not be publicly
                postable to the feed either.

    `exclude_buyers` is set explicitly (its default is already True) so the row
    is self-describing rather than relying on a default that could change.
    """
    out: list[dict[str, Any]] = []
    for st in plan.get("sets") or []:
        if not st["media_ids"]:
            continue                       # nothing to sell — never emit an empty PPV
        out.append({
            "id": f"ppv_vault_{st['key']}",
            "name": st["name"][:60],
            "media_ids": st["media_ids"][:MAX_MEDIA],
            "preview_options": st["preview_options"][:MAX_PREVIEWS],
            "caption_pool_key": st["caption_pool_key"],
            "caption_texts": [],
            "base_price_cents": st["base_price_cents"],
            "sends_per_week": st["sends_per_week"],
            "resend_monthly": False,
            "exclude_buyers": True,
            "enabled": False,              # draft — never armed by a suggestion
            "feed_enabled": False,         # ditto for the public feed
            "also_post_to_feed": False,
        })
    return out


def audit_live(ppvs: list[dict[str, Any]], *, min_set: int = MIN_SET
               ) -> list[dict[str, Any]]:
    """Grade already-configured PPVs against the same levers.

    `no_preview` is the one that costs opens outright: OF shows a locked bundle
    with no free thumb, so the fan is asked to pay for something he cannot see.
    """
    rows = []
    for p in ppvs:
        media = list(p.get("media_ids") or [])
        prev = list(p.get("preview_options") or [])
        rows.append({
            "name": str(p.get("name") or "(unnamed)"),
            "media": len(media), "previews": len(prev),
            "price_cents": int(p.get("base_price_cents") or 0),
            "pool": p.get("caption_pool_key") or "",
            "enabled": bool(p.get("enabled")),
            "no_preview": not prev,
            "thin": len(media) < min_set,
            "stray_previews": [x for x in prev if x not in set(media)],
        })
    return rows
