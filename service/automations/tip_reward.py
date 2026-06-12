"""service/automations/tip_reward.py — reward a fan with vault media when they tip.

Trigger: real-time, off the inbound tip event. `event_transcoder` records the tip,
then `webhook_dispatch.on_inbound_tip` enqueues ONE fan-scoped `tip_reward` job
carrying {fan_id, tip_message_id, tip_cents}. The periodic executor drains it.

The reward is photos AND videos (whatever lives in the tier's folders) — the config
keys keep their historical `*_image*` names for back-compat, but "image" here means
"a vault media item, photo or video". DRM-only videos can't be previewed in the
browser but ARE valid vault attachments, so they're included like any other item.

The reward rule (all knobs in account_ai_config.tip_reward_config_json):
  • COUNT  = clamp(min_images, tip_dollars // dollars_per_image, max_images).
             A $25 tip at $10/image → 2 media items; a $3 tip still gets `min_images`.
  • FOLDER = picked by a TIER. The tier basis is max(this tip, the fan's tip sum
             over the last `window_hours`) — so a fan who has tipped past the
             premium threshold in the window gets premium folders even on a small
             follow-up tip. The highest tier whose `min_basis_cents` ≤ basis AND
             that has folders configured wins (so a single 'basic' folder serves
             every tier until premium folders are filled in).
  • Media is FREE (price=0) and ONLY items this fan has never received (VaultSend
    history) — so repeat tippers keep getting fresh content.

Idempotency: a webhook can replay a tip, so the FIRST thing a run does (and the
LAST, on success) is consult `tip_reward_log` keyed on (account, tip_message_id).
One reward per tip, guaranteed. `images_sent=0` is a recorded outcome (fan has
seen every image in the tier's folders) and still blocks re-processing.

Deliberately does NOT take the W3 fan lease or set the post-send cooldown: a tip
reward is a direct response to a fan action and should fire even if another
automation (e.g. an of_ai_chat reply) is messaging the same fan this moment.

Ships DISABLED with empty folders — a creator enables it and fills folder names.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta

import automation_executor as ax        # _make_client / _parse_iso seams
from attribution import write_outbound_attribution
from automation_registry import register
from automations._common import apply_word_restriction
from db.engine import get_session
from db.models import AccountAiConfig, TipRewardLog, Transaction, VaultSend
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

log = logging.getLogger("of-relay.automation.tip_reward")

# Tip transaction kinds (mirror the attribution view + transactions.py).
_TIP_KINDS = ("tip", "tip_post", "tip_stream")

# Built-in defaults — used for any key the account config omits. DISABLED + empty
# folders so the automation is inert until a creator configures it.
_DEFAULTS: dict = {
    "enabled": False,
    "dollars_per_image": 10,   # 1 image per $10 of the tip
    "min_images": 1,           # any tip ≥ $0.01 still gets at least this many
    "max_images": 5,           # cap so a whale tip can't drain a folder in one shot
    "caption": "",             # optional thank-you text ('' → media-only message)
    "window_hours": 72,        # rolling window for the cumulative tier basis
    "tiers": [
        {"name": "basic",   "min_basis_cents": 0,      "folders": []},
        {"name": "mid",     "min_basis_cents": 1000,   "folders": []},   # ≥ $10
        {"name": "premium", "min_basis_cents": 10000,  "folders": []},   # ≥ $100
    ],
}

# How many items to pull per folder when scanning for unseen ones — generous so a
# fan deep into a folder still finds fresh media.
_VAULT_SCAN_LIMIT = 100

# Vault item types we reward with. Photos, videos (incl. DRM-only — still sendable
# as a vault attachment) and gifs; audio is excluded (not a "reward image/clip").
_REWARD_MEDIA_TYPES = ("photo", "video", "gif")


async def _load_config(account_id: str) -> dict:
    """account_ai_config.tip_reward_config_json shallow-merged over _DEFAULTS.
    Absent/NULL/parse-error → defaults (disabled). `tiers`, when present, REPLACES
    the default list wholesale (it's a list, not a dict to merge)."""
    async with get_session() as s:
        cfg = await s.get(AccountAiConfig, str(account_id))
    raw = getattr(cfg, "tip_reward_config_json", None) if cfg else None
    merged = dict(_DEFAULTS)
    if raw:
        try:
            stored = json.loads(raw) or {}
            merged.update({k: v for k, v in stored.items() if v is not None})
        except Exception:
            log.warning("bad tip_reward_config account=%s", account_id, exc_info=True)
    return merged


async def is_enabled(account_id: str) -> bool:
    """Cheap gate for the dispatcher: skip enqueuing a job for a disabled account."""
    cfg = await _load_config(account_id)
    return bool(cfg.get("enabled"))


def _media_count(tip_cents: int, cfg: dict) -> int:
    """clamp(min_images, tip_dollars // dollars_per_image, max_images). The config
    keys keep their `*_image*` names for back-compat; the count governs photos AND
    videos alike (one media item per `dollars_per_image`)."""
    per = max(1, int(cfg.get("dollars_per_image") or 10)) * 100   # → cents/item
    lo = max(0, int(cfg.get("min_images") or 0))
    hi = max(lo, int(cfg.get("max_images") or lo or 1))
    return max(lo, min(hi, int(tip_cents) // per))


def _pick_tier(basis_cents: int, cfg: dict) -> dict | None:
    """Highest tier whose min_basis_cents ≤ basis AND that has folders. Walking
    DOWN past empty tiers lets a single 'basic' folder serve every tier until the
    premium folders are filled in. None when no eligible tier has folders."""
    tiers = cfg.get("tiers") or []
    eligible = [
        t for t in tiers
        if isinstance(t, dict) and int(t.get("min_basis_cents") or 0) <= basis_cents
    ]
    eligible.sort(key=lambda t: int(t.get("min_basis_cents") or 0), reverse=True)
    for t in eligible:
        if [f for f in (t.get("folders") or []) if str(f).strip()]:
            return t
    return None


async def _window_tip_sum(account_id: str, fan_id: int, window_hours: int) -> int:
    """Sum of this fan's tip transactions over the last `window_hours` (incl. the
    just-recorded tip). 0 when none."""
    since = datetime.utcnow() - timedelta(hours=max(1, int(window_hours)))
    async with get_session() as s:
        total = (await s.execute(
            select(Transaction.amount_cents).where(
                Transaction.account_id == str(account_id),
                Transaction.fan_id == int(fan_id),
                Transaction.kind.in_(_TIP_KINDS),
                Transaction.status.in_(("cleared", "pending")),
                Transaction.occurred_at >= since,
            )
        )).scalars().all()
    return sum(int(x or 0) for x in total)


async def _already_rewarded(account_id: str, tip_message_id: int) -> bool:
    async with get_session() as s:
        row = await s.get(TipRewardLog, (str(account_id), int(tip_message_id)))
    return row is not None


async def _seen_media(account_id: str, fan_id: int) -> set[int]:
    """media_ids this fan has already been sent (VaultSend history)."""
    async with get_session() as s:
        ids = (await s.execute(
            select(VaultSend.media_id).where(
                VaultSend.account_id == str(account_id),
                VaultSend.fan_id == int(fan_id),
            )
        )).scalars().all()
    return {int(x) for x in ids}


def _resolve_folders(client, folder_names: list[str]) -> dict[str, int]:
    """folder name (lowercased) → vault list id, via vault_lists. Best-effort."""
    try:
        lists = client.vault_lists(view="main", limit=100)
    except Exception:
        log.debug("tip_reward vault_lists failed", exc_info=True)
        return {}
    folders = lists.get("list") if isinstance(lists, dict) else lists
    by_name: dict[str, int] = {}
    for f in (folders or []):
        if isinstance(f, dict) and f.get("id") is not None:
            by_name.setdefault(str(f.get("name", "")).strip().lower(), int(f["id"]))
    return by_name


def _folder_media_ids(client, list_id: int) -> list[int]:
    """Ordered media ids in one vault folder (recent-first), PHOTOS AND VIDEOS (plus
    gifs). `type="all"` so a tip reward can hand out a clip as readily as a photo;
    audio is filtered out. DRM-only videos are kept — they can't be previewed but
    ARE sendable as a vault attachment. Best-effort (empty on error)."""
    try:
        media = client.vault_media(list_id=int(list_id), type="all",
                                   limit=_VAULT_SCAN_LIMIT)
    except Exception:
        log.debug("tip_reward vault_media failed folder=%s", list_id, exc_info=True)
        return []
    items = media.get("list") if isinstance(media, dict) else media
    out: list[int] = []
    for it in (items or []):
        if not isinstance(it, dict) or it.get("id") is None:
            continue
        # Tolerate a missing/blank type (older payloads / test fakes) — only an
        # explicit non-reward type (e.g. audio) is skipped.
        mtype = str(it.get("type") or "").strip().lower()
        if mtype and mtype not in _REWARD_MEDIA_TYPES:
            continue
        try:
            out.append(int(it["id"]))
        except (TypeError, ValueError):
            continue
    return out


def _gather_unseen(client, folders: list[str], by_name: dict[str, int],
                   seen: set[int], count: int) -> list[int]:
    """Up to `count` unseen media ids (photos AND videos), scanning the tier's
    folders in order and de-duplicating across them (an id can live in two
    folders)."""
    picked: list[int] = []
    taken: set[int] = set()
    for nm in folders:
        list_id = by_name.get(str(nm).strip().lower())
        if list_id is None:
            log.info("tip_reward folder not found name=%r", nm)
            continue
        for mid in _folder_media_ids(client, list_id):
            if mid in seen or mid in taken:
                continue
            picked.append(mid)
            taken.add(mid)
            if len(picked) >= count:
                return picked
    return picked


async def _record_reward(account_id: str, fan_id: int, *, tip_message_id: int | None,
                         tip_cents: int, basis_cents: int, tier_name: str | None,
                         media_ids: list[int], reward_message_id: int | None,
                         body: str) -> None:
    """Persist the outbound message (Automation employee, automation_kind=
    tip_reward), one VaultSend per image (so we never re-send), and the
    tip_reward_log idempotency/audit row."""
    now = datetime.utcnow()
    if reward_message_id:
        await write_outbound_attribution(
            account_id=account_id,
            fan_id=int(fan_id),
            message_id=int(reward_message_id),
            sent_by_employee_id=None,            # → system Automation employee
            body=body,
            price_cents=0,
            created_at=now,
            automation_kind="tip_reward",
            emit_live=True,
        )
    async with get_session() as s:
        for mid in media_ids:
            s.add(VaultSend(account_id=str(account_id), fan_id=int(fan_id),
                            media_id=int(mid),
                            message_id=int(reward_message_id) if reward_message_id else None,
                            price_cents=0, sent_at=now))
        if tip_message_id is not None:
            await s.execute(
                sqlite_insert(TipRewardLog)
                .values(account_id=str(account_id), tip_message_id=int(tip_message_id),
                        fan_id=int(fan_id), tip_cents=int(tip_cents),
                        basis_cents=int(basis_cents), tier_name=tier_name,
                        images_sent=len(media_ids),
                        reward_message_id=int(reward_message_id) if reward_message_id else None,
                        created_at=now)
                .on_conflict_do_nothing(index_elements=["account_id", "tip_message_id"])
            )


@register("tip_reward")
async def run(account_id: str, payload: dict, *, run_id: int) -> dict:
    payload = payload or {}
    dry_run = bool(payload.get("dry_run"))
    force = bool(payload.get("force"))               # bypass idempotency (manual re-reward)

    fan_id = payload.get("fan_id")
    tip_cents = int(payload.get("tip_cents") or 0)
    tip_message_id = payload.get("tip_message_id")
    if fan_id is None or tip_cents <= 0:
        return {"status": "skipped", "reason": "no_tip", "fan_id": fan_id, "tip_cents": tip_cents}
    fan_id = int(fan_id)
    tip_message_id = int(tip_message_id) if tip_message_id is not None else None

    cfg = await _load_config(account_id)
    if not cfg.get("enabled"):
        return {"status": "skipped", "reason": "disabled"}

    # Idempotency: one reward per tip (webhooks replay). `force` re-rewards.
    if tip_message_id is not None and not force and await _already_rewarded(account_id, tip_message_id):
        return {"status": "skipped", "reason": "already_rewarded", "tip_message_id": tip_message_id}

    count = _media_count(tip_cents, cfg)
    window_sum = await _window_tip_sum(account_id, fan_id, cfg.get("window_hours") or 72)
    basis_cents = max(tip_cents, window_sum)
    tier = _pick_tier(basis_cents, cfg)
    tier_name = tier.get("name") if tier else None
    folders = [f for f in (tier.get("folders") if tier else []) if str(f).strip()]

    base = {
        "fan_id": fan_id, "tip_cents": tip_cents, "window_sum_cents": window_sum,
        "basis_cents": basis_cents, "tier": tier_name, "image_count": count,
        "dry_run": dry_run,
    }
    if not folders:
        # No tier folders configured for this basis → nothing to send. Still log the
        # tip so a misconfig doesn't make us re-scan it every webhook replay.
        if not dry_run and tip_message_id is not None:
            await _record_reward(account_id, fan_id, tip_message_id=tip_message_id,
                                 tip_cents=tip_cents, basis_cents=basis_cents,
                                 tier_name=tier_name, media_ids=[],
                                 reward_message_id=None, body="")
        return {**base, "status": "skipped", "reason": "no_folders", "images_sent": 0}

    client = await asyncio.to_thread(ax._make_client, account_id)
    by_name = await asyncio.to_thread(_resolve_folders, client, folders)
    seen = await _seen_media(account_id, fan_id)
    media_ids = await asyncio.to_thread(_gather_unseen, client, folders, by_name, seen, count)

    if not media_ids:
        # Fan has already received every item in the tier's folders. Record a
        # zero-media reward so we stop re-scanning this tip.
        if not dry_run and tip_message_id is not None:
            await _record_reward(account_id, fan_id, tip_message_id=tip_message_id,
                                 tip_cents=tip_cents, basis_cents=basis_cents,
                                 tier_name=tier_name, media_ids=[],
                                 reward_message_id=None, body="")
        return {**base, "status": "ok", "reason": "no_unseen_media", "images_sent": 0}

    caption = apply_word_restriction(str(cfg.get("caption") or ""))

    if dry_run:
        return {**base, "status": "ok", "images_sent": len(media_ids),
                "media_ids": media_ids, "would_send": True}

    try:
        result = await asyncio.to_thread(
            lambda: client.send_message(fan_id, caption, media_files=media_ids, price=0)
        )
    except Exception as e:
        log.warning("tip_reward send failed account=%s fan=%s", account_id, fan_id, exc_info=True)
        return {**base, "status": "error", "images_sent": 0, "error": repr(e)[:300]}

    reward_message_id = result.get("id") if isinstance(result, dict) else None
    await _record_reward(account_id, fan_id, tip_message_id=tip_message_id,
                         tip_cents=tip_cents, basis_cents=basis_cents,
                         tier_name=tier_name, media_ids=media_ids,
                         reward_message_id=reward_message_id, body=caption)

    log.info("tip_reward sent account=%s fan=%s tip=%s basis=%s tier=%s images=%d msg=%s",
             account_id, fan_id, tip_cents, basis_cents, tier_name, len(media_ids),
             reward_message_id)
    return {**base, "status": "ok", "images_sent": len(media_ids),
            "media_ids": media_ids, "reward_message_id": reward_message_id}
