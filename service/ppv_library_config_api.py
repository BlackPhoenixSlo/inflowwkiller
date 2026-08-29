"""service/ppv_library_config_api.py — read/write account_ai_config.ppv_library_config_json.

The "PPV Library" tab persists a per-account store of premade PPVs through these
owner-gated routes. On save it ALSO syncs the automation rules: ONE `ppv_send`
AutomationRule for the whole account (the hourly rotator tick, no ppv_id in its
steps). `ppv_send._pick_due_ppv` decides which enabled PPV each tick belongs to
— a week ÷ sends_per_week apart per PPV — so the per-PPV ticks in this tab are
the only per-PPV switch, and the rules page shows one row, not one per PPV.

  GET /admin/ppv-library-config?account_id=  → {config, defaults, pools, matrix}
  PUT /admin/ppv-library-config              → upsert the JSON + sync rules

Default (absent/NULL) → DISABLED with an empty library until enabled + configured.
"""
from __future__ import annotations

import json
import logging
import random
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

import account_page
import audiences
import ppv_captions
from auth import assert_account_owned
from db.engine import get_session
from db.models import AccountAiConfig, AutomationRule, CatalogItem, Message
from automations.upsell import derive_band, media_key
from automations.ppv_send import (
    PPV_CAPTION_POOLS,
    _REF_RESERVED,
    PPV_CAPTION_POOLS_BY_LANG,
    PPV_CAPTION_POOLS_HIM,
    PPV_FEED_CAPTION_POOLS,
    RECENCY_BANDS,
    SPEND_BANDS,
    _DEFAULTS,
    _PRICE_CEIL_CENTS,
    _PRICE_FLOOR_CENTS,
    pick_feed_caption,
    post_to_feed,
    broadcast_audience,
    is_list_id,
    price_bounds,
    spend_bound,
)

log = logging.getLogger("of-relay.ppv_library_config_api")

router = APIRouter()

_MAX_PPVS = 50
# OF's built-in audience NAMES. Legal in `userLists`, silently ignored inside
# `excludedLists` — which is why the exclude field refuses them outright.
# DERIVED from the house broadcast constant: these are the same two names the
# sender defaults to, and a second hardcoded copy would let "what we default to"
# and "what we refuse to exclude" drift apart.
_VIRTUAL_AUDIENCES = {str(x).lower() for x in audiences.BROADCAST_LISTS}
_MAX_MEDIA = 50
_MAX_PREVIEWS = 10
_MAX_CAPTION_TEXTS = 20
# Long multi-paragraph PPV copy (the "screenshot" look) fits. Owned by
# `ppv_captions` so a composed box and this clamp can never disagree.
_CAPTION_MAX = ppv_captions.TEXT_MAX
_NAME_MAX = 60
_ROTATOR_NAME = "PPV sends"
# The single rule's tick. The rotator inside ppv_send decides which PPV (if any)
# each tick belongs to, so this is just "how often we look": per-PPV gaps floor
# at 12h and the default ppv_caps gap is 12h, so hourly keeps a due PPV's real
# send within an hour of its slot.
_ROTATOR_TICK_S = 3600


def _int_list(raw: Any, cap: int) -> list[int]:
    out: list[int] = []
    for x in (raw or []):
        try:
            v = int(x)
        except (TypeError, ValueError):
            continue
        if v > 0 and v not in out:
            out.append(v)
        if len(out) >= cap:
            break
    return out


def _validate_ppv(p: Any) -> dict:
    if not isinstance(p, dict):
        raise HTTPException(422, "each ppv must be an object")
    pid = str(p.get("id") or "").strip()
    if not pid:
        raise HTTPException(422, "each ppv needs a stable id")
    if _REF_RESERVED in pid:
        # ppv_send carves `{id}#sub` out of this namespace for the owner leg's
        # ledger ref and folds the suffix back off when it ranks starvation.
        # An id carrying the reserved character would alias onto another entry.
        raise HTTPException(
            422, f"ppv id must not contain {_REF_RESERVED!r} — it is reserved by "
                 "the sender's ledger refs")
    name = str(p.get("name") or "").strip()[:_NAME_MAX]
    try:
        base = int(p.get("base_price_cents") or 0)
    except (TypeError, ValueError):
        raise HTTPException(422, "base_price_cents must be a number")
    base = max(_PRICE_FLOOR_CENTS, min(base, _PRICE_CEIL_CENTS))
    pool_key = str(p.get("caption_pool_key") or "").strip()
    caption_texts = p.get("caption_texts")
    clean_texts: list[str] = []
    if isinstance(caption_texts, (list, tuple)):
        for t in caption_texts[:_MAX_CAPTION_TEXTS]:
            s = str(t or "").strip()[:_CAPTION_MAX]
            if s:
                clean_texts.append(s)
    # Feed-post captions: optional override used ONLY by /post-now (a public feed
    # post wants a different voice than a 1:1 DM). Empty → the feed post falls back
    # to caption_texts / the pool. Same shape/caps as caption_texts.
    feed_caps_raw = p.get("feed_captions")
    clean_feed: list[str] = []
    if isinstance(feed_caps_raw, (list, tuple)):
        for t in feed_caps_raw[:_MAX_CAPTION_TEXTS]:
            s = str(t or "").strip()[:_CAPTION_MAX]
            if s:
                clean_feed.append(s)
    # Feed-post caption STYLE pool (auto-picked, public voice). "" = none.
    feed_pool_key = str(p.get("feed_caption_pool_key") or "").strip()
    if feed_pool_key and feed_pool_key not in PPV_FEED_CAPTION_POOLS:
        raise HTTPException(422, f"unknown feed_caption_pool_key: {feed_pool_key}")
    if pool_key and pool_key not in PPV_CAPTION_POOLS:
        raise HTTPException(422, f"unknown caption_pool_key: {pool_key}")
    if not pool_key and not clean_texts:
        raise HTTPException(422, f"ppv {pid}: pick a caption pool or write captions")
    try:
        spw = int(p.get("sends_per_week") or 1)
    except (TypeError, ValueError):
        raise HTTPException(422, "sends_per_week must be a number")
    spw = max(1, min(spw, 14))
    media = _int_list(p.get("media_ids"), _MAX_MEDIA)
    media_set = set(media)
    # OF previews are the attached media shown free — keep only previews that are
    # part of the content (anything else → OF "Wrong preview" at send time).
    previews = [x for x in _int_list(p.get("preview_options"), _MAX_PREVIEWS) if x in media_set]
    return {
        "id": pid,
        "name": name,
        "media_ids": media,
        "caption_pool_key": pool_key,
        "caption_texts": clean_texts,
        "feed_captions": clean_feed,
        "feed_caption_pool_key": feed_pool_key,
        # Two INDEPENDENT delivery enables: `enabled` = sent as PPV messages on cadence
        # (gates the AutomationRule); `feed_enabled` = available for feed posting (the
        # button + random picker + auto-post). A PPV can be messages-only, feed-only,
        # or both. Both default on so nothing regresses.
        "feed_enabled": bool(p.get("feed_enabled", True)),
        # "Auto post to feed with each send" — when this PPV's mass send fires, also
        # drop it on the feed as a paid post (same base price + ⭐ preview). Default off.
        "also_post_to_feed": bool(p.get("also_post_to_feed")),
        "base_price_cents": base,
        "preview_options": previews,
        "sends_per_week": spw,
        # Owned by an approved vault-AI arc (`vault_arc`): this entry exists to be
        # fired ONCE by its own scheduled job on its own day. Preserved through
        # validation so `_sync_rules` can refuse to give it a cadence.
        "arc_owned": bool(p.get("arc_owned")),
        "resend_monthly": bool(p.get("resend_monthly")),
        # Skip fans who already unlocked this PPV's media (don't re-pitch owned content).
        "exclude_buyers": bool(p.get("exclude_buyers", True)),
        "enabled": bool(p.get("enabled", True)),
    }


def _validate(cfg: dict) -> dict:
    if not isinstance(cfg, dict):
        raise HTTPException(422, "config must be an object")
    out: dict[str, Any] = {"enabled": bool(cfg.get("enabled"))}
    # "Send to everyone": also broadcast each PPV to ALL subscribers at the
    # default price (every known fan excluded so no double-send). UI default ON;
    # the runtime treats an ABSENT key as off, so it never blasts until saved.
    out["reach_all"] = bool(cfg.get("reach_all", True))
    # The owner leg: when the ownership guard removes the fans who already
    # unlocked a PPV, send them a DIFFERENT library PPV in the same tick instead
    # of nothing. Default OFF and, unlike `reach_all` above, OFF on save too —
    # this is a per-account rollout (the all-lanes count says three of the six
    # live accounts already over-serve buyers through the 1:1 seller lane), so
    # an account that has never been assessed must not acquire the behaviour by
    # opening the tab. Named here because `_validate` rebuilds the blob from
    # scratch: a key it does not list is DROPPED by the next save, and an
    # operator-enabled flag that a UI save silently reverts is worse than none.
    out["owner_second_leg"] = bool(cfg.get("owner_second_leg"))
    # AI caption at send: ppv_send writes ONE line about the media per run and
    # composes it above the style-pool line. `_validate` rebuilds this blob from
    # scratch, so a key it does not name is dropped by the next save from the
    # tab. Default OFF — a deploy alone never puts model copy in front of fans.
    out["ai_caption_at_send"] = bool(cfg.get("ai_caption_at_send"))
    # Pause between re-messaging the same fan (the contact guard), in hours.
    # 0 = no pause (send to everyone). Clamp to a sane ceiling.
    try:
        out["pause_hours"] = max(0, min(int(cfg.get("pause_hours") or 0), 168))
    except (TypeError, ValueError):
        out["pause_hours"] = 0
    # Optional creator-local quiet window [start_hour, end_hour] (0-23). Applies to
    # every PPV rule (baked into each rule's quiet_hours_json). Null/absent = 24/7.
    qh = cfg.get("quiet_hours")
    if isinstance(qh, (list, tuple)) and len(qh) == 2:
        try:
            out["quiet_hours"] = [int(qh[0]) % 24, int(qh[1]) % 24]
        except (TypeError, ValueError):
            raise HTTPException(422, "quiet_hours must be two hour numbers")
    # Per-account throttle: max PPV sends per day / week / month, EVEN-SPREAD — cap
    # N/window = one send every window/N (2/day → every 12h). A too-soon send is held
    # to its slot. Zeros are STORED, not dropped: a blob with no ppv_caps key runs
    # the runtime house default (2/14/60 → 12h gap), so explicit zeros are the only
    # way an operator can say "no spacing" and have it survive the save.
    caps = cfg.get("ppv_caps")
    if isinstance(caps, dict):
        clean_caps: dict[str, int] = {}
        for k in ("per_day", "per_week", "per_month"):
            try:
                v = int(caps.get(k) or 0)
            except (TypeError, ValueError):
                v = 0
            clean_caps[k] = max(0, min(v, 10_000))
        out["ppv_caps"] = clean_caps
    # Global price limits: every COMPUTED send/post price (per-cell tier, the
    # everyone-broadcast, feed posts) is clamped into [min, max] at RUNTIME —
    # authored base prices are never rewritten. Always emitted (self-describing
    # blob); price_bounds applies the defaults ($3/$200) + OF hard limits + order.
    out["price_min_cents"], out["price_max_cents"] = price_bounds(cfg)
    # Whale gate — lifetime-spend ceiling in cents; 0/absent = OFF. Stored
    # NORMALIZED through the same helper the sender reads it with, so the tab and
    # the runtime can never disagree about what "off" means. House key name,
    # shared with ai_chatter / autoreply / nudge_online.
    out["max_lifetime_spend_cents"] = spend_bound(cfg) or 0
    # ── the "Send to everyone" audience ──────────────────────────────────
    # Two DIFFERENTLY-TYPED knobs, not one list picker, because OF's wire is
    # asymmetric: `userLists` takes virtual audience NAMES ("fans"/"following")
    # as well as list ids, while `excludedLists` takes list IDS ONLY and
    # silently IGNORES a virtual name.
    #
    # This endpoint owns the REFUSALS (below); `broadcast_audience` owns the
    # NORMALIZATION — the sender reads the blob through that same helper, so a
    # value that saves clean and a value that sends can't drift apart.
    for key in ("broadcast_lists", "broadcast_exclude_lists"):
        if key in cfg and cfg[key] is not None and not isinstance(cfg[key], (list, tuple)):
            raise HTTPException(422, f"{key} must be a list")
    for x in (cfg.get("broadcast_exclude_lists") or []):
        if isinstance(x, str) and x.strip().lower() in _VIRTUAL_AUDIENCES:
            # Refuse LOUDLY. OF drops these from excludedLists without an error,
            # so accepting one would hand the operator a save that looks like it
            # worked and an exclusion that does nothing.
            raise HTTPException(
                422, f"'{x}' is a built-in audience, not a list — OF ignores it in "
                     "excludedLists. Untick it under 'Send to' instead.")
        if not is_list_id(x):
            raise HTTPException(422, "broadcast_exclude_lists must be OF list ids")
    # None = never configured (the historical fans + following); [] is legal and
    # means "reach nobody new" — never collapse the two.
    out["broadcast_lists"], out["broadcast_exclude_lists"] = broadcast_audience(cfg)
    raw = cfg.get("ppvs") or []
    if not isinstance(raw, (list, tuple)):
        raise HTTPException(422, "ppvs must be a list")
    seen: set[str] = set()
    ppvs: list[dict] = []
    for p in raw[:_MAX_PPVS]:
        item = _validate_ppv(p)
        if item["id"] in seen:
            raise HTTPException(422, f"duplicate ppv id: {item['id']}")
        seen.add(item["id"])
        ppvs.append(item)
    out["ppvs"] = ppvs
    return out


async def _sync_rules(s, account_id: str, cfg: dict) -> dict[str, int]:
    """ONE `ppv_send` rule per account — the rotator tick. `ppv_send.run` with no
    ppv_id in the payload picks which enabled PPV is due (a week ÷ sends_per_week
    apart each), so the per-PPV ticks in the Library tab are the only per-PPV
    switch: untick them all (or the master) and the rule goes dark. Legacy
    per-PPV rules (steps carry a ppv_id) are disabled, never deleted — an
    in-flight job's FK still points at them. Arc-owned entries never count
    toward arming the rule: they are fired once by vault_arc's own one-shot, and
    the rotator refuses to pick them."""
    master = bool(cfg.get("enabled"))
    qh = cfg.get("quiet_hours")
    quiet_json = json.dumps(qh) if isinstance(qh, list) and len(qh) == 2 else None
    existing = (await s.execute(
        select(AutomationRule).where(
            AutomationRule.account_id == account_id,
            AutomationRule.kind == "ppv_send",
        ).order_by(AutomationRule.id)
    )).scalars().all()

    rotator: AutomationRule | None = None
    stats = {"created": 0, "updated": 0, "disabled": 0}
    for r in existing:
        try:
            pid = str((json.loads(r.steps_json or "{}") or {}).get("ppv_id") or "")
        except Exception:
            pid = ""
        # The lowest-id no-ppv_id rule is THE rotator (deterministic across
        # re-syncs); everything else — old per-PPV rules and any duplicate a
        # concurrent save minted — goes quiet.
        if not pid and rotator is None:
            rotator = r
            continue
        if r.is_enabled:
            r.is_enabled = False
            stats["disabled"] += 1

    ppvs = [p for p in (cfg.get("ppvs") or [])
            if isinstance(p, dict) and not p.get("arc_owned")]
    if rotator is None and not ppvs:
        return stats            # nothing to schedule and nothing to repair
    enable = master and any(p.get("enabled", True) for p in ppvs)
    trigger = json.dumps({"every_seconds": _ROTATOR_TICK_S})
    steps = json.dumps({"account_id": account_id})
    if rotator is not None:
        rotator.trigger_json = trigger
        rotator.steps_json = steps
        rotator.name = _ROTATOR_NAME
        rotator.is_enabled = enable
        rotator.quiet_hours_json = quiet_json
        stats["updated"] += 1
    else:
        s.add(AutomationRule(
            account_id=account_id, name=_ROTATOR_NAME, kind="ppv_send",
            trigger_json=trigger, steps_json=steps, is_enabled=enable,
            quiet_hours_json=quiet_json))
        stats["created"] += 1
    return stats


async def resync_all_rules() -> dict[str, Any]:
    """Re-run `_sync_rules` for every account with a stored library config.

    Rides the boot-time heal section in `server._start_event_pumps` (after
    db_init, before the automation supervisor claims jobs) — that is what
    migrated the old per-PPV rules to the single rotator on the first deploy,
    and it stays as a self-heal: idempotent, one small transaction per account,
    so a hand-edited or drifted rule set is repaired on the next boot. A broken
    blob skips that account only. No expiry.
    """
    async with get_session() as s:
        rows = (await s.execute(
            select(AccountAiConfig.account_id,
                   AccountAiConfig.ppv_library_config_json)
            .where(AccountAiConfig.ppv_library_config_json.is_not(None)))).all()
    out: dict[str, Any] = {"accounts": 0, "created": 0, "updated": 0,
                           "disabled": 0, "skipped": []}
    for aid, blob in rows:
        try:
            # The stored blob already went through _validate on save; _sync_rules
            # reads only enabled/quiet_hours/ppvs[*].{id,enabled,arc_owned}.
            cfg = json.loads(blob) or {}
        except Exception:
            out["skipped"].append(aid)
            continue
        async with get_session() as s:
            stats = await _sync_rules(s, aid, cfg)
        out["accounts"] += 1
        for k in ("created", "updated", "disabled"):
            out[k] += stats[k]
    log.info("ppv_rules_resync %s", out)
    return out


# The band derivation reads what HUMANS actually charged for each piece of media.
# Bounded — the relay runs OF calls on the same thread pool, so an unbounded scan
# here shows up as a 500 somewhere else entirely.
_BAND_SCAN_LIMIT = 4000


async def _content_band_max_cents(account_id: str, stored: dict) -> int:
    """The top of the priciest piece of content's DERIVED band, in cents (0 = we
    have no content / no evidence).

    This is what backs the one-shot "your Max is below what some clips are worth"
    warning: the Min/Max in this tab is the single price authority — `upsell.next_price`
    clamps every 1:1 ladder quote into it (`library_bounds`), so a Max under the band
    top silently truncates the ceiling on the account's best content, and the operator
    would never see why. Bands come from `upsell.derive_band` (what humans actually
    charged for that exact media, falling back to the account's median ask, then to
    the operator's own sticker on the item) — never from duration, which is NULL on
    essentially every catalog row in prod.

    That third tier must be passed HERE too, not just at the chatter's quote site: an
    account with no human 1:1 ask history gets the $3-$20 constant for every item, so
    this warning would keep reporting a $30 ceiling while the chatter quotes the $90
    rung — the one instrument meant to catch that truncation, blind to it."""
    async with get_session() as s:
        priced = (await s.execute(
            select(Message.media_ids, Message.price_cents)
            .where(Message.account_id == account_id,
                   Message.direction == "out",
                   Message.price_cents > 0)
            .order_by(Message.created_at.desc())
            .limit(_BAND_SCAN_LIMIT))).all()
        catalog = (await s.execute(
            select(CatalogItem.media_ids, CatalogItem.price_cents)
            .where(CatalogItem.account_id == account_id,
                   CatalogItem.enabled.is_(True)))).all()

    asks_by_key: dict[str, list[int]] = {}
    all_asks: list[int] = []
    for media_ids, cents in priced:
        c = int(cents or 0)
        all_asks.append(c)
        k = media_key(media_ids)
        if k:
            asks_by_key.setdefault(k, []).append(c)
    median = sorted(all_asks)[len(all_asks) // 2] if all_asks else None

    # Every sellable media SET the account owns, paired with its sticker: the 1:1
    # catalog (priced by the operator) + this library (no per-PPV price here, so 0
    # → derive_band falls through to the constant exactly as it always did).
    media_sets: list[tuple[Any, int]] = [(ids, int(p or 0)) for ids, p in catalog]
    media_sets += [(p.get("media_ids"), 0) for p in (stored.get("ppvs") or [])
                   if isinstance(p, dict)]

    best = 0
    for ids, sticker in media_sets:
        key = media_key(ids)
        if not key:
            continue
        (_lo, hi), _src = derive_band(human_asks_cents=asks_by_key.get(key, []),
                                      account_median_cents=median,
                                      item_price_cents=sticker)
        best = max(best, int(hi))
    return min(best, _PRICE_CEIL_CENTS)


def _matrix_view() -> dict:
    return {
        "spend_bands": [
            {"name": n, "min_cents": lo, "max_cents": (None if hi == float("inf") else hi), "mult": m}
            for n, lo, hi, m in SPEND_BANDS
        ],
        "recency_bands": [
            {"name": n, "max_days": (None if hi == float("inf") else hi), "mult": m}
            for n, hi, m in RECENCY_BANDS
        ],
    }


@router.get("/admin/ppv-library-config/suggest")
async def suggest_ppvs_from_vault(
    account_id: str = Query(...),
    min_set: int = Query(0),
) -> dict[str, Any]:
    """Propose a WEEK of PPV bundles from the vault. Read-only.

    Free — no LLM and no OF traffic; the bundles are cut from the stored V2
    describe fields. Nothing is saved: the client appends the rows it wants to
    its own list and the operator still presses Save.

    Every row comes back `enabled: false` / `feed_enabled: false`, so accepting
    a suggestion can never start a send. Each is run through the real
    `_validate_ppv` before it leaves here, so a suggestion the UI accepts can
    never be rejected at save time.
    """
    import vault_ppv_sets

    assert_account_owned(account_id)
    kw = {"min_set": min_set} if min_set > 0 else {}
    plan = await vault_ppv_sets.propose_week(account_id, **kw)
    rows = vault_ppv_sets.to_config_ppvs(plan)
    validated = [_validate_ppv(p) for p in rows]

    # The vault-side reasoning the operator needs to judge a row, keyed by id —
    # kept OUT of the PPV objects so they stay exactly config-shaped.
    notes = {f"ppv_vault_{st['key']}": {
        "why": st["why"], "note": st["note"], "thin": st["thin"],
        "preview_unsafe": bool(st.get("preview_unsafe")),
        "photos": st["photos"], "videos": st["videos"],
        "closers": st["closers"], "reused": len(st.get("reused") or []),
        "tiers": st["tiers"],
    } for st in plan["sets"]}

    return {
        "account_id": account_id,
        "ppvs": validated,
        "notes": notes,
        "summary": plan["summary"],
        "lanes": plan.get("lanes") or {},
    }


def _parse_library_blob(raw: str | None) -> dict:
    """`ppv_library_config_json` as a dict, `{}` for missing or unparseable.
    The one spelling of that fallback — every read of the column that treats a
    bad blob as "no config" goes through here."""
    try:
        return json.loads(raw or "{}") or {}
    except Exception:
        return {}


# What the box-set button is told when the account will stack a SECOND
# model-written opener at send time. `ppv_send._pick_caption` prepends
# `describe_media.send_time_hook`'s line to whatever box it drew — a feature
# built for hand-written operator copy; against a generated box the fan reads
# two openers about the same garment plus the ask. It rides the response
# `warning` field so the answer does not live only in the UI. Advice, never a
# refusal: the boxes are a suggestion the operator still reviews, and they are
# perfectly good copy the moment that switch is off.
STACKED_HOOK_WARNING = (
    "This account has \"Write a fresh caption line at every send\" on. These boxes "
    "already open with a written hook, so at send time a SECOND one is placed "
    "above it. Turn that switch off for this account, or write the boxes by hand."
)


class _CaptionBoxSetBody(BaseModel):
    account_id: str
    # The PPV's media, in ITS order — deliberately NOT a ppv_id, so the button
    # works on a draft row the operator has not saved yet (the same reason
    # `_PreviewBody` carries draft price limits instead of reading the store).
    media_ids: list[int] = []
    compose: ppv_captions.ComposeSpec = Field(default_factory=ppv_captions.ComposeSpec)


@router.post("/admin/ppv-library-config/caption-box-set")
async def caption_box_set(body: _CaptionBoxSetBody = Body(...)) -> dict[str, Any]:
    """A rotating SET of caption boxes for one PPV — see `ppv_captions` for what
    a set is and why the frame is the lane's own line.

    Suggest-only, exactly like `/suggest`: nothing is written to
    `ppv_library_config_json`. The operator keeps the boxes they want and presses
    Save, and the send path is untouched — it already picks one box at random,
    which is what turns the set into a rotation.

    This route is the HTTP boundary and nothing else: auth, the media list, the
    account's voice lane, delegate.
    """
    assert_account_owned(body.account_id)
    media_ids = _int_list(body.media_ids, _MAX_MEDIA)
    if not media_ids:
        raise HTTPException(422, "media_ids is required")
    async with get_session() as s:
        cfg_row = await s.get(AccountAiConfig, body.account_id)
    out = await ppv_captions.build_box_set(
        body.account_id, media_ids, body.compose,
        voice=getattr(cfg_row, "voice", None) or "",
        lang=getattr(cfg_row, "language", None) or "en",
    )
    # The composer knows nothing about the SEND path's flags; the stacked-hook
    # warning is this boundary's to give. Read off the row already in hand — a
    # bad blob means "off", because advice must never 500 the button.
    stored = _parse_library_blob(getattr(cfg_row, "ppv_library_config_json", None))
    if stored.get("ai_caption_at_send"):
        out["warning"] = STACKED_HOOK_WARNING
    return out


@router.get("/admin/ppv-library-config")
async def get_ppv_library_config(account_id: str = Query(...)) -> dict[str, Any]:
    assert_account_owned(account_id)
    async with get_session() as s:
        row = await s.get(AccountAiConfig, account_id)
    stored = _parse_library_blob(row.ppv_library_config_json if row else None)
    return {
        "account_id": account_id,
        "config": stored,
        "defaults": dict(_DEFAULTS),
        # Top of the priciest content's derived band — the UI warns ONCE when the
        # operator's Max sits below it (his Max is the ceiling on 1:1 ladder quotes).
        "content_band_max_cents": await _content_band_max_cents(account_id, stored),
        "price_ceil_cents": _PRICE_CEIL_CENTS,
        "pools": sorted(PPV_CAPTION_POOLS),
        "caption_pools": PPV_CAPTION_POOLS,   # key → lines, for the UI caption preview
        "feed_pools": sorted(PPV_FEED_CAPTION_POOLS),
        "feed_caption_pools": PPV_FEED_CAPTION_POOLS,   # public-voice feed caption styles
        "matrix": _matrix_view(),
        # The caption-set ceilings, so the panel's "% of sends" readout is
        # computed against the SAME bound the route clamps to. A client-side
        # copy of this number drifts silently, and the number it would lie
        # about is the headline one.
        "caption_limits": {"boxes_max": ppv_captions.BOXES_MAX,
                           "styles_max": ppv_captions.CALLS_MAX},
    }


class _ConfigBody(BaseModel):
    account_id: str
    config: dict


@router.put("/admin/ppv-library-config")
async def put_ppv_library_config(body: _ConfigBody = Body(...)) -> dict[str, Any]:
    assert_account_owned(body.account_id)
    clean = _validate(body.config)
    now = datetime.utcnow()
    payload = json.dumps(clean)
    async with get_session() as s:
        await s.execute(
            sqlite_insert(AccountAiConfig)
            .values(account_id=body.account_id, utc_offset=0,
                    ppv_library_config_json=payload, updated_at=now)
            .on_conflict_do_update(
                index_elements=["account_id"],
                set_={"ppv_library_config_json": payload, "updated_at": now})
        )
        rules = await _sync_rules(s, body.account_id, clean)
    log.info("ppv_library_config_saved account=%s ppvs=%d rules=%s",
             body.account_id, len(clean["ppvs"]), rules)
    return {"account_id": body.account_id, "config": clean, "rules": rules}


class _PreviewBody(BaseModel):
    account_id: str
    base_price_cents: int = 2500
    # Optional DRAFT price limits (unsaved UI state) — absent → the stored config's.
    price_min_cents: int | None = None
    price_max_cents: int | None = None
    # Optional DRAFT whale gate. 0 is MEANINGFUL here ("preview with the cap
    # off"), so absent must stay None and be distinguishable from it — this is
    # the number the operator is trying to choose, and the preview is how they
    # choose it.
    max_lifetime_spend_cents: int | None = None


@router.post("/admin/ppv-library-config/preview")
async def preview_ppv_library(body: _PreviewBody = Body(...)) -> dict[str, Any]:
    """Dry-run: with the account's CURRENT fans, how many land in each spend×recency
    cell and what would they pay for this base price. No send, no state write — the
    operator's safety net (surfaces a thin-data 'everyone is cheap' collapse).
    Draft price limits in the body win over the stored ones, so an UNSAVED Min/Max
    edit previews exactly what a save would send."""
    assert_account_owned(body.account_id)
    from automations.ppv_send import segment_preview
    draft = dict(await _load_stored_config(body.account_id))
    if body.price_min_cents is not None:
        draft["price_min_cents"] = body.price_min_cents
    if body.price_max_cents is not None:
        draft["price_max_cents"] = body.price_max_cents
    if body.max_lifetime_spend_cents is not None:
        draft["max_lifetime_spend_cents"] = body.max_lifetime_spend_cents
    bounds = price_bounds(draft)
    cap = spend_bound(draft)
    return {"account_id": body.account_id,
            # Echo the cap back so the tab can label the drop ("N fans above $X")
            # without re-deriving the normalization the validator just applied.
            "spend_cap_cents": cap,
            **await segment_preview(body.account_id, int(body.base_price_cents or 0),
                                    bounds, cap)}


async def _load_stored_config(account_id: str) -> dict:
    async with get_session() as s:
        row = await s.get(AccountAiConfig, account_id)
    return _parse_library_blob(row.ppv_library_config_json if row else None)


def _pick_feed_ppv(stored: dict, *, ppv_id: str | None) -> dict:
    """Resolve WHICH PPV a feed post/preview targets — shared by preview and
    post-now so they always agree. Explicit `ppv_id` wins; otherwise a random
    feed-enabled PPV, skipping the one posted last time (best-effort — the
    marker lives in the config blob)."""
    all_ppvs = [p for p in (stored.get("ppvs") or []) if isinstance(p, dict)]

    if ppv_id:
        chosen = next((p for p in all_ppvs if str(p.get("id")) == str(ppv_id)), None)
        if chosen is None:
            raise HTTPException(404, "ppv not found")
        return chosen

    # Feed posting is gated by `feed_enabled` (NOT the message-send `enabled`) — a
    # feed-only PPV (messages off) is still pickable here.
    pool = [p for p in all_ppvs if p.get("feed_enabled", True)]
    last = str(stored.get("last_posted_ppv_id") or "")
    # Skip the one posted last time so back-to-back clicks vary; fall back to the
    # full pool when that leaves nothing (e.g. only one feed-enabled PPV).
    choices = [p for p in pool if str(p.get("id")) != last] or pool
    if not choices:
        raise HTTPException(422, "no feed-enabled PPVs to post — add one and turn on 'Post to feed'")
    return random.choice(choices)


class _PostNowBody(BaseModel):
    account_id: str
    ppv_id: str | None = None     # absent → a random ENABLED PPV (skips the last posted)
    # Optional WYSIWYG override: the exact caption shown in a preview step. When set
    # (non-empty), used verbatim — skips pick_feed_caption entirely.
    caption: str | None = None
    # Optional media override: the operator's chosen media in the EXACT order to post.
    # When present & non-empty it OVERRIDES the PPV's media_ids (filtered to the PPV's
    # own media server-side). previews is the operator's chosen free-preview subset,
    # filtered to a ⊆ subset of the effective media set.
    media_files: list[int] | None = None
    previews: list[int] | None = None


@router.post("/admin/ppv-library-config/post-now")
async def post_ppv_to_feed(body: _PostNowBody = Body(...)) -> dict[str, Any]:
    """One-click: post one library PPV to the FEED as a paid post at its BASE price
    (the un-tiered "normal" price the messages start from), via the shared
    `ppv_send.post_to_feed` (feed-voice caption + ⭐ preview + `Post` row), then stamps
    `last_posted_ppv_id` so the random picker varies. No scheduler, no contact guard —
    a feed post is one public drop, not a per-fan message.

    Picks the PPV via the shared `_pick_feed_ppv` helper (explicit `ppv_id`, else a
    random feed-enabled one skipping the last posted). If `caption` is supplied
    (e.g. the exact text a `/post-now/preview` call showed the operator), it is used
    verbatim for a WYSIWYG post-now."""
    assert_account_owned(body.account_id)

    # A feed PPV is a PAID post, and a paid-subscription page has no paid-post
    # lane on OF (service/account_page.py). Refused here rather than in
    # post_to_feed so the operator gets the real reason instead of a 502.
    if await account_page.is_paid_page(body.account_id):
        raise HTTPException(
            409, "this page charges for a subscription — OF has no paid-post lane "
                 "for it. The PPV still sells in DMs.")

    stored = await _load_stored_config(body.account_id)
    chosen = _pick_feed_ppv(stored, ppv_id=body.ppv_id)

    if not _int_list(chosen.get("media_ids"), _MAX_MEDIA):
        raise HTTPException(422, "this PPV has no content to post")

    # Post + record via the shared helper (feed-voice caption priority, ⭐ preview).
    # Thread the operator's chosen media order + preview subset (both filtered to the
    # PPV's own media inside post_to_feed; absent/empty → the PPV's own media/previews).
    res = await post_to_feed(
        body.account_id, chosen, caption=body.caption,
        media_files=body.media_files, previews_override=body.previews,
        bounds=price_bounds(stored))
    if res.get("status") != "ok":
        raise HTTPException(502, f"feed post failed: {res.get('reason') or 'unknown'}")

    # Stamp skip-last onto the LATEST blob, RE-loaded after the (slow) OF post —
    # stamping the pre-post snapshot would clobber a config save that landed
    # mid-post (best-effort; a UI Save resets the marker).
    latest = await _load_stored_config(body.account_id)
    if latest:
        latest["last_posted_ppv_id"] = str(chosen.get("id") or "")
        blob = json.dumps(latest)
        now = datetime.utcnow()
        async with get_session() as s:
            await s.execute(
                sqlite_insert(AccountAiConfig)
                .values(account_id=body.account_id, utc_offset=0,
                        ppv_library_config_json=blob, updated_at=now)
                .on_conflict_do_update(
                    index_elements=["account_id"],
                    set_={"ppv_library_config_json": blob, "updated_at": now})
            )
    log.info("ppv_post_now account=%s ppv=%s of_post=%s price=%s previews=%s",
             body.account_id, chosen.get("id"), res.get("of_post_id"),
             res.get("price"), res.get("preview_count"))
    return {
        "account_id": body.account_id,
        "ppv_id": chosen.get("id"),
        "name": chosen.get("name") or "",
        "of_post_id": res.get("of_post_id"),
        "price": res.get("price"),
        "caption": res.get("caption"),
        "used_feed_caption": res.get("used_feed_caption", False),
        "media_count": res.get("media_count", 0),
        "preview_count": res.get("preview_count", 0),
    }


class _PreviewPostBody(BaseModel):
    account_id: str
    ppv_id: str | None = None     # absent → a random feed-enabled PPV (skips the last posted)


@router.post("/admin/ppv-library-config/post-now/preview")
async def preview_ppv_to_feed(body: _PreviewPostBody = Body(...)) -> dict[str, Any]:
    """Resolve the SAME pick + feed caption + price + media/preview counts that
    `/post-now` would use — WITHOUT calling create_post and WITHOUT stamping
    `last_posted_ppv_id`. Lets the operator see the exact candidate (name, caption,
    price, content counts) before committing; the confirm step re-POSTs the same
    ppv_id + this caption to `/post-now` for a WYSIWYG result."""
    assert_account_owned(body.account_id)

    stored = await _load_stored_config(body.account_id)
    chosen = _pick_feed_ppv(stored, ppv_id=body.ppv_id)

    if not _int_list(chosen.get("media_ids"), _MAX_MEDIA):
        raise HTTPException(422, "this PPV has no content to post")

    lo, hi = price_bounds(stored)
    base_cents = max(lo, min(int(chosen.get("base_price_cents") or 0), hi))
    media_ids = _int_list(chosen.get("media_ids"), _MAX_MEDIA)
    media_set = set(media_ids)
    previews = [x for x in _int_list(chosen.get("preview_options"), _MAX_PREVIEWS) if x in media_set]
    # WYSIWYG means WYSIWYG: this endpoint exists so the operator sees the exact
    # line `/post-now` would send, so it has to resolve the caption on the same two
    # axes that path does. Neither was passed here — `lang` was already missing, so
    # a Spanish account has always previewed English and posted Spanish; `voice`
    # would have previewed her caption and posted his.
    from automations import _language
    from automations._common import load_voice_blocks
    _fp_lang = await _language.load_account_language(body.account_id)
    _fp_v = await load_voice_blocks(body.account_id)
    caption, used_feed_caption = pick_feed_caption(
        chosen, base_cents, _fp_lang, _fp_v.voice)

    return {
        "account_id": body.account_id,
        "ppv_id": chosen.get("id"),
        "name": chosen.get("name") or "",
        "caption": caption,
        "used_feed_caption": used_feed_caption,
        "price": base_cents / 100,
        "media_count": len(media_ids),
        "preview_count": len(previews),
        # True ⇒ /post-now will refuse: a paid-subscription page has no paid-post
        # lane. Surfaced here so the confirm step can say so before it is pressed.
        "paid_page": await account_page.is_paid_page(body.account_id),
        # The candidate's ordered media + the ⭐ free-preview subset — lets the operator
        # reorder/reselect before confirming. previews is always ⊆ media_ids.
        "media_ids": media_ids,
        "previews": previews,
    }
