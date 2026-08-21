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
             At the $5/item default a $25 tip → 5 media items ($/5); a $3 tip
             still gets `min_images`.
  • BUNDLE = a tease warm-up share (_TEASE_SHARE from hot_teaser_free_folder,
             2 of 5) + the rest from the tier folders — with up to
             `context_pick_max` of those swapped for photos the LLM matched to
             what the fan asked for in the thread (context_pick_enabled, ON).
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
import logging
from datetime import datetime, timedelta

import automation_executor as ax        # _make_client / _parse_iso seams
from attribution import write_outbound_attribution
from automation_registry import register
from automations._common import (
    apply_word_restriction, load_operator_stop_ids, should_skip_muted_creator,
)
from automations.fan_state import fan_state, put_fan_state
from db.engine import get_session
from automations.tip_context import pick_context_media
from db.models import Fan, TipRewardLog, Transaction, VaultSend
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

# The config contract (defaults + loader) lives in tip_reward_config so this
# module and teaser_select both import it top-level, cycle-free.
from automations.tip_reward_config import _load_config

# Teaser selection lives in teaser_select.py — ONE home (split out 07-23), and
# this module does not import it at all any more. It used to, twice over: first
# as a second COPY of that whole surface whose local defs silently shadowed the
# imports (`tr.pick_hot_teaser` resolved here, not to teaser_select; five of the
# nine had diverged, so test_tip_reward was exercising an implementation
# production never ran), and then, after the copies went, as a block of twelve
# re-exports kept alive so `tr.<name>` still resolved for that same test file.
# The readers now name the module they mean. Two scars worth remembering:
#   • The CONVO ladder was the last fork — this module kept the proven-spend-floor
#     / climb-step variant while ai_chatter called teaser_select's simpler one with
#     `max_paid_cents`, which it did not accept, so every pick raised TypeError
#     into an `except Exception: log.debug` and the convo teaser never fired.
#   • Both scars are the same shape: a name that resolves to something other than
#     where a reader thinks it lives. Import the home, not a forwarder.
#
# What the reward pipeline needs is the pull UNDERNEATH the teaser ladder, plus
# the fan's sent-media set — neither of which is a teaser concept:
from automations._vault_pick import gather_unseen, pull_stages, resolve_folders
from ownership import seen_media
# Slot costing (2026-08-19): a clip is billed its rate-card worth in photo slots,
# so a $25 tip can never walk off with a $30 video. `PER_ITEM` is the unbudgeted
# default; `PHOTOS_ONLY` is the "Videos too" checkbox left off. Its own leaf —
# both this module and the ladder bill pulls, neither owns the rule.
from automations._slot_cost import (
    PER_ITEM, PHOTOS_ONLY, cents_per_slot, video_slot_cost,
)

log = logging.getLogger("of-relay.automation.tip_reward")

# Tip transaction kinds (mirror the attribution view + transactions.py).
_TIP_KINDS = ("tip", "tip_post", "tip_stream")

# How many items to pull per folder when scanning for unseen ones — generous so a
# fan deep into a folder still finds fresh media.
_VAULT_SCAN_LIMIT = 100

# Vault item types we reward with. Photos, videos (incl. DRM-only — still sendable
# as a vault attachment) and gifs; audio is excluded (not a "reward image/clip").
_REWARD_MEDIA_TYPES = ("photo", "video", "gif")

# Bundle composition: this share of the reward comes from the TEASE folder
# (hot_teaser_free_folder — the warm-up shots), the rest from the basis tier's
# folders. $25 → 5 items = 2 tease + 3 normal. No tease folder configured →
# the whole bundle stays a tier pull (legacy).
_TEASE_SHARE = 0.4


async def is_enabled(account_id: str) -> bool:
    """Cheap gate for the dispatcher: skip enqueuing a job for a disabled account."""
    cfg = await _load_config(account_id)
    return bool(cfg.get("enabled"))


async def reward_flags(account_id: str) -> tuple[bool, bool]:
    """(enabled, always_reward) in ONE config read — for the tip dispatcher.
    `always_reward` makes a tip reward fire even when an ai_chatter PPV offer is
    open for the fan (the standdown `on_inbound_tip` normally takes); the offer
    is still credited."""
    cfg = await _load_config(account_id)
    return bool(cfg.get("enabled")), bool(cfg.get("always_reward"))



def _media_count(tip_cents: int, cfg: dict) -> int:
    """clamp(min_images, tip_dollars // dollars_per_image, max_images). The config
    keys keep their `*_image*` names for back-compat. With `videos_in_rewards` on
    this number is a SLOT budget rather than an item count: a photo is one slot, a
    clip is whatever the rate card says it is worth in photos (`SlotCost`),
    and the bundle can only get SMALLER in items — never larger."""
    per = cents_per_slot(cfg)
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


# ── Inbound-image reply (Flag 1) ─────────────────────────────────────────────
# A fan sending US a photo is a buying signal — reply with ONE free vault item
# from the "under $10" (basic) tier. Reuses the tip path's folder/unseen/send
# machinery; the only new state is a per-fan cooldown so a photo-spamming fan
# can't drain a folder (and webhook replays of the same image don't re-send).
# Stamp lives in fans.custom_fields['_image_reply'] = {'at': iso} — an otherwise
# AI-owned JSON column, namespaced under a leading "_" → no migration, mirrors
# the cross-automation '*fix' typo throttle.
_IMAGE_REPLY_STATE_KEY = "_image_reply"


async def _image_reply_recent(account_id: str, fan_id: int, cooldown_hours: int) -> bool:
    """True if an image-reply freebie was sent to this fan within the last
    `cooldown_hours` (per-fan throttle; also dedups webhook replays). 0 → never
    throttle (every inbound image replies)."""
    if cooldown_hours <= 0:
        return False
    async with get_session() as s:
        fan = await s.get(Fan, (str(account_id), int(fan_id)))
    at = fan_state(fan, _IMAGE_REPLY_STATE_KEY).get("at")
    if not at:
        return False
    try:
        last = datetime.fromisoformat(at)
    except Exception:
        return False
    return (datetime.utcnow() - last) < timedelta(hours=max(1, int(cooldown_hours)))


async def _run_image_reply(account_id: str, payload: dict, cfg: dict, *,
                           dry_run: bool) -> dict:
    """Flag 1: a fan sent us a photo → send ONE (config: image_reply_count) free,
    unseen vault item from the 'under $10' tier straight back. NO tip required, NO
    TipRewardLog (there's no tip to key on); a per-fan cooldown both throttles a
    photo-spamming fan and dedups webhook replays. Mirrors the tip path's restricted
    skip, unseen filter and free (price=0) send."""
    fan_id = int(payload["fan_id"])
    force = bool(payload.get("force"))
    base = {"fan_id": fan_id, "image_reply": True, "dry_run": dry_run}

    # ONLY an operator stop blocks the pic-back (hand-restrict / OF-restrict /
    # muted peer-creator). Deliberately NOT the bot-inferred ladder reasons and
    # NOT automation_paused_until (that column is a routine send cooldown):
    # policy (07-23) — a fan's photo is ALWAYS answered unless a human said stop.
    if not force:
        async with get_session() as s:
            fan = await s.get(Fan, (str(account_id), fan_id))
        if fan_id in await load_operator_stop_ids(account_id) or should_skip_muted_creator(fan):
            return {**base, "status": "skipped", "reason": "restricted"}

    # Per-fan cooldown (also dedups webhook replays of the same image). `force`
    # (manual re-send) bypasses.
    cooldown_h = int(cfg.get("image_reply_cooldown_hours") or 0)
    if not force and await _image_reply_recent(account_id, fan_id, cooldown_h):
        return {**base, "status": "skipped", "reason": "throttled"}

    count = max(1, int(cfg.get("image_reply_count") or 1))
    basis_cents = max(0, int(cfg.get("image_reply_basis_cents") or 0))
    tier = _pick_tier(basis_cents, cfg)
    tier_name = tier.get("name") if tier else None
    folders = [f for f in (tier.get("folders") if tier else []) if str(f).strip()]
    base.update({"basis_cents": basis_cents, "tier": tier_name, "image_count": count})
    if not folders:
        return {**base, "status": "skipped", "reason": "no_folders", "images_sent": 0}

    client = await asyncio.to_thread(ax._make_client, account_id)
    by_name = await asyncio.to_thread(resolve_folders, client)
    seen = await seen_media(account_id, fan_id)
    # The images-only checkbox governs this freebie too, but the SLOT budget does
    # not: nothing was paid, so there is no price for a clip's length to overflow —
    # with the flag on, one clip is one freebie, as it always was.
    media_ids = await asyncio.to_thread(
        gather_unseen, client, folders, by_name, seen, count,
        cost=PER_ITEM if cfg.get("videos_in_rewards") else PHOTOS_ONLY)
    if not media_ids:
        # Fan has seen everything in the tier — nothing fresh to send back. Don't
        # stamp the cooldown (we sent nothing); the next image can try again.
        return {**base, "status": "ok", "reason": "no_unseen_media", "images_sent": 0}

    caption = apply_word_restriction(str(cfg.get("image_reply_caption") or ""))
    if dry_run:
        return {**base, "status": "ok", "images_sent": len(media_ids),
                "media_ids": media_ids, "would_send": True}

    try:
        result = await asyncio.to_thread(
            lambda: client.send_message(fan_id, caption, media_files=media_ids, price=0)
        )
    except Exception as e:
        log.warning("image_reply send failed account=%s fan=%s", account_id, fan_id,
                    exc_info=True)
        return {**base, "status": "error", "images_sent": 0, "error": repr(e)[:300]}

    reward_message_id = result.get("id") if isinstance(result, dict) else None
    now = datetime.utcnow()
    if reward_message_id:
        await write_outbound_attribution(
            account_id=account_id, fan_id=fan_id, message_id=int(reward_message_id),
            sent_by_employee_id=None, body=caption, price_cents=0, created_at=now,
            automation_kind="image_reply", emit_live=True,
        )
    # Persist the VaultSend rows AND the cooldown stamp in ONE transaction (mirrors
    # the tip path's _record_reward batching VaultSend + log). If these were two
    # commits, a crash between them would leave a SENT freebie with no cooldown
    # stamped — and the orphan-requeue on restart would re-run the job and double-
    # send (the unseen filter only caps it, doesn't prevent a 2nd freebie). The fan
    # row is upserted by event_transcoder before this fires, so `fan is None` is a
    # defensive best-effort skip. NOTE: this stamp can still race a concurrent
    # ai_chatter typo-throttle write of the SAME custom_fields row when both flags
    # (and ai_chatter + typos) are on — two un-leased jobs of different kinds. The
    # loser drops one key: bounded to one extra freebie OR one typo-throttle reset,
    # both self-healing on the next message. Accepted as a low-severity exposure.
    async with get_session() as s:
        for mid in media_ids:
            s.add(VaultSend(account_id=str(account_id), fan_id=fan_id, media_id=int(mid),
                            message_id=int(reward_message_id) if reward_message_id else None,
                            price_cents=0, sent_at=now))
        fan = await s.get(Fan, (str(account_id), fan_id))
        if fan is not None:
            put_fan_state(fan, _IMAGE_REPLY_STATE_KEY, {"at": now.isoformat()})

    log.info("image_reply sent account=%s fan=%s tier=%s images=%d msg=%s",
             account_id, fan_id, tier_name, len(media_ids), reward_message_id)
    return {**base, "status": "ok", "images_sent": len(media_ids),
            "media_ids": media_ids, "reward_message_id": reward_message_id}


async def _compose_reward_bundle(client, account_id: str, fan_id: int, cfg: dict,
                                 folders: list[str], count: int, seen: set[int],
                                 ) -> tuple[list[int], dict]:
    """The reward's bundle plan (2026-07-23): tip$/5 items total. A tease warm-up
    share comes from the free-tease folder (2 of 5), the rest ("normal" slots)
    from the basis tier's folders — and when the context matcher is on, up to
    context_pick_max of those normal slots are swapped for photos matching what
    he asked for in the thread. Total NEVER exceeds `count`. Returns (media_ids
    in escalating order, the `bundle` breakdown run() reports)."""
    tease_folder = str(cfg.get("hot_teaser_free_folder") or "").strip()
    tease_n = (min(count - 1, round(count * _TEASE_SHARE))
               if (count >= 2 and tease_folder) else 0)
    normal_n = count - tease_n

    matched: list[int] = []
    if cfg.get("context_pick_enabled"):
        matched = await pick_context_media(
            account_id, fan_id, seen=seen,
            limit=min(int(cfg.get("context_pick_max") or 0), normal_n),
            n_msgs=int(cfg.get("context_pick_messages") or 20))

    # Videos ride only with the checkbox on, and only WITH a coster: `count`
    # becomes a slot budget, so a clip is charged its rate-card worth in
    # photo-slots and a $25 tip can never walk off with a $30 video. OFF (the
    # default) strips clips at the scan and never pays the mirror read.
    cost = (await video_slot_cost(account_id, cents_per_slot(cfg))
            if cfg.get("videos_in_rewards") else PHOTOS_ONLY)

    (tease_ids, normal_ids), extras = await asyncio.to_thread(
        pull_stages, client,
        [([tease_folder] if tease_n else [], tease_n),
         (folders, normal_n - len(matched))],
        set(seen) | set(matched),
        # Tease warm-up is filler — may repeat, never blocks the bundle; but
        # never re-pick a photo the matcher already reserved for the payoff.
        repeat_ok={0}, never_repeat=set(matched), cost=cost)
    # Escalating order: tease warm-up → tier shots (+ any backfill) → the
    # matched-to-his-ask photos land last (the payoff).
    media_ids = tease_ids + normal_ids + extras + matched
    return media_ids, {"tease": len(tease_ids),
                       "normal": len(normal_ids) + len(extras),
                       "matched": list(matched)}


@register("tip_reward")
async def run(account_id: str, payload: dict, *, run_id: int) -> dict:
    payload = payload or {}
    dry_run = bool(payload.get("dry_run"))

    # Inbound-image path (Flag 1) — a separate trigger from a tip; its own gate
    # and machinery. Branches BEFORE the tip-required check below.
    if payload.get("image_reply"):
        cfg = await _load_config(account_id)
        if not cfg.get("image_reply_enabled"):
            return {"status": "skipped", "reason": "image_reply_disabled",
                    "fan_id": payload.get("fan_id")}
        return await _run_image_reply(account_id, payload, cfg, dry_run=dry_run)

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

    # ONLY an operator stop blocks a reward (hand-restrict / OF-restrict / muted
    # peer-creator) — a man who just PAID gets his reward regardless of what the
    # ladder classifier concluded about him. `force` (manual re-reward) bypasses.
    if not force:
        async with get_session() as s:
            fan = await s.get(Fan, (str(account_id), fan_id))
        if fan_id in await load_operator_stop_ids(account_id) or should_skip_muted_creator(fan):
            return {"status": "skipped", "reason": "restricted", "fan_id": fan_id}

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
    seen = await seen_media(account_id, fan_id)
    media_ids, base["bundle"] = await _compose_reward_bundle(
        client, account_id, fan_id, cfg, folders, count, seen)

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
