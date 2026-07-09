"""service/ppv_library_config_api.py — read/write account_ai_config.ppv_library_config_json.

The "PPV Library" tab persists a per-account store of premade PPVs through these
owner-gated routes. On save it ALSO syncs the automation rules: one `ppv_send`
AutomationRule per enabled PPV, cadence `every_seconds = 604800 / sends_per_week`.
The rule materializer is the scheduler — there is no rotator.

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
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from auth import assert_account_owned
from db.engine import get_session
from db.models import AccountAiConfig, AutomationRule
from automations.ppv_send import (
    PPV_CAPTION_POOLS,
    PPV_FEED_CAPTION_POOLS,
    RECENCY_BANDS,
    SPEND_BANDS,
    _DEFAULTS,
    _PRICE_CEIL_CENTS,
    _PRICE_FLOOR_CENTS,
    pick_feed_caption,
    post_to_feed,
    price_bounds,
)

log = logging.getLogger("of-relay.ppv_library_config_api")

router = APIRouter()

_MAX_PPVS = 50
_MAX_MEDIA = 50
_MAX_PREVIEWS = 10
_MAX_CAPTION_TEXTS = 20
_CAPTION_MAX = 1500     # long multi-paragraph PPV copy (the "screenshot" look) fits
_NAME_MAX = 60
_WEEK_SECONDS = 7 * 24 * 60 * 60
_MIN_EVERY_S = 3600                 # never faster than hourly per PPV
_MAX_EVERY_S = 30 * 24 * 60 * 60    # the rules-engine ceiling


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
    # to its slot. 0/absent = that cap is off.
    caps = cfg.get("ppv_caps")
    if isinstance(caps, dict):
        clean_caps: dict[str, int] = {}
        for k in ("per_day", "per_week", "per_month"):
            try:
                v = int(caps.get(k) or 0)
            except (TypeError, ValueError):
                v = 0
            if v > 0:
                clean_caps[k] = min(v, 10_000)
        if clean_caps:
            out["ppv_caps"] = clean_caps
    # Global price limits: every COMPUTED send/post price (per-cell tier, the
    # everyone-broadcast, feed posts) is clamped into [min, max] at RUNTIME —
    # authored base prices are never rewritten. Always emitted (self-describing
    # blob); price_bounds applies the defaults ($3/$200) + OF hard limits + order.
    out["price_min_cents"], out["price_max_cents"] = price_bounds(cfg)
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


def _every_seconds(sends_per_week: int) -> int:
    return max(_MIN_EVERY_S, min(round(_WEEK_SECONDS / max(1, sends_per_week)), _MAX_EVERY_S))


async def _sync_rules(s, account_id: str, cfg: dict) -> dict[str, int]:
    """One `ppv_send` rule per PPV. Match existing rules by the ppv_id baked into
    steps_json (so a rename never orphans a rule). Enabled rule iff master-enabled
    AND the PPV is enabled; removed PPVs' rules are disabled (not deleted — keeps
    any in-flight job's FK intact)."""
    master = bool(cfg.get("enabled"))
    qh = cfg.get("quiet_hours")
    quiet_json = json.dumps(qh) if isinstance(qh, list) and len(qh) == 2 else None
    existing = (await s.execute(
        select(AutomationRule).where(
            AutomationRule.account_id == account_id,
            AutomationRule.kind == "ppv_send",
        )
    )).scalars().all()
    by_ppv: dict[str, AutomationRule] = {}
    for r in existing:
        try:
            pid = str((json.loads(r.steps_json or "{}") or {}).get("ppv_id") or "")
        except Exception:
            pid = ""
        if pid:
            by_ppv[pid] = r

    stats = {"created": 0, "updated": 0, "disabled": 0}
    desired_ids: set[str] = set()
    for p in cfg.get("ppvs") or []:
        pid = p["id"]
        desired_ids.add(pid)
        trigger = json.dumps({"every_seconds": _every_seconds(p["sends_per_week"])})
        steps = json.dumps({"account_id": account_id, "ppv_id": pid})
        enable = master and p.get("enabled", True)
        name = f"PPV: {p.get('name') or pid}"[:_NAME_MAX]
        r = by_ppv.get(pid)
        if r is not None:
            r.trigger_json = trigger
            r.steps_json = steps
            r.name = name
            r.is_enabled = enable
            r.quiet_hours_json = quiet_json
            stats["updated"] += 1
        else:
            s.add(AutomationRule(
                account_id=account_id, name=name, kind="ppv_send",
                trigger_json=trigger, steps_json=steps, is_enabled=enable,
                quiet_hours_json=quiet_json))
            stats["created"] += 1
    for pid, r in by_ppv.items():
        if pid not in desired_ids and r.is_enabled:
            r.is_enabled = False
            stats["disabled"] += 1
    return stats


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


@router.get("/admin/ppv-library-config")
async def get_ppv_library_config(account_id: str = Query(...)) -> dict[str, Any]:
    assert_account_owned(account_id)
    async with get_session() as s:
        row = await s.get(AccountAiConfig, account_id)
    stored: dict = {}
    if row is not None and row.ppv_library_config_json:
        try:
            stored = json.loads(row.ppv_library_config_json) or {}
        except Exception:
            stored = {}
    return {
        "account_id": account_id,
        "config": stored,
        "defaults": dict(_DEFAULTS),
        "pools": sorted(PPV_CAPTION_POOLS),
        "caption_pools": PPV_CAPTION_POOLS,   # key → lines, for the UI caption preview
        "feed_pools": sorted(PPV_FEED_CAPTION_POOLS),
        "feed_caption_pools": PPV_FEED_CAPTION_POOLS,   # public-voice feed caption styles
        "matrix": _matrix_view(),
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
    bounds = price_bounds(draft)
    return {"account_id": body.account_id,
            **await segment_preview(body.account_id, int(body.base_price_cents or 0), bounds)}


async def _load_stored_config(account_id: str) -> dict:
    async with get_session() as s:
        row = await s.get(AccountAiConfig, account_id)
    stored: dict = {}
    if row is not None and row.ppv_library_config_json:
        try:
            stored = json.loads(row.ppv_library_config_json) or {}
        except Exception:
            stored = {}
    return stored


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
    caption, used_feed_caption = pick_feed_caption(chosen, base_cents)

    return {
        "account_id": body.account_id,
        "ppv_id": chosen.get("id"),
        "name": chosen.get("name") or "",
        "caption": caption,
        "used_feed_caption": used_feed_caption,
        "price": base_cents / 100,
        "media_count": len(media_ids),
        "preview_count": len(previews),
        # The candidate's ordered media + the ⭐ free-preview subset — lets the operator
        # reorder/reselect before confirming. previews is always ⊆ media_ids.
        "media_ids": media_ids,
        "previews": previews,
    }
