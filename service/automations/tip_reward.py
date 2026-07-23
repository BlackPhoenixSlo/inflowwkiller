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
import json
import logging
from datetime import datetime, timedelta

import automation_executor as ax        # _make_client / _parse_iso seams
from attribution import write_outbound_attribution
from automation_registry import register
from automations._common import (
    DEFAULT_TIP_ASK_ENABLED, apply_word_restriction, load_hard_skip_ids,
    should_skip_muted_creator,
)
import random
from db.engine import get_session
from automations import tip_ladder
from automations.tip_context import pick_context_media
from db.models import AccountAiConfig, Fan, TipRewardLog, Transaction, VaultSend
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

log = logging.getLogger("of-relay.automation.tip_reward")

# Tip transaction kinds (mirror the attribution view + transactions.py).
_TIP_KINDS = ("tip", "tip_post", "tip_stream")

# Built-in defaults — used for any key the account config omits. DISABLED + empty
# folders so the automation is inert until a creator configures it.
_DEFAULTS: dict = {
    "enabled": False,
    # "Always reward" — fire the reward on EVERY tip even when an ai_chatter PPV
    # offer is open for the fan (the normal standdown, so the fan doesn't get
    # bonus media on top of the unlock). The offer is STILL credited; the reward
    # just also fires. Default OFF (keep the standdown). See webhook_dispatch.
    "always_reward": False,
    "dollars_per_image": 5,    # 1 media item per $5 of the tip ($25 → 5 photos)
    "min_images": 2,           # any tip ≥ $0.01 still gets at least this many
                               # (house default 2 since 07-23: one photo reads
                               # stingy as a thank-you; two is a real reward)
    "max_images": 12,          # cap so a whale tip can't drain a folder in one shot
                               # (12 = the bundle hard cap; $60 → the full 12)
    "caption": "",             # optional thank-you text ('' → media-only message)
    "window_hours": 72,        # rolling window for the cumulative tier basis
    # ── Context-aware picking (2026-07-23, the off-promise reward incident) ──
    # The seller's LLM promises SPECIFIC content ("the whole lingerie set") but
    # the reward used to pull blindly from the tier folder — off-promise every
    # time the promise wasn't that folder. When ON, the reward reads the last
    # `context_pick_messages` thread messages, matches them against the local
    # vault-AI descriptions (VaultItem.search_text / describe fields) and swaps
    # up to `context_pick_max` of the bundle's normal slots for photos that
    # match what he asked for / was promised. No candidates or any LLM failure
    # → clean fallback to the pure folder pull (never blocks a reward).
    "context_pick_enabled": True,
    "context_pick_max": 3,         # at most this many matched swaps per reward
    "context_pick_messages": 20,   # thread messages the matcher reads
    # The ASK side of the loop (read by of_ai_chat/autoreply, not tip_reward itself):
    # when a fan asks to SEE content via text, those senders ask him to tip. ON by
    # default; ask_amount_dollars=None → she asks naturally WITHOUT naming a price
    # (set a number to suggest one). One config home for the whole tip loop.
    "ask_enabled": DEFAULT_TIP_ASK_ENABLED,
    "ask_amount_dollars": None,
    "ask_template": "",        # optional phrasing seed ('' → the model phrases it)
    # ── Inbound-IMAGE buying-signal handler (a fan sends a photo) ────────────
    # A fan sending US a picture is a buying signal ("look what I've got" → "what
    # have YOU got"). Two independent switches, both fired by webhook_dispatch.
    # on_inbound_image off an inbound, non-tip media DM. Default OFF.
    "image_reply_enabled": False,   # Flag 1: send ONE free vault image straight back
    "image_closer_enabled": False,  # Flag 2: kick the ai_chatter CLOSER for this fan
    # Image-reply knobs (only matter when image_reply_enabled):
    "image_reply_count": 1,            # how many free items to send back (usually 1)
    "image_reply_basis_cents": 999,    # tier basis for the freebie folder — "under
                                       # $10 spend" → the basic tier (mid starts at
                                       # $10 = 1000c). _pick_tier walks DOWN from here.
    "image_reply_cooldown_hours": 6,   # per-fan throttle so a photo-spamming fan
                                       # doesn't drain a folder; also dedups webhook
                                       # replays. 0 → every inbound image (replay risk).
    "image_reply_caption": "",         # optional caption ('' → media-only)
    # ── Hot-thread proactive teaser (SELECTED here, SENT by ai_chatter) ───────
    # When ai_chatter's thread_heat says the thread is HOT and no priced offer is
    # already going out this turn, attach a few UNSEEN vault items to the reply she
    # is already sending — FREE to warm a fan up, or a priced tease PPV for a proven
    # buyer. The images ARE the lead-up: a hot thread that only ever gets words is
    # the exact gap this closes. NOT an extra message — it rides her reply, so it
    # spends no extra cadence. Spend-gated: lifetime_spend == 0 → free branch (hard-
    # capped per fan so a $0 fan can't farm content); lifetime_spend > 0 → paid PPV.
    # Default OFF; inert until at least one folder is filled. See ai_chatter.
    "hot_teaser_enabled": False,
    "hot_teaser_count": 3,                 # vault items per teaser
    "hot_teaser_cooldown_hours": 6,        # per-fan throttle (BOTH branches)
    "hot_teaser_free_folder": "",          # vault folder for $0 fans (sent FREE)
    "hot_teaser_free_max": 3,              # hard cap on FREE teasers a fan ever gets
    "hot_teaser_paid_folder": "",          # vault folder for proven buyers (priced)
    "hot_teaser_price_cents": 1500,        # price of the paid tease PPV ($15)
    # ── Price-scaled photo bundle (workstream 3) ─────────────────────────────
    # When on, a PAID teaser's photo count SCALES with its price by the weight-
    # budget model (tip_ladder.bundle_plan): budget = price / $10 photos, which
    # auto-satisfies ≥3-over-$30 / ≥5-over-$50. The free branch sends
    # `bundle_free_count`. Default OFF → legacy fixed hot_teaser_count unchanged.
    "bundle_scaling_enabled": False,
    "bundle_free_count": 1,                # free-branch taste (0 = none)
    "bundle_cents_per_weight": 1000,       # $10 of ask → 1 weight/photo of budget
    "bundle_hard_cap": 12,                 # never drain a folder in one send
    # ── Conversational teaser LADDER (SELECTED here, SENT by ai_chatter) ──────
    # Not gated on thread_heat — fires during ORDINARY chat. After every
    # `teaser_convo_after_fan_msgs` of HIS messages (counted since the last teaser),
    # send the next RUNG: rung 0 is usually a free tease, rung 1 the $10 one, rung 2
    # the $50 one — the price CLIMBS as the conversation goes. The rung advances one
    # step each time a convo teaser lands (and holds at the top). Default OFF; inert
    # until rungs have folders. Shares the hot-teaser's per-fan state + brakes.
    "teaser_convo_enabled": False,
    "teaser_convo_after_fan_msgs": 20,     # HIS messages between rungs
    "teaser_convo_count": 1,               # vault items per tease (legacy single-folder)
    "teaser_convo_rungs": [
        {"folder": "", "price_cents": 0},       # rung 0 — free tease
        {"folder": "", "price_cents": 1000},    # $10
        {"folder": "", "price_cents": 4000},    # $40
        {"folder": "", "price_cents": 8000},    # $80
        {"folder": "", "price_cents": 12000},   # $120
        {"folder": "", "price_cents": 16000},   # $160
        {"folder": "", "price_cents": 20000},   # $200
    ],
    # ── ADAPTIVE convo teaser (DEFAULT ON since a live incident where legacy
    # climb-every-send walked an unbought fan $40→$80→$120→$160→$200 overnight,
    # with the fan complaining about the price in-thread) ─────────────────────
    # The ladder climbs ONLY when the teaser she sent actually SELLS (her own
    # teaser unlock — NEVER an ai_chatter catalog buy), and on a no-buy it
    # SOFTENS the ask to 65–73% of the last price (floored at 0 → down to a
    # free tease), holding the rung. Photos come from a price-scaled WEIGHTED
    # BUNDLE (bundle_plan → the same premium/normal/free tiers the hot teaser
    # uses); when no tier folders are configured it falls back to the rung's own
    # folder. An explicit stored `false` restores legacy climb-every-send.
    "teaser_convo_adaptive": True,
    "teaser_convo_cut_lo": 0.65,           # no-buy soften keeps 65–73% of last ask
    "teaser_convo_cut_hi": 0.73,
    "teaser_convo_floor_cents": 0,         # soften floor ($0 → eases to a free tease)
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

# Bundle composition: this share of the reward comes from the TEASE folder
# (hot_teaser_free_folder — the warm-up shots), the rest from the basis tier's
# folders. $25 → 5 items = 2 tease + 3 normal. No tease folder configured →
# the whole bundle stays a tier pull (legacy).
_TEASE_SHARE = 0.4


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


async def reward_flags(account_id: str) -> tuple[bool, bool]:
    """(enabled, always_reward) in ONE config read — for the tip dispatcher.
    `always_reward` makes a tip reward fire even when an ai_chatter PPV offer is
    open for the fan (the standdown `on_inbound_tip` normally takes); the offer
    is still credited."""
    cfg = await _load_config(account_id)
    return bool(cfg.get("enabled")), bool(cfg.get("always_reward"))


async def image_reply_flags(account_id: str) -> tuple[bool, bool]:
    """(image_reply_enabled, image_closer_enabled) in ONE config read — for the
    inbound-image dispatcher (webhook_dispatch.on_inbound_image). Both default
    OFF and are independent of the tip `enabled` master switch: a fan sending a
    photo can trigger a freebie and/or the closer without tip rewards being on."""
    cfg = await _load_config(account_id)
    return bool(cfg.get("image_reply_enabled")), bool(cfg.get("image_closer_enabled"))


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


# ── Inbound-image reply (Flag 1) ─────────────────────────────────────────────
# A fan sending US a photo is a buying signal — reply with ONE free vault item
# from the "under $10" (basic) tier. Reuses the tip path's folder/unseen/send
# machinery; the only new state is a per-fan cooldown so a photo-spamming fan
# can't drain a folder (and webhook replays of the same image don't re-send).
# Stamp lives in fans.custom_fields['_image_reply'] = {'at': iso} — an otherwise
# AI-owned JSON column, namespaced under a leading "_" → no migration, mirrors
# the cross-automation '*fix' typo throttle.
_IMAGE_REPLY_STATE_KEY = "_image_reply"


def _load_custom_fields(fan: Fan | None) -> dict:
    try:
        cf = json.loads(fan.custom_fields) if fan and fan.custom_fields else {}
        return cf if isinstance(cf, dict) else {}
    except Exception:
        return {}


async def _image_reply_recent(account_id: str, fan_id: int, cooldown_hours: int) -> bool:
    """True if an image-reply freebie was sent to this fan within the last
    `cooldown_hours` (per-fan throttle; also dedups webhook replays). 0 → never
    throttle (every inbound image replies)."""
    if cooldown_hours <= 0:
        return False
    async with get_session() as s:
        fan = await s.get(Fan, (str(account_id), int(fan_id)))
    at = (_load_custom_fields(fan).get(_IMAGE_REPLY_STATE_KEY) or {}).get("at")
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

    # Durably restricted (muted peer-creator / hand-restricted) → never auto-reply.
    if not force:
        async with get_session() as s:
            fan = await s.get(Fan, (str(account_id), fan_id))
        if fan_id in await load_hard_skip_ids(account_id) or should_skip_muted_creator(fan):
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
    by_name = await asyncio.to_thread(_resolve_folders, client, folders)
    seen = await _seen_media(account_id, fan_id)
    media_ids = await asyncio.to_thread(_gather_unseen, client, folders, by_name, seen, count)
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
            cf = _load_custom_fields(fan)
            cf[_IMAGE_REPLY_STATE_KEY] = {"at": now.isoformat()}
            fan.custom_fields = json.dumps(cf)

    log.info("image_reply sent account=%s fan=%s tier=%s images=%d msg=%s",
             account_id, fan_id, tier_name, len(media_ids), reward_message_id)
    return {**base, "status": "ok", "images_sent": len(media_ids),
            "media_ids": media_ids, "reward_message_id": reward_message_id}


# ── Hot-thread proactive teaser ──────────────────────────────────────────────
# ai_chatter owns the trigger (thread_heat) and the SEND (it attaches the media to
# the reply it is already composing). These three helpers are the only tip_reward
# surface it touches: a cheap enabled-gate, a pure SELECTION (no send), and a
# post-send record. State lives in fans.custom_fields['_hot_teaser'] = {'at': iso,
# 'free_sent': N} — same AI-owned JSON column, leading-"_" namespace, no migration,
# mirroring the '_image_reply' cooldown above.
_HOT_TEASER_STATE_KEY = "_hot_teaser"


async def hot_teaser_config(account_id: str) -> dict | None:
    """The hot-teaser knobs iff enabled, else None — one config read for ai_chatter's
    per-run setup (None ⇒ don't even look per fan). Independent of the tip `enabled`
    master switch: warming a hot thread with vault media has nothing to do with
    rewarding tips."""
    cfg = await _load_config(account_id)
    if not cfg.get("hot_teaser_enabled"):
        return None
    return {
        "count": max(1, int(cfg.get("hot_teaser_count") or 1)),
        "cooldown_hours": max(0, int(cfg.get("hot_teaser_cooldown_hours") or 0)),
        "free_folder": str(cfg.get("hot_teaser_free_folder") or "").strip(),
        "free_max": max(0, int(cfg.get("hot_teaser_free_max") or 0)),
        "paid_folder": str(cfg.get("hot_teaser_paid_folder") or "").strip(),
        "price_cents": max(0, int(cfg.get("hot_teaser_price_cents") or 0)),
        # Price-scaled bundle knobs (default-off; pick_hot_teaser reads them).
        "bundle_scaling_enabled": bool(cfg.get("bundle_scaling_enabled")),
        "bundle_free_count": max(0, int(cfg.get("bundle_free_count") or 0)),
        "bundle_cents_per_weight": max(1, int(cfg.get("bundle_cents_per_weight") or 1000)),
        "bundle_hard_cap": max(1, int(cfg.get("bundle_hard_cap") or 12)),
        # Tier→folder mapping for the composed bundle (reuse the tiers config):
        # premium = the 'premium' tier's folders, normal = basic+mid, free = the
        # hot-teaser free folder. Empty lists → that tier contributes nothing.
        "bundle_premium_folders": _tier_folders(cfg, "premium"),
        "bundle_normal_folders": _tier_folders(cfg, "basic") + _tier_folders(cfg, "mid"),
        "bundle_free_folders": [f for f in [str(cfg.get("hot_teaser_free_folder") or "").strip()] if f],
    }


def _tier_folders(cfg: dict, tier_name: str) -> list[str]:
    """The non-empty folder names for the named tier in the `tiers` config."""
    for t in cfg.get("tiers") or []:
        if isinstance(t, dict) and str(t.get("name") or "").strip().lower() == tier_name:
            return [str(f).strip() for f in (t.get("folders") or []) if str(f).strip()]
    return []


def _pull_stages(client, stages: list[tuple[list[str], int]],
                 seen: set[int], *,
                 repeat_ok: frozenset[int] | set[int] = frozenset(),
                 never_repeat: frozenset[int] | set[int] = frozenset(),
                 ) -> tuple[list[list[int]], list[int]]:
    """THE one folder-bundle composer: ordered stage pulls with cross-stage dedup
    + shortfall backfill. Each (folder_names, count) stage pulls up to `count`
    unseen items from its folders; a short stage does not shrink the bundle —
    the total shortfall is backfilled from ALL stages' folders at the end.

    `repeat_ok` names stage INDICES whose folders hold tease/filler content
    (operator ruling 07-23: filler may repeat, and a filler shortfall must
    never shrink what tease-share we promised). When the unseen backfill still
    leaves the bundle short, those stages' folders are re-pulled ignoring the
    fan's send history — capped at the repeat_ok stages' OWN share (a payoff
    shortfall keeps shrinking the bundle: the price is for the payoff, and a
    priced send of 100% repeated tease is not a send), and deduped against
    THIS bundle plus `never_repeat` (ids the CALLER will append itself, e.g.
    tip_reward's context-matched photos) so a repeat never duplicates within
    one send. Payoff stages are never repeated from.

    Returns (per_stage_ids, backfill_ids). Both the teaser composer and the tip
    reward's tease/normal split ride this. Sync (OF reads) — call via to_thread."""
    stages = [([f for f in (fs or []) if str(f).strip()], int(n))
              for fs, n in stages]
    taken = set(seen)
    bundle: set[int] = set()
    picked: list[list[int]] = []
    for folders, n in stages:
        ids: list[int] = []
        if n > 0 and folders:
            by_name = _resolve_folders(client, folders)
            ids = _gather_unseen(client, folders, by_name, taken, n)
            taken.update(ids)
            bundle.update(ids)
        picked.append(ids)
    want = sum(max(0, n) for _, n in stages)
    short = want - sum(len(p) for p in picked)
    extras: list[int] = []
    if short > 0:
        all_folders = [f for folders, _ in stages for f in folders]
        if all_folders:
            by_name = _resolve_folders(client, all_folders)
            extras = _gather_unseen(client, all_folders, by_name, taken, short)
            bundle.update(extras)
    short -= len(extras)
    if short > 0 and repeat_ok:
        # Repeats may only restore the repeat_ok stages' OWN share — a payoff
        # shortfall keeps shrinking the bundle rather than becoming filler.
        repeat_want = sum(max(0, n) for i, (_, n) in enumerate(stages)
                          if i in repeat_ok)
        repeat_have = sum(len(picked[i]) for i in repeat_ok if i < len(picked))
        short = min(short, max(0, repeat_want - repeat_have))
        r_folders = [f for i, (folders, _) in enumerate(stages)
                     if i in repeat_ok for f in folders]
        if short > 0 and r_folders:
            by_name = _resolve_folders(client, r_folders)
            extras = extras + _gather_unseen(
                client, r_folders, by_name,
                set(bundle) | set(never_repeat), short)
    return picked, extras


def _compose_bundle_ids(client, plan, seen: set[int],
                        premium_folders: list[str], normal_folders: list[str],
                        free_folders: list[str]) -> tuple[list[int], dict]:
    """Teaser adapter over _pull_stages: pull the plan's premium/normal/free photo
    ids from their tier folders (dedup + backfill live in _pull_stages) and keep
    the breakdown/weight shape the teaser callers report. Sync — call via
    to_thread."""
    (prem, norm, free), extras = _pull_stages(
        client, [(premium_folders, plan.premium), (normal_folders, plan.normal),
                 (free_folders, plan.free)], seen,
        repeat_ok={2})  # the free tier is tease filler — may repeat, never blocks
    out = prem + norm + free + extras
    weight = len(prem) * tip_ladder.WEIGHT_PREMIUM + len(norm) * tip_ladder.WEIGHT_STANDARD
    return out, {"premium": len(prem), "normal": len(norm), "free": len(free),
                 "total": len(out), "weight": weight}


async def pick_hot_teaser(client, account_id: str, fan_id: int, *,
                          lifetime_spend_cents: int, tcfg: dict,
                          now: datetime | None = None) -> dict | None:
    """Pure SELECTION for ai_chatter's hot-thread teaser — resolves the spend branch,
    the per-fan cooldown/free-cap, and the unseen vault items. Returns
    {media_ids, price_cents, is_free, folder} or None (throttled / capped / no folder /
    nothing unseen left). Sends NOTHING and writes NOTHING: ai_chatter attaches the
    media to its reply and calls `record_hot_teaser` only once the send confirms — so a
    dropped/failed reply never burns the cooldown or the fan's free allowance."""
    now = now or datetime.utcnow()
    async with get_session() as s:
        fan = await s.get(Fan, (str(account_id), int(fan_id)))
    state = _load_custom_fields(fan).get(_HOT_TEASER_STATE_KEY) or {}
    state = state if isinstance(state, dict) else {}

    cd = int(tcfg.get("cooldown_hours") or 0)
    if cd > 0 and state.get("at"):
        try:
            if (now - datetime.fromisoformat(str(state["at"]))) < timedelta(hours=cd):
                return None
        except Exception:
            pass  # unparseable stamp → treat as no cooldown, re-stamp on this send

    # Spend branch. A fan who has PAID (lifetime > 0) is a proven buyer → priced tease
    # PPV. A $0 fan gets a FREE warm-up, but only `free_max` of them across his life so
    # a perpetual free-loader can't drain the folder one hot thread at a time.
    is_free = int(lifetime_spend_cents or 0) <= 0
    if is_free:
        if int(state.get("free_sent") or 0) >= int(tcfg.get("free_max") or 0):
            return None
        folder = str(tcfg.get("free_folder") or "").strip()
        price_cents = 0
    else:
        folder = str(tcfg.get("paid_folder") or "").strip()
        price_cents = int(tcfg.get("price_cents") or 0)

    seen = await _seen_media(account_id, fan_id)
    scaling = bool(tcfg.get("bundle_scaling_enabled"))
    plan = tip_ladder.bundle_plan(
        price_cents,
        cents_per_weight=int(tcfg.get("bundle_cents_per_weight") or 1000),
        free_count=int(tcfg.get("bundle_free_count") or 0),
        hard_cap=int(tcfg.get("bundle_hard_cap") or 12),
    ) if scaling else None

    # PAID + scaling: compose premium/normal/free from the tier folders (a
    # full-looking set whose value is concentrated in the premium shots). Does
    # NOT require the single paid_folder — the tiers config supplies the media.
    if scaling and not is_free:
        if plan.total <= 0:
            return None
        media_ids, breakdown = await asyncio.to_thread(
            _compose_bundle_ids, client, plan, seen,
            list(tcfg.get("bundle_premium_folders") or []),
            list(tcfg.get("bundle_normal_folders") or []),
            list(tcfg.get("bundle_free_folders") or []),
        )
        if not media_ids:
            return None
        return {"media_ids": media_ids, "price_cents": int(price_cents),
                "is_free": False, "folder": "composed", "bundle": breakdown}

    # Otherwise a single-folder pull. Count is the legacy fixed value, or the
    # bundle TOTAL when scaling is on (the free branch has no tiers to compose).
    if not folder:
        return None
    if scaling:
        count = plan.total
        if count <= 0:  # free_count=0 → nothing to send
            return None
    else:
        count = max(1, int(tcfg.get("count") or 1))
    by_name = await asyncio.to_thread(_resolve_folders, client, [folder])
    media_ids = await asyncio.to_thread(_gather_unseen, client, [folder], by_name, seen, count)
    if not media_ids:
        return None
    return {"media_ids": media_ids, "price_cents": int(price_cents),
            "is_free": is_free, "folder": folder}


async def record_hot_teaser(account_id: str, fan_id: int, *, media_ids: list[int],
                            message_id: int | None, price_cents: int, is_free: bool,
                            set_rung: int | None = None,
                            now: datetime | None = None) -> None:
    """After the teaser media actually went out on ai_chatter's reply: one VaultSend
    per item (so the unseen filter never re-attaches it) and the per-fan cooldown +
    free-counter bump, in ONE transaction (a crash between them would let the unseen
    filter re-attach OR the cap re-trip). Mirrors `_run_image_reply`'s batching.
    `set_rung` (convo ladder) stamps the fan's NEXT rung so the price climbs."""
    now = now or datetime.utcnow()
    async with get_session() as s:
        for mid in media_ids:
            s.add(VaultSend(account_id=str(account_id), fan_id=int(fan_id),
                            media_id=int(mid),
                            message_id=int(message_id) if message_id else None,
                            price_cents=int(price_cents or 0), sent_at=now))
        fan = await s.get(Fan, (str(account_id), int(fan_id)))
        if fan is not None:
            cf = _load_custom_fields(fan)
            st = cf.get(_HOT_TEASER_STATE_KEY)
            st = st if isinstance(st, dict) else {}
            st["at"] = now.isoformat()
            if is_free:
                st["free_sent"] = int(st.get("free_sent") or 0) + 1
            if set_rung is not None:
                st["rung"] = int(set_rung)
            # Adaptive convo-teaser buy-detection: remember the exact ask + its
            # message so next turn can ask "did THIS teaser sell?" (Message.is_paid
            # on this id) — scoped to her own teaser, never an ai_chatter catalog buy.
            st["last_price"] = int(price_cents or 0)
            st["last_msg"] = int(message_id) if message_id else None
            st["last_free"] = bool(is_free)
            cf[_HOT_TEASER_STATE_KEY] = st
            fan.custom_fields = json.dumps(cf)


def teaser_state(fan: Fan | None) -> dict:
    """The fan's teaser state ({at, free_sent, rung}) parsed off the Fan row ai_chatter
    already has in hand — no extra DB read. `at` (iso) is the last teaser, `rung` the
    convo-ladder position."""
    st = _load_custom_fields(fan).get(_HOT_TEASER_STATE_KEY)
    return st if isinstance(st, dict) else {}


async def convo_teaser_config(account_id: str) -> dict | None:
    """The conversational-ladder knobs iff enabled, else None — one config read for
    ai_chatter's per-run setup. Each rung is {folder, price_cents}; a rung with no
    folder is a dead step (skipped at selection)."""
    cfg = await _load_config(account_id)
    if not cfg.get("teaser_convo_enabled"):
        return None
    rungs = []
    for r in (cfg.get("teaser_convo_rungs") or []):
        if not isinstance(r, dict):
            continue
        # Per-rung image `count` (e.g. $10→1, $30→3, $50→5). 0/absent → fall back to
        # the ladder-wide teaser_convo_count.
        rungs.append({"folder": str(r.get("folder") or "").strip(),
                      "price_cents": max(0, int(r.get("price_cents") or 0)),
                      "count": max(0, int(r.get("count") or 0))})
    return {
        "after": max(1, int(cfg.get("teaser_convo_after_fan_msgs") or 20)),
        "count": max(1, int(cfg.get("teaser_convo_count") or 1)),
        "rungs": rungs,
        # Adaptive (buy-aware climb / soften) + the weighted-bundle knobs it shares
        # with the hot teaser. All inert unless teaser_convo_adaptive is on.
        "adaptive": bool(cfg.get("teaser_convo_adaptive")),
        "cut_lo": float(cfg.get("teaser_convo_cut_lo") or 0.65),
        "cut_hi": float(cfg.get("teaser_convo_cut_hi") or 0.73),
        "floor_cents": max(0, int(cfg.get("teaser_convo_floor_cents") or 0)),
        "bundle_cents_per_weight": max(1, int(cfg.get("bundle_cents_per_weight") or 1000)),
        "bundle_free_count": max(0, int(cfg.get("bundle_free_count") or 0)),
        "bundle_hard_cap": max(1, int(cfg.get("bundle_hard_cap") or 12)),
        "bundle_premium_folders": _tier_folders(cfg, "premium"),
        "bundle_normal_folders": _tier_folders(cfg, "basic") + _tier_folders(cfg, "mid"),
        "bundle_free_folders": [f for f in [str(cfg.get("hot_teaser_free_folder") or "").strip()] if f],
    }


async def pick_convo_teaser(client, account_id: str, fan_id: int, *, tcfg: dict,
                            msgs_since_last: int, rung: int,
                            last_price_cents: int = 0, last_sold: bool = False,
                            last_was_free: bool = False, rand: float | None = None,
                            now: datetime | None = None) -> dict | None:
    """SELECTION for the conversational ladder. Fires only once he has sent `after`
    messages since his last teaser.

    LEGACY (tcfg['adaptive'] falsy): picks the CURRENT rung's single folder + price;
    climbs one rung every send. Byte-for-byte the old behavior.

    ADAPTIVE (tcfg['adaptive'] on): the ladder ($0/$10/$40/$80/$120/$160/$200)
    moves on WHAT HAPPENED to the last teaser — climb one rung if it SOLD (or was a
    free tease he received), else SOFTEN to 65–73% of the last ask (floored at
    floor_cents, holding the rung). Photos come from a price-scaled WEIGHTED BUNDLE
    (bundle_plan → premium/normal/free tier folders), free rung → a free taste.
    `last_sold`/`last_price_cents`/`last_was_free` are supplied by the caller from
    HER OWN teaser's sold state; `rand` is injectable for deterministic tests.

    Returns {media_ids, price_cents, is_free, folder, rung, next_rung, convo:True
    (+softened/bundle in adaptive)} or None."""
    rungs = tcfg.get("rungs") or []
    if not rungs or msgs_since_last < int(tcfg.get("after") or 0):
        return None

    if not tcfg.get("adaptive"):
        idx = max(0, min(int(rung or 0), len(rungs) - 1))
        r = rungs[idx]
        folder = str(r.get("folder") or "").strip()
        price_cents = max(0, int(r.get("price_cents") or 0))
        if not folder:
            return None
        # Per-rung image count wins ($10→1, $30→3, $50→5); else the ladder-wide count.
        count = max(1, int(r.get("count") or 0) or int(tcfg.get("count") or 1))
        by_name = await asyncio.to_thread(_resolve_folders, client, [folder])
        seen = await _seen_media(account_id, fan_id)
        media_ids = await asyncio.to_thread(_gather_unseen, client, [folder], by_name, seen, count)
        if not media_ids:
            return None
        return {"media_ids": media_ids, "price_cents": int(price_cents),
                "is_free": price_cents == 0, "folder": folder, "rung": idx,
                "next_rung": min(idx + 1, len(rungs) - 1), "convo": True}

    # ── ADAPTIVE: buy-aware climb / soften, price-scaled weighted bundle ──
    idx = max(0, min(int(rung or 0), len(rungs) - 1))
    prices = [max(0, int((r or {}).get("price_cents") or 0)) for r in rungs]
    last_px = max(0, int(last_price_cents or 0))
    first_ever = last_px <= 0 and idx == 0 and not last_sold and not last_was_free
    softened = False
    if first_ever:
        new_idx, price = 0, prices[0]
    elif last_sold or last_was_free:                 # it landed → climb a rung
        new_idx = min(idx + 1, len(rungs) - 1)
        price = prices[new_idx]
    else:                                            # no buy → soften, hold the rung
        lo = float(tcfg.get("cut_lo") or 0.65)
        hi = float(tcfg.get("cut_hi") or 0.73)
        if lo > hi:
            lo, hi = hi, lo
        rv = random.random() if rand is None else float(rand)
        frac = lo + (hi - lo) * max(0.0, min(1.0, rv))
        floor = max(0, int(tcfg.get("floor_cents") or 0))
        price = max(floor, int(round(last_px * frac)))
        new_idx, softened = idx, True

    if price <= 0:
        plan = tip_ladder.bundle_plan(0, free_count=max(1, int(tcfg.get("bundle_free_count") or 1)))
    else:
        plan = tip_ladder.bundle_plan(
            price, cents_per_weight=int(tcfg.get("bundle_cents_per_weight") or 1000),
            free_count=int(tcfg.get("bundle_free_count") or 0),
            hard_cap=int(tcfg.get("bundle_hard_cap") or 12))
    if plan.total <= 0:
        return None
    seen = await _seen_media(account_id, fan_id)
    media_ids, breakdown = await asyncio.to_thread(
        _compose_bundle_ids, client, plan, seen,
        list(tcfg.get("bundle_premium_folders") or []),
        list(tcfg.get("bundle_normal_folders") or []),
        list(tcfg.get("bundle_free_folders") or []))
    if not media_ids:
        # No tier folders configured (or all exhausted). Adaptive is the house
        # default now, and an account whose media lives in the per-rung folders
        # must not go silent — pull from the rung's own folder instead; only
        # the PHOTO SOURCE falls back, the price stays adaptive.
        r = rungs[new_idx] if isinstance(rungs[new_idx], dict) else {}
        folder = str(r.get("folder") or "").strip()
        if not folder:
            return None
        count = max(1, int(r.get("count") or 0) or int(tcfg.get("count") or 1))
        by_name = await asyncio.to_thread(_resolve_folders, client, [folder])
        media_ids = await asyncio.to_thread(
            _gather_unseen, client, [folder], by_name, seen, count)
        if not media_ids:
            return None
        return {"media_ids": media_ids, "price_cents": int(price),
                "is_free": price <= 0, "folder": folder, "rung": new_idx,
                "next_rung": new_idx, "convo": True, "softened": softened}
    return {"media_ids": media_ids, "price_cents": int(price),
            "is_free": price <= 0, "folder": "composed", "rung": new_idx,
            "next_rung": new_idx, "convo": True, "softened": softened,
            "bundle": breakdown}


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

    # Durably restricted (muted peer-creator / hand-restricted "no automations")
    # → never auto-reward, even on a real tip. `force` (manual re-reward) bypasses.
    if not force:
        async with get_session() as s:
            fan = await s.get(Fan, (str(account_id), fan_id))
        if fan_id in await load_hard_skip_ids(account_id) or should_skip_muted_creator(fan):
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
    seen = await _seen_media(account_id, fan_id)

    # ── Bundle plan (2026-07-23): tip$/5 items total. A tease warm-up share
    # comes from the free-tease folder (2 of 5), the rest ("normal" slots) from
    # the basis tier's folders — and when the context matcher is on, up to
    # context_pick_max of those normal slots are swapped for photos matching
    # what he asked for in the thread. Total NEVER exceeds `count`.
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

    (tease_ids, normal_ids), extras = await asyncio.to_thread(
        _pull_stages, client,
        [([tease_folder] if tease_n else [], tease_n),
         (folders, normal_n - len(matched))],
        set(seen) | set(matched),
        # Tease warm-up is filler — may repeat, never blocks the bundle; but
        # never re-pick a photo the matcher already reserved for the payoff.
        repeat_ok={0}, never_repeat=set(matched))
    # Escalating order: tease warm-up → tier shots (+ any backfill) → the
    # matched-to-his-ask photos land last (the payoff).
    media_ids = tease_ids + normal_ids + extras + matched
    base["bundle"] = {"tease": len(tease_ids),
                      "normal": len(normal_ids) + len(extras),
                      "matched": list(matched)}

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
