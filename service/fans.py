"""
fans.py — server-side endpoints for the per-fan drawer / profile view.

Two endpoints, both keyed on (account_id, fan_id):

  GET    /admin/fans/{account_id}/{fan_id}   → full row (creates a stub if
                                               we haven't seen this fan yet)
  PATCH  /admin/fans/{account_id}/{fan_id}   → write custom_nickname, notes,
                                               tags (string list as JSON)

The OF user details (display name, avatar) are intentionally NOT mirrored
here — those go stale, and the inbox already batch-fetches /users/list to
get fresh ones. Our DB stores the human-curated overlay: nickname our team
picked, notes, tags, plus the auto-computed lifetime_spend_cents the event
transcoder will fill once Phase B.3 starts persisting transactions.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from db.engine import get_session
from db.models import (
    Account, Employee, Fan, FanProfile, GrokCall, Message, ScheduledJob, Transaction,
)
from auth import assert_account_owned
import fan_status_copy as copy   # the badge sentences — see its module docstring

# Statuses that mean "this money is gone / never landed" — chargebacks,
# refunds, hard failures. Everything else (cleared AND still-pending payout
# holds) counts as revenue the fan actually paid. Mirrors
# transaction_ingest._NON_REVENUE_STATUSES, kept local so the router doesn't
# import the ingest module. The drawer reads gross-paid, NOT settled-only:
# OF holds new PPV payments pending for ~7 days, so a settled-only filter
# leaves a fresh purchase invisible in the chart for days (see
# transaction_ingest._link_ppv_message / payoutPendingDays).
_NON_REVENUE_STATUSES = ("chargedback", "refunded", "failed")


async def _ensure_account_row(s: AsyncSession, account_id: str) -> None:
    """Insert a minimal accounts row if missing. The FS-based account
    registry (sessions/accounts/<id>/) is the source of truth for who's
    logged in, but the DB `accounts` table only gets populated by
    `import_legacy` or explicit calls. Without a row here, any FK→accounts
    insert (fans, transactions, etc.) raises IntegrityError. Idempotent —
    safe to call on every write path that touches account-scoped tables."""
    stmt = (
        sqlite_insert(Account)
        .values(id=account_id, is_active_default=False)
        .on_conflict_do_nothing(index_elements=["id"])
    )
    await s.execute(stmt)

log = logging.getLogger("of-relay.fans")

# Strong refs to fire-and-forget background tasks so the event loop doesn't GC
# them mid-flight (asyncio only holds weak refs to tasks).
_bg_tasks: set[asyncio.Task] = set()


def _spawn_bg(coro) -> None:
    """Run `coro` detached on the loop, keeping a ref until it finishes."""
    t = asyncio.ensure_future(coro)
    _bg_tasks.add(t)
    t.add_done_callback(_bg_tasks.discard)
router = APIRouter()


def _row_to_dict(f: Fan) -> dict[str, Any]:
    return {
        "account_id": f.account_id,
        "fan_id": f.fan_id,
        "of_username": f.of_username,
        "of_display_name": f.of_display_name,
        "avatar_url": f.avatar_url,
        "custom_nickname": f.custom_nickname,
        "generated_nickname": f.generated_nickname,
        "real_name": f.real_name,
        "his_age": f.his_age,
        "home_country": f.home_country,
        "home_city": f.home_city,
        "hobbies": f.hobbies,
        "fetishes": f.fetishes,
        "self_description": f.self_description,
        "description": f.description,
        "notes": f.notes,
        # Written by automation A03 apply_profiles (our DB only — never pushed to
        # OF; no native OF endpoint exists for fan notes). Surfaced for the drawer.
        "applied_notes": f.applied_notes,
        "applied_notes_at": (
            f.applied_notes_at.isoformat() if f.applied_notes_at else None
        ),
        "tags": _safe_load_list(f.tags),
        "lifetime_spend_cents": f.lifetime_spend_cents,
        "subscription_status": f.subscription_status,
        "subscribed_at": f.subscribed_at.isoformat() if f.subscribed_at else None,
        "last_message_received_at": (
            f.last_message_received_at.isoformat() if f.last_message_received_at else None
        ),
        "source": f.source,
        "is_followed": f.is_followed,
        "language": f.language,
        "language_source": f.language_source,
        # ── §2.4 extended Grok facts ─
        "occupation": f.occupation,
        "employer": f.employer,
        "relationship_status": f.relationship_status,
        "partner_name": f.partner_name,
        "has_kids": f.has_kids,
        "family_names": _safe_load_obj(f.family_names),
        "pets": _safe_load_array(f.pets),
        "recent_events_timeline": _safe_load_array(f.recent_events_timeline),
        "mood_at_last_message": f.mood_at_last_message,
        "sentiment_trend": f.sentiment_trend,
        "relationship_stage": f.relationship_stage,
        "communication_style": _safe_load_obj(f.communication_style),
        "automation_paused_until": (
            f.automation_paused_until.isoformat() if f.automation_paused_until else None
        ),
        # Persona continuity (Tier B) — the three scalars are the RESOLVED answer
        # per topic and are operator-writable. The ledger and the merged view are
        # NOT here: see _with_persona_claims for why they are per-fan only.
        "persona_age_claimed": f.persona_age_claimed,
        "persona_location_claimed": f.persona_location_claimed,
        "persona_job_claimed": f.persona_job_claimed,
        "profile_last_synced_at": (
            f.profile_last_synced_at.isoformat() if f.profile_last_synced_at else None
        ),
        "grok_facts_updated_at": (
            f.grok_facts_updated_at.isoformat() if f.grok_facts_updated_at else None
        ),
        "created_at": f.created_at.isoformat() if f.created_at else None,
        "updated_at": f.updated_at.isoformat() if f.updated_at else None,
    }


def _safe_load_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        v = json.loads(raw)
        return [str(x) for x in v] if isinstance(v, list) else []
    except (ValueError, TypeError):
        return []


def _safe_load_obj(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        v = json.loads(raw)
        return v if isinstance(v, dict) else {}
    except (ValueError, TypeError):
        return {}


def _with_persona_claims(row: dict[str, Any], f: Fan) -> dict[str, Any]:
    """Add the Tier B ledger + merged view to a SINGLE-fan response.

    Deliberately not in `_row_to_dict`: that serializer also backs the fans LIST
    (`{"fans": [...]}`), and only the drawer reads these. Building them there put
    a JSON parse and a merge on every row of a list that can run to thousands,
    for two keys nobody in that response uses.

    Both halves go through `parse_persona_claims`, NOT `_safe_load_array`. That
    parser clips each claim to _CLAIM_TEXT_CLIP, and the merged view is built
    from it — so reading the raw column here instead would hand the client two
    different texts for the same claim, and the drawer (which pairs them by text
    to find superseded versions) would show a long claim as BOTH current and
    struck-through. One parser, one normalisation.

    Lazy import: `automations._persona` pulls the llm stack, which nothing else
    in this module needs — verified, not assumed.
    """
    from automations._persona import parse_persona_claims, resolved_claims
    return {
        **row,
        "persona_claims": parse_persona_claims(f.persona_claims_json),
        "persona_claims_resolved": resolved_claims(
            f.persona_claims_json,
            age_claimed=f.persona_age_claimed,
            location_claimed=f.persona_location_claimed,
            job_claimed=f.persona_job_claimed),
    }


def _safe_load_array(raw: str | None) -> list[Any]:
    if not raw:
        return []
    try:
        v = json.loads(raw)
        return v if isinstance(v, list) else []
    except (ValueError, TypeError):
        return []


_RELATIONSHIP_STATUSES = frozenset({
    "single", "dating", "engaged", "married",
    "divorced", "widowed", "complicated", "unknown",
})


class FanUpdateBody(BaseModel):
    custom_nickname: str | None = Field(None, description="Set null to clear")
    notes: str | None = None
    tags: list[str] | None = None
    real_name: str | None = None
    home_country: str | None = None
    home_city: str | None = None
    his_age: str | None = None
    hobbies: str | None = None
    fetishes: str | None = None
    # ── §2.4 extended Grok facts (T-A4 schema) ─
    occupation: str | None = None
    employer: str | None = None
    relationship_status: str | None = None
    partner_name: str | None = None
    has_kids: bool | None = None
    family_names: dict[str, Any] | None = None
    pets: list[Any] | None = None
    mood_at_last_message: str | None = None
    sentiment_trend: str | None = None
    relationship_stage: str | None = None
    communication_style: dict[str, Any] | None = None
    automation_paused_until: datetime | None = None
    # Persona continuity — humans may correct what gen_info claimed.
    persona_age_claimed: str | None = None
    persona_location_claimed: str | None = None
    persona_job_claimed: str | None = None
    profile_last_synced_at: datetime | None = None
    grok_facts_updated_at: datetime | None = None
    # Per-fan language override (ISO 639-1). Setting it marks language_source='manual'
    # so gen_info's detection can never overwrite it. "" / null clears the override.
    language: str | None = None

    @field_validator("relationship_status")
    @classmethod
    def _check_relationship_status(cls, v: str | None) -> str | None:
        if v is None or v in _RELATIONSHIP_STATUSES:
            return v
        raise ValueError(
            f"relationship_status must be one of {sorted(_RELATIONSHIP_STATUSES)}"
        )

    @field_validator("language")
    @classmethod
    def _check_language(cls, v: str | None) -> str | None:
        if not v:
            return None
        from automations._language import norm_lang
        code = norm_lang(v)
        if not code:
            raise ValueError(f"language {v!r} is not a supported ISO 639-1 code")
        return code


# NOTE: route order matters. FastAPI matches paths in declaration order
# and falls through on path-param TYPE failure — so the literal-third-
# segment routes (`by-ids`, `spend-batch`) MUST be declared BEFORE the
# int-parameter route `/{fan_id}`, otherwise "by-ids" gets matched as
# fan_id, fails int parsing, and returns 422 ("Input should be a valid
# integer"). The single-segment `/admin/fans/{account_id}` route can
# live anywhere — its shape doesn't collide.


@router.get("/admin/fans/{account_id}")
async def list_recent_fans(account_id: str, limit: int = 50) -> dict[str, Any]:
    """Recently-active fans for this account — primarily used by a future
    fan-browser screen. The inbox doesn't call this; it uses OF's chat list."""
    assert_account_owned(account_id)
    limit = max(1, min(int(limit or 50), 200))
    async with get_session() as s:
        q = (
            select(Fan)
            .where(Fan.account_id == account_id)
            .order_by(Fan.last_message_received_at.desc().nullslast())
            .limit(limit)
        )
        rows = (await s.execute(q)).scalars().all()
        return {"fans": [_row_to_dict(r) for r in rows]}


@router.get("/admin/fans/{account_id}/by-ids")
async def fans_by_ids(
    account_id: str,
    ids: str = Query("", description="Comma-separated fan ids (max 200)"),
) -> dict[str, Any]:
    """Bulk identity lookup against our LOCAL SQLite `fans` table.

    Returns `{fans: {fan_id_str: {id, name, username, avatar}}}` for every id
    we have a row for. Missing ids are omitted; the caller decides whether
    to back-fill from OF /users/list (paying the upstream cost) or accept
    the gap. Avatars come from the WS transcoder, which writes them
    whenever a message lands — covers anyone the model has talked to.

    Used by the chat-list enrichment to instantly paint names+avatars
    from local data instead of firing 8 parallel /users/list batches on
    every chats refetch. Cap at 200 ids per call to keep the IN-clause
    scan cheap on the (account_id, fan_id) composite primary key."""
    assert_account_owned(account_id)
    parsed: list[int] = []
    for chunk in ids.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            parsed.append(int(chunk))
        except ValueError:
            continue
    if not parsed:
        return {"fans": {}}
    parsed = parsed[:200]

    out: dict[str, dict[str, Any]] = {}
    async with get_session() as s:
        rows = (await s.execute(
            select(
                Fan.fan_id, Fan.of_username, Fan.of_display_name,
                Fan.avatar_url, Fan.custom_nickname,
            )
            .where(Fan.account_id == account_id, Fan.fan_id.in_(parsed))
        )).all()
        for fid, uname, dname, avatar, nickname in rows:
            out[str(fid)] = {
                "id": int(fid),
                "name": dname,
                "username": uname,
                "avatar": avatar,
                "customNickname": nickname,
            }
    return {"fans": out}


@router.get("/admin/fans/{account_id}/spend-batch")
async def fans_spend_batch(
    account_id: str,
    ids: str = Query("", description="Comma-separated fan ids"),
) -> dict[str, Any]:
    """Bulk spend lookup for ChatList row chips.

    Returns lifetime_spend_cents + last_purchase_at per fan, keyed by
    fan_id (string). Missing fans are omitted (frontend treats absence
    as $0 / no purchase). Caps at 200 ids per call so the index scan
    on (account_id, fan_id) stays cheap."""
    assert_account_owned(account_id)
    parsed: list[int] = []
    for chunk in ids.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            parsed.append(int(chunk))
        except ValueError:
            continue
    if not parsed:
        return {"spend": {}}
    parsed = parsed[:200]

    out: dict[str, dict[str, Any]] = {}
    async with get_session() as s:
        rows = (await s.execute(
            select(
                Fan.fan_id,
                Fan.lifetime_spend_cents,
                Fan.subscription_status,
                Fan.subscription_expires_at,
            )
            .where(Fan.account_id == account_id, Fan.fan_id.in_(parsed))
        )).all()
        for fid, cents, sub_status, sub_expires in rows:
            out[str(fid)] = {
                "spend_cents": int(cents or 0),
                "last_purchase_at": None,
                "subscription_status": sub_status,
                "expired_at": sub_expires.isoformat() if sub_expires else None,
            }

        # Last purchase timestamp — single grouped MAX query.
        tx_rows = (await s.execute(
            select(Transaction.fan_id, func.max(Transaction.occurred_at))
            .where(
                Transaction.account_id == account_id,
                Transaction.fan_id.in_(parsed),
                Transaction.amount_cents > 0,
            )
            .group_by(Transaction.fan_id)
        )).all()
        for fid, ts in tx_rows:
            if fid is None:
                continue
            entry = out.setdefault(str(fid), {
                "spend_cents": 0,
                "last_purchase_at": None,
                "subscription_status": None,
                "expired_at": None,
            })
            entry["last_purchase_at"] = ts.isoformat() if ts else None

    return {"spend": out}


# ── /{fan_id} routes — MUST be declared after every literal-third-segment
#    route above (see ordering note near FanUpdateBody). ────────────────

@router.get("/admin/fans/{account_id}/{fan_id}")
async def get_fan(account_id: str, fan_id: int) -> dict[str, Any]:
    """Return the fan row. Creates an empty stub on first access so the
    drawer always has a row to edit — avoids 404-then-create dance from
    the UI."""
    assert_account_owned(account_id)
    async with get_session() as s:
        f = await s.get(Fan, (account_id, fan_id))
        if f is None:
            await _ensure_account_row(s, account_id)
            f = Fan(account_id=account_id, fan_id=fan_id, source="onlyfans")
            s.add(f)
            await s.commit()
            await s.refresh(f)
        row = _with_persona_claims(_row_to_dict(f), f)
        # Join the gen_info profile so the drawer can show the rich note + openers.
        prof = await s.get(FanProfile, (account_id, fan_id))
    row["profile"] = _profile_block(prof, f.lifetime_spend_cents or 0)
    return row


def _profile_block(prof: "FanProfile | None", lifetime_spend_cents: int) -> dict[str, Any]:
    """The gen_info profile for the drawer's AI-Profile card. `rich_note` is the app-only
    spend-tiered projection of the STORED bullet_points (not the 200-char OF note)."""
    from automations._common import build_rich_note
    if prof is None:
        return {"rich_note": "", "short_bio": None, "nickname": None,
                "q": [], "tease": [], "last_generated_at": None}
    return {
        "rich_note": build_rich_note(prof.bullet_points, prof.short_bio or "", lifetime_spend_cents),
        "short_bio": prof.short_bio,
        "nickname": prof.nickname,
        "q": [x for x in (prof.q1, prof.q2, prof.q3) if x],
        "tease": [x for x in (prof.tease1, prof.tease2, prof.tease3) if x],
        "last_generated_at": prof.last_generated_at.isoformat() if prof.last_generated_at else None,
    }


@router.get("/admin/fans/{account_id}/{fan_id}/ai-status")
async def get_fan_ai_status(account_id: str, fan_id: int) -> dict[str, Any]:
    """What is the AI doing with THIS fan right now — and if nothing, why, and until when.

    Answering that used to mean SSH-ing into prod and reading four tables by hand
    (fans / skip_list / rhythm_state / ladder_state), which is how a 23-minute rhythm
    break on the hottest thread on the roster went unnoticed for an hour.

    The gates are evaluated in the SAME ORDER `ai_chatter.run()` checks them and the
    FIRST one that fires is returned, so the badge names the thing that actually stopped
    her — not merely a thing that is true. It reads the engine's own state and helpers
    (never a hand-rolled second copy of the logic): a status light that drifts from the
    engine is worse than no status light.

    Read-only. No sends, no writes, no OF calls.
    """
    assert_account_owned(account_id)
    # Imported here, not at module import: fans.py is loaded by the relay on boot and
    # ai_chatter pulls in the LLM client + the whole offer engine behind it.
    from automations import ai_chatter as ac
    from automations import upsell

    now = datetime.utcnow()
    cfg = await ac._load_config(account_id)
    engine_on = bool(cfg.get("enabled"))
    gate_on = bool(cfg.get("qualification_gate_enabled"))

    async with get_session() as s:
        fan = (await s.execute(select(Fan).where(
            Fan.account_id == str(account_id), Fan.fan_id == int(fan_id)))).scalar_one_or_none()

    from automations import _language as _lang
    _lang_resolved = _lang.resolve_language(
        await _lang.load_account_language(account_id),
        fan.language if fan is not None else None)

    blacklist, skip_reasons = await ac._load_stop_lists(account_id)
    reason = skip_reasons.get(int(fan_id))
    ladders = await ac._load_ladders(account_id, [int(fan_id)])
    rstates = await ac._load_rhythm(account_id, [int(fan_id)])
    lad = ladders.get(int(fan_id))
    rst = rstates.get(int(fan_id))
    human = (await ac._human_money_signals(account_id, [int(fan_id)], now)).get(
        int(fan_id), (None, None))
    open_offers = await ac._open_offers(account_id, int(fan_id))

    def _iso(dt: datetime | None) -> str | None:
        return dt.isoformat() + "Z" if dt is not None else None

    def _future(dt: datetime | None) -> datetime | None:
        """A deadline still ahead of us, else nothing — the builders take facts, not
        comparisons, so 'is this state live' is decided once, here."""
        return dt if dt is not None and dt > now else None

    # ── The gates, in run()'s order. First hit wins: a later gate may only speak if
    # nothing before it did. The SENTENCES live in fan_status_copy.
    badge = copy.standing_badge(
        engine_on=engine_on,
        blacklisted=int(fan_id) in blacklist,
        # ⚠️ ASK THE ENGINE, never re-list the exemptions here. This line used to spell
        # out `reason not in _GRADUATION_SKIPS` and stopped there, missing the
        # engage_old_fans lift — so 96 fans on one account showed "Skipped
        # (old_fan_pre_ai)" while ai_chatter was engaging them, and because the badge
        # returns on its first hit, their REAL state (a rhythm break) became
        # unreachable. Exactly the drift this function's docstring forbids.
        skip_reason=(reason if ac.skip_reason_blocks(
            reason, engage_old_fans=bool(cfg.get("engage_old_fans"))) else None),
        rhythm_wake_at=_future(rst.wake_at if rst is not None else None),
        paused_until=_future(fan.automation_paused_until if fan is not None else None),
        companion_until=_future(lad.companion_until if lad is not None else None),
        post_buy_until=_future(lad.cooldown_until if lad is not None else None))

    # The engine that owns this fan. A payer welcome_chatter_for_info graduated ('spent') belongs to
    # the upseller — that's precisely the fan it exists for.
    engine = "ai_chatter"
    if not engine_on:
        engine = "none"
    elif open_offers or human[0] is not None or (
            lad is not None and lad.status in (upsell.STATUS_OPEN, upsell.STATUS_HOT)):
        engine = "ai_upseller"

    # ── The reply budget. NOT the same thing as a break, and conflating them would be
    # a lie: the CAP is deterministic (she goes quiet after N replies in a burst, until
    # a session-gap of silence resets it or a buying signal upgrades her tier), while a
    # BREAK is a random "she's a person" pause that only ever rolls on a COLD thread.
    # A live ask or a recent purchase makes the thread break-proof outright.
    cadence_on = bool(cfg.get("cadence_enabled"))
    gap_min = int(cfg.get("session_gap_minutes") or 60)
    # ONE gather for the whole endpoint. It was called twice (cadence + gate), and
    # _gather is a full-thread scan with no LIMIT — two of them per 15s poll, per open
    # chat, on the same to_thread pool that serves live OF sends (see the relay
    # threadpool-starvation note). `session_gap_min` only populates session_out_n; every
    # field the gate reads is identical, so the cadence gather is a strict superset.
    # Item 21c — the daily quota sits above the burst cap and needs its own (rolling
    # 24h) reply count, so ask the same single gather for it rather than adding a pass.
    quota_on = cadence_on and bool(cfg.get("daily_quota_enabled"))
    cand = None
    if cadence_on or gate_on:
        cand = (await ac._gather(
            account_id, {int(fan_id)}, session_gap_min=gap_min,
            day_window=ac.QUOTA_WINDOW if quota_on else None)).get(int(fan_id))
    cadence: dict[str, Any] = {"enabled": cadence_on}
    if cadence_on and cand is not None:
        # ASK the leash; never re-run it here. This was eight calls inlined in a
        # specific order — spend windows, both gates, the burst gate's `tier`
        # threaded into the quota gate — and every time that pipeline was retyped
        # the drawer drifted from the bot. `read_leash` is the one home for the
        # ordering; this endpoint's job is to RENDER the verdict, nothing else.
        leash = await ac.read_leash(
            account_id, int(fan_id), cand, cfg=cfg, now=now,
            pending=(open_offers[0] if open_offers else None))
        cadence = copy.burst_payload(
            tier=leash.tier, used=leash.used, cap=leash.cap, stopped=leash.stopped,
            gap_min=gap_min, last_message_at=leash.last_message_at)
        if badge.state == "active":
            badge = copy.burst_badge(stopped=leash.stopped, tier=leash.tier,
                                     used=leash.used, cap=leash.cap,
                                     gap_min=gap_min) or badge
        # The DAILY ceiling (item 21c). In shadow mode the hold is computed but not
        # served, and the UI must say so rather than claim she's paused when she isn't.
        if leash.quota is not None:
            enforce = bool(cfg.get("daily_quota_enforce"))
            # `cand.her_last_at` is HER OWN last reply (ai_chatter / ai_upseller only —
            # a human's manual send and a mass row are both excluded upstream), which
            # is the anchor the hold is actually measured from. Reading the last
            # OUTBOUND row here instead would answer a different question and be wrong
            # by days on any thread a person has typed into.
            free_at = copy.quota_free_at(leash.quota, cand.her_last_at)
            cadence["daily"] = copy.daily_payload(leash.quota, enforced=enforce,
                                                  cad=cfg, free_at=free_at)
            if badge.state == "active":
                badge = copy.daily_quota_badge(leash.quota, enforced=enforce,
                                               cad=cfg, free_at=free_at) or badge

    # ── The conversational teaser: how many of HIS messages until the next one, and
    # at what price. `convo_teaser_forecast` runs the sender's own pricing policy, so
    # this cannot quote a number the engine would not send — but it stops before
    # choosing PHOTOS, which costs OF folder resolution and a dedup read. This
    # endpoint promises no OF calls.
    teaser: dict[str, Any] | None = None
    from automations import tip_reward as _tip_reward
    tcfg = await _tip_reward.convo_teaser_config(account_id)
    if tcfg and fan is not None:
        tstate = _tip_reward.teaser_state(fan)
        since_at: datetime | None = None
        if tstate.get("at"):
            try:
                since_at = datetime.fromisoformat(str(tstate["at"]))
            except (TypeError, ValueError):
                since_at = None
        # The sender HALVES the gap to 10 when she has no offer working (ai_chatter
        # ~L5425), which is the difference between "in 3" and "in 13" on the strip.
        # Mirrored on `pending` only: the other half of its condition is whether she
        # is about to make an offer THIS turn, which exists solely inside a run.
        if not open_offers:
            tcfg = {**tcfg, "after": min(int(tcfg.get("after") or 20), 10)}
        last_px = int(tstate.get("last_price") or 0)
        sold = False
        if tcfg.get("adaptive") and last_px > 0 and tstate.get("last_msg"):
            sold = await ac._teaser_sold(account_id, int(fan_id), int(tstate["last_msg"]))
        teaser = _tip_reward.convo_teaser_forecast(
            tcfg=tcfg, state=tstate,
            msgs_since=await ac._fan_msgs_since(account_id, int(fan_id), since_at),
            last_sold=sold,
            max_paid_cents=((await ac._paid_ppv_facts(account_id, int(fan_id)))[0]
                            if tcfg.get("adaptive") else 0))

    # Why she can't put a price in front of him even when she's talking.
    caps_ok = await ac._offer_caps_ok(account_id, int(fan_id), cfg) if gate_on else None

    # (1) The next thing already SCHEDULED for this fan — a deferred reply is pending
    # work, not silence, and it looked exactly like silence until now.
    #
    # The LIKE is only a PREFILTER, and it must never be trusted as the answer: payloads
    # carry other numbers (`post_id: 2592062075`, `account_id: "25166249"`), so a bare
    # `%{fan_id}%` substring match hands you a job belonging to a different fan whenever
    # his id happens to appear inside one of them. Prefilter in SQL, then PARSE the JSON
    # and confirm the id sits in a fan-scoped field. A status badge that confidently
    # reports another fan's job is worse than one that reports nothing.
    _FAN_KEYS = ("only_fan_ids", "force_ids", "refill_ids", "fan_ids", "nudge_fan_ids")
    # Only jobs that CAN be fan-scoped — a bulk sweep (auto_stories, scrape_chats without
    # ids, mass sends) is never "this fan's next action" even if his id appears somewhere
    # in its payload. Combined with the JSON parse below, this is what stops another
    # fan's job showing up on his strip. (tip_reward IS fan-scoped — on_inbound_tip.)
    _FAN_KINDS = ("ai_chatter", "welcome_chatter_for_info", "send_welcome", "send_followup",
                  "autoreply", "gen_info", "tip_reward")

    def _has_fan(p: dict, fid: int) -> bool:
        # EVERY coercion is guarded — a hand-inserted / legacy payload with a string id
        # or a scalar where a list is expected must not 500 the drawer's 15s poll.
        try:
            if int(p.get("fan_id") or 0) == fid:
                return True
        except (TypeError, ValueError):
            pass
        for k in _FAN_KEYS:
            v = p.get(k)
            if not isinstance(v, (list, tuple)):
                continue
            for x in v:
                try:
                    if int(x) == fid:
                        return True
                except (TypeError, ValueError):
                    continue
        return False

    async with get_session() as s:
        rows = (await s.execute(
            select(ScheduledJob.kind, ScheduledJob.run_at, ScheduledJob.payload_json)
            .where(ScheduledJob.account_id == str(account_id),
                   ScheduledJob.status.in_(("pending", "running")),
                   ScheduledJob.kind.in_(_FAN_KINDS),
                   ScheduledJob.payload_json.like(f"%{int(fan_id)}%"))
            .order_by(ScheduledJob.run_at.asc()).limit(25))).all()
    next_action = None
    for kind, run_at, payload in rows:
        try:
            p = json.loads(payload or "{}")
        except Exception:
            continue
        if isinstance(p, dict) and _has_fan(p, int(fan_id)):
            next_action = {"kind": kind, "at": _iso(run_at)}
            break

    # (2) THE GATE — would she be allowed to put a price in front of him this turn, and
    # if not, what refused. "She's talking but won't sell" had no answer before this.
    gate: dict[str, Any] = {"enabled": gate_on}
    if gate_on:
        if cand is not None:  # reused from the single gather above
            ok, why = upsell.qualify(upsell.GateCtx(
                fan_id=int(fan_id), last_inbound_text=cand.last_body,
                last_inbound_at=cand.last_in_at, last_outbound_at=cand.last_out_at,
                fan_spoke_last=(cand.last_dir == "in"),
                status=(lad.status if lad is not None else upsell.STATUS_IDLE),
                offers_paused_until=(lad.offers_paused_until if lad is not None else None),
                last_ask_at=ac._newest(
                    lad.last_ask_at if lad is not None else None, human[0]),
            ), now)
            gate = {"enabled": True, "ok": bool(ok), "why": (None if ok else why)}

    # (3) The 7-day spend brake — past the cap he is not silenced, he stops being
    # CHARGED (COMPANION for the window). Silent until you know to look for it.
    cap7 = int(cfg.get("spend_velocity_cap_7d_cents") or upsell.SPEND_VELOCITY_CAP_7D)
    paid7 = await ac._paid_cents_7d(account_id, int(fan_id), now) if gate_on else 0
    spend_7d = {"paid_cents": int(paid7), "cap_cents": cap7 or None,
                "capped": bool(cap7 and paid7 >= cap7)}

    # (4) Is she even THINKING about him — the last LLM call on this fan.
    # NB: grok_calls.cost_cents actually holds MILLICENTS (cents x100); stats.py
    # relabels it. Divide, or every cost on this strip is 100x too big.
    async with get_session() as s:
        call = (await s.execute(
            select(GrokCall.called_at, GrokCall.model, GrokCall.purpose,
                   GrokCall.cost_cents)
            .where(GrokCall.account_id == str(account_id),
                   GrokCall.fan_id == int(fan_id))
            .order_by(GrokCall.called_at.desc()).limit(1))).first()
        spend_today = (await s.execute(
            select(func.count(), func.coalesce(func.sum(GrokCall.cost_cents), 0))
            .where(GrokCall.account_id == str(account_id),
                   GrokCall.fan_id == int(fan_id),
                   GrokCall.called_at >= now - timedelta(days=1)))).first()
    llm = {
        "last_at": _iso(call[0]) if call else None,
        "model": call[1] if call else None,
        "purpose": call[2] if call else None,
        "calls_24h": int(spend_today[0] or 0) if spend_today else 0,
        "cost_24h_cents": round(float(spend_today[1] or 0) / 100.0, 3) if spend_today else 0.0,
    }

    return {
        "state": badge.state,               # active | paused | companion | blocked | off
        "label": badge.label,
        "detail": badge.detail,
        "until": badge.until,
        "engine": engine,
        "graduated": reason if reason in ac._GRADUATION_SKIPS else None,
        "cadence": cadence,
        "teaser": teaser,
        "offer_caps_ok": caps_ok,
        "next_action": next_action,
        "gate": gate,
        "spend_7d": spend_7d,
        "llm": llm,
        # An ask in the last _ASK_BREAKPROOF_WINDOW (30 min), or a purchase in the last
        # hour, suppresses the random "she stepped away" roll — a ladder stranded
        # mid-sell is the worst outcome in the system. Past 30 minutes she is allowed to
        # be a person again. Computed with the ENGINE's own predicate so the badge can
        # never drift from what she'll actually do.
        "break_proof": bool(
            ac._breakproof(ac._newest(
                human[0], open_offers[0].offered_at if open_offers else None), now)
            or (human[1] is not None and (now - human[1]).total_seconds() <= 3600)),
        "ladder": {
            "status": (lad.status if lad is not None else "idle"),
            "rung": int(lad.rung_index or 0) if lad is not None else 0,
        },
        # The language she writes to THIS fan (resolved: per-fan override → account
        # default → en) + where it came from, so the strip can show a 🌐 chip.
        "language": _lang_resolved,
        "language_source": (
            fan.language_source if (fan is not None and fan.language) else "account"),
        # A live ask he hasn't paid — OURS or a human chatter's. This is what makes the
        # thread break-proof, and what the strip shows as "🔒 $45 ask open".
        "open_ask": (
            {"price_cents": int(open_offers[0].price_cents or 0),
             "at": _iso(open_offers[0].offered_at), "by": "ai"}
            if open_offers else
            {"price_cents": None, "at": _iso(human[0]), "by": "human"}
            if human[0] is not None else None
        ),
        "last_paid_at": _iso(human[1]),
        "force_ask": bool(cfg.get("force_ask")) and gate_on,
    }


_LINE_SLOTS = ("q1", "q2", "q3", "tease1", "tease2", "tease3")


@router.get("/admin/fans/{account_id}/{fan_id}/lines")
async def get_fan_lines(account_id: str, fan_id: int) -> dict[str, Any]:
    """The fan's saved conversation openers from gen_info — questions (Q1-3) and
    teases (Tease1-3) — powering the composer's 'Lines' picker. Empty slots are
    omitted; each item carries its `slot` so the UI can consume it after sending."""
    assert_account_owned(account_id)
    questions: list[dict[str, str]] = []
    teases: list[dict[str, str]] = []
    async with get_session() as s:
        p = await s.get(FanProfile, (account_id, fan_id))
        if p is not None:
            for slot in ("q1", "q2", "q3"):
                v = (getattr(p, slot, None) or "").strip()
                if v:
                    questions.append({"slot": slot, "text": v})
            for slot in ("tease1", "tease2", "tease3"):
                v = (getattr(p, slot, None) or "").strip()
                if v:
                    teases.append({"slot": slot, "text": v})
    return {"questions": questions, "teases": teases}


@router.delete("/admin/fans/{account_id}/{fan_id}/lines/{slot}")
async def consume_fan_line(account_id: str, fan_id: int, slot: str) -> dict[str, Any]:
    """Delete (null) ONE used line so it's never offered twice. `slot` ∈ the six
    Q/Tease columns. Idempotent: a missing profile / already-empty slot still 200s."""
    assert_account_owned(account_id)
    if slot not in _LINE_SLOTS:
        raise HTTPException(status_code=400, detail=f"bad line slot: {slot!r}")
    async with get_session() as s:
        p = await s.get(FanProfile, (account_id, fan_id))
        deleted = False
        class_emptied = False
        if p is not None and (getattr(p, slot, None) or ""):
            setattr(p, slot, None)
            await s.commit()
            deleted = True
            # After nulling, did the consumed slot's whole class empty out? (all three
            # Q slots — or all three Tease slots — now null.) If so, the picker has no
            # more openers of that kind for this fan.
            cls = ("q1", "q2", "q3") if slot.startswith("q") else ("tease1", "tease2", "tease3")
            class_emptied = not any((getattr(p, s_, None) or "").strip() for s_ in cls)
    # Refill the emptied class via a GATED one-fan gen_info (refill_ids, NOT force_ids):
    # it only regenerates if there are new inbound messages since the last gen, so an
    # idle fan's lines stay empty until they write again — no wasted LLM call.
    if deleted and class_emptied:
        try:
            import automation_executor as ax  # lazy: avoid an import cycle at load
            await ax.enqueue_job(account_id, "gen_info", payload={"refill_ids": [fan_id]})
        except Exception:
            log.warning("line-refill enqueue failed account=%s fan=%s",
                        account_id, fan_id, exc_info=True)
    return {"ok": True, "slot": slot, "deleted": deleted}


@router.post("/admin/fans/{account_id}/{fan_id}/lines/generate")
async def generate_fan_lines(account_id: str, fan_id: int) -> dict[str, Any]:
    """Manually kick off a FRESH batch of lines for one fan — the composer's
    'Generate new batch' button. Unlike the consume-triggered refill (refill_ids,
    gated on new inbound messages) this FORCES a regen (force_ids): the chatter
    explicitly wants new openers now, so it bypasses the stale/new-message gates.
    Enqueues a gen_info job; the executor claims it on its next ~30s tick and the
    picker polls /lines for the result."""
    assert_account_owned(account_id)
    try:
        import automation_executor as ax  # lazy: avoid an import cycle at load
        job_id = await ax.enqueue_job(account_id, "gen_info", payload={"force_ids": [fan_id]})
        # Don't make the chatter wait for the supervisor's next ~30s tick: kick a
        # run_once NOW. It atomically CLAIMS the just-enqueued job (so the supervisor
        # can't double-run it) and its per-(account, kind) lock no-ops if a gen_info
        # for this account is already in flight — the queued job is picked up then.
        _spawn_bg(ax.run_once(account_id, "gen_info"))
    except Exception:
        log.warning("line-generate enqueue failed account=%s fan=%s",
                    account_id, fan_id, exc_info=True)
        raise HTTPException(status_code=500, detail="failed to enqueue line generation")
    return {"ok": True, "job_id": job_id}


@router.get("/admin/fans/{account_id}/{fan_id}/ppv-history")
async def fan_ppv_history(
    account_id: str,
    fan_id: int,
    limit: int = Query(12, ge=1, le=200),
) -> dict[str, Any]:
    """Recent PPV unlocks by this fan — powers the spend chart in the
    fan drawer (max/avg + per-purchase price points).

    Source: the `transactions` ledger (NOT the `messages` table). The
    messages table only carries rows from after we started watching the
    account; the transactions ledger is backfilled by the Phase F
    payouts ingest, so it has historical purchases too — without it,
    long-tenure fans look empty in the chart.

    Filter: `kind='ppv_message'`, `amount_cents > 0`, and status NOT IN
    the non-revenue set (chargeback/refund/failed). We show gross-paid,
    NOT settled-only: OF holds new PPV payments in a ~7-day pending payout
    window, so a `status='cleared'`-only filter would hide a just-bought
    PPV from the chart for days. Tips are kind='tip' and so are excluded
    by the filter. Ordered newest first via the (account_id, fan_id,
    occurred_at) partial index, then reversed so the response is
    chronological (oldest → newest) — the chart reads left to right."""
    assert_account_owned(account_id)
    async with get_session() as s:
        # Outer-join on messages so we get sent_by_employee_id + the
        # original sent_at for rows where the transaction carries a
        # message_id (i.e. WS-pump-originated tx). Phase-F-ingested
        # ledger rows (message_id NULL) just yield NULLs here — the
        # frontend enriches those via /chats/{fan_id}/media opened=1.
        rows = (await s.execute(
            select(
                Transaction.id,
                Transaction.message_id,
                Transaction.amount_cents,
                Transaction.net_cents,
                Transaction.fee_cents,
                Transaction.vat_cents,
                Transaction.occurred_at,
                Message.created_at.label("sent_at"),
                Message.sent_by_employee_id,
                Employee.display_name.label("employee_name"),
                Employee.color.label("employee_color"),
            )
            .select_from(Transaction)
            .outerjoin(
                Message,
                (Message.account_id == Transaction.account_id)
                & (Message.fan_id == Transaction.fan_id)
                & (Message.message_id == Transaction.message_id),
            )
            .outerjoin(Employee, Employee.id == Message.sent_by_employee_id)
            .where(
                Transaction.account_id == account_id,
                Transaction.fan_id == fan_id,
                Transaction.kind == "ppv_message",
                Transaction.amount_cents > 0,
                Transaction.status.notin_(_NON_REVENUE_STATUSES),
            )
            .order_by(Transaction.occurred_at.desc())
            .limit(limit)
        )).all()

    items = [
        {
            "transaction_id": int(r.id),
            "message_id": int(r.message_id) if r.message_id is not None else None,
            "price_cents": int(r.amount_cents or 0),
            "net_cents": int(r.net_cents) if r.net_cents is not None else None,
            "fee_cents": int(r.fee_cents) if r.fee_cents is not None else None,
            "vat_cents": int(r.vat_cents) if r.vat_cents is not None else None,
            "purchased_at": r.occurred_at.isoformat() if r.occurred_at else None,
            "sent_at": r.sent_at.isoformat() if r.sent_at else (
                r.occurred_at.isoformat() if r.occurred_at else None
            ),
            "sent_by_employee_id": (
                int(r.sent_by_employee_id) if r.sent_by_employee_id is not None else None
            ),
            "employee_name": r.employee_name,
            "employee_color": r.employee_color,
        }
        for r in reversed(rows)
    ]
    # Per-kind lifetime totals for the drawer's stats grid. OF's
    # subscribedOnData.{tips,messages,posts}Summ is the primary source,
    # but it's frequently missing on long-tenure / banned / hidden
    # accounts — fall back to our ledger so the grid never shows "—" for
    # a fan who clearly has activity. Sum over ALL transactions (not just
    # the limited PPV list above); counts gross-paid (`amount > 0`,
    # status NOT chargeback/refund/failed) so pending payouts are included
    # — consistent with the chart above.
    async with get_session() as s2:
        breakdown_rows = (await s2.execute(
            select(Transaction.kind, func.sum(Transaction.amount_cents))
            .where(
                Transaction.account_id == account_id,
                Transaction.fan_id == fan_id,
                Transaction.amount_cents > 0,
                Transaction.status.notin_(_NON_REVENUE_STATUSES),
            )
            .group_by(Transaction.kind)
        )).all()
    tips_cents = 0
    messages_cents = 0
    posts_cents = 0
    for kind, total in breakdown_rows:
        cents = int(total or 0)
        if kind in ("tip", "tip_post", "tip_stream"):
            tips_cents += cents
        elif kind == "ppv_message":
            messages_cents += cents
        elif kind == "ppv_post":
            posts_cents += cents
        # other kinds (subscription, rebill, ppv_stream, unknown) aren't
        # surfaced in the drawer's grid right now — they're aggregated in
        # the `Lifetime spend` figure via subscribedOnData.totalSumm.

    if items:
        prices = [it["price_cents"] for it in items]
        summary = {
            "count": len(prices),
            "max_cents": max(prices),
            "avg_cents": sum(prices) // len(prices),
            "tips_cents": tips_cents,
            "messages_cents": messages_cents,
            "posts_cents": posts_cents,
        }
    else:
        summary = {
            "count": 0, "max_cents": 0, "avg_cents": 0,
            "tips_cents": tips_cents,
            "messages_cents": messages_cents,
            "posts_cents": posts_cents,
        }
    return {"items": items, "summary": summary}


@router.patch("/admin/fans/{account_id}/{fan_id}")
async def update_fan(
    account_id: str, fan_id: int, body: FanUpdateBody = Body(...),
) -> dict[str, Any]:
    """Partial update. Only fields explicitly present in the body are
    written; null is a deliberate clear, omission is "leave alone"."""
    assert_account_owned(account_id)
    payload = body.model_dump(exclude_unset=True)
    async with get_session() as s:
        f = await s.get(Fan, (account_id, fan_id))
        if f is None:
            await _ensure_account_row(s, account_id)
            f = Fan(account_id=account_id, fan_id=fan_id, source="onlyfans")
            s.add(f)
        for k, v in payload.items():
            if k == "tags":
                f.tags = json.dumps(v or [])
            elif k == "family_names":
                f.family_names = json.dumps(v or {})
            elif k == "communication_style":
                f.communication_style = json.dumps(v or {})
            elif k == "pets":
                f.pets = json.dumps(v or [])
            elif k == "language":
                # An operator-set language is authoritative: mark it 'manual' so
                # gen_info detection never overwrites it (or clear the override).
                f.language = v or None
                f.language_source = "manual" if v else None
            else:
                setattr(f, k, v)
        f.updated_at = datetime.utcnow()
        await s.commit()
        await s.refresh(f)
        # The drawer writes this straight into its query cache, so the PATCH
        # response has to carry the claims too — without them, correcting one
        # would blank the panel until the next refetch.
        return _with_persona_claims(_row_to_dict(f), f)
