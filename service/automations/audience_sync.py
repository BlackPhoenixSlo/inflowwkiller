"""service/automations/audience_sync.py — reconcile the include-only automation
audience (audience_mode shadow|enforce).

Three jobs, in a deliberate order:
  1. roster-diff AUTO-ADD (pull, not push): page the ACTIVE subscriber roster,
     diff against the enrollment ledger, and add the PROVABLY-NEW fans to the
     operator's OF folder — OF add first, local admit only after a complete list
     crawl confirms membership. The mixed notifications feed is only a latency
     fast-path (send_welcome calls `fast_path_enroll`); this scan is the
     guarantee. OF dropped the notifications `type=` filter (it 400s — see
     send_welcome.py) so a push channel cannot be trusted with enrollment.
  2. transactional OF→local MIRROR reconcile: the local List/list_members
     snapshot is the gate-time read for every gated engine. Deletions apply only
     after a COMPLETE successful crawl; a crawl that hits the 20k page backstop
     is a FAILED sync, loudly — the stale-but-complete snapshot keeps authorizing
     sends through the grace window, then enforcement halts.
  3. ledger transitions + AUTOFENCE upkeep (enforce mode, under broadcast_lock).

Baseline discipline (pre-mortem amendment): auto-add activates only after TWO
complete, consecutive roster scans — every fan seen before that pre-dates the
feature and can never be "provably new". Un-provable fans land in the visible
"outside the fence" queue (status rejected_not_new) with one-click admit; the
machine NEVER silently adds them. Once a fan is confirmed, later absence from
the folder is operator intent — FINAL, never re-added.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta

from sqlalchemy import delete as sa_delete, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

import audience_include
from audience_include import (
    AUDIENCE_INCLUDE_KIND, AudienceHalt, _crawl_list_ids_blocking,
    established_fan_ids, norm_audience_mode,
)
from audiences import _page_all_checked, broadcast_lock
from automation_registry import register
from db.engine import get_session
from db.models import AccountAiConfig, AudienceEnrollment, List as ListModel, ListMember

log = logging.getLogger("of-relay.automation.audience_sync")

_KIND = "audience_sync"
_ROSTER_PAGE = 50            # OF caps subscriber pages low; offset math is len-based
_ADD_MAX_ATTEMPTS = 5
_ADD_RETRY_MINUTES = (5, 15, 60, 240)   # bounded backoff between add retries

ST_PENDING = "pending_add"
ST_CONFIRMED = "confirmed"
ST_REMOVED = "removed_after_confirmed"
ST_REJECTED = "rejected_not_new"


def _meta(row: ListModel) -> dict:
    try:
        v = json.loads(row.sync_meta_json or "{}")
        return v if isinstance(v, dict) else {}
    except (TypeError, ValueError):
        return {}


async def _config(account_id: str) -> tuple[str, int | None, bool]:
    async with get_session() as s:
        cfg = await s.get(AccountAiConfig, str(account_id))
    mode = norm_audience_mode(getattr(cfg, "audience_mode", None))
    list_id = getattr(cfg, "audience_list_id", None) if cfg else None
    auto_add = bool(getattr(cfg, "audience_auto_add", None)) if cfg else False
    return mode, (int(list_id) if list_id is not None else None), auto_add


async def _include_row(account_id: str, list_id: int) -> ListModel | None:
    async with get_session() as s:
        row = await s.get(ListModel, int(list_id))
    if (row is None or row.account_id != str(account_id)
            or row.kind != AUDIENCE_INCLUDE_KIND):
        return None
    return row


def _scan_roster_blocking(client) -> tuple[set[int], bool]:
    """(active subscriber fan ids, hit_backstop). Complete-or-truncated, never
    silently partial — the diff below must not judge fans off a short read."""
    rows, truncated = _page_all_checked(
        lambda off: client.subscribers(type="active", limit=_ROSTER_PAGE, offset=off))
    ids = set()
    for u in rows:
        uid = u.get("id")
        if uid is not None:
            try:
                ids.add(int(uid))
            except (TypeError, ValueError):
                continue
    return ids, truncated


async def _ledger_rows(account_id: str) -> dict[int, AudienceEnrollment]:
    async with get_session() as s:
        rows = (await s.execute(
            select(AudienceEnrollment).where(
                AudienceEnrollment.account_id == str(account_id))
        )).scalars().all()
    return {int(r.fan_id): r for r in rows}


async def _upsert_ledger(account_id: str, fan_id: int, *, status: str,
                         reason: str | None = None, gen: int | None = None,
                         attempts: int | None = None,
                         next_retry_at: datetime | None = None) -> None:
    now = datetime.utcnow()
    vals = dict(account_id=str(account_id), fan_id=int(fan_id), status=status,
                reason=reason, first_seen_gen=gen, first_seen_at=now,
                updated_at=now)
    if attempts is not None:
        vals["attempts"] = attempts
    if next_retry_at is not None:
        vals["next_retry_at"] = next_retry_at
    set_ = {"status": status, "reason": reason, "updated_at": now}
    if attempts is not None:
        set_["attempts"] = attempts
    set_["next_retry_at"] = next_retry_at
    async with get_session() as s:
        await s.execute(
            sqlite_insert(AudienceEnrollment).values(**vals)
            .on_conflict_do_update(index_elements=["account_id", "fan_id"], set_=set_)
        )


async def fast_path_enroll(account_id: str, fan_id, *, client) -> bool:
    """The notifications-feed LATENCY fast-path (called by send_welcome the
    moment it sees a new sub). Same rules as the roster scan — auto_add on,
    baseline ready, fan unknown to ledger AND mirror — or it does nothing; the
    roster diff remains the guarantee. Returns True when the fan was enrolled
    (pending_add + OF add issued), so the caller may treat him as inside."""
    mode, list_id, auto_add = await _config(account_id)
    if mode == "off" or not auto_add or list_id is None:
        return False
    row = await _include_row(account_id, list_id)
    if row is None or not row.of_list_id:
        return False
    meta = _meta(row)
    if not meta.get("baseline_ready"):
        return False
    fid = int(fan_id)
    ledger = await _ledger_rows(account_id)
    if fid in ledger:
        return ledger[fid].status in (ST_PENDING, ST_CONFIRMED)
    async with get_session() as s:
        member = (await s.execute(
            select(ListMember.fan_id).where(
                ListMember.list_id == int(row.id),
                ListMember.fan_id == fid)
        )).scalar_one_or_none()
    if member is not None:
        return True
    await _upsert_ledger(account_id, fid, status=ST_PENDING, reason="fast_path",
                         gen=meta.get("roster_gen"), attempts=1,
                         next_retry_at=datetime.utcnow()
                         + timedelta(minutes=_ADD_RETRY_MINUTES[0]))
    try:
        await asyncio.to_thread(client.add_user_to_list, int(row.of_list_id), fid)
    except Exception as e:  # noqa: BLE001 — pending row retries on the next sync
        log.warning("audience fast-path add failed account=%s fan=%s (%s)",
                    account_id, fid, str(e).splitlines()[0][:80])
    log.info("audience fast-path enrolled account=%s fan=%s (pending confirm)",
             account_id, fid)
    return True


@register(_KIND)
async def run(account_id: str, payload: dict, *, run_id: int) -> dict:
    payload = payload or {}
    mode, list_id, auto_add = await _config(account_id)
    if mode == "off" or list_id is None:
        return {"status": "skipped", "reason": "audience_off"}
    row = await _include_row(account_id, list_id)
    if row is None:
        log.error("audience_sync account=%s include list %s missing/foreign",
                  account_id, list_id)
        return {"status": "error", "reason": "include_list_missing"}
    if not row.of_list_id:
        return {"status": "error", "reason": "include_list_unlinked"}
    of_list_id = int(row.of_list_id)
    meta = _meta(row)
    now = datetime.utcnow()
    stats: dict = {"mode": mode, "auto_add": auto_add, "list_id": int(row.id)}

    import automation_executor as ax
    client = await asyncio.to_thread(ax._make_client, account_id)

    # ── 1. roster scan + provably-new auto-add (OF add first, confirm later) ──
    roster_gen = int(meta.get("roster_gen") or 0)
    baseline_ready = bool(meta.get("baseline_ready"))
    ledger = await _ledger_rows(account_id)
    if auto_add:
        roster, truncated = await asyncio.to_thread(_scan_roster_blocking, client)
        stats["roster_seen"] = len(roster)
        if truncated:
            # An incomplete roster must QUEUE, never admit — and it breaks the
            # consecutive-complete-scan chain the baseline needs.
            meta["roster_complete_streak"] = 0
            stats["roster_truncated"] = True
            log.error("audience_sync account=%s roster scan truncated — no "
                      "auto-add this tick", account_id)
        else:
            roster_gen += 1
            streak = int(meta.get("roster_complete_streak") or 0) + 1
            meta.update(roster_gen=roster_gen, roster_complete_streak=streak)
            new_confirmable, ambiguous, added = 0, 0, 0
            if not baseline_ready:
                # Baseline scans: every fan seen now PRE-DATES auto-add — they
                # can never be provably new. Ledger them into the visible queue
                # (never overwriting a real state).
                for fid in roster:
                    if fid not in ledger:
                        await _upsert_ledger(account_id, fid, status=ST_REJECTED,
                                             reason="baseline", gen=roster_gen)
                        ambiguous += 1
                if streak >= 2:
                    baseline_ready = True
                    meta.update(baseline_ready=True, baseline_gen=roster_gen)
                    log.info("audience_sync account=%s baseline established at "
                             "gen=%d", account_id, roster_gen)
            else:
                fresh = sorted(fid for fid in roster if fid not in ledger)
                # send_welcome's renewal tolerance, reused verbatim: real message
                # history means a RETURNING CHURNER, not a new sub — he can't be
                # proven new, so he queues for one-click admit, never auto-adds.
                churners = await established_fan_ids(
                    account_id, fresh, max_outbound=2, max_inbound=8)
                for fid in fresh:
                    if fid in churners:
                        await _upsert_ledger(account_id, fid, status=ST_REJECTED,
                                             reason="returning_churner",
                                             gen=roster_gen)
                        ambiguous += 1
                        continue
                    new_confirmable += 1
                    await _upsert_ledger(
                        account_id, fid, status=ST_PENDING, reason="roster_diff",
                        gen=roster_gen, attempts=1,
                        next_retry_at=now + timedelta(minutes=_ADD_RETRY_MINUTES[0]))
                    try:
                        await asyncio.to_thread(client.add_user_to_list, of_list_id, fid)
                        added += 1
                    except Exception as e:  # noqa: BLE001 — bounded retry next tick
                        log.warning("audience auto-add failed account=%s fan=%s (%s)",
                                    account_id, fid, str(e).splitlines()[0][:80])
            stats.update(new_confirmed=new_confirmable, ambiguous=ambiguous,
                         adds_issued=added, baseline_ready=baseline_ready)
        # Retry NEVER-CONFIRMED adds whose backoff expired (only pending_add
        # retries — confirmed-then-absent is operator intent and final).
        retried = 0
        for fid, r in ledger.items():
            if r.status != ST_PENDING or (r.next_retry_at and r.next_retry_at > now):
                continue
            if r.attempts >= _ADD_MAX_ATTEMPTS:
                await _upsert_ledger(account_id, fid, status=ST_REJECTED,
                                     reason="add_failed", gen=r.first_seen_gen,
                                     attempts=r.attempts)
                continue
            backoff = _ADD_RETRY_MINUTES[min(r.attempts, len(_ADD_RETRY_MINUTES) - 1)]
            await _upsert_ledger(account_id, fid, status=ST_PENDING, reason=r.reason,
                                 gen=r.first_seen_gen, attempts=r.attempts + 1,
                                 next_retry_at=now + timedelta(minutes=backoff))
            try:
                await asyncio.to_thread(client.add_user_to_list, of_list_id, fid)
                retried += 1
            except Exception as e:  # noqa: BLE001
                log.warning("audience add retry failed account=%s fan=%s (%s)",
                            account_id, fid, str(e).splitlines()[0][:80])
        if retried:
            stats["adds_retried"] = retried

    # ── 2. transactional mirror reconcile (deletions only after a COMPLETE crawl)
    members, truncated = await asyncio.to_thread(
        _crawl_list_ids_blocking, client, of_list_id)
    if truncated:
        meta["truncated"] = True
        async with get_session() as s:
            db_row = await s.get(ListModel, int(row.id))
            if db_row is not None:
                db_row.sync_meta_json = json.dumps(meta)
        log.error("audience_sync account=%s list=%s crawl hit the 20k backstop — "
                  "FAILED sync, snapshot untouched (lists ≥20k are unsupported "
                  "for enforcement)", account_id, of_list_id)
        stats.update(status="error", reason="sync_truncated")
        return stats
    meta["truncated"] = False
    meta["member_count"] = len(members)
    async with get_session() as s:
        current = {int(m) for m in (await s.execute(
            select(ListMember.fan_id).where(ListMember.list_id == int(row.id))
        )).scalars().all()}
        adds, removes = members - current, current - members
        for fid in sorted(adds):
            await s.execute(
                sqlite_insert(ListMember)
                .values(list_id=int(row.id), fan_id=int(fid), added_at=now)
                .on_conflict_do_nothing(index_elements=["list_id", "fan_id"]))
        if removes:
            await s.execute(sa_delete(ListMember).where(
                ListMember.list_id == int(row.id),
                ListMember.fan_id.in_(sorted(removes))))
        db_row = await s.get(ListModel, int(row.id))
        if db_row is not None:
            db_row.synced_at = now
            db_row.sync_meta_json = json.dumps(meta)
    stats.update(members=len(members), mirror_added=len(adds),
                 mirror_removed=len(removes))

    # ── 3. ledger transitions off the fresh, complete membership crawl ──
    ledger = await _ledger_rows(account_id)
    confirmed = removed = 0
    for fid, r in ledger.items():
        if r.status == ST_PENDING and fid in members:
            await _upsert_ledger(account_id, fid, status=ST_CONFIRMED,
                                 reason=r.reason, gen=r.first_seen_gen,
                                 attempts=r.attempts)
            confirmed += 1
        elif r.status == ST_CONFIRMED and fid not in members:
            # Operator curation — FINAL. Removal takes effect NOW (the mirror
            # already dropped him above); never re-add, no grace.
            await _upsert_ledger(account_id, fid, status=ST_REMOVED,
                                 reason="operator_removed", gen=r.first_seen_gen)
            removed += 1
    # Operator-curated members with no ledger row: record confirmed so a later
    # removal is recognized as operator intent (and stays final).
    adopted = 0
    for fid in members:
        if fid not in ledger:
            await _upsert_ledger(account_id, fid, status=ST_CONFIRMED,
                                 reason="operator_add")
            adopted += 1
    stats.update(confirmed=confirmed, removed_final=removed, adopted=adopted)

    # ── 4. AUTOFENCE upkeep (enforce only) — same lock the broadcasts take ──
    if mode == "enforce":
        try:
            async with broadcast_lock(account_id):
                fence_id = await audience_include.ensure_autofence(account_id, client=client)
            stats["fence_list_id"] = fence_id
        except AudienceHalt as e:
            stats["fence_unhealthy"] = e.reason
            log.error("audience_sync account=%s fence unhealthy: %s — enforce-mode "
                      "broadcasts will HALT until it rebuilds", account_id, e.reason)

    stats["status"] = "ok"
    return stats
