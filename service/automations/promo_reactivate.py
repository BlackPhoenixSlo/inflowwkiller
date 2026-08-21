"""service/automations/promo_reactivate.py — Automation: promo_reactivate.

Keep a profile discount permanently on offer. OnlyFans promos END on their own —
the clock runs out or the claim cap fills, and OF flips the promo to
`isFinished`. This automation notices that and re-creates the SAME campaign
(same discount %, claim cap, duration, message), then repoints the mirror row at
the fresh OF promo. Net effect: an always-on offer that renews itself instead of
quietly lapsing.

Opt-in PER CAMPAIGN — only `promo_campaigns.auto_reactivate = 1` rows are
touched, so a one-off promo is never resurrected behind your back.

steps_json (per-run knobs):
  max_reactivations  cycle cap per campaign (0 = unlimited)
  cooldown_minutes   min gap between re-arms of the same campaign (default 10)
  dry_run            plan only, hit no OF endpoint (DEFAULT TRUE — safe)

Safety — every unknown is read as "leave it alone", never as "re-create it":
  • Fails CLOSED when OF is unreachable — a failed fetch must never be read as
    "the promo is gone", which would create a duplicate live promo every tick.
  • Fails CLOSED on a campaign with no `of_promo_id` — without it we can't look
    the promo up, so we can't tell "finished" from "still live".
  • A campaign with no usable discount (1-100) is skipped: OF rejects the create
    and we'd burn a call per tick.
  • Deleting a campaign in the UI removes its mirror row, so it stops re-arming.

Self-registers via @register("promo_reactivate"); scheduled by an
automation_rules row (kind="promo_reactivate", trigger_json={"every_seconds": N}).
Promos run for days, so an hourly cadence is plenty.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from sqlalchemy import select

import automation_executor as ax
from automation_registry import register
from db.engine import get_session
from db.models import PromoCampaign
from of_promos import extract_created_promo, fetch_of_promos, normalise_of_promo

log = logging.getLogger("of-relay.automation.promo_reactivate")

_DEFAULT_COOLDOWN_MIN = 10


@register("promo_reactivate")
async def run(account_id: str, payload: dict, *, run_id: int) -> dict:
    payload = payload or {}
    dry_run = bool(payload.get("dry_run", True))   # default SAFE
    try:
        max_reactivations = int(payload.get("max_reactivations") or 0)
    except (TypeError, ValueError):
        max_reactivations = 0
    try:
        cooldown_min = int(payload.get("cooldown_minutes") or _DEFAULT_COOLDOWN_MIN)
    except (TypeError, ValueError):
        cooldown_min = _DEFAULT_COOLDOWN_MIN

    async with get_session() as s:
        managed = (await s.execute(
            select(PromoCampaign).where(
                PromoCampaign.account_id == str(account_id),
                PromoCampaign.auto_reactivate.is_(True),
            )
        )).scalars().all()
    if not managed:
        return {"managed": 0, "reactivated": 0, "dry_run": dry_run}

    # Fail CLOSED: if we can't read OF, we cannot tell "finished" from
    # "unreachable" — re-creating on a failed fetch would duplicate live promos.
    # Pass the ids we manage so paging stops as soon as they're all located:
    # /promotions holds the creator's whole promo history (230+ on a real
    # account) and page 1 alone would show an older-but-LIVE promo as absent,
    # i.e. "expired", i.e. re-create it.
    managed_ids = {int(r.of_promo_id) for r in managed if r.of_promo_id is not None}
    live = await fetch_of_promos(account_id, need_ids=managed_ids)
    if live is None:
        log.info("promo_reactivate: OF unreachable account=%s — standing down", account_id)
        return {"managed": len(managed), "reactivated": 0,
                "skipped": "of_unreachable", "dry_run": dry_run}

    # OnlyFans runs ONE subscription promo at a time — creating a new one flips
    # the current one to isFinished. So a campaign can be "finished" because it
    # was SUPERSEDED by a sibling, not because it lapsed. With two armed
    # campaigns that produced a ping-pong: re-arming A finished B, so next tick
    # B looked expired and was re-armed, which finished A... a real promo minted
    # on the creator's public profile every tick, forever.
    #
    # The invariant that kills it: if any promo is live, a discount IS on offer
    # and there is nothing to restore. Stand down. (/promotions is newest-first,
    # so a live promo is always on the first page we fetch.)
    live_now = [p for p in live if not p.get("is_finished")]
    if live_now:
        return {"managed": len(managed), "reactivated": 0,
                "skipped": "promo_already_live",
                "live_of_promo_id": live_now[0].get("of_promo_id"),
                "dry_run": dry_run}

    # Past this point NOTHING is on offer — the return above is the single home of
    # the liveness question, so a campaign's own OF row can only be finished (or
    # gone from history entirely). Both mean the same thing here: re-arm it. There
    # is deliberately no per-campaign "still active?" re-check; it could only ever
    # be false, and having one invited two rival notions of "active".
    now = datetime.utcnow()
    due: list[PromoCampaign] = []
    skipped_cap = skipped_cooldown = skipped_no_discount = 0
    skipped_unverifiable = 0

    for r in managed:
        disc = int(r.discount_percent or 0)
        if not (1 <= disc <= 100):
            skipped_no_discount += 1
            continue
        if max_reactivations and int(r.reactivate_count or 0) >= max_reactivations:
            skipped_cap += 1
            continue
        if r.last_reactivated_at and (now - r.last_reactivated_at) < timedelta(minutes=cooldown_min):
            skipped_cooldown += 1
            continue
        if r.of_promo_id is None:
            # No OF id → we cannot tell "finished" from "still live", so
            # re-creating would mint a DUPLICATE promo every cycle (and the new
            # id may not be captured either, so it would never converge). Same
            # fail-closed rule as an unreachable fetch.
            skipped_unverifiable += 1
            log.info("promo_reactivate: campaign=%s has no of_promo_id — cannot verify, skipping", r.id)
            continue
        due.append(r)

    base = {"managed": len(managed), "due": len(due),
            "skipped_cap": skipped_cap, "skipped_cooldown": skipped_cooldown,
            "skipped_no_discount": skipped_no_discount,
            "skipped_unverifiable": skipped_unverifiable}

    if not due:
        return {**base, "reactivated": 0, "dry_run": dry_run}
    if dry_run:
        return {**base, "reactivated": 0, "dry_run": True,
                "would_reactivate": [{"id": r.id, "name": r.name,
                                      "discount": int(r.discount_percent or 0)} for r in due]}

    client = await asyncio.to_thread(ax._make_client, account_id)
    reactivated = errors = 0
    # At most ONE re-arm per run: OF only holds one live promo, so re-arming a
    # second here would immediately finish the one we just created.
    for r in due[:1]:
        try:
            res = await asyncio.to_thread(
                lambda rr=r: client.create_promo(
                    discount=int(rr.discount_percent),
                    subscribe_counts=int(rr.subscribe_counts or 1),
                    subscribe_days=int(rr.subscribe_days or 30),
                    message=rr.message or "",
                    type=rr.promo_type or "all"),
            )
        except Exception:
            errors += 1
            log.warning("promo_reactivate create failed account=%s campaign=%s",
                        account_id, r.id, exc_info=True)
            continue
        of = normalise_of_promo(extract_created_promo(res))
        new_id = of.get("of_promo_id")
        stamp = datetime.utcnow()
        async with get_session() as s:
            row = await s.get(PromoCampaign, r.id)
            if row is None:      # deleted mid-run → don't resurrect the mirror
                continue
            row.of_promo_id = new_id if new_id is not None else row.of_promo_id
            row.status = "active"
            row.subscriber_count = 0          # fresh promo, fresh claim count
            row.reactivate_count = int(row.reactivate_count or 0) + 1
            row.last_reactivated_at = stamp
        reactivated += 1
        log.info("promo_reactivate re-armed account=%s campaign=%s new_of_id=%s cycle=%s",
                 account_id, r.id, new_id, int(r.reactivate_count or 0) + 1)

    return {**base, "reactivated": reactivated, "errors": errors,
            "deferred": max(0, len(due) - 1), "dry_run": False}
