"""service/vault_ppv_week.py — arrange the vault into a WEEK-LONG story.

`vault_ppv_sets.propose_week` already builds a week of PPV bundles, but it is a
FUNNEL template: its seven slots are shaped by price and audience (a whale drop,
a $50 reveal, a wide teaser), and nothing threads content or copy from one day
to the next. A fan who buys Monday's set has no reason to expect Tuesday's.

This builds the other axis: a seven-day ESCALATION ARC. The week reads as one
story — Monday opens soft, each day is one notch hotter than the last, Saturday
is the payoff the week was promising, and the copy on every day refers back to
the day before ("you liked last night? watch this…"). It reuses the SAME selling
taxonomy as the rest of the vault pipeline — `vault_scripts.send_sort_key` is the
escalation order the days are sliced from, and `vault_ai_brief.sellability` /
`vault_ppv_sets.preview_ok` decide what closes a sale and what is tame enough to
give away — so the arc cannot drift from the rest of the system.

Delivery is a MIX: the opener and the finale are feed POSTS with a free preview
and locked sauce, to reach fans who never open a DM; the five days between are
mass PPVs to the DM audience; a closing FREE recap post on Sunday pulls the
week's stragglers back in.

Read-only and SUGGEST-ONLY. `plan_week` is pure — no DB, no network, no LLM — so
it is trivially testable and cannot send. `build_week` loads the vault and hands
back a plan for the operator to review; a `copy_fn` seam lets a copy LLM write
the connective captions later without touching the planner. Nothing here writes
`ppv_library_config_json`, and nothing touches OF.
"""
from __future__ import annotations

import asyncio
import html
import logging
from typing import Any, Callable
from urllib.parse import quote

import account_page
import vault_ai_brief
import vault_scripts
from vault_ppv_sets import (
    MAX_MEDIA, MAX_PREVIEWS, PRICE_CEIL_CENTS, PRICE_FLOOR_CENTS, preview_ok,
)

log = logging.getLogger("of-relay.vault_ppv_week")

WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

# A selling day under this floor is returned flagged `thin` rather than padded
# across days — reuse would break the "buy the whole week, never pay twice" rule
# that makes the arc worth sending. Knowing the vault cannot fill a day is the
# useful answer (same call `vault_ppv_sets` makes with MIN_SET).
MIN_DAY = 5
PREVIEWS_PER_DAY = 3

# describe tier (vault_ai_brief.EXPLICITNESS_LADDER) → the Vault-AI config's
# price-band key. The describe pass writes sfw/suggestive/explicit/hardcore; the
# operator's bands are keyed safe/suggestive/explicit/graphic/unknown.
_TIER_TO_BAND: dict[str, str] = {
    "sfw": "safe", "safe": "safe",
    "suggestive": "suggestive",
    "explicit": "explicit",
    "hardcore": "graphic", "graphic": "graphic",
    "": "unknown", "unknown": "unknown",
}

# Fallback price bands (cents), mirroring scripts_api._VAULT_AI_DEFAULTS so this
# module stays importable without the FastAPI app. The operator's own bands from
# the Vault-AI config override these when passed to `plan_week`/`build_week`.
_DEFAULT_BANDS: dict[str, tuple[int, int]] = {
    "safe": (300, 800), "suggestive": (500, 1500), "explicit": (1000, 3000),
    "graphic": (2000, 6000), "unknown": (500, 1500),
}


class _Chapter:
    """One day in the arc.

    `heat` is 0→1 along the week and does two things: it interpolates the price
    WITHIN the day's tier band (a suggestive Tuesday sits low in the suggestive
    band, a suggestive-but-late day sits high), and it is the shape the HTML
    review draws as the escalation curve. `pool` names an existing
    `ppv_send.PPV_CAPTION_POOLS` key, so a day the operator arms as a real PPV
    inherits a written caption pool instead of a bare string.
    """

    __slots__ = ("key", "weekday", "role", "delivery", "want", "heat", "pool",
                 "headline")

    def __init__(self, key: str, weekday: str, role: str, delivery: str,
                 want: int, heat: float, pool: str, headline: str) -> None:
        self.key, self.weekday, self.role = key, weekday, role
        self.delivery = delivery          # paid_post | ppv | free_post
        self.want, self.heat = want, heat
        self.pool, self.headline = pool, headline


# The week. Six selling days climbing the ladder, then a free recap.
# Delivery mix (operator's call): opener + finale are POSTS (free preview + lock)
# to reach non-DMers; the five middle days are mass PPVs.
ARC: tuple[_Chapter, ...] = (
    _Chapter("opener",   "Mon", "opener",   "paid_post", 8,  0.15,
             "teaser_free",        "Opener · a soft tease"),
    _Chapter("build",    "Tue", "build",    "ppv",       12, 0.32,
             "photoset_striptease", "Build · a little more"),
    _Chapter("reveal",   "Wed", "reveal",   "ppv",       12, 0.52,
             "intimate_reveal",    "Reveal · no more teasing"),
    _Chapter("escalate", "Thu", "escalate", "ppv",       10, 0.68,
             "video_ppv",          "Escalate · hotter"),
    _Chapter("heat",     "Fri", "heat",     "ppv",       10, 0.84,
             "bundle_long",        "Heat · friday night"),
    _Chapter("finish",   "Sat", "finish",   "ppv",       8,  1.00,
             "vip_whale",          "Finish · the payoff"),
    _Chapter("recap",    "Sun", "recap",    "free_post", 6,  0.20,
             "winback_dormant",    "Recap · a free taste"),
)
_SELLING = ARC[:6]
_RECAP = ARC[6]


# ── selling taxonomy helpers (reuse, never re-derive) ────────────────

def _tameness(item: dict[str, Any]) -> tuple:
    """Lowest = safest to give away free. Mirrors `vault_ppv_sets._tameness`:
    a payoff piece is the product, so it sorts last."""
    s = vault_ai_brief.sellability(item.get("fields") or {})
    return (bool(s["stands_alone"]),
            s["rung"] if s["rung"] is not None else 9,
            item.get("media_id") or 0)


def _closes(item: dict[str, Any]) -> bool:
    return bool(vault_ai_brief.sellability(item.get("fields") or {})["stands_alone"])


def _dedupe(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[int] = set()
    out: list[dict[str, Any]] = []
    for i in items:
        mid = i["media_id"]
        if mid not in seen:
            seen.add(mid)
            out.append(i)
    return out


def _pick_previews(items: list[dict[str, Any]], n: int = PREVIEWS_PER_DAY
                   ) -> list[int]:
    """The n tamest ELIGIBLE media as ids, photos preferred. Reuses
    `vault_ppv_sets.preview_ok` for the safety bar (not a payoff piece AND at or
    below suggestive) so a preview can never hand over the thing being sold.
    Empty when nothing in the day may legally be given away — the caller flags
    that (`preview_unsafe`) rather than previewing the product."""
    pool = [i for i in items if preview_ok(i)]
    photos = sorted((i for i in pool if i.get("kind") == "photo"), key=_tameness)
    rest = sorted((i for i in pool if i.get("kind") != "photo"), key=_tameness)
    want = min(n, MAX_PREVIEWS, len(pool))
    return [i["media_id"] for i in (photos + rest)[:want]]


def _dominant_tier(items: list[dict[str, Any]]) -> str:
    counts: dict[str, int] = {}
    for i in items:
        t = i["why"]["tier"]
        counts[t] = counts.get(t, 0) + 1
    return max(counts, key=lambda k: counts[k]) if counts else "unknown"


# ── pricing ──────────────────────────────────────────────────────────

def _round_to_99(amount_cents: float, bounds: tuple[int, int]) -> int:
    """Nearest whole dollar ending in .99, clamped into bounds. Mirrors
    `ppv_send.round_to_99` (5750 → 5799); a clamped price sits on the bound."""
    lo, hi = bounds
    dollars = round(amount_cents / 100)
    cents = 99 if dollars < 1 else dollars * 100 - 1
    return max(lo, min(cents, hi))


def _bands(config: dict[str, Any] | None) -> dict[str, tuple[int, int]]:
    out = dict(_DEFAULT_BANDS)
    by_tier = ((config or {}).get("pricing") or {}).get("bands_by_tier") or {}
    for key, val in by_tier.items():
        try:
            out[key] = (int(val[0]), int(val[1]))
        except (TypeError, ValueError, IndexError, KeyError):
            continue
    return out


def _price_bounds(config: dict[str, Any] | None) -> tuple[int, int]:
    cfg = config or {}
    try:
        lo = int(cfg.get("price_min_cents") or PRICE_FLOOR_CENTS)
    except (TypeError, ValueError):
        lo = PRICE_FLOOR_CENTS
    try:
        hi = int(cfg.get("price_max_cents") or PRICE_CEIL_CENTS)
    except (TypeError, ValueError):
        hi = PRICE_CEIL_CENTS
    lo = max(PRICE_FLOOR_CENTS, min(lo, PRICE_CEIL_CENTS))
    hi = max(lo, min(hi, PRICE_CEIL_CENTS))
    return lo, hi


def _price(tier: str, heat: float, bands: dict[str, tuple[int, int]],
           bounds: tuple[int, int], floor_prev: int) -> int:
    """A price inside the day's tier band, interpolated by arc heat, then held
    non-decreasing across the week — escalation must never cheapen. Vision only
    classified the tier; this picks the number, exactly as the panel promises."""
    lo, hi = bands.get(_TIER_TO_BAND.get(tier, "unknown"), _DEFAULT_BANDS["unknown"])
    raw = lo + max(0.0, min(1.0, heat)) * (hi - lo)
    return max(_round_to_99(raw, bounds), floor_prev)


# ── themes + connective copy (the STORY) ─────────────────────────────

# A day's theme is one short phrase the copy can hand to the next day
# ("you liked <theme> last night?"). Derived from the describe fields every item
# carries, most-specific first: the content folder, then the setting, then the
# outfit, then the bare tier label.
_FOLDER_THEME: dict[str, str] = {
    "solo_fingering": "my fingers", "solo_toy": "my toys",
    "solo_tease": "teasing you", "nude_stills": "my nudes", "lingerie": "lingerie",
    "ass_twerk": "that ass", "boobs": "my tits", "blowjob": "the real thing",
    "sex_with_partner": "the real thing", "shower_bath": "the shower",
    "feet": "my feet", "outfit_cosplay": "a lil cosplay", "sfw_selfie": "just me",
    "talking_head": "just me",
}
_SETTING_THEME: dict[str, str] = {
    "bedroom": "in bed", "bathroom": "the bathroom", "shower": "the shower",
    "kitchen": "the kitchen", "outdoors": "outside", "car": "the car",
    "pool": "the pool",
}
_TIER_THEME: dict[str, str] = {
    "sfw": "something soft", "suggestive": "a lil tease",
    "explicit": "the good stuff", "hardcore": "everything",
}

# The V1 describe shape (most of the vault) carries no `primary_folder`, only
# `tags` — but the tags are concrete ("lingerie", "ass", "shower", "toy"), which
# make far better copy than a bare tier label. Priority order: the most specific
# / sellable concept wins so a day reads as a scene, not an adjective.
_TAG_THEME: tuple[tuple[str, str], ...] = (
    ("cosplay", "a lil cosplay"),
    ("shower", "the shower"), ("bath", "the shower"),
    ("toy", "my toys"), ("dildo", "my toys"), ("vibrator", "my toys"),
    ("pussy", "everything"), ("spread", "everything"),
    ("lingerie", "lingerie"), ("stockings", "lingerie"),
    ("feet", "my feet"),
    ("ass", "that ass"), ("booty", "that ass"), ("twerk", "that ass"),
    ("boobs", "my tits"), ("topless", "my tits"), ("tits", "my tits"),
    ("mirror", "a mirror set"),
    ("nude", "my nudes"),
    ("bed", "in bed"),
)


def _theme(items: list[dict[str, Any]], avoid: str = "") -> str:
    """One short phrase naming the day's scene, most-specific source first, so
    the next day's copy can reference it. `avoid` skips the previous day's theme
    when an alternative exists — the arc should progress, not repeat."""
    candidates: list[str] = []
    folders: dict[str, int] = {}
    settings: dict[str, int] = {}
    tag_counts: dict[str, int] = {}
    for i in items:
        f = (i.get("fields") or {}).get("primary_folder")
        if isinstance(f, str) and f:
            folders[f] = folders.get(f, 0) + 1
        st = (i.get("fields") or {}).get("setting")
        if isinstance(st, str) and st:
            settings[st] = settings.get(st, 0) + 1
        for tag in (i.get("fields") or {}).get("tags") or []:
            if isinstance(tag, str):
                tag_counts[tag.lower().strip()] = tag_counts.get(tag.lower().strip(), 0) + 1

    if folders:
        candidates.append(_FOLDER_THEME.get(max(folders, key=lambda k: folders[k]), ""))
    if settings:
        candidates.append(_SETTING_THEME.get(max(settings, key=lambda k: settings[k]), ""))

    threshold = max(2, len(items) // 4)
    for tag, phrase in _TAG_THEME:
        if tag_counts.get(tag, 0) >= threshold:
            candidates.append(phrase)

    for i in items:
        key = vault_scripts.outfit_key(i.get("fields") or {})
        if key:
            candidates.append(vault_scripts.short_outfit(key))
            break

    candidates.append(_TIER_THEME.get(_dominant_tier(items), "me"))

    seen: set[str] = set()
    uniq = [c for c in candidates if c and not (c in seen or seen.add(c))]
    if not uniq:
        return "me"
    for c in uniq:
        if c != avoid:
            return c
    return uniq[0]


def _default_copy(ctx: dict[str, Any]) -> str:
    """Deterministic connective caption, referencing the previous day for every
    day except the opener and the recap. This is the STORY made visible: the
    thread is in the text, not just the escalation. A copy LLM can replace this
    via the `copy_fn` seam later; the connectivity contract stays the same."""
    role = ctx["role"]
    theme = ctx.get("theme") or "me"
    prev = ctx.get("prev_theme") or "last night"
    return {
        "opener": f"easing into your week 😇 just {theme} today… but it builds all "
                  f"week, trust me 🤭",
        "build": f"you liked {prev} last night? 🙈 tonight it's {theme}, a lil more "
                 f"of me…",
        "reveal": f"ok no more teasing after {prev} 🙊 tonight i finally give you "
                  f"{theme}",
        "escalate": f"still thinking about {prev}? 🥵 tonight i take it further — "
                    f"{theme}",
        "heat": f"been building all week since {prev}… friday me is dangerous 😈 "
                f"{theme} tonight",
        "finish": f"this is what i promised you all week 🥵 {theme} — the finish. "
                  f"after {prev} you earned it 💦",
        "recap": "missed me this week? 😘 here's a lil taste of everything — it's "
                 "all still waiting in your dms 💕",
    }.get(role, theme)


# ── channels — combine paid feed posts WITH mass messages ────────────
#
# The operator's ask: don't pick one surface, hit both. Every drop fires a feed
# action AND a DM action, so a fan who never opens DMs still sees the free
# preview on the wall, and a fan who never checks the feed still gets the PPV.
# `mass_ppv` + `feed_paid` reuse the existing `also_post_to_feed` wiring
# (`ppv_send.post_to_feed`); the free variants are unpriced nudges.

def _delivery_for(chapter_delivery: str, paid_page: bool) -> str:
    """A chapter's delivery, remapped for the page it lands on. On a paid page a
    `paid_post` day has no wall to land on (no paid-post lane), so it IS a mass
    PPV — say so, or the review card and the LLM copy brief would both promise a
    feed drop `_channels_for` has already removed."""
    if paid_page and chapter_delivery == "paid_post":
        return "ppv"
    return chapter_delivery


def _channels_for(role: str, price_cents: int, theme: str, *, combine: bool = True,
                  paid_page: bool = False) -> list[dict[str, Any]]:
    """The concrete sends for a day. With `combine` (the operator's default) every
    drop fires a feed action AND a DM action; without it, just the primary.

    `paid_page` (OF says this page charges for a subscription — see
    service/account_page.py) removes every `feed_paid` channel: that page has no
    paid-post lane, so the drop is sold in DMs only. The opener, whose whole
    point was the wall, becomes a priced DM rather than a free nudge pointing at
    a post that will never exist. `feed_free` is untouched — a free post is
    exactly what a subscription page's wall is for."""
    if role == "opener":
        if paid_page:
            return [{"kind": "mass_ppv", "label": "Mass PPV · DMs (paid page: no wall drop)",
                     "priced": True, "price_cents": price_cents}]
        feed = {"kind": "feed_paid", "label": "Paid post · feed (free preview + lock)",
                "priced": True, "price_cents": price_cents}
        dm = {"kind": "mass_free", "label": "Mass DM nudge · free", "priced": False,
              "price_cents": 0,
              "text": f"just put something new up on my wall 👀 {theme}… go peek 💕"}
        return [feed, dm] if combine else [feed]
    if role == "recap":
        feed = {"kind": "feed_free", "label": "Free post · feed", "priced": False,
                "price_cents": 0}
        dm = {"kind": "mass_free", "label": "Mass DM · free taste", "priced": False,
              "price_cents": 0,
              "text": "missed my week? 😘 sent u a lil taste — check ur dms 💕"}
        return [feed, dm] if combine else [feed]
    dm = {"kind": "mass_ppv", "label": "Mass PPV · DMs", "priced": True,
          "price_cents": price_cents}
    if paid_page:
        return [dm]
    feed = {"kind": "feed_paid", "label": "Also paid post · feed", "priced": True,
            "price_cents": price_cents}
    return [dm, feed] if combine else [dm]


# The mass-caption framing that rides ABOVE PAINFUL_TEXTING for a broadcast
# (PAINFUL_TEXTING was written for 1:1 chat; a PPV/post caption is a one-to-many
# sell, so it must not pretend to answer a specific fan).
#
# The clock ban mirrors describe_media's (`project_prompt_clock_all_engines`):
# this brief used to say "promise tomorrow", and the model slid from teasing the
# next drop into inventing expiry — "tomorrow i lose it" went out live on a
# $3.99 send (2026-08-29). Yesterday's callback stays because it is REAL data
# (`connects_to` names the actual previous drop); a deadline is not — nothing
# enforces one, and every fake one trains fans to read urgency as noise. The
# ask requirement is the other half of the same incident: arc days were the
# source of the 56 saved caption boxes with no unlock ask under them.
MASS_CAPTION_BRIEF = (
    "You are writing ONE caption for a MASS send / feed post that sells this "
    "locked content to ALL your fans at once — not a 1:1 reply, so never answer a "
    "specific message. lowercase, no quotes, no hashtags. it is one day of a "
    "seven-day build that gets hotter each day, so you may call back to "
    "yesterday's drop and make him NEED to unlock today. the content does not "
    "expire and is not leaving: never claim a time, a deadline, a limited "
    "window, or that anything disappears. the caption must carry a direct ask "
    "to unlock or open it — a mood line alone is not a caption. "
    "one line, occasionally two. punch the feeling; do not explain."
)


# ── the plan ─────────────────────────────────────────────────────────

def _item_view(item: dict[str, Any]) -> dict[str, Any]:
    """The slice of an item the review UI needs — no ai_fields_json blob.

    `account_id` rides along so the renderer can address the relay's own media
    route for this item; OF's `thumb_url` is kept only as the fallback for a
    pure plan built outside an account (see `_media_src`)."""
    fields = item.get("fields") or {}
    return {
        "media_id": item["media_id"],
        "account_id": item.get("account_id") or "",
        "kind": item.get("kind") or "",
        "tier": item["why"]["tier"],
        "closes": _closes(item),
        "description": (item.get("description")
                        or fields.get("description") or "").strip(),
        "thumb_url": item.get("thumb_url") or "",
    }


def _day_plan(ch: _Chapter, items: list[dict[str, Any]], *, prev_theme: str,
              bands: dict[str, tuple[int, int]], bounds: tuple[int, int],
              prev_price: int, copy_fn: Callable[[dict[str, Any]], str],
              pad_pool: list[dict[str, Any]] | None = None,
              combine: bool = True, reused: bool = False,
              paid_page: bool = False) -> dict[str, Any]:
    items = _dedupe(items)[:MAX_MEDIA]

    # Borrow a tame HOOK when a priced day holds nothing it may preview — an
    # explicit bundle with no free thumb is the "locked box with no picture"
    # `vault_ppv_sets` flags on 8 live PPVs. Pulled from unclaimed leftovers
    # (`pad_pool`), so the frame becomes part of THIS bundle and is not re-sold
    # elsewhere. Nothing to borrow → the day ships flagged `preview_unsafe`.
    if (ch.delivery != "free_post" and pad_pool is not None and items
            and not any(preview_ok(i) for i in items)):
        held = {i["media_id"] for i in items}
        while pad_pool and len(items) < MAX_MEDIA:
            cand = pad_pool.pop(0)
            if cand["media_id"] not in held:
                items.append(cand)
                held.add(cand["media_id"])
            if sum(1 for i in items if preview_ok(i)) >= PREVIEWS_PER_DAY:
                break

    tier = _dominant_tier(items) if items else "unknown"
    theme = _theme(items, avoid=prev_theme) if items else "me"

    delivery = _delivery_for(ch.delivery, paid_page)

    if ch.delivery == "free_post":
        price = 0
        # A free post IS the giveaway — the whole thing is unlocked. Prefer the
        # tame frames but never leave it empty.
        previews = [i["media_id"] for i in items if preview_ok(i)] \
            or [i["media_id"] for i in items]
        preview_unsafe = False
    else:
        price = _price(tier, ch.heat, bands, bounds, prev_price)
        previews = _pick_previews(items, PREVIEWS_PER_DAY)
        preview_unsafe = bool(items) and not previews

    caption = copy_fn({
        "role": ch.role, "weekday": ch.weekday, "theme": theme,
        "prev_theme": prev_theme, "tier": tier, "price_cents": price,
        "delivery": delivery, "items": items,
    })

    kinds: dict[str, int] = {}
    tier_counts: dict[str, int] = {}
    for i in items:
        kinds[i["kind"]] = kinds.get(i["kind"], 0) + 1
        t = i["why"]["tier"]
        tier_counts[t] = tier_counts.get(t, 0) + 1

    # The "hero" of the set — hottest, closer-preferred — is what the copy LLM is
    # briefed on (its facts, via item_facts) so the caption sells something real.
    hero_facts = ""
    if items:
        hero = max(items, key=lambda i: (
            (vault_ai_brief.sellability(i["fields"])["rung"] or 0),
            1 if _closes(i) else 0, i["media_id"]))
        hero_facts = vault_ai_brief.item_facts(
            hero["fields"], description=hero.get("description") or "",
            duration_seconds=hero.get("duration_seconds"), kind=hero.get("kind") or "")

    return {
        "key": ch.key, "weekday": ch.weekday, "role": ch.role,
        "headline": ch.headline, "delivery": delivery, "heat": ch.heat,
        "caption_pool_key": ch.pool,
        "channels": _channels_for(ch.role, price, theme, combine=combine,
                                  paid_page=paid_page),
        "hero_facts": hero_facts,
        "tier": tier, "band": _TIER_TO_BAND.get(tier, "unknown"),
        "price_cents": price,
        "media_ids": [i["media_id"] for i in items],
        "preview_media_ids": previews,
        "size": len(items),
        "photos": kinds.get("photo", 0), "videos": kinds.get("video", 0),
        "closers": sum(1 for i in items if _closes(i)),
        "tier_counts": tier_counts,
        "theme": theme, "caption": caption,
        "connects_to": prev_theme or None,
        "thin": ch.delivery != "free_post" and len(items) < MIN_DAY,
        "preview_unsafe": preview_unsafe,
        "reused": reused,
        "items": [_item_view(i) for i in items],
    }


def plan_week(items: list[dict[str, Any]], *, config: dict[str, Any] | None = None,
              copy_fn: Callable[[dict[str, Any]], str] | None = None,
              paid_page: bool = False) -> dict[str, Any]:
    """A seven-day escalation arc from a pool of described vault items. PURE —
    no DB, no network, no LLM.

    `items` are described vault rows: each needs `media_id`, `kind`, `fields`
    (the parsed ai_fields_json); `why` (from `vault_scripts.send_order_reason`),
    `thumb_url`, `description`, `script_id`, `script_seq` are used when present.

    Allocation is EXCLUSIVE across the six selling days (a fan who buys the whole
    week is never re-sold a frame he unlocked), and it is scarcest-first: the
    finish day claims the hottest tail of the escalation line, the opener the
    tamest head, and the four middle days split the remainder in ascending order
    — so the arc always spans the full range the vault has and heat never goes
    backward. Sunday's recap is a free taste, drawn (reused) from the week's own
    tame previews.

    `paid_page` = OF says this page charges for a subscription, so it has no
    paid-post lane: every `feed_paid` channel is dropped and the opener sells in
    DMs instead (see `_channels_for`). Passed in, not looked up — this function
    stays pure.
    """
    copy_fn = copy_fn or _default_copy
    bands = _bands(config)
    bounds = _price_bounds(config)
    combine = bool(((config or {}).get("ppv_week") or {}).get(
        "combine_feed_and_dm", True))

    enriched: list[dict[str, Any]] = []
    for it in items:
        fields = it.get("fields") or {}
        why = it.get("why") or vault_scripts.send_order_reason(fields)
        enriched.append({**it, "fields": fields, "why": why})
    by_id = {i["media_id"]: i for i in enriched}

    # Placeable = has a rung on the ladder. An item we cannot place on the arc
    # never opens a day; it stays in the pool only as recap fodder.
    placeable = [i for i in enriched if i["why"]["rung"] is not None]
    placeable.sort(key=lambda i: vault_scripts.send_sort_key(
        i["fields"], script_id=i.get("script_id"),
        script_seq=i.get("script_seq"), media_id=i["media_id"]))

    alloc: dict[str, list[dict[str, Any]]] = {c.key: [] for c in ARC}
    line = list(placeable)

    finish, opener = _SELLING[5], _SELLING[0]
    if finish.want and line:                      # hottest tail → the finish
        n = min(finish.want, len(line))
        alloc["finish"] = line[len(line) - n:]
        line = line[:len(line) - n]
    if opener.want and line:                      # tamest head → the opener
        n = min(opener.want, len(line))
        alloc["opener"] = line[:n]
        line = line[n:]

    middle = _SELLING[1:5]                         # build, reveal, escalate, heat
    total_want = sum(c.want for c in middle) or 1
    n_left = len(line)
    idx = 0
    for k, ch in enumerate(middle):
        if k == len(middle) - 1:
            chunk = line[idx:]
        else:
            share = int(round(n_left * ch.want / total_want))
            chunk = line[idx: idx + share]
            idx += len(chunk)
        alloc[ch.key] = chunk[:ch.want]           # cap; excess left unused

    # Tame leftovers no selling day claimed — the hook pool a priced day borrows
    # from when it holds nothing previewable. Tamest first, exclusive (popped).
    allocated = {i["media_id"] for c in _SELLING for i in alloc[c.key]}
    pad_pool = sorted((i for i in placeable
                       if i["media_id"] not in allocated and preview_ok(i)),
                      key=_tameness)

    # Six selling days, in weekday order, threading theme + price forward.
    days: list[dict[str, Any]] = []
    prev_theme = ""
    prev_price = 0
    for ch in _SELLING:
        day = _day_plan(ch, alloc[ch.key], prev_theme=prev_theme, bands=bands,
                        bounds=bounds, prev_price=prev_price, copy_fn=copy_fn,
                        pad_pool=pad_pool, combine=combine, paid_page=paid_page)
        days.append(day)
        if day["theme"]:
            prev_theme = day["theme"]
        prev_price = max(prev_price, day["price_cents"])

    # Sunday recap — a FREE taste, drawn from the week's tame previews (reuse is
    # intentional here: it is a recap, and previews were already vetted safe).
    recap_ids: list[int] = []
    seen: set[int] = set()
    for day in days:
        for mid in day["preview_media_ids"]:
            if mid not in seen:
                seen.add(mid)
                recap_ids.append(mid)
    if len(recap_ids) < _RECAP.want:
        for i in sorted((x for x in placeable if preview_ok(x)), key=_tameness):
            if i["media_id"] not in seen:
                seen.add(i["media_id"])
                recap_ids.append(i["media_id"])
            if len(recap_ids) >= _RECAP.want:
                break
    recap_items = [by_id[m] for m in recap_ids[:_RECAP.want] if m in by_id]
    days.append(_day_plan(_RECAP, recap_items, prev_theme=prev_theme, bands=bands,
                          bounds=bounds, prev_price=0, copy_fn=copy_fn,
                          combine=combine, reused=True, paid_page=paid_page))

    selling = days[:6]
    prices = [d["price_cents"] for d in selling]
    tier_counts: dict[str, int] = {}
    rich = 0
    flagged = 0
    for i in placeable:
        tier_counts[i["why"]["tier"]] = tier_counts.get(i["why"]["tier"], 0) + 1
        f = i["fields"]
        if any(f.get(k) for k in ("acts", "beats", "clothing_state", "primary_folder")):
            rich += 1
        if vault_ai_brief.flags_known(f):
            flagged += 1

    return {
        "days": days,
        "paid_page": paid_page,
        "arc": [{"weekday": c.weekday, "role": c.role, "heat": c.heat,
                 "delivery": _delivery_for(c.delivery, paid_page)} for c in ARC],
        "coverage": {
            "items_total": len(enriched),
            "placeable": len(placeable),
            "rich_taxonomy": rich,
            "with_flags": flagged,
            "tier_counts": tier_counts,
        },
        "summary": {
            "days": len(days),
            "selling_days": len(selling),
            "media_bound": sum(d["size"] for d in selling),
            "thin_days": sum(1 for d in selling if d["thin"]),
            "preview_unsafe_days": sum(1 for d in days if d["preview_unsafe"]),
            "price_low_cents": min(prices) if prices else 0,
            "price_high_cents": max(prices) if prices else 0,
            "recap_reused": len(recap_items),
        },
    }


# ── the vault loader + review build (read-only) ──────────────────────

async def _load_items(account_id: str) -> list[dict[str, Any]]:
    """Every described, non-removed vault item for an account, shaped for
    `plan_week` and carrying `thumb_url`/`description` for the HTML review.
    Read-only."""
    from sqlalchemy import select

    from db.engine import get_session
    from db.models import VaultItem

    async with get_session() as s:
        recs = (await s.execute(
            select(VaultItem.media_id, VaultItem.kind,
                   VaultItem.duration_seconds, VaultItem.ai_fields_json,
                   VaultItem.thumb_url, VaultItem.description,
                   VaultItem.video_description, VaultItem.script_id,
                   VaultItem.script_seq)
            .where(VaultItem.account_id == account_id,
                   VaultItem.removed_at.is_(None),
                   VaultItem.ai_fields_json.is_not(None))
        )).all()

    items: list[dict[str, Any]] = []
    for mid, kind, dur, fj, thumb, desc, vdesc, sid, seq in recs:
        fields = vault_ai_brief.load_fields(fj)
        if not fields:
            continue
        items.append({
            "media_id": int(mid), "account_id": account_id,
            "kind": kind, "duration_seconds": dur,
            "fields": fields, "thumb_url": thumb or "",
            "description": (desc or vdesc or fields.get("description") or ""),
            "script_id": sid, "script_seq": seq,
        })
    return items


async def _llm_context(account_id: str) -> tuple[str, bool]:
    """The model + painful-texting flag for the caption pass, resolved exactly as
    the conversational automations do (account config wins), with safe fallbacks."""
    model = "deepseek-v4-flash"
    painful = True
    try:
        from automations._common import resolve_model
        model = await resolve_model(account_id, "vault_ppv_week_caption", None)
    except Exception as exc:  # noqa: BLE001
        log.warning("resolve_model fell back for %s: %r", account_id, exc)
    try:
        from automations._common import load_painful_texting_flag
        painful = await load_painful_texting_flag(account_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("painful flag fell back for %s: %r", account_id, exc)
    return model, painful


async def _write_captions(days: list[dict[str, Any]], *, account_id: str,
                          model: str, painful: bool, week_index: int = 1,
                          weeks: int = 1, sem: asyncio.Semaphore | None = None
                          ) -> None:
    """Replace each day's template caption with an LLM line written in the house
    voice — `PAINFUL_TEXTING` (brevity + emotion) over `MASS_CAPTION_BRIEF` (this
    is a broadcast) over `SELLING_BRIEF` (never promise what the facts don't
    show). Threads the week: every day is told what yesterday was. Mutates
    `days` in place; a failed call keeps the deterministic template line."""
    import llm_client  # noqa: E402 — heavy import kept off the pure-planner path
    from vault_ai_brief import SELLING_BRIEF
    from automations._common import PAINFUL_TEXTING

    system = (((PAINFUL_TEXTING + "\n\n") if painful else "")
              + MASS_CAPTION_BRIEF + "\n\n" + SELLING_BRIEF)
    sem = sem or asyncio.Semaphore(6)

    async def _one(day: dict[str, Any]) -> None:
        if not day.get("hero_facts"):
            return
        prev = day.get("connects_to") or "nothing yet"
        user = (
            f"{day['hero_facts']}\n\n"
            f"CONTEXT: {day['weekday']}, the {day['role']} of a 7-day build "
            f"(week {week_index} of {weeks}). yesterday's set was '{prev}'. today is "
            f"'{day['theme']}', a {day['tier']} "
            f"{day['delivery'].replace('_', ' ')} priced ${day['price_cents'] / 100:.0f}. "
            "write ONE caption now.")
        async with sem:
            try:
                res = await llm_client.chat(
                    model=model,
                    messages=[{"role": "system", "content": system},
                              {"role": "user", "content": user}],
                    purpose="vault_ppv_week_caption", account_id=account_id,
                    temperature=0.95)
                txt = (res.content or "").strip().strip('"').strip()
                if txt:
                    day["caption"] = txt
                    day["caption_source"] = "llm"
            except Exception as exc:  # noqa: BLE001 — keep the template line
                log.warning("caption llm failed day=%s: %r", day["weekday"], exc)

    await asyncio.gather(*(_one(d) for d in days))


async def build_week(account_id: str, *, config: dict[str, Any] | None = None,
                     copy_fn: Callable[[dict[str, Any]], str] | None = None,
                     use_llm: bool = False) -> dict[str, Any]:
    """Load the vault and plan its week. Read-only, suggest-only. `use_llm` writes
    the connective captions through the house voice (network)."""
    items = await _load_items(account_id)
    plan = plan_week(items, config=config, copy_fn=copy_fn,
                     paid_page=await account_page.is_paid_page(account_id))
    plan["account_id"] = account_id
    if use_llm:
        model, painful = await _llm_context(account_id)
        await _write_captions(plan["days"], account_id=account_id, model=model,
                              painful=painful)
    log.info("vault_ppv_week planned account=%s items=%d bound=%d thin=%d",
             account_id, plan["coverage"]["items_total"],
             plan["summary"]["media_bound"], plan["summary"]["thin_days"])
    return plan


def plan_month(items: list[dict[str, Any]], *, weeks: int = 4,
               config: dict[str, Any] | None = None,
               copy_fn: Callable[[dict[str, Any]], str] | None = None,
               paid_page: bool = False) -> dict[str, Any]:
    """`weeks` escalation arcs, exclusive ACROSS the month — week 2 never re-sells
    what week 1 already sold. Each week still opens soft and finishes hot; a
    described vault of a few hundred items is deep enough for several waves. Pure."""
    pool = list(items)
    week_plans: list[dict[str, Any]] = []
    for w in range(1, weeks + 1):
        wp = plan_week(pool, config=config, copy_fn=copy_fn, paid_page=paid_page)
        wp["week_index"] = w
        week_plans.append(wp)
        used = {m for d in wp["days"][:6] for m in d["media_ids"]}
        if used:
            pool = [it for it in pool if it["media_id"] not in used]

    selling = [d for wp in week_plans for d in wp["days"][:6]]
    prices = [d["price_cents"] for d in selling]
    chans = [c for wp in week_plans for d in wp["days"] for c in d["channels"]]
    return {
        "weeks": week_plans,
        "paid_page": paid_page,
        "summary": {
            "weeks": weeks, "days": weeks * 7,
            "media_bound": sum(d["size"] for d in selling),
            "dm_sends": sum(1 for c in chans if c["kind"].startswith("mass")),
            "feed_posts": sum(1 for c in chans if c["kind"].startswith("feed")),
            "priced_sends": sum(1 for c in chans if c["priced"]),
            "free_sends": sum(1 for c in chans if not c["priced"]),
            "thin_days": sum(1 for d in selling if d["thin"]),
            "price_low_cents": min(prices) if prices else 0,
            "price_high_cents": max(prices) if prices else 0,
        },
    }


async def build_month(account_id: str, *, weeks: int = 4, use_llm: bool = True,
                      config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Load the vault and plan a whole month of exclusive weekly arcs. Read-only,
    suggest-only. `use_llm` (default on) writes every day's caption in voice."""
    items = await _load_items(account_id)
    plan = plan_month(items, weeks=weeks, config=config,
                      paid_page=await account_page.is_paid_page(account_id))
    plan["account_id"] = account_id
    if use_llm:
        model, painful = await _llm_context(account_id)
        sem = asyncio.Semaphore(6)
        for wp in plan["weeks"]:
            await _write_captions(wp["days"], account_id=account_id, model=model,
                                  painful=painful, week_index=wp["week_index"],
                                  weeks=weeks, sem=sem)
    log.info("vault_ppv_week month account=%s weeks=%d bound=%d dm=%d feed=%d",
             account_id, weeks, plan["summary"]["media_bound"],
             plan["summary"]["dm_sends"], plan["summary"]["feed_posts"])
    return plan


# ── HTML review (what the operator confirms) ─────────────────────────

def _money(cents: int) -> str:
    return "FREE" if not cents else f"${cents / 100:.2f}".replace(".00", "")


_DELIVERY_LABEL = {
    "paid_post": "Paid post · free preview + locked",
    "ppv": "Mass PPV",
    "free_post": "Free post · the whole thing",
}
_TIER_BADGE = {
    "sfw": "#5b8def", "safe": "#5b8def", "suggestive": "#8b5cf6",
    "explicit": "#ec4899", "hardcore": "#ef4444", "graphic": "#ef4444",
    "unknown": "#94a3b8",
}


def _media_src(item: dict[str, Any], base: str = "", *, full: bool = False) -> str:
    """The `<img src>` for one item — the RELAY's own media route, not OF's url.

    A vault item's `thumb_url` is a CloudFront signed url whose policy pins
    `AWS:SourceIp` to the relay's IP and expires within a day, so it 403s in the
    operator's browser and every tile renders blank. `/admin/vault-ai/{thumb,image}`
    serves the permanent on-disk copy (re-fetching through the account's own
    client on a miss), which is what the vault review UI already shows.

    `base` prefixes an absolute origin for a page opened OUTSIDE the app (the
    `_gen_ppv_week_preview` file lands on `file://`, where a root-relative path
    resolves nowhere); "" keeps it same-origin for the in-app iframe/popup.
    Falls back to the raw url for a pure plan carrying no account."""
    aid = str(item.get("account_id") or "").strip()
    mid = item.get("media_id")
    if not (aid and mid):
        return item.get("thumb_url") or ""
    route = "image" if full else "thumb"
    return (f"{base.rstrip('/')}/admin/vault-ai/{route}"
            f"?account_id={quote(aid, safe='')}&media_id={int(mid)}")


def _thumb_html(item: dict[str, Any], href: str | None = None, base: str = "") -> str:
    desc = html.escape((item.get("description") or "")[:140])
    tier = html.escape(item.get("tier") or "")
    star = " ⭐" if item.get("closes") else ""
    url = html.escape(_media_src(item, base))
    kind = "🎬" if item.get("kind") == "video" else "🖼"
    img = (f'<img src="{url}" loading="lazy" alt="" '
           f'onerror="this.style.display=\'none\';this.nextElementSibling.style.display=\'flex\'">'
           if url else "")
    fig = (
        f'<figure class="thumb" title="{desc}">'
        f'{img}'
        f'<div class="ph" style="{"display:none" if url else "display:flex"}">{kind}</div>'
        f'<figcaption>{kind} {tier}{star}<br><span>{desc or "(no description)"}</span></figcaption>'
        f'</figure>'
    )
    # A CSS-only ":target" lightbox — the page carries no scripts (the review
    # iframe is sandboxed without allow-scripts), so a thumb opens its day modal
    # by navigating to a fragment, never by JS.
    if href:
        return f'<a class="thumb-link" href="{html.escape(href)}">{fig}</a>'
    return fig


def _pv_thumb_html(item: dict[str, Any], dn: int, k: int, free: bool,
                   base: str = "") -> str:
    """A larger, badged thumb inside a day modal — links to its own image zoom."""
    desc = html.escape((item.get("description") or "")[:200])
    tier = html.escape(item.get("tier") or "")
    kind = "🎬" if item.get("kind") == "video" else "🖼"
    star = " ⭐" if item.get("closes") else ""
    url = html.escape(_media_src(item, base))
    img = (f'<img src="{url}" loading="lazy" alt="" '
           f'onerror="this.style.display=\'none\';this.nextElementSibling.style.display=\'flex\'">'
           if url else "")
    badge = ('<span class="pvb free">FREE PREVIEW</span>' if free
             else '<span class="pvb lock">🔒 locked</span>')
    return (
        f'<a class="pvthumb" href="#im-d{dn}-{k}" title="{desc}">'
        f'{img}<div class="ph2" style="{"display:none" if url else "display:flex"}">{kind}</div>'
        f'{badge}'
        f'<span class="pvk">{kind} {tier}{star}</span></a>'
    )


def _img_modal_html(item: dict[str, Any], dn: int, k: int, free: bool,
                    base: str = "") -> str:
    """Full-size zoom for one picked media — closing returns to its day modal.
    Uses the aspect-preserving FULL frame (`/image`), not the square crop: the
    operator is judging what he'll be sold, and the 300px centre-crop cuts the
    top and bottom off a 3:4 portrait."""
    desc = html.escape((item.get("description") or "")[:300])
    tier = html.escape(item.get("tier") or "")
    kind = "🎬 video" if item.get("kind") == "video" else "🖼 photo"
    star = " · ⭐ closer" if item.get("closes") else ""
    url = html.escape(_media_src(item, base, full=True))
    tag = ('<span class="pvb free">FREE PREVIEW</span>' if free
           else '<span class="pvb lock">🔒 locked</span>')
    # lazy: the zoom lives in a closed `:target` modal, and a month page holds one
    # per picked media — eager loading would fire hundreds of cold full-frame
    # fetches through the OF client the moment the page opened.
    big = (f'<img class="imbig" src="{url}" loading="lazy" alt="" '
           f'onerror="this.style.display=\'none\';this.nextElementSibling.style.display=\'flex\'">'
           if url else "")
    return (
        f'<div id="im-d{dn}-{k}" class="imlb">'
        f'<a class="lb-bg" href="#pv-d{dn}" aria-label="Close"></a>'
        f'<div class="im-wrap">'
        f'<a class="lb-x" href="#pv-d{dn}">✕</a>'
        f'{big}<div class="ph3" style="{"display:none" if url else "display:flex"}">🖼</div>'
        f'<div class="im-cap">{tag} {kind} · {tier}{star}<br>{desc or "(no description)"}</div>'
        f'</div></div>'
    )


def _day_modal_html(day: dict[str, Any], dn: int, back: str, base: str = "") -> str:
    """The picked-content preview a calendar cell (or a day card) opens on click.
    Pure CSS ":target" — no scripts. Shows every media picked for the day, marks
    the free previews vs the locked payoff, and drills into a full-size zoom."""
    colour = _TIER_BADGE.get(day["tier"], "#94a3b8")
    prevset = set(day["preview_media_ids"])
    items = day["items"]
    grid = "".join(
        _pv_thumb_html(it, dn, k, it["media_id"] in prevset, base)
        for k, it in enumerate(items)) or \
        '<div class="pv-empty">No content picked for this day.</div>'
    imgs = "".join(
        _img_modal_html(it, dn, k, it["media_id"] in prevset, base)
        for k, it in enumerate(items))

    flags = []
    if day["thin"]:
        flags.append('<span class="flag thin">thin · under floor</span>')
    if day["preview_unsafe"]:
        flags.append('<span class="flag unsafe">no safe preview</span>')
    if day["reused"]:
        flags.append('<span class="flag reuse">recap · reuses previews</span>')

    n_prev = len(prevset)
    prev_line = (f'{n_prev} free preview' + ('s' if n_prev != 1 else '')
                 if day["delivery"] != "free_post" else 'all free')

    return (
        f'<div id="pv-d{dn}" class="lb">'
        f'<a class="lb-bg" href="#{back}" aria-label="Close"></a>'
        f'<div class="lb-card">'
        f'<a class="lb-x" href="#{back}">✕</a>'
        f'<div class="lb-head"><span class="wd">{day["weekday"]}</span>'
        f'<span class="hl">{html.escape(day["headline"])}</span>'
        f'<span class="price" style="border-color:{colour}">{_money(day["price_cents"])}</span></div>'
        f'<div class="lb-sub"><span class="tier" style="background:{colour}">{html.escape(day["tier"])}</span>'
        f'<span>{day["size"]} media · {day["photos"]}📷 {day["videos"]}🎬 · {day["closers"]}⭐ · {prev_line}</span></div>'
        f'<div class="chans">{_chan_html(day)}</div>'
        f'<blockquote class="caption">{html.escape(day["caption"])}</blockquote>'
        f'<div class="flags">{"".join(flags)}</div>'
        f'<div class="pv-legend">🟢 free preview (shown before unlock) · 🔒 locked (pay to unlock) · ⭐ the closer · click any tile to enlarge</div>'
        f'<div class="pv-grid">{grid}</div>'
        f'</div></div>{imgs}'
    )


_CHAN_ICON = {"mass_ppv": "💬💰", "feed_paid": "📌💰", "feed_free": "📌",
              "mass_free": "💬"}


def _chan_html(day: dict[str, Any]) -> str:
    chips = []
    for c in day.get("channels") or []:
        price = _money(c["price_cents"]) if c["priced"] else "free"
        tip = html.escape(c.get("text") or "")
        chips.append(
            f'<span class="chan {c["kind"]}" title="{tip}">'
            f'{_CHAN_ICON.get(c["kind"], "•")} {html.escape(c["label"])} · {price}</span>')
    return "".join(chips)


def _day_card_html(day: dict[str, Any], dn: int, base: str = "") -> str:
    colour = _TIER_BADGE.get(day["tier"], "#94a3b8")
    flags = []
    if day["thin"]:
        flags.append('<span class="flag thin">thin · under floor</span>')
    if day["preview_unsafe"]:
        flags.append('<span class="flag unsafe">no safe preview</span>')
    if day["reused"]:
        flags.append('<span class="flag reuse">recap · reuses previews</span>')
    src = ('<span class="flag llm">✍ in-voice copy</span>'
           if day.get("caption_source") == "llm"
           else '<span class="flag tmpl">template copy</span>')
    flags.append(src)
    connect = (f'<div class="connect">↳ follows <b>{html.escape(day["connects_to"])}</b></div>'
               if day.get("connects_to") else '<div class="connect">— the opener —</div>')

    n_prev = len(day["preview_media_ids"])
    prev_line = (f'{n_prev} free preview' + ('s' if n_prev != 1 else '')
                 if day["delivery"] != "free_post" else 'all free')

    # Every tile (and the "+N more") opens the day's picked-content preview.
    thumbs = "".join(_thumb_html(i, f"#pv-d{dn}", base) for i in day["items"][:12])
    more = (f'<a class="more" href="#pv-d{dn}">+{day["size"] - 12} more</a>'
            if day["size"] > 12 else "")
    peek = (f'<a class="cardpeek" href="#pv-d{dn}">🔍 Preview picked content '
            f'· {day["size"]} media</a>' if day["items"]
            else '<span class="cardpeek disabled">no content picked</span>')

    return f"""
    <article class="day">
      <header>
        <div class="wd">{day['weekday']}</div>
        <div class="hl">{html.escape(day['headline'])}</div>
        <div class="price" style="border-color:{colour}">{_money(day['price_cents'])}</div>
      </header>
      {connect}
      <div class="meta">
        <span class="tier" style="background:{colour}">{html.escape(day['tier'])}</span>
        <span class="count">{day['size']} media · {day['photos']}📷 {day['videos']}🎬 · {day['closers']}⭐ · {prev_line}</span>
      </div>
      <div class="chans">{_chan_html(day)}</div>
      <blockquote class="caption">{html.escape(day['caption'])}</blockquote>
      <div class="flags">{''.join(flags)}</div>
      {peek}
      <div class="thumbs">{thumbs}{more}</div>
    </article>"""


# Plain string (single braces) so it drops into either page f-string verbatim.
_BASE_CSS = """<style>
  :root { color-scheme: light dark; }
  * { box-sizing: border-box; }
  body { font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
         margin: 0; background: #0b0e14; color: #e6e8ee; }
  .wrap { max-width: 1180px; margin: 0 auto; padding: 24px 20px 90px; }
  h1 { font-size: 22px; margin: 0 0 4px; }
  .sub { color: #9aa3b2; margin: 0 0 18px; font-size: 13px; }
  .nav { position: sticky; top: 0; z-index: 5; display: flex; gap: 8px; flex-wrap: wrap;
         padding: 10px 0; margin-bottom: 16px; background: rgba(11,14,20,.86);
         backdrop-filter: blur(8px); }
  .nav a { color: #c9a7ff; text-decoration: none; font-size: 13px; padding: 4px 11px;
           border: 1px solid #232a3a; border-radius: 20px; }
  .nav a:hover { background: #1a2130; }
  .panel { background: #141925; border: 1px solid #232a3a; border-radius: 12px;
           padding: 16px 18px; margin-bottom: 22px; }
  .panel h2 { font-size: 12px; text-transform: uppercase; letter-spacing: .08em;
              color: #9aa3b2; margin: 0 0 10px; }
  .stats { display: flex; gap: 26px; flex-wrap: wrap; }
  .stat b { display: block; font-size: 20px; }
  .stat span { color: #9aa3b2; font-size: 12px; }
  .curve { display: flex; align-items: flex-end; gap: 6px; height: 52px; margin: 12px 0; }
  .tick { width: 34px; border-radius: 3px 3px 0 0; display: inline-block; }
  .note { color: #f5b74e; font-size: 12.5px; margin-top: 10px; }
  .cal { display: grid; grid-template-columns: 64px repeat(7, 1fr); gap: 6px; }
  .cal .h { color: #7f8a9e; font-size: 11px; text-align: center; padding: 2px 0; }
  .cal .wk { color: #c9cfdb; font-size: 13px; font-weight: 700; display: flex; align-items: center; }
  .cell { border-radius: 8px; padding: 7px 7px 8px; min-height: 66px; color: #fff;
          display: flex; flex-direction: column; gap: 2px; font-size: 11px;
          border: 1px solid rgba(255,255,255,.09); }
  .cell .cp { font-weight: 700; font-size: 14px; }
  .cell .ct { opacity: .92; text-transform: capitalize; }
  .cell .cc { font-size: 12px; opacity: .9; margin-top: auto; }
  .days { display: grid; gap: 14px; }
  .day { background: #141925; border: 1px solid #232a3a; border-radius: 12px; padding: 16px 18px; }
  .day header { display: flex; align-items: center; gap: 12px; }
  .wd { font-weight: 700; font-size: 18px; width: 44px; }
  .hl { flex: 1; color: #c9cfdb; }
  .price { font-weight: 700; font-size: 18px; border: 2px solid; border-radius: 8px; padding: 2px 10px; }
  .connect { color: #7f8a9e; font-size: 12.5px; margin: 6px 0 10px; }
  .connect b { color: #c9a7ff; }
  .meta { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; font-size: 12.5px;
          color: #9aa3b2; margin-bottom: 10px; }
  .tier { color: #fff; padding: 2px 9px; border-radius: 20px; font-weight: 600; text-transform: capitalize; }
  .chans { display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 10px; }
  .chan { font-size: 11px; padding: 3px 9px; border-radius: 7px; font-weight: 600; border: 1px solid transparent; }
  .chan.mass_ppv { background: #2a1533; color: #e9b6ff; border-color: #4a2a5a; }
  .chan.feed_paid { background: #102a29; color: #8ff0e0; border-color: #204a48; }
  .chan.feed_free { background: #12233a; color: #9ec7ff; border-color: #264a6a; }
  .chan.mass_free { background: #1a2233; color: #9ab0d0; border-color: #2a3a55; }
  .caption { margin: 0 0 10px; padding: 10px 14px; background: #0e1420; border-left: 3px solid #8b5cf6;
             border-radius: 0 8px 8px 0; color: #e6e8ee; font-size: 15px; }
  .flags { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 10px; }
  .flag { font-size: 11px; padding: 2px 8px; border-radius: 20px; font-weight: 600; }
  .flag.thin { background: #3a2a12; color: #f5b74e; }
  .flag.unsafe { background: #3a1520; color: #ff8fab; }
  .flag.reuse { background: #12233a; color: #7fb0ff; }
  .flag.llm { background: #123a20; color: #79f0a0; }
  .flag.tmpl { background: #26262e; color: #9aa3b2; }
  .thumbs { display: flex; gap: 8px; flex-wrap: wrap; align-items: flex-start; }
  .thumb { width: 88px; margin: 0; }
  .thumb img, .thumb .ph { width: 88px; height: 88px; object-fit: cover; border-radius: 8px;
           background: #0e1420; align-items: center; justify-content: center; font-size: 26px; }
  .thumb .ph { display: flex; }
  .thumb figcaption { font-size: 10px; color: #8b93a4; margin-top: 3px; line-height: 1.3; }
  .thumb figcaption span { display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
  .more { align-self: center; color: #7f8a9e; font-size: 12px; padding: 0 6px; text-decoration: none; }
  a.more:hover { color: #c9a7ff; }
  .wksec { margin-top: 26px; scroll-margin-top: 60px; }
  .wksec > h2 { font-size: 16px; margin: 0 0 2px; }
  .hint { color: #9aa3b2; font-size: 12.5px; margin-top: 10px; }
  /* clickable calendar cells (day → picked-content preview) */
  a.cell { text-decoration: none; cursor: pointer; position: relative;
           transition: transform .08s ease, box-shadow .08s ease; }
  a.cell:hover, a.cell:focus-visible { transform: translateY(-1px);
           box-shadow: 0 6px 16px rgba(0,0,0,.42); outline: 2px solid rgba(255,255,255,.6); outline-offset: -1px; }
  .cell .peek { position: absolute; top: 4px; right: 5px; font-size: 11px; opacity: .4; }
  a.cell:hover .peek { opacity: 1; }
  /* clickable thumbs + the day-card preview button */
  .thumb-link { display: inline-block; text-decoration: none; cursor: pointer; }
  .cardpeek { display: inline-flex; align-items: center; gap: 6px; font-size: 12.5px; font-weight: 600;
              color: #c9a7ff; text-decoration: none; border: 1px solid #2a2140; background: #171326;
              border-radius: 20px; padding: 4px 12px; margin: 0 0 10px; }
  .cardpeek:hover { background: #1f1836; }
  .cardpeek.disabled { color: #6b7280; border-color: #232a3a; background: transparent; }
  /* pure-CSS :target lightbox — day preview + single-image zoom */
  .lb, .imlb { position: fixed; inset: 0; z-index: 60; display: none;
               align-items: center; justify-content: center; padding: 22px; }
  .lb:target, .imlb:target { display: flex; }
  .lb-bg { position: absolute; inset: 0; background: rgba(4,6,11,.74); backdrop-filter: blur(3px); }
  .lb-card { position: relative; z-index: 1; width: min(980px, 96vw); max-height: 92vh; overflow: auto;
             background: #141925; border: 1px solid #2a3346; border-radius: 14px;
             padding: 20px 22px 24px; box-shadow: 0 24px 60px rgba(0,0,0,.6); }
  .lb-x { position: absolute; top: 10px; right: 12px; z-index: 2; color: #c9cfdb; text-decoration: none;
          font-size: 17px; line-height: 1; width: 30px; height: 30px; display: flex; align-items: center;
          justify-content: center; border-radius: 50%; background: rgba(255,255,255,.08); }
  .lb-x:hover { background: rgba(255,255,255,.2); }
  .lb-head { display: flex; align-items: center; gap: 12px; padding-right: 36px; }
  .lb-sub { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; color: #9aa3b2;
            font-size: 12.5px; margin: 9px 0 12px; }
  .pv-legend { color: #8b93a4; font-size: 11.5px; margin: 2px 0 12px; }
  .pv-empty { color: #9aa3b2; font-size: 13px; padding: 22px 0; }
  .pv-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(148px, 1fr)); gap: 12px; }
  .pvthumb { position: relative; display: block; text-decoration: none; cursor: zoom-in;
             border-radius: 10px; overflow: hidden; background: #0e1420; }
  .pvthumb img, .pvthumb .ph2 { width: 100%; aspect-ratio: 1 / 1; object-fit: cover; display: block; }
  .pvthumb .ph2 { align-items: center; justify-content: center; font-size: 34px; color: #4a5468; }
  .pvthumb .pvb { position: absolute; top: 7px; left: 7px; font-size: 9.5px; font-weight: 700;
                  padding: 2px 7px; border-radius: 20px; letter-spacing: .02em; }
  .pvb.free { background: #123a20; color: #79f0a0; }
  .pvb.lock { background: rgba(4,6,11,.72); color: #cfd6e2; }
  .pvthumb .pvk { position: absolute; left: 0; right: 0; bottom: 0; padding: 16px 8px 5px;
                  font-size: 11px; color: #e6e8ee; background: linear-gradient(transparent, rgba(4,6,11,.88)); }
  .im-wrap { position: relative; z-index: 1; max-width: 96vw; max-height: 92vh;
             display: flex; flex-direction: column; align-items: center; }
  .im-wrap .imbig, .im-wrap .ph3 { max-width: 96vw; max-height: 82vh; border-radius: 10px;
             object-fit: contain; background: #0e1420; }
  .im-wrap .ph3 { width: 60vw; height: 46vh; align-items: center; justify-content: center; font-size: 72px; color: #4a5468; }
  .im-cap { color: #cfd6e2; font-size: 13px; line-height: 1.5; margin-top: 12px; max-width: 820px; text-align: center; }
  .im-cap .pvb { display: inline-block; margin-right: 6px; }
  @media (prefers-color-scheme: light) {
    body { background: #f6f7f9; color: #1a1f2b; }
    .nav { background: rgba(246,247,249,.9); }
    .nav a { border-color: #e3e7ee; color: #7c3aed; }
    .panel, .day { background: #fff; border-color: #e3e7ee; }
    .caption { background: #f4f0ff; color: #1a1f2b; }
    .hl { color: #465063; }
    .lb-card { background: #fff; border-color: #e3e7ee; }
    .lb-x { background: rgba(0,0,0,.06); color: #333; }
    .cardpeek { background: #f4f0ff; border-color: #e6ddfb; }
  }
</style>"""


def _curve_html(arc: list[dict[str, Any]]) -> str:
    return "".join(
        f'<span class="tick" style="height:{int(8 + a["heat"] * 40)}px;'
        f'background:{"#22c55e" if a["delivery"] == "free_post" else "#8b5cf6"}" '
        f'title="{a["weekday"]} · {a["role"]}"></span>'
        for a in arc)


def _coverage_panel(cov: dict[str, Any]) -> str:
    rich_pct = (100 * cov["rich_taxonomy"] // cov["placeable"]) if cov["placeable"] else 0
    tier_bar = " · ".join(f"{html.escape(t)} {n}"
                          for t, n in sorted(cov["tier_counts"].items(),
                                             key=lambda kv: -kv[1]))
    warn = ("<div class='note'>⚠ Most items carry tier + description + tags only — "
            "the arc leans on that; run a V2 describe + flags sweep to deepen payoff "
            "detection.</div>" if rich_pct < 40 else "")
    return f"""<div class="panel"><h2>Vault coverage</h2>
    <div class="stats">
      <div class="stat"><b>{cov['placeable']}</b><span>sellable described items</span></div>
      <div class="stat"><b>{rich_pct}%</b><span>rich taxonomy (acts/beats)</span></div>
      <div class="stat"><b>{cov['with_flags']}</b><span>with exposure flags</span></div>
    </div><div class="note">Tiers: {tier_bar or '—'}</div>{warn}</div>"""


def render_week_html(plan: dict[str, Any], *, title: str | None = None,
                     media_base: str = "") -> str:
    """One week as a self-contained page. Read-only — nothing is sent by viewing.
    `media_base` prefixes the relay origin on the media routes for a page viewed
    outside the app (see `_media_src`); "" is right for the in-app preview."""
    account = plan.get("account_id", "?")
    smry = plan["summary"]
    title = title or f"Vault PPV week · {account}"
    days = plan["days"]
    cards = "".join(_day_card_html(d, dn, media_base) for dn, d in enumerate(days))
    # Picked-content preview modals (+ per-image zoom), pure CSS ":target".
    modals = "".join(_day_modal_html(d, dn, "wtop", media_base)
                     for dn, d in enumerate(days))

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
{_BASE_CSS}</head>
<body><span id="wtop"></span><div class="wrap">
  <h1>{html.escape(title)}</h1>
  <p class="sub">A seven-day escalation arc from already-derived vault content · read-only · nothing is sent</p>
  <div class="panel"><h2>The week</h2>
    <div class="stats">
      <div class="stat"><b>{smry['media_bound']}</b><span>media · 6 selling days</span></div>
      <div class="stat"><b>{_money(smry['price_low_cents'])}→{_money(smry['price_high_cents'])}</b><span>price arc</span></div>
      <div class="stat"><b>{smry['thin_days']}</b><span>thin days</span></div>
      <div class="stat"><b>{smry['preview_unsafe_days']}</b><span>no-safe-preview days</span></div>
    </div><div class="curve">{_curve_html(plan['arc'])}</div>
    <div class="hint">🔍 Click any day below to preview the exact media picked for it.</div>
  </div>
  {_coverage_panel(plan['coverage'])}
  <div class="days">{cards}</div>
</div>{modals}</body></html>"""


def _cell_html(day: dict[str, Any], dn: int) -> str:
    colour = _TIER_BADGE.get(day["tier"], "#94a3b8")
    icons = "".join(_CHAN_ICON.get(c["kind"], "") for c in day.get("channels") or [])
    n = day["size"]
    cap = html.escape(day["caption"])
    hint = (f"{n} media picked — click to preview" if n else "no content picked")
    # The whole cell is a link to the day's picked-content preview modal.
    return (f'<a class="cell" href="#pv-d{dn}" style="background:{colour}" '
            f'title="{cap} — {hint}">'
            f'<span class="peek">🔍</span>'
            f'<div class="cp">{_money(day["price_cents"])}</div>'
            f'<div class="ct">{html.escape(day["tier"])}</div>'
            f'<div class="cc">{icons}{f" · {n}📎" if n else ""}</div></a>')


def render_month_html(plan: dict[str, Any], *, title: str | None = None,
                      media_base: str = "") -> str:
    """A whole month — calendar overview (monthly), per-week arcs (weekly), and
    every day expanded with its channels + captions (daily). Self-contained,
    read-only. `build_month` fills the captions in-voice before this renders."""
    account = plan.get("account_id", "?")
    weeks = plan["weeks"]
    smry = plan["summary"]
    title = title or f"Vault PPV month · {account}"

    nav = ('<div class="nav"><a href="#month">Month</a>'
           + "".join(f'<a href="#wk{w["week_index"]}">Week {w["week_index"]}</a>'
                     for w in weeks) + "</div>")

    # One stable index per day across the whole month, shared by the calendar
    # cell, the day card, and the preview modal so all three point at each other.
    dn_of: dict[int, int] = {}
    for wp in weeks:
        for d in wp["days"]:
            dn_of[id(d)] = len(dn_of)

    cal = ['<div class="h"></div>'] + [f'<div class="h">{wd}</div>' for wd in WEEKDAYS]
    for wp in weeks:
        cal.append(f'<div class="wk">W{wp["week_index"]}</div>')
        cal += [_cell_html(d, dn_of[id(d)]) for d in wp["days"]]

    sections = []
    for wp in weeks:
        wsm = wp["summary"]
        cards = "".join(_day_card_html(d, dn_of[id(d)], media_base) for d in wp["days"])
        sections.append(
            f'<section id="wk{wp["week_index"]}" class="wksec">'
            f'<h2>Week {wp["week_index"]} — {_money(wsm["price_low_cents"])}→'
            f'{_money(wsm["price_high_cents"])} · {wsm["media_bound"]} media · '
            f'{wsm["thin_days"]} thin</h2>'
            f'<div class="curve">{_curve_html(wp["arc"])}</div>'
            f'<div class="days">{cards}</div></section>')

    legend = ("💬💰 mass PPV (DM) · 📌💰 paid post (feed) · 📌 free post · "
              "💬 free DM nudge")

    # Picked-content preview modals (+ per-image zoom) for every day, pure CSS.
    modals = "".join(
        _day_modal_html(d, dn_of[id(d)], "month", media_base)
        for wp in weeks for d in wp["days"])

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
{_BASE_CSS}</head>
<body><div class="wrap">
  <h1>{html.escape(title)}</h1>
  <p class="sub">Four escalation-arc weeks, exclusive across the month · each drop hits feed + DMs · captions written in-voice · read-only · nothing is sent</p>
  {nav}
  <div id="month" class="panel"><h2>Month at a glance</h2>
    <div class="stats">
      <div class="stat"><b>{smry['weeks']}×7</b><span>days planned</span></div>
      <div class="stat"><b>{smry['media_bound']}</b><span>media across the month</span></div>
      <div class="stat"><b>{smry['dm_sends']}</b><span>mass DMs</span></div>
      <div class="stat"><b>{smry['feed_posts']}</b><span>feed posts</span></div>
      <div class="stat"><b>{_money(smry['price_low_cents'])}→{_money(smry['price_high_cents'])}</b><span>price band</span></div>
    </div>
    <div class="cal" style="margin-top:14px">{''.join(cal)}</div>
    <div class="note" style="color:#9aa3b2">{legend}</div>
    <div class="hint">🔍 Click any day to preview the exact media picked for it — free previews, the locked payoff, and a click-to-enlarge zoom.</div>
  </div>
  {_coverage_panel(weeks[0]['coverage'] if weeks else {'placeable':0,'rich_taxonomy':0,'with_flags':0,'tier_counts':{}})}
  {''.join(sections)}
</div>{modals}</body></html>"""
