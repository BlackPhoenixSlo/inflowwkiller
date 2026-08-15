"""service/automations/online_blast.py — broadcast to EVERY fan online now.

The SCALE sibling of mass_nudge. The two are deliberately different tools:

  • mass_nudge  — resolves the online ids and sends explicit `userIds`, so it
    keeps a precise per-fan 12h cooldown (NudgeState). Caps out around a few
    thousand online because it has to enumerate them (page the online list).
  • online_blast (THIS) — does ONE OnlyFans list-broadcast to the whole online
    audience: `send_mass_message(userLists=["fans"], online_only=True)`. OF
    resolves + fans out server-side in a SINGLE call, so it scales to 100k-fan
    accounts with tens of thousands online. The price: NO per-fan memory.

Dedup is the shared cross-automation contact guard
(`audiences.contact_guard_excludes` — fans we DMed or nudged inside the window
∪ fans who messaged us, NudgeState included so mass_nudge / nudge_online
recipients are skipped too) plus any explicit `excluded_users` ids. OF has NO
per-user exclusion field on broadcasts (`excludedUsers` is silently dropped —
verified live 2026-06-12), so the guard set is mirrored into the per-account
Auto_Exclude OF list and excluded server-side via `excludedLists` — together
with any explicit `excluded_user_lists`. To give blast→blast a real cooldown
(OF leaves no per-fan rows for a list send), the online snapshot is stamped
into NudgeState after the send (`stamp_recipients`, default on), so the next
blast / nudge sees these fans. CADENCE is still the backstop: run hourly+.

Config (steps_json):
  with_image            true   # attach the slot image (one, rotated)
  exclude_replied_hours 8      # skip fans we DMed/nudged in last N hrs (absent → 8, 0 = off)
  exclude_inbound_hours 8      # skip fans who messaged US in last N hrs (absent → 8, 0 = off)
  excluded_users        [...]  # explicit fan ids to never blast (optional)
  excluded_user_lists   [...]  # OF custom-list ids excluded server-side (optional)
  stamp_recipients      true   # stamp the online snapshot into NudgeState (cross-automation guard)
  max_stamp             5000   # cap the online re-fetch used for stamping
  unsend_after_hours    1      # auto-unsend the broadcast after N hrs (0 = keep)
  dry_run               false  # compose + resolve exclusions, send nothing
  slots                 {...}  # day-bucket → slot → {text:[...], image:[...]}

This is yesterday's original mass_nudge (a pure list-broadcast) brought back as
its own automation, plus the inbound-direction exclusion.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

import automation_executor as ax
from audiences import (
    MASSDMEXCLUDE_LIST,
    contact_guard_excludes,
    exclude_list_fan_ids,
    resolve_window_hours,
)
from automation_registry import register
from ._common import load_hard_skip_ids
from . import mass_nudge   # reuse slot composition + the default pools
from . import send_welcome  # _model_hour / _model_weekday / _slot_key

log = logging.getLogger("of-relay.automation.online_blast")

_DEFAULT_WINDOW_H = 8  # default guard window — matches the kind catalog + tab

# Identical slot composition to mass_nudge — reuse its preview for the Settings UI.
preview_compose = mass_nudge.preview_compose


async def _excluded_ids(account_id: str, cfg: dict) -> list[int]:
    """Fan ids to exclude (mirrored into the Auto_Exclude OF list — OF has no
    per-user exclusion field on broadcasts): the shared cross-automation
    contact guard — recent OUTBOUND/nudges ∪ recent INBOUND, each window
    absent → 8h default, explicit 0 = off — plus explicit `excluded_users`."""
    out_h = resolve_window_hours(
        cfg.get("exclude_replied_hours"), _DEFAULT_WINDOW_H)
    in_h = resolve_window_hours(
        cfg.get("exclude_inbound_hours"), _DEFAULT_WINDOW_H)
    extra = [int(x) for x in (cfg.get("excluded_users") or [])
             if str(x).lstrip("-").isdigit()]
    excl = await contact_guard_excludes(
        account_id, outbound_hours=out_h, inbound_hours=in_h, extra_ids=extra)
    # Durably restricted fans (muted peer-creator / hand-restricted) — mirrored into
    # the Auto_Exclude OF list with the rest so the broadcast skips them too.
    excl |= await load_hard_skip_ids(account_id)
    # MASSDMEXCLUDE members — online_blast is a mass DM broadcast, so it honors
    # the DM opt-out list (its own exclude path bypasses resolve_mass_audience).
    excl |= await exclude_list_fan_ids(account_id, MASSDMEXCLUDE_LIST)
    return sorted(excl)


@register("online_blast")
async def run(account_id: str, payload: dict, *, run_id: int) -> dict:
    cfg = {"with_image": True, **(payload or {})}
    off = await mass_nudge._utc_offset(account_id)
    hour = send_welcome._model_hour(off)
    slot = send_welcome._slot_key(hour)
    weekday = send_welcome._model_weekday(off)

    slots = cfg.get("slots") or mass_nudge._DEFAULT_SLOTS
    pool = mass_nudge._pick(slots, slot, weekday)
    texts = pool.get("text") or []
    if not texts:
        return {"sent": 0, "skipped": "no_text", "slot": slot}

    idx = mass_nudge._rotation_idx(len(texts))
    text = str(texts[idx])  # NO placeholder substitution — generic broadcast

    media: list[int] = []
    if cfg.get("with_image", True):
        imgs = pool.get("image") or []
        if imgs:
            media = [int(imgs[idx % len(imgs)])]

    excluded = await _excluded_ids(account_id, cfg)
    # OF custom-list ids excluded server-side (the composer's "Custom lists —
    # exclude"); system names like 'fans' are NOT accepted here by OF.
    excluded_lists = [x for x in (cfg.get("excluded_user_lists") or []) if x]
    if len(excluded) > 5000:
        # That's 5000+ list add/remove calls per tick on the Auto_Exclude sync —
        # watch this in prod before widening the guard windows on big accounts.
        log.warning("online_blast exclude set is large account=%s ids=%d",
                    account_id, len(excluded))

    if cfg.get("dry_run"):
        return {"dry_run": True, "slot": slot, "variation_idx": idx, "text": text,
                "image_attached": bool(media), "excluded": len(excluded),
                "excluded_lists": len(excluded_lists)}

    from audiences import broadcast_lock, ensure_exclude_list, stamp_broadcast_touch
    import audience_include as _audiences
    client = await asyncio.to_thread(ax._make_client, account_id)

    # Serialize sync+send per account, and mirror the guard set into the
    # Auto_Exclude OF list (OF ignores per-user `excludedUsers` on broadcasts —
    # verified live 2026-06-12). Fail CLOSED: blasting without the guard is the
    # over-send bug itself.
    audience_stats: dict = {}
    async with broadcast_lock(account_id):
        # Include-only audience: the blast keeps its original online-fans
        # audience and subtracts the AUTOFENCE list; unhealthy fence in enforce
        # mode HALTS the blast loudly — never an unfenced send.
        try:
            fence_ids = await _audiences.audience_fence_for_broadcast(
                account_id, client=client, stats=audience_stats)
        except _audiences.AudienceHalt as e:
            log.error("online_blast: AUTOFENCE unhealthy account=%s (%s) — "
                      "blast halted", account_id, e.reason)
            return {"sent": 0, "skipped": f"audience_halt:{e.reason}",
                    "slot": slot, **audience_stats}
        for _lid in fence_ids:
            if _lid not in excluded_lists:
                excluded_lists = [*excluded_lists, _lid]
        if excluded:
            try:
                auto_lid = await ensure_exclude_list(
                    account_id, excluded, client=client)
            except Exception:
                log.warning("online_blast: Auto_Exclude sync failed — skipping "
                            "blast account=%s", account_id, exc_info=True)
                return {"sent": 0, "skipped": "exclude_list_sync_failed",
                        "slot": slot, "excluded": len(excluded)}
            if auto_lid is not None and auto_lid not in excluded_lists:
                excluded_lists = [*excluded_lists, auto_lid]

        try:
            result = await asyncio.to_thread(lambda: client.send_mass_message(
                text,
                user_lists=["fans"],
                online_only=True,
                media_files=media,
                excluded_user_lists=excluded_lists or None,
            ))
        except Exception as e:
            log.warning("online_blast send failed account=%s", account_id, exc_info=True)
            return {"sent": 0, "skipped": "error", "error": repr(e)[:200], "slot": slot}

    queue_id = result.get("id") if isinstance(result, dict) else None

    # Feed the cross-automation contact guard: stamp the online fans we just
    # blasted into NudgeState (the SAME ledger mass_nudge/nudge_online use) so
    # the next proactive touch skips them. OF resolves "online" server-side and
    # echoes no ids, so we re-fetch the online snapshot (capped) to know whom we
    # hit. Default-on; set stamp_recipients=False (or max_stamp) on a 100k-scale
    # account where the per-fan stamp defeats the point of a list-broadcast.
    stamped = 0
    if cfg.get("stamp_recipients", True):
        cap = int(cfg.get("max_stamp") or 5000)
        try:
            online_now = await mass_nudge._resolve_online_ids(client, cap)
            excl_set = set(excluded)
            stamped = await stamp_broadcast_touch(
                account_id, [f for f in online_now if f not in excl_set])
            if len(online_now) >= cap:
                log.info("online_blast stamp capped account=%s cap=%d — tail "
                         "not guard-stamped", account_id, cap)
        except Exception:
            # Stamping is best-effort — never fail a delivered blast over it.
            log.warning("online_blast guard-stamp failed account=%s",
                        account_id, exc_info=True)

    # Attribute this broadcast to online_blast in the Mass Messages tab (no
    # per-fan messages rows are written, so this is the only attribution link).
    # Audience is a list (online "fans"), so the recipient count isn't known
    # here — leave it 0; the cache's sent_count carries the real number.
    from attribution import record_broadcast_mass_run
    await record_broadcast_mass_run(
        account_id=account_id, queue_id=queue_id, automation_kind="online_blast",
    )

    # Optional auto-unsend: enqueue the A12 unsend job for the broadcast.
    unsend_h = cfg.get("unsend_after_hours")
    unsend_job = None
    if isinstance(unsend_h, (int, float)) and unsend_h > 0 and queue_id:
        unsend_job = await ax.enqueue_job(
            account_id, "unsend_messages",
            payload={"targets": [{"queue_id": int(queue_id)}]},
            run_at=datetime.utcnow() + timedelta(hours=float(unsend_h)))

    return {
        "sent": 1,
        "queue_id": queue_id,
        "slot": slot,
        "variation_idx": idx,
        "text_preview": text[:80],
        "image_attached": bool(media),
        "excluded": len(excluded),
        "excluded_lists": len(excluded_lists),
        "guard_stamped": stamped,
        "unsend_job": unsend_job,
        **audience_stats,
    }
