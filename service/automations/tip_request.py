"""service/automations/tip_request.py — nudge a quiet mass-buyer with a free
teaser image + an ask-for-a-tip caption (backlog item 42).

Trigger (SWEEP): a fan BOUGHT a mass PPV inside the window and has NOT replied
since — the money's warm but the conversation went cold, so we send ONE free
vault image (the account's GLOBAL DEFAULT tip-request teaser) with a caption
that asks for a tip. No LLM, no per-fan generation: one configured image + one
caption. The purchase signal is `messages.is_paid` (flipped by the ledger's
`transaction_ingest._link_ppv_message` — the only reliable PPV-unlock signal);
"mass" = the buy rode a broadcast (`mass_run_id IS NOT NULL`).

Design constraints (lane L7):
  • Config lives NESTED under `account_ai_config.tip_reward_config_json`
    ['tip_request'], NOT its own column — item 42's lane must not touch the
    cross-lane `models.py` schema seam, and the tip_reward `image_reply` flags
    already precedent sharing that column. Persisted/validated via
    tip_reward_config_api._validate_tip_request.
  • Per-fan send dedup is a cooldown stamp in `fans.custom_fields['_tip_request']`
    — namespaced, migration-free, mirroring tip_reward's `_image_reply` stamp.

Ships DISABLED (enabled=false, no media_id). A creator enables it and picks the
global tip-request image; a `tip_request` automation_rule with a cadence trigger
fires this sweep.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta

import automation_executor as ax        # _make_client seam
from attribution import write_outbound_attribution
from audiences import contact_guard_excludes
from automation_registry import register
from automations._common import (
    apply_word_restriction, load_hard_skip_ids, should_skip_muted_creator,
)
from db.engine import get_session
from db.models import AccountAiConfig, Fan, Message
from sqlalchemy import func, select

log = logging.getLogger("of-relay.automation.tip_request")

_AUTOMATION_KIND = "tip_request"
_STATE_KEY = "_tip_request"    # fans.custom_fields per-fan cooldown stamp

# Built-in defaults for any key the config omits. DISABLED + no media → a fresh
# account never fires until a creator enables it and picks the teaser image.
_DEFAULTS = {
    "enabled": False,
    "media_id": None,          # GLOBAL-default tip-request teaser (vault media id)
    "caption": "hope you loved that 🥺 wanna send me a lil tip so i keep going?",
    "min_wait_hours": 2,       # give the fan time to reply naturally first
    "max_age_hours": 48,       # don't chase a purchase older than this
    "cooldown_hours": 168,     # per-fan: at most one tip-request / week
    "guard_hours": 6,          # cross-automation contact-guard window
    "limit": 200,              # max fans nudged per sweep
}


async def _load_config(account_id: str) -> dict:
    """`tip_reward_config_json['tip_request']` shallow-merged over _DEFAULTS.
    A None value in the stored sub-config falls back to the default."""
    async with get_session() as s:
        cfg = await s.get(AccountAiConfig, str(account_id))
    raw = getattr(cfg, "tip_reward_config_json", None) if cfg else None
    sub: dict = {}
    if raw:
        try:
            sub = (json.loads(raw) or {}).get("tip_request") or {}
        except Exception:
            log.warning("bad tip_request config account=%s", account_id, exc_info=True)
    merged = dict(_DEFAULTS)
    if isinstance(sub, dict):
        merged.update({k: v for k, v in sub.items() if v is not None})
    return merged


async def is_enabled(account_id: str) -> bool:
    """On AND a teaser image is set — both required to fire."""
    cfg = await _load_config(account_id)
    return bool(cfg.get("enabled") and cfg.get("media_id"))


def _load_custom_fields(fan: Fan | None) -> dict:
    try:
        cf = json.loads(fan.custom_fields) if fan and fan.custom_fields else {}
        return cf if isinstance(cf, dict) else {}
    except Exception:
        return {}


def _cooled_down(fan: Fan | None, cooldown_hours: int) -> bool:
    """True if a tip-request was sent to this fan within cooldown_hours (per-fan
    throttle so a repeat mass-buyer isn't nudged every sweep). 0 → never throttle."""
    if cooldown_hours <= 0:
        return False
    at = (_load_custom_fields(fan).get(_STATE_KEY) or {}).get("at")
    if not at:
        return False
    try:
        last = datetime.fromisoformat(at)
    except Exception:
        return False
    return (datetime.utcnow() - last) < timedelta(hours=max(1, int(cooldown_hours)))


async def _mass_buyers_no_reply(
    account_id: str, *, ready_before: datetime, fresh_after: datetime,
) -> dict[int, datetime]:
    """{fan_id: last_mass_purchase_at} for fans who bought a MASS PPV in the
    window [fresh_after, ready_before] AND have sent NO inbound message since
    that purchase (they went quiet after buying)."""
    async with get_session() as s:
        bought_rows = (await s.execute(
            select(Message.fan_id, func.max(Message.purchased_at))
            .where(
                Message.account_id == str(account_id),
                Message.direction == "out",
                Message.mass_run_id.is_not(None),
                Message.is_paid.is_(True),
                Message.purchased_at.is_not(None),
                Message.purchased_at <= ready_before,
                Message.purchased_at >= fresh_after,
            )
            .group_by(Message.fan_id)
        )).all()
        cand = {int(fid): dt for fid, dt in bought_rows if fid is not None and dt is not None}
        if not cand:
            return {}
        # Last inbound per candidate — drop anyone who replied after buying.
        last_in = {
            int(fid): dt for fid, dt in (await s.execute(
                select(Message.fan_id, func.max(Message.created_at))
                .where(
                    Message.account_id == str(account_id),
                    Message.fan_id.in_(list(cand.keys())),
                    Message.direction == "in",
                )
                .group_by(Message.fan_id)
            )).all() if fid is not None
        }
    return {
        fid: bought_at for fid, bought_at in cand.items()
        if not (last_in.get(fid) and last_in[fid] > bought_at)
    }


@register("tip_request")
async def run(account_id: str, payload: dict, *, run_id: int) -> dict:
    """Sweep quiet mass-buyers and send each the global tip-request teaser.

    payload:
      • dry_run   — resolve candidates + eligibility, send nothing.
      • fan_id    — scope to ONE fan and bypass the window/reply gates (manual
                    test / operator "send now"); still honours dry_run.
      • force     — with fan_id, also bypass skip-list / guard / cooldown.
    """
    payload = payload or {}
    dry_run = bool(payload.get("dry_run"))
    force = bool(payload.get("force"))
    only_fan = payload.get("fan_id")

    cfg = await _load_config(account_id)
    media_id = cfg.get("media_id")
    if not cfg.get("enabled") and not force:
        return {"status": "skipped", "reason": "disabled"}
    if not media_id:
        return {"status": "skipped", "reason": "no_media_id"}

    caption = apply_word_restriction(str(cfg.get("caption") or ""))
    cooldown_h = int(cfg.get("cooldown_hours") or 0)
    limit = max(1, int(cfg.get("limit") or 200))
    now = datetime.utcnow()

    # ── Candidate set ────────────────────────────────────────────────
    if only_fan is not None:
        # Fan-scoped (manual / force): bypass the window + reply gates.
        candidates: dict[int, datetime] = {int(only_fan): now}
    else:
        min_wait_h = max(0, int(cfg.get("min_wait_hours") or 0))
        max_age_h = max(1, int(cfg.get("max_age_hours") or 48))
        candidates = await _mass_buyers_no_reply(
            account_id,
            ready_before=now - timedelta(hours=min_wait_h),
            fresh_after=now - timedelta(hours=max_age_h),
        )
    if not candidates:
        return {"status": "ok", "reason": "no_candidates", "sent": 0}

    # ── Eligibility sets (skip-list + cross-automation contact guard) ─
    hard_skip: set[int] = set() if force else await load_hard_skip_ids(account_id)
    guard_ids: set[int] = set()
    if not force:
        guard_h = int(cfg.get("guard_hours") or 0)
        if guard_h > 0:
            guard_ids = await contact_guard_excludes(account_id, outbound_hours=guard_h)

    sent = skipped = errors = 0
    client = None

    for fid in candidates:
        if sent >= limit:
            break
        async with get_session() as s:
            fan = await s.get(Fan, (str(account_id), int(fid)))

        if not force:
            if fid in hard_skip or should_skip_muted_creator(fan):
                skipped += 1
                continue
            if fid in guard_ids:
                skipped += 1
                continue
            if _cooled_down(fan, cooldown_h):
                skipped += 1
                continue

        if dry_run:
            sent += 1
            continue

        if client is None:
            client = await asyncio.to_thread(ax._make_client, account_id)
        try:
            result = await asyncio.to_thread(
                lambda fid=int(fid), mid=int(media_id):
                    client.send_message(fid, caption, media_files=[mid], price=0)
            )
        except Exception:
            log.warning("tip_request send failed account=%s fan=%s",
                        account_id, fid, exc_info=True)
            errors += 1
            continue

        msg_id = result.get("id") if isinstance(result, dict) else None
        ts = datetime.utcnow()
        if msg_id:
            await write_outbound_attribution(
                account_id=account_id, fan_id=int(fid), message_id=int(msg_id),
                sent_by_employee_id=None, body=caption, price_cents=0,
                created_at=ts, automation_kind=_AUTOMATION_KIND, emit_live=True,
            )
        # Stamp the per-fan cooldown so the next sweep doesn't re-nudge. Own
        # session; best-effort (the send already happened).
        try:
            async with get_session() as s:
                fan = await s.get(Fan, (str(account_id), int(fid)))
                if fan is not None:
                    cf = _load_custom_fields(fan)
                    cf[_STATE_KEY] = {"at": ts.isoformat()}
                    fan.custom_fields = json.dumps(cf)
        except Exception:
            log.debug("tip_request cooldown stamp failed account=%s fan=%s",
                      account_id, fid, exc_info=True)
        sent += 1
        log.info("tip_request sent account=%s fan=%s media=%s msg=%s",
                 account_id, fid, media_id, msg_id)

    return {"status": "ok", "sent": sent, "skipped": skipped, "errors": errors,
            "candidates": len(candidates), "dry_run": dry_run}
