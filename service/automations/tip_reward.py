"""service/automations/tip_reward.py — reward a fan with vault media when they tip.

Trigger: real-time, off the inbound tip event. `event_transcoder` records the tip,
then `webhook_dispatch.on_inbound_tip` enqueues ONE fan-scoped `tip_reward` job
carrying {fan_id, tip_message_id, tip_cents}. The periodic executor drains it.

The reward is photos by default; `videos_in_rewards` (a Tip Reward tab checkbox,
default OFF) admits clips too. With it ON a clip is not smuggled in as one
photo-slot: it consumes slots per the pack_pricing rate card ($5–10 per 10s by
explicitness), so a $25 tip can never walk off with a $30 video. The config keys
keep their historical `*_image*` names for back-compat. DRM-only videos can't be
previewed in the browser but ARE valid vault attachments, so once clips are
allowed they're included like any other item.

The convo teaser LADDER carries its own twin of that switch — `teaser_convo_videos`
(same tab, the "Escalating teases" section, also default OFF) — because the two
lanes pull from different folders for different people: clips in a $120 rung are
not a reason for clips in a $5 thank-you. It bills against the same `_SlotCost`,
sized off the rung's ask instead of the tip. The hot-thread teaser lane stays
ungated (one clip, one slot) — it has no such checkbox yet.

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
automation (e.g. an welcome_chatter_for_info reply) is messaging the same fan this moment.

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
    DEFAULT_TIP_ASK_ENABLED, apply_word_restriction,
    load_operator_stop_ids, should_skip_muted_creator,
)
from automations.fan_state import fan_state, put_fan_state
# ONE definition of "a vid" — the same tuple the pack caption counts by and
# pack_pricing prices by, so the images-only filter and the rate card agree.
from automations.pack_claim import MOVING_KINDS
import random
from db.engine import get_session
from automations import tip_ladder
from automations.tip_context import pick_context_media
# The $3 wire minimum is a PROTOCOL fact about OnlyFans, not a pricing policy — one
# definition, imported rather than re-declared. `upsell` is a pure policy module
# (no DB, no client), so this cannot cycle back into tip_reward.
from automations.upsell import OF_PRICE_FLOOR_CENTS
from db.models import (AccountAiConfig, Fan, TipRewardLog, Transaction, VaultItem,
                       VaultSend)
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
    # ── Videos in rewards (operator checkbox, 2026-08-19) ────────────────────
    # OFF (default): the tip-reward and image-reply pulls are IMAGES ONLY — moving
    # items (video/gif) are dropped at the folder scan, which visibly shrinks a
    # bundle whose folder holds clips. ON: clips ride too, and in the TIP bundle a
    # clip consumes ceil(rate-card value / dollars_per_image) photo-slots
    # (pack_pricing.item_value_cents: $5/$7/$10 per 10s by explicitness; an
    # unmirrored or undescribed clip bills at the sfw base — never over-charge) so
    # a $25 tip can never buy a $30 video. The TEASER lanes are deliberately NOT
    # gated by this flag: their price is the ladder's, not a per-item sum, and
    # they have always attached whatever the folder holds.
    "videos_in_rewards": False,
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
    # The ASK side of the loop (read by welcome_chatter_for_info/autoreply, not tip_reward itself):
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
    # Flag 3: run the Qwen3-VL vision model on the photo he sent and cache the
    # description on the Message row (messages.image_desc). The closer/chat engines
    # then read that back into their history as "[he sent: …]" so the AI can
    # react to it — rate a dick pic, clock what he's wearing, answer "what do you
    # think?" Blocks the closer kick until the describe lands (a few seconds, reads
    # as human typing) so the FIRST reply already sees the picture. Default ON.
    "image_describe_enabled": True,
    "image_describe_prompt": "",    # optional phrasing seed ('' → the built-in rater prompt)
    # WHO gets their photos described — "all" (every fan) or "paid" (only fans with
    # lifetime_spend_cents > 0, so a $0 fan spamming pics never burns a vision call).
    # Default "all": with the feature on by default, describe every inbound photo.
    "image_describe_scope": "all",  # "all" | "paid"
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
    # budget model (tip_ladder.bundle_plan). There are no separate sizing knobs:
    # the bundle is sized by `dollars_per_image` / `min_images` / `max_images`
    # above — see `_bundle_sizing`. Default OFF → the hot teaser keeps its fixed
    # `hot_teaser_count`; the convo ladder always scales.
    "bundle_scaling_enabled": False,
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
    # "Videos too" for the LADDER — its OWN switch, deliberately not the tip reward's
    # `videos_in_rewards`: the two lanes pull from different folders for different
    # people, and an operator who wants clips in a $120 rung does not thereby want
    # them in a $5 thank-you. OFF (default) keeps every rung PHOTOS-ONLY; ON admits
    # clips and turns the tease's count into a SLOT budget (`_SlotCost`, the same
    # pack_pricing rate card the tip reward bills against), so a $10 rung can never
    # ship a $60 clip while several short ones still fit inside the ask.
    "teaser_convo_videos": False,
    "teaser_convo_rungs": [
        {"folders": [], "price_cents": 0},       # rung 0 — free tease
        {"folders": [], "price_cents": 1000},    # $10
        {"folders": [], "price_cents": 4000},    # $40
        {"folders": [], "price_cents": 8000},    # $80
        {"folders": [], "price_cents": 12000},   # $120
        {"folders": [], "price_cents": 16000},   # $160
        {"folders": [], "price_cents": 20000},   # $200
    ],
    # ── ADAPTIVE convo teaser (DEFAULT ON since a live incident where legacy
    # climb-every-send walked an unbought fan $40→$80→$120→$160→$200 overnight,
    # with the fan complaining about the price in-thread) ─────────────────────
    # The ladder climbs ONLY when the teaser she sent actually SELLS (her own
    # teaser unlock — NEVER an ai_chatter catalog buy). On a no-buy the ask
    # JITTERS: usually a cut to 40–60% of the last price, and one roll in four a
    # RAISE of 40–60% (capped at the ladder top), holding the rung. Photos come
    # from a price-scaled WEIGHTED BUNDLE (bundle_plan → the same
    # premium/normal/free tiers the hot teaser uses); when no tier folders are
    # configured it falls back to the rung's own folder. An explicit stored
    # `false` restores legacy climb-every-send.
    "teaser_convo_adaptive": True,
    "teaser_convo_cut_lo": 0.40,           # no-buy cut keeps 40–60% of last ask
    "teaser_convo_cut_hi": 0.60,
    # ── FLUCTUATE-DOWN pricing (operator ruling 2026-08-19; supersedes the 08-01
    # acquire-then-escalate floor FOR THIS LANE — the catalog lane keeps it) ──────
    # A stalled ask decays for EVERYONE, proven buyer or not. The floor is ONE
    # static number (default $10); at the floor the ladder mostly alternates floor ↔
    # free bait, much as the $3 acquisition mechanic did (the bounce can also fire
    # there and lift the ask back off the floor). The set-price
    # floor it replaces produced the worst shape the corpus measures: a proven
    # buyer parked on a top rung takes the IDENTICAL priced ask every breaker
    # cycle indefinitely — repeat-same-price is the lowest-converting reply to a
    # refusal there is, and the identical bundle rides every one of them.
    # The RAISE leg is the anti-pattern guard: a monotone decay teaches a watching
    # fan that ignoring her makes the next ask cheaper, so one no-buy roll in four
    # goes UP instead — the trend is down, the path is not a slide.
    "teaser_convo_floor_cents": 1000,      # STATIC decay floor, everyone
    "teaser_convo_raise_chance": 0.25,     # P(a no-buy ask RAISES instead of cutting)
    "teaser_convo_raise_lo": 1.4,          # a raise multiplies the last ask by
    "teaser_convo_raise_hi": 1.6,          #   1.4–1.6, capped at the top rung's price
    # ── The free BAIT leg for a PROVEN buyer (operator ask, 2026-08-08) ──────────
    # At the floor the only move left is `bait_floor`. ON: a proven buyer's
    # bait_floor is $0 like everyone else's, so the bottom of his ladder alternates
    # floor ↔ free (2 photos from the free folder, `hot_teaser_free_folder`, which
    # `_compose_bundle_ids` may repeat) instead of repeating one number. OFF: his
    # bait_floor equals the floor, and the identical ask repeats until the circuit
    # breaker stops it — the shape that produced six consecutive identical
    # top-rung locks on one prod fan, with three more due every 48h indefinitely.
    #
    # ⚠️ A free leg is not a failed ask, so it does NOT advance `unbought` (see
    # pick_convo_teaser). The breaker therefore still stops him after the same number
    # of PRICED asks; the free legs are additional sends between them, not instead of
    # them. This raises his media volume — it does not lower the number of times he is
    # asked for money.
    # ⚠️ Nor does a $0 teaser pass ai_chatter's PRICED-send guards (§6567): neither the
    # broke/declined pause nor the per-run `_MAX_FORCED_ASKS_PER_TICK` cap applies to
    # it. Both sit inside `if price_cents > 0`, so with this on, the half of the
    # alternation that is free reaches a fan a priced rung would not, and is bounded
    # only by `max_fans_per_tick` (8).
    # Default ON 2026-08-08 (operator call). An account opts out via an explicit stored
    # False — the Tip Reward tab writes one.
    "teaser_convo_bait_for_buyers": True,
    # When he BUYS a softened/floor ask (one he haggled below the rung's list price),
    # the next ask escalates off WHAT HE PAID × this step (capped at the ladder max) —
    # NOT a jump up to the rung list he already refused. A $15.20 floor buy → ~$30 →
    # ~$60, respecting the willingness he just proved. A full-price rung buy still
    # climbs the configured ladder as before.
    "teaser_convo_climb_step": 2.0,
    # ── The CIRCUIT BREAKER on repeated unbought asks ────────────────────────────
    # After this many consecutive PRICED teasers he did not unlock, stop pricing and
    # just talk to him. He gets no new priced tease until he buys something, replies
    # to a free one, or `teaser_convo_unbought_reset_h` passes.
    #
    # This is the brake whose absence produced the worst behaviour in the system:
    # measured 2026-08-01 on Isabelle, fan 374095202 received EIGHTY-FIVE consecutive
    # $3.00 locked messages across three days and unlocked none of them. Under
    # fluctuate-down (08-19) there is almost always a cheaper ask to send again, so
    # this breaker is the ONLY thing that guarantees a wall of ignored asks ends.
    # 0 disables the brake (the old unbounded behaviour) — not recommended.
    "teaser_convo_max_unbought": 3,
    "teaser_convo_unbought_reset_h": 48,
    # ── OVERRIDE the automated brakes for the ESCALATION ladder (operator ask,
    # 2026-07-25) ─────────────────────────────────────────────────────────────
    # When ON, the convo-teaser ladder ($0/$10/$40/$80 …) fires every `after`
    # messages REGARDLESS of the fan-state brakes ai_chatter normally honours:
    #   • seller_off  — companion window / post-buy cooldown / bot-accusation
    #   • offers_paused_until — the "he said he's broke / hard-declined" pause
    # ⚠️ This deliberately overrides the POVERTY BRAKE — the codebase elsewhere
    # calls escalating a man who typed "my wallet is tapped out" the single worst
    # thing this system can do, and the adaptive ladder exists precisely because a
    # climb-every-send once walked an unbought fan $40→$200 overnight with him
    # complaining about price in-thread. Enabling this accepts that risk. The ONLY
    # thing that still halts the ladder is a MANUAL stop — blacklist, a skip_list
    # row (manual_restrict / of_restricted / unreachable), or a muted peer-creator —
    # all enforced UPSTREAM in run() before this code is reached. Default ON since
    # 2026-07-28 (operator call): the brakes were eating the ladder on exactly the
    # fans it exists for — a $849-lifetime buyer who paid $147 that morning tripped
    # the $300/7d spend cap, went COMPANION for 24h, and sat 94 messages past his
    # `after: 20` threshold with the next rung gagged every single turn. Note the
    # config API has NO branch for this key (it is dropped from any UI save), so an
    # account can only opt back out via an explicit stored False or a code change.
    "teaser_convo_ignore_brakes": True,
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


async def image_describe_flags(account_id: str) -> tuple[bool, str, str]:
    """(image_describe_enabled, image_describe_prompt, image_describe_scope) — for the
    inbound-image dispatcher's vision step. Kept separate from `image_reply_flags` so
    that tuple's callers/tests stay stable. Default ON, scope "all"; independent of the
    tip `enabled` master switch. scope is "all" | "paid" (paid → only fans who spent)."""
    cfg = await _load_config(account_id)
    scope = str(cfg.get("image_describe_scope") or "all").strip().lower()
    if scope not in ("all", "paid"):
        scope = "all"
    return (bool(cfg.get("image_describe_enabled")),
            str(cfg.get("image_describe_prompt") or ""), scope)


def _cents_per_slot(cfg: dict) -> int:
    """ONE derivation of the "$ per image" knob, in cents. It is the number that
    ties the tip's slot budget (`_media_count`), the bundle sizing
    (`_bundle_sizing`) and the video slot cost (`_SlotCost`) together — three
    inline copies had already grown two different (dead — `_load_config` always
    merges the default) fallbacks, and the day those disagree for real, the
    budget and the cost stop meaning the same thing."""
    return max(1, int(cfg.get("dollars_per_image") or 5)) * 100


def _media_count(tip_cents: int, cfg: dict) -> int:
    """clamp(min_images, tip_dollars // dollars_per_image, max_images). The config
    keys keep their `*_image*` names for back-compat. With `videos_in_rewards` on
    this number is a SLOT budget rather than an item count: a photo is one slot, a
    clip is whatever the rate card says it is worth in photos (`_SlotCost`),
    and the bundle can only get SMALLER in items — never larger."""
    per = _cents_per_slot(cfg)
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


def folder_list(v, *, max_len: int | None = None) -> list[str]:
    """PUBLIC seam: THE config→folder-names coercion, for every slot that holds
    folders — on BOTH sides of the wire. `tip_reward_config_api` validates saves
    through it too, so "what a folder slot means" has one definition and a save can
    never store a shape the read rejects.

    A slot may be stored as a list (the shape the tab writes now) or as the single
    string it was before — `""` → `[]`, `"tease10"` → `["tease10"]`. Reading through
    here is why multi-folder needed NO migration: an account whose rungs still hold
    a bare string keeps working, and the first save from the tab widens it.

    Names are stripped, blanks dropped, and duplicates removed case-insensitively
    (order preserved) — a folder listed twice must not make the scan read it twice,
    and `_resolve_folders` is first-wins on name collisions anyway.

    `max_len` (the write side's per-name cap) truncates BEFORE the dedupe, which is
    why it belongs here and not in the caller: cut afterwards, two names that differ
    only past the cap would land as one name twice.

    🚨 NEVER `str()` a stored list to get here. The repr `"['a','b']"` matches no
    folder in the vault, which reads downstream as "no media" — a silently DEAD
    lane rather than an error."""
    raw = [v] if isinstance(v, str) else list(v or [])
    out: list[str] = []
    seen: set[str] = set()
    for nm in raw:
        nm = str(nm or "").strip()
        if max_len:
            nm = nm[:max_len]
        key = nm.lower()
        if nm and key not in seen:
            seen.add(key)
            out.append(nm)
    return out


def rung_folder_slot(rung: dict):
    """PUBLIC seam: the RAW value of a rung's folder slot — the `folders` key when the
    rung has it, else the single `folder` string it used to be. Both the validator
    that WRITES a rung and the engine that READS one go through here, so they can
    never disagree about which key wins (`folders: []` alongside a stale `folder`
    means the operator cleared the rung, not that the old string is back)."""
    return rung.get("folders") if "folders" in rung else rung.get("folder")


def _folder_media_items(client, list_id: int) -> list[tuple[int, str, int]]:
    """Ordered `(media_id, type, duration_seconds)` in one vault folder
    (recent-first), PHOTOS AND VIDEOS (plus gifs). `type="all"` so a reward can
    hand out a clip as readily as a photo; audio is filtered out. DRM-only videos
    are kept — they can't be previewed but ARE sendable as a vault attachment.
    Type and duration ride along because the OF payload already carries them
    (zero extra calls): the images-only filter and the video slot-coster both
    read them downstream. Best-effort (empty on error)."""
    try:
        media = client.vault_media(list_id=int(list_id), type="all",
                                   limit=_VAULT_SCAN_LIMIT)
    except Exception:
        log.debug("tip_reward vault_media failed folder=%s", list_id, exc_info=True)
        return []
    items = media.get("list") if isinstance(media, dict) else media
    out: list[tuple[int, str, int]] = []
    for it in (items or []):
        if not isinstance(it, dict) or it.get("id") is None:
            continue
        # Tolerate a missing/blank type (older payloads / test fakes) — only an
        # explicit non-reward type (e.g. audio) is skipped.
        mtype = str(it.get("type") or "").strip().lower()
        if mtype and mtype not in _REWARD_MEDIA_TYPES:
            continue
        dur = it.get("duration")
        try:
            out.append((int(it["id"]), mtype,
                        int(dur) if isinstance(dur, (int, float)) and dur else 0))
        except (TypeError, ValueError):
            continue
    return out


def folder_media_pool(client, folder_name: str) -> list[int] | None:
    """PUBLIC seam: one vault folder's media ids, by NAME. None = no such folder.

    The name → list-id → media-ids read every folder-configured lane shares —
    the tip tiers and hot teaser here, welcome_chatter_for_info's gather-close from outside.
    Folder NAMES are what the configs store (the UI's VaultFolderPicker shape),
    so this is the one place the name resolves. Sync client calls — run it via
    `asyncio.to_thread` off the event loop."""
    list_id = _resolve_folders(client, [folder_name]).get(
        str(folder_name).strip().lower())
    if list_id is None:
        return None
    return [mid for mid, _t, _d in _folder_media_items(client, list_id)]


class _SlotCost:
    """HOW A LANE BILLS ONE MEDIA ITEM against its pull budget: the item's price in
    photo-slots, or None for "may never ride". ONE value carries the whole policy,
    so a pull takes a single `cost` — eligibility and price cannot disagree, and
    there is no combination of them left to get wrong.

    The three shapes, all `_SlotCost`:
      • `_PER_ITEM`     — everything is one slot, clips included. The unbudgeted
                          lanes: the hot teaser, and the $0 image-reply freebie
                          where nothing was paid for a clip's length to overflow.
      • `_PHOTOS_ONLY`  — clips are ineligible; a "Videos too" checkbox left off.
      • `_video_slot_cost(...)` — clips ride, each billed off the rate card.
    `_PER_ITEM` is the base class itself: the plain count every pull used before
    any of this existed, and still the default."""

    def __call__(self, mid: int, mtype: str, duration: int) -> int | None:
        return 1

    def slots_of(self, ids: list[int]) -> int:
        """Slots a picked list consumed — how `_pull_stages` measures a shortfall
        without re-reading anything."""
        return len(ids)


class _PhotosOnly(_SlotCost):
    """Images only (a "Videos too" checkbox left off): video and gif are refused at
    the folder scan. A blank type still passes as a photo, as it always has (older
    payloads / test fakes)."""

    def __call__(self, mid: int, mtype: str, duration: int) -> int | None:
        return None if mtype in MOVING_KINDS else 1


class _VideoSlotCost(_SlotCost):
    """Clips priced by the rate card ("Videos too" on). A clip costs
    ceil(rate-card value / dollars_per_image): 60s of sfw at the $5/item default is
    3000¢/500¢ = 6 slots, explicit footage twice that. Built by `_video_slot_cost`
    (which does the ONE mirror read); pure and memoizing afterwards, so an instance
    can cross into `asyncio.to_thread`.

    The tier and (preferably) the duration come from the vault mirror; a clip the
    mirror has never seen — or one the describe pass hasn't tiered — bills at the
    SFW base off the OF payload's own duration, so missing data can only ever
    UNDER-charge, never inflate a clip out of the bundle."""

    def __init__(self, mirror: dict[int, tuple[int | None, str | None]],
                 cents_per_slot: int) -> None:
        self._mirror = mirror
        self._per = cents_per_slot
        self._memo: dict[int, int] = {}

    def __call__(self, mid: int, mtype: str, duration: int) -> int:
        mid = int(mid)
        got = self._memo.get(mid)
        if got is None:
            if mtype in MOVING_KINDS:
                from automations import pack_pricing  # local: its import chain
                # drags the whole vault stack (content_resolver →
                # vault_pack_picker/vault_scripts) — pay for it on the first
                # videos-ON reward, not at module import.
                dur, tier = self._mirror.get(mid, (None, None))
                value = pack_pricing.item_value_cents(
                    mtype, int(dur or duration or 0), str(tier or "sfw"))
                got = max(1, -(-value // self._per))     # ceil division
            else:
                got = 1                                  # a photo is one slot
            self._memo[mid] = got
        return got

    def slots_of(self, ids: list[int]) -> int:
        # Every picked id was priced on the way in (`_gather_unseen` costs an item
        # before taking it), so the memo has it — no second read, and a KeyError
        # here would mean someone counted ids this cost never priced.
        return sum(self._memo[int(m)] for m in ids)


# The two stateless policies are shared singletons; only the rate-card one holds
# per-account data (and a memo), so only it is built per use.
_PER_ITEM = _SlotCost()
_PHOTOS_ONLY = _PhotosOnly()


async def _video_slot_cost(account_id: str, cents_per_slot: int) -> _VideoSlotCost:
    """Build the rate-card cost: ONE account-wide select over the vault mirror's
    moving items. Account-wide because the folder ids aren't known until the sync
    scan runs (and vaults are hundreds of rows, not thousands).

    Takes the RATE, not the config: the teaser ladder reaches this holding a
    `BundleSizing` rather than a cfg dict, and both callers must bill at the one
    `_cents_per_slot` number rather than re-deriving it."""
    async with get_session() as s:
        rows = (await s.execute(
            select(VaultItem.media_id, VaultItem.duration_seconds,
                   VaultItem.explicitness_tier).where(
                VaultItem.account_id == str(account_id),
                VaultItem.kind.in_(MOVING_KINDS))
        )).all()
    return _VideoSlotCost({int(m): (d, t) for m, d, t in rows}, int(cents_per_slot))


def _gather_unseen(client, folders: list[str], by_name: dict[str, int],
                   seen: set[int], count: int, *,
                   cost: _SlotCost = _PER_ITEM) -> list[int]:
    """Up to `count` SLOTS of unseen media ids, scanning the tier's folders in
    order and de-duplicating across them (an id can live in two folders).

    `cost` is the lane's billing policy (see `_SlotCost`): it decides both what an
    item is worth and whether it may ride at all. Under the default every item
    costs one slot, so `count` is the plain item count it always was. Under the
    rate-card cost `count` is a SLOT budget: an item that would overflow it is
    SKIPPED and the scan continues, because a cheaper item further down may still
    fit — the budget is a ceiling, never trimmed after the fact."""
    picked: list[int] = []
    taken: set[int] = set()
    total = 0
    for nm in folders:
        list_id = by_name.get(str(nm).strip().lower())
        if list_id is None:
            log.info("tip_reward folder not found name=%r", nm)
            continue
        for mid, mtype, duration in _folder_media_items(client, list_id):
            if mid in seen or mid in taken:
                continue
            c = cost(mid, mtype, duration)
            if c is None or total + c > count:
                continue
            picked.append(mid)
            taken.add(mid)
            total += c
            if total >= count:
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
    by_name = await asyncio.to_thread(_resolve_folders, client, folders)
    seen = await _seen_media(account_id, fan_id)
    # The images-only checkbox governs this freebie too, but the SLOT budget does
    # not: nothing was paid, so there is no price for a clip's length to overflow —
    # with the flag on, one clip is one freebie, as it always was.
    media_ids = await asyncio.to_thread(
        _gather_unseen, client, folders, by_name, seen, count,
        cost=_PER_ITEM if cfg.get("videos_in_rewards") else _PHOTOS_ONLY)
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
        # Price-scaled bundle (default-off; pick_hot_teaser reads it).
        "bundle_scaling_enabled": bool(cfg.get("bundle_scaling_enabled")),
        "sizing": _bundle_sizing(cfg),
        # Tier→folder mapping for the composed bundle (reuse the tiers config):
        # premium = the 'premium' tier's folders, normal = basic+mid, free = the
        # hot-teaser free folder. Empty lists → that tier contributes nothing.
        "bundle_premium_folders": _tier_folders(cfg, "premium"),
        "bundle_normal_folders": _tier_folders(cfg, "basic") + _tier_folders(cfg, "mid"),
        "bundle_free_folders": [f for f in [str(cfg.get("hot_teaser_free_folder") or "").strip()] if f],
    }


def _bundle_sizing(cfg: dict) -> tip_ladder.BundleSizing:
    """THE one place a config becomes a photo count.

    `dollars_per_image`, `min_images` and `max_images` are the only sizing numbers
    that exist: they are what the Tip Reward tab renders and the only ones
    `tip_reward_config_api._validate` persists. There deliberately are no separate
    bundle_* knobs — a set of those used to sit in `_DEFAULTS` with no branch in
    that allowlist, so no save could ever reach them and the teaser quietly sized
    on a $10/photo ladder nobody had set while the visible field said $5. One
    number, one meaning: change "$ per image" and every priced send re-sizes.

    `free_photos` tracks `min_images` so a free tease never looks stingier than the
    paid floor it is bait for; 0 means no free tease, on every caller."""
    lo = max(0, int(cfg.get("min_images") or 0))
    return tip_ladder.BundleSizing(
        cents_per_photo=_cents_per_slot(cfg),
        min_photos=max(1, lo),
        max_photos=max(1, int(cfg.get("max_images") or 1), lo),
        free_photos=lo,
    )


def _rung_count(tcfg: dict, rung: dict) -> int:
    """Photos for a SINGLE-FOLDER rung pull (no tiers to compose from) — SLOTS once
    "Videos too" is on for the ladder, so a rung whose folder is all clips needs this
    number (or the tab's Minimum) raised to fit more than the shortest one.

    Per-rung `count` wins ($10→1, $30→3, $50→5), else the ladder-wide
    `teaser_convo_count` — but never below the tab's Minimum images. Both the
    legacy ladder and the adaptive no-tiers fallback size a rung this way, and
    they must agree: the fallback swaps the photo SOURCE, not the floor."""
    return max(max(1, int(tcfg["sizing"].min_photos)),
               int(rung.get("count") or 0) or int(tcfg.get("count") or 1))


async def _pull_rung(client, *, tcfg: dict, rung: dict, seen: set[int],
                     cost: _SlotCost) -> tuple[list[str], list[int]]:
    """A rung's OWN folders → up to its count of unseen media. Returns
    `(folders, media_ids)`; empty ids means the rung is dead (no folders, or the
    fan has seen everything they hold) and the caller sends nothing.

    Both ladder paths pull exactly this way — the legacy climb, and the adaptive
    no-tiers fallback — and they had grown two copies of it, which is how the
    videos flag reached one of them a change later than the other.

    Folders are scanned IN ORDER and deduped across each other (`_gather_unseen`),
    so listing a second folder deepens a rung rather than replacing it: the first
    is exhausted before the next is touched.

    `rung` is a NORMALISED tcfg rung — `convo_teaser_config` already ran the stored
    slot through `rung_folder_slot`/`folder_list` — which is why this reads
    `folders` plainly instead of coercing again."""
    folders = list(rung.get("folders") or [])
    if not folders:
        return [], []
    by_name = await asyncio.to_thread(_resolve_folders, client, folders)
    ids = await asyncio.to_thread(_gather_unseen, client, folders, by_name, seen,
                                  _rung_count(tcfg, rung), cost=cost)
    return folders, ids


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
                 cost: _SlotCost = _PER_ITEM,
                 ) -> tuple[list[list[int]], list[int]]:
    """THE one folder-bundle composer: ordered stage pulls with cross-stage dedup
    + shortfall backfill. Each (folder_names, count) stage pulls up to `count`
    unseen items from its folders; a short stage does not shrink the bundle —
    the total shortfall is backfilled from ALL stages' folders at the end.

    `cost` rides through to `_gather_unseen`. Under the rate-card cost every count
    here is a SLOT budget, and the shortfall is measured in slots too
    (`cost.slots_of`) — an item-counted shortfall would over-fill: a stage that
    spent its 5 slots on a 3-slot clip plus 2 photos is FULL, not 2 short.

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
            ids = _gather_unseen(client, folders, by_name, taken, n, cost=cost)
            taken.update(ids)
            bundle.update(ids)
        picked.append(ids)
    want = sum(max(0, n) for _, n in stages)
    short = want - sum(cost.slots_of(p) for p in picked)
    extras: list[int] = []
    if short > 0:
        all_folders = [f for folders, _ in stages for f in folders]
        if all_folders:
            by_name = _resolve_folders(client, all_folders)
            extras = _gather_unseen(client, all_folders, by_name, taken, short,
                                    cost=cost)
            bundle.update(extras)
    short -= cost.slots_of(extras)
    if short > 0 and repeat_ok:
        # Repeats may only restore the repeat_ok stages' OWN share — a payoff
        # shortfall keeps shrinking the bundle rather than becoming filler.
        repeat_want = sum(max(0, n) for i, (_, n) in enumerate(stages)
                          if i in repeat_ok)
        repeat_have = sum(cost.slots_of(picked[i]) for i in repeat_ok
                          if i < len(picked))
        short = min(short, max(0, repeat_want - repeat_have))
        r_folders = [f for i, (folders, _) in enumerate(stages)
                     if i in repeat_ok for f in folders]
        if short > 0 and r_folders:
            by_name = _resolve_folders(client, r_folders)
            extras = extras + _gather_unseen(
                client, r_folders, by_name,
                set(bundle) | set(never_repeat), short, cost=cost)
    return picked, extras


def _compose_bundle_ids(client, plan, seen: set[int],
                        premium_folders: list[str], normal_folders: list[str],
                        free_folders: list[str], *,
                        cost: _SlotCost = _PER_ITEM) -> tuple[list[int], dict]:
    """Teaser adapter over _pull_stages: pull the plan's premium/normal/free photo
    ids from their tier folders (dedup + backfill live in _pull_stages) and keep
    the breakdown/weight shape the teaser callers report. Sync — call via
    to_thread.

    `cost` rides straight through (see `_gather_unseen`): under the rate-card cost
    each stage's plan number is a SLOT budget, so one clip can consume a whole
    stage. The breakdown stays an ITEM count — it is what the caller logs, and
    "3 premium" reads truer than "3 slots of premium"."""
    (prem, norm, free), extras = _pull_stages(
        client, [(premium_folders, plan.premium), (normal_folders, plan.normal),
                 (free_folders, plan.free)], seen, cost=cost,
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
    {media_ids, price_cents, is_free, folders} or None (throttled / capped / no
    folder / nothing unseen left). `folders` names WHERE the media came from — empty
    when the bundle was composed from the tier folders rather than pulled from one.
    It is a record/diagnostic field; nothing branches on it.

    Sends NOTHING and writes NOTHING: ai_chatter attaches the media to its reply and
    calls `record_hot_teaser` only once the send confirms — so a dropped/failed reply
    never burns the cooldown or the fan's free allowance."""
    now = now or datetime.utcnow()
    async with get_session() as s:
        fan = await s.get(Fan, (str(account_id), int(fan_id)))
    state = fan_state(fan, _HOT_TEASER_STATE_KEY)
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
    plan = tip_ladder.bundle_plan(price_cents, tcfg["sizing"]) if scaling else None

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
                "is_free": False, "folders": [], "bundle": breakdown}

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
            "is_free": is_free, "folders": [folder]}


async def record_hot_teaser(account_id: str, fan_id: int, *, media_ids: list[int],
                            message_id: int | None, price_cents: int, is_free: bool,
                            set_rung: int | None = None,
                            unbought: int | None = None,
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
            st = fan_state(fan, _HOT_TEASER_STATE_KEY)
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
            # The last teaser that carried a PRICE, kept across free sends. A $0 bait
            # overwrites last_msg with a message that can never be "sold", which would
            # otherwise blind the ladder to a LATE unlock of the priced ask underneath
            # it — see `teaser_sale_check_msg`.
            if int(price_cents or 0) > 0 and message_id:
                st["last_paid_msg"] = int(message_id)
            # Consecutive PRICED teasers he has not unlocked — the circuit breaker's
            # numerator. Supplied by the caller (pick_convo_teaser resolved it against
            # the sale signal); None leaves it untouched for the hot-teaser lane.
            if unbought is not None:
                st["unbought"] = int(unbought)
            put_fan_state(fan, _HOT_TEASER_STATE_KEY, st)


def teaser_sale_check_msg(state: dict) -> int | None:
    """WHICH of her teaser messages the ladder should ask `is_paid` about next turn —
    or None when there is no sale worth querying. Pure; the caller runs the query.

    Normally that is simply her last teaser. But the free BAIT leg sends a $0 message,
    and a $0 message can never be unlocked, so on the turn after a bait the obvious
    read (`last_msg`) asks a question whose answer is always False — and the PRICED ask
    underneath it silently stops being watched. That ask is still open and still
    unlockable: he can pay for it hours later, and it can carry any rung price on the
    ladder, not just a floor tease. Missing it would
    keep his unbought streak climbing toward the circuit breaker after he had paid.

    So: last_price > 0 → her last teaser; otherwise `last_paid_msg`, the last one that
    carried a price, which `record_hot_teaser` keeps across free sends.

    (A ladder whose rungs put a $0 rung ABOVE rung 0 could climb twice off one such
    late sale, because the climb it triggers would itself be a free send that leaves
    `last_paid_msg` in place. It terminates — the next climb is priced — and no
    authored ladder has that shape: free is the opening rung.)"""
    if int(state.get("last_price") or 0) > 0:
        return int(state.get("last_msg") or 0) or None
    return int(state.get("last_paid_msg") or 0) or None


def teaser_state(fan: Fan | None) -> dict:
    """The fan's teaser state ({at, free_sent, rung}) parsed off the Fan row ai_chatter
    already has in hand — no extra DB read. `at` (iso) is the last teaser, `rung` the
    convo-ladder position."""
    return fan_state(fan, _HOT_TEASER_STATE_KEY)


def teaser_last_at(state: dict) -> datetime | None:
    """When the last teaser went out, or None. The `at` field is written here as an
    ISO string, so it is READ here too — ai_chatter had grown its own inline
    `datetime.fromisoformat` over this module's storage format."""
    raw = state.get("at")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw))
    except Exception:
        return None


async def convo_teaser_config(account_id: str) -> dict | None:
    """The conversational-ladder knobs iff enabled, else None — one config read for
    ai_chatter's per-run setup. Each rung is {folders, price_cents, count}; a rung
    with no folders is a dead step (skipped at selection)."""
    cfg = await _load_config(account_id)
    if not cfg.get("teaser_convo_enabled"):
        return None
    videos = bool(cfg.get("teaser_convo_videos"))
    rungs = []
    for r in (cfg.get("teaser_convo_rungs") or []):
        if not isinstance(r, dict):
            continue
        # Per-rung image `count` (e.g. $10→1, $30→3, $50→5). 0/absent → fall back to
        # the ladder-wide teaser_convo_count.
        # `folders` (a list) is the stored shape; `folder` is the single-string one
        # it grew out of. `folder_list` takes either, so no account needed migrating.
        rungs.append({"folders": folder_list(rung_folder_slot(r)),
                      "price_cents": max(0, int(r.get("price_cents") or 0)),
                      "count": max(0, int(r.get("count") or 0))})
    return {
        "after": max(1, int(cfg.get("teaser_convo_after_fan_msgs") or 20)),
        "count": max(1, int(cfg.get("teaser_convo_count") or 1)),
        "rungs": rungs,
        # Operator override: run the ladder past the companion/bot-accused/broke
        # brakes (manual stops still apply upstream). See _DEFAULTS for the risk.
        "ignore_brakes": bool(cfg.get("teaser_convo_ignore_brakes")),
        # Adaptive (buy-aware climb / jitter) + the weighted-bundle knobs it shares
        # with the hot teaser. All inert unless teaser_convo_adaptive is on.
        # DIRECT key access for the numeric knobs (like the breaker pair below):
        # `_load_config` merges _DEFAULTS, so the keys always exist, and an explicit
        # stored 0 (raise_chance off-switch, floor_cents → $3 wire floor) must
        # survive — a `.get(...) or DEFAULT` would silently eat it.
        "adaptive": bool(cfg.get("teaser_convo_adaptive")),
        "cut_lo": float(cfg["teaser_convo_cut_lo"]),
        "cut_hi": float(cfg["teaser_convo_cut_hi"]),
        "floor_cents": max(0, int(cfg["teaser_convo_floor_cents"])),
        "raise_chance": max(0.0, min(1.0, float(cfg["teaser_convo_raise_chance"]))),
        "raise_lo": float(cfg["teaser_convo_raise_lo"]),
        "raise_hi": float(cfg["teaser_convo_raise_hi"]),
        # Does a PROVEN buyer get the free bait leg too (floor ↔ free) instead of
        # holding on one repeated number? See _DEFAULTS for the volume caveat.
        "bait_for_buyers": bool(cfg.get("teaser_convo_bait_for_buyers")),
        # Escalation ×step off a SOFTENED/floor buy (never off the rung list). See _DEFAULTS.
        "climb_step": max(1.0, float(cfg.get("teaser_convo_climb_step") or 2.0)),
        # Circuit breaker on consecutive unbought priced teasers. See _DEFAULTS.
        # DIRECT key access: `_load_config` merges _DEFAULTS and drops stored Nones, so
        # both keys are always present — a `.get(... ) or DEFAULT` here would be dead
        # code that also silently ate a deliberate 0 ("never release on time alone").
        "max_unbought": max(0, int(cfg["teaser_convo_max_unbought"])),
        "unbought_reset_h": max(0, int(cfg["teaser_convo_unbought_reset_h"])),
        "sizing": _bundle_sizing(cfg),
        "bundle_premium_folders": _tier_folders(cfg, "premium"),
        "bundle_normal_folders": _tier_folders(cfg, "basic") + _tier_folders(cfg, "mid"),
        "bundle_free_folders": [f for f in [str(cfg.get("hot_teaser_free_folder") or "").strip()] if f],
        # Clips ride the rungs (and cost slots off the ask) — the flag only, NOT a
        # built coster: `/ai-status` calls this config on every strip poll and never
        # pulls media, and the coster is a whole vault read. `pick_convo_teaser` builds
        # it when it is about to spend it.
        "videos": videos,
    }


def _jitter_band(tcfg: dict) -> tuple[float, float, float, float, float]:
    """(cut_lo, cut_hi, chance, raise_lo, raise_hi) — the no-buy jitter model,
    normalized ONCE (bands sorted, chance clamped). Both the sender's roll and the
    forecast's band ends come from here, so a default can never drift between them."""
    lo = float(tcfg.get("cut_lo") or 0.40)
    hi = float(tcfg.get("cut_hi") or 0.60)
    if lo > hi:
        lo, hi = hi, lo
    chance = max(0.0, min(1.0, float(tcfg.get("raise_chance") or 0.0)))
    r_lo = float(tcfg.get("raise_lo") or 1.4)
    r_hi = float(tcfg.get("raise_hi") or 1.6)
    if r_lo > r_hi:
        r_lo, r_hi = r_hi, r_lo
    return lo, hi, chance, r_lo, r_hi


def _jitter_frac(band: tuple[float, float, float, float, float], rv: float) -> float:
    """ONE roll → the no-buy multiplier. `rv` below `chance` lands in the raise band
    (the bounce); the rest of the interval maps across the cut band. A single number
    deciding both the leg and the spot keeps the injectable-`rand` test seam intact."""
    lo, hi, chance, r_lo, r_hi = band
    rv = max(0.0, min(1.0, rv))
    if rv < chance:
        return r_lo + (r_hi - r_lo) * (rv / chance)
    span = (rv - chance) / (1.0 - chance) if chance < 1.0 else 0.0
    return lo + (hi - lo) * span


def convo_teaser_floors(tcfg: dict, *, max_paid_cents: int) -> tuple[int, int]:
    """`(floor, bait_floor)` — the bottom of his ladder, and the bottom of the free
    BAIT leg. ONE resolution, used by both the sender and the forecast so the drawer
    can never quote a floor the engine would not honour.

    The floor is ONE static number for everyone (`floor_cents`, default $10) —
    fluctuate-down pricing (operator ruling 2026-08-19, see _DEFAULTS): a stalled ask
    decays whether he has paid or not, and at the floor the ladder alternates
    floor ↔ free. This supersedes the 08-01 set-price floor for THIS lane only; a
    proven buyer's history still shapes the CATALOG lane (`upsell.next_price`).

    What still turns on HAS HE EVER PAID is the bait leg: a never-buyer always
    alternates floor ↔ free (the acquisition mechanic), a proven buyer only when
    `teaser_convo_bait_for_buyers` is on — off, his bait_floor equals the floor and
    the number repeats until the breaker trips.

    Returning both from one call is the point: they are two views of one policy, and
    when the sender and the forecast each derived them separately they could disagree.

    NEVER below `OF_PRICE_FLOOR_CENTS`. That is not defensive decoration — the floor
    used to be `0.38 × max_paid`, which for a $3 buyer is $1.14, a price OF will not
    accept. The send site clamped it up to $3 without telling the ladder, so the stored
    state drifted to numbers the fan never saw (1¢ on two prod fans) and
    `round(1 × 0.7) == 1` made the decay a FIXED POINT that could never reach the
    floor↔free oscillation. A floor the wire can't honour is not a floor."""
    floor = max(OF_PRICE_FLOOR_CENTS, int(tcfg.get("floor_cents") or 0))
    if int(max_paid_cents or 0) > 0:
        return floor, (0 if tcfg.get("bait_for_buyers") else floor)
    return floor, 0


def _next_convo_teaser_price(*, idx: int, prices: list[int], last_px: int,
                             last_sold: bool, last_was_free: bool,
                             floor: int, bait_floor: int,
                             step: float, frac: float) -> tuple[int, int, bool]:
    """PURE pricing policy for the adaptive convo ladder — no I/O. Given where he is
    (idx, the ladder prices) and what happened to the last teaser, return
    (new_rung, price_cents, softened). `floor`/`bait_floor`/`frac`/`step` are
    pre-resolved by the caller (the floors from `convo_teaser_floors`, frac = the
    jittered cut, step = climb ×mult).

      • first ever            → the opening rung-0 tease
      • bought a SOFTENED ask → escalate off what he PAID × step (capped, rung tracks
                                to the first rung ≥ the new ask); never the rung list
                                he already refused
      • full-price sale / the
        opening free tease     → climb the configured ladder one rung
      • no buy                → jitter ×frac. Usually frac < 1 (a cut toward the
                                floor, then to `bait_floor`: $0 ⇒ the ladder
                                alternates floor ↔ free bait; a `bait_floor` equal
                                to the floor ⇒ he holds on one number. Which one a
                                PROVEN buyer gets is `teaser_convo_bait_for_buyers`;
                                a never-buyer always alternates). One roll in
                                `raise_chance` the caller hands a frac > 1 — the
                                BOUNCE — and the ask goes UP instead, capped at the
                                ladder top so a lucky streak can't leave the ladder.
    """
    if last_px <= 0 and idx == 0 and not last_sold and not last_was_free:
        return 0, prices[0], False               # first ever
    if last_sold and 0 < last_px < prices[idx]:  # softened buy → climb off what he PAID
        price = min(prices[-1], int(round(last_px * step)))
        new_idx = next((i for i, p in enumerate(prices) if p >= price), len(prices) - 1)
        return new_idx, price, False
    if last_sold or (last_was_free and idx == 0):  # full-price sale / opening free → climb
        new_idx = min(idx + 1, len(prices) - 1)
        return new_idx, prices[new_idx], False
    # No buy → jitter (a cut, or the bounce), landing on WHOLE DOLLARS — a $20.91
    # lock reads like a bot (the retired tip ladder's human-rounding lesson, kept
    # minimal). The climb branches above stay exact: they escalate off what he PAID.
    cand = int((last_px * frac + 50) // 100) * 100
    if frac > 1.0 and prices and max(prices) > 0:
        cand = min(cand, max(prices))            # a raise never exceeds the ladder top
    if cand >= floor:        price = cand        # still above the floor → decay
    elif last_px > floor:    price = floor       # first dip below → land ON floor
    elif last_px == floor:   price = bait_floor  # at the floor → bait ($0, or hold)
    else:                    price = floor       # was $0 bait → bounce to floor
    return idx, price, True


def convo_teaser_forecast(*, tcfg: dict, state: dict, msgs_since: int,
                          last_sold: bool = False, max_paid_cents: int = 0) -> dict:
    """What the convo teaser will do NEXT, and how many of his messages away it is.

    Pure and I/O-free — it runs `_next_convo_teaser_price`, the same policy the
    sender runs, so the drawer can never quote a price the engine would not send.
    It deliberately stops short of `pick_convo_teaser`: choosing the PHOTOS costs
    OF folder resolution and a `VaultSend` dedup read, and a status endpoint must
    not pay that (nor warm caches) just to render a countdown.

    The no-buy move is jittered — a cut (`cut_lo`..`cut_hi`) or, one roll in
    `raise_chance`, a bounce UP (`raise_lo`..`raise_hi`) — so on a stall the next ask
    is a RANGE, not a number. Every band end is evaluated: `cents` is the lowest
    outcome and `cents_max` the highest, equal whenever the outcome doesn't depend on
    the roll. Reporting one jittered sample as "the" next ask would be a number the
    operator could watch the engine contradict.
    """
    rungs = tcfg.get("rungs") or []
    after = max(1, int(tcfg.get("after") or 20))
    since = max(0, int(msgs_since or 0))
    idx = max(0, min(int(state.get("rung") or 0), max(0, len(rungs) - 1)))
    out: dict = {
        "after": after,
        "msgs_since": since,
        "remaining": max(0, after - since),
        "rung": idx if rungs else None,
        "rungs": len(rungs),
        "adaptive": bool(tcfg.get("adaptive")),
        "cents": None, "cents_max": None, "softened": None,
    }
    if not rungs:
        return out

    prices = [max(0, int((r or {}).get("price_cents") or 0)) for r in rungs]
    if not tcfg.get("adaptive"):
        out["cents"] = out["cents_max"] = prices[idx]
        out["softened"] = False
        return out

    last_px = max(0, int(state.get("last_price") or 0))
    floor, bait_floor = convo_teaser_floors(tcfg, max_paid_cents=max_paid_cents)
    lo, hi, chance, r_lo, r_hi = _jitter_band(tcfg)
    fracs = (lo, hi) + ((r_lo, r_hi) if chance > 0 else ())
    step = float(tcfg.get("climb_step") or 2.0)
    ends = [
        _next_convo_teaser_price(
            idx=idx, prices=prices, last_px=last_px, last_sold=last_sold,
            last_was_free=bool(state.get("last_free")), floor=floor,
            bait_floor=bait_floor, step=step, frac=f)
        for f in fracs
    ]
    out["cents"] = min(e[1] for e in ends)
    out["cents_max"] = max(e[1] for e in ends)
    out["softened"] = any(e[2] for e in ends)
    return out


async def pick_convo_teaser(client, account_id: str, fan_id: int, *, tcfg: dict,
                            state: dict, msgs_since_last: int,
                            last_sold: bool = False, max_paid_cents: int = 0,
                            rand: float | None = None,
                            now: datetime | None = None) -> dict | None:
    """SELECTION for the conversational ladder. Fires only once he has sent `after`
    messages since his last teaser.

    LEGACY (tcfg['adaptive'] falsy): picks the CURRENT rung's single folder + price;
    climbs one rung every send. Byte-for-byte the old behavior.

    Both paths obey tcfg['videos'] ("Videos too" for this ladder): off = photos only,
    on = clips ride and cost slots off the ask (see `cost` below).

    ADAPTIVE (tcfg['adaptive'] on): the ladder ($0/$10/$40/$80/$120/$160/$200)
    moves on WHAT HAPPENED to the last teaser — climb one rung if it SOLD (or was a
    free tease he received), else JITTER: usually a cut to 40–60% of the last ask
    down to the floor `convo_teaser_floors` resolves, one roll in four a raise of
    40–60% capped at the ladder top, holding the rung. Photos come from a price-scaled
    WEIGHTED BUNDLE (bundle_plan → premium/normal/free tier folders), free rung → a
    free taste.

    `state` is the fan's `_hot_teaser` slot (`teaser_state(fan)`) — the caller already
    holds it, and rung / last_price / last_free / unbought / at are all fields OF it.
    Unpacking them into separate parameters only created five chances for the sender
    and `convo_teaser_forecast` (which has always taken `state`) to disagree. The two
    facts that genuinely are NOT in the slot stay explicit: `last_sold` costs a query
    against her own teaser message, and `max_paid_cents` is his payment history.
    `rand` is injectable for deterministic tests.

    Returns {media_ids, price_cents, is_free, folders, rung, next_rung, convo:True
    (+softened/bundle in adaptive)} or None. `folders` is the same record field the
    hot teaser returns: the rung's own folders, or empty when the bundle came from
    the tier folders instead."""
    rungs = tcfg.get("rungs") or []
    if not rungs or msgs_since_last < int(tcfg.get("after") or 0):
        return None

    # ── CIRCUIT BREAKER ──────────────────────────────────────────────────────────
    # He has ignored `max_unbought` priced teasers in a row. Stop selling to him; the
    # reply still goes out, just without a paywall bolted to it. THE WHOLE RULE LIVES
    # HERE: he is released by a purchase, or by `unbought_reset_h` of quiet — because
    # once we stopped asking, the streak stopped being evidence about him.
    #
    # This gate has to exist BEFORE the pricing, not inside it. The old design's only
    # answer to a non-buyer was a cheaper ask, so there was always one more thing to
    # send and the loop had no exit — 85 consecutive ignored locks to one prod fan.
    # Fluctuate-down (08-19) reintroduces the ever-cheaper ask, so this stop is the
    # one guarantee the wall ends; at the floor the engine would otherwise alternate
    # floor ↔ free forever.
    unbought = int(state.get("unbought") or 0)
    reset_h = int(tcfg["unbought_reset_h"])
    last_at = teaser_last_at(state)
    if unbought and reset_h and last_at is not None:
        if (now or datetime.utcnow()) - last_at >= timedelta(hours=reset_h):
            unbought = 0
    max_unbought = int(tcfg["max_unbought"])
    if max_unbought > 0 and not last_sold and unbought >= max_unbought:
        return None

    # Clips. OFF (the default, and what a tcfg built without the key means) drops
    # video/gif at every folder scan below — the ladder is photos-only. ON admits them
    # and turns each pull's `count` into a SLOT budget: the coster charges a clip what
    # the rate card says it is worth in photos, so several short clips ride a big rung
    # while one that costs more than the ask is SKIPPED — the budget is a ceiling,
    # never trimmed after the fact.
    #
    # Built HERE, below the threshold and breaker returns, because it is a whole-vault
    # read and this function is called for every fan in the run — most of whom are not
    # being teased this turn. Async (and pure afterwards) so it can cross into the
    # to_thread scans below.
    cost = (await _video_slot_cost(account_id, tcfg["sizing"].cents_per_photo)
            if tcfg.get("videos") else _PHOTOS_ONLY)

    rung = int(state.get("rung") or 0)
    if not tcfg.get("adaptive"):
        idx = max(0, min(rung, len(rungs) - 1))
        r = rungs[idx]
        price_cents = max(0, int(r.get("price_cents") or 0))
        seen = await _seen_media(account_id, fan_id)
        folders, media_ids = await _pull_rung(client, tcfg=tcfg, rung=r, seen=seen,
                                              cost=cost)
        if not media_ids:
            return None
        return {"media_ids": media_ids, "price_cents": int(price_cents),
                "is_free": price_cents == 0, "folders": folders, "rung": idx,
                "next_rung": min(idx + 1, len(rungs) - 1), "convo": True}

    # ── ADAPTIVE: buy-aware climb / jitter, price-scaled weighted bundle ──
    # Resolve the scalars the PURE pricing policy needs, then delegate. The floor is
    # the static `floor_cents` (default $10) for everyone (`convo_teaser_floors`);
    # frac is the jittered no-buy move (`_jitter_frac` — a cut, or the bounce).
    idx = max(0, min(rung, len(rungs) - 1))
    prices = [max(0, int((r or {}).get("price_cents") or 0)) for r in rungs]
    last_px = max(0, int(state.get("last_price") or 0))
    floor, bait_floor = convo_teaser_floors(tcfg, max_paid_cents=max_paid_cents)
    frac = _jitter_frac(_jitter_band(tcfg),
                        random.random() if rand is None else float(rand))
    new_idx, price, softened = _next_convo_teaser_price(
        idx=idx, prices=prices, last_px=last_px, last_sold=last_sold,
        last_was_free=bool(state.get("last_free")), floor=floor,
        bait_floor=bait_floor, step=float(tcfg.get("climb_step") or 2.0), frac=frac)

    # ONE clamp, HERE, before this number is used for anything. Free ($0) stays free;
    # any priced ask is raised to what OF will actually accept.
    #
    # The clamp used to live only at ai_chatter's send site, so the price on the wire
    # and the price written back to `_hot_teaser.last_price` / `vault_sends` were
    # different numbers — and the ladder then computed its NEXT move from the one the
    # fan never saw. Prod on 2026-08-01: fan 487133280 was charged $3.00 twice while
    # his state recorded 114, and vault_sends holds 114/3/2/1 for asks that all went
    # out at $3.00. Clamping downstream of the decision means the decision is made on
    # fiction; clamping here means every consumer sees the same, sendable number.
    if 0 < price < OF_PRICE_FLOOR_CENTS:
        price = OF_PRICE_FLOOR_CENTS

    # The streak the breaker reads next turn. A SALE clears it outright; free bait is
    # not a failed ask and does not advance it.
    next_unbought = 0 if last_sold else int(unbought or 0) + (1 if price > 0 else 0)

    plan = tip_ladder.bundle_plan(price, tcfg["sizing"])
    if plan.total <= 0:
        return None
    seen = await _seen_media(account_id, fan_id)
    media_ids, breakdown = await asyncio.to_thread(
        _compose_bundle_ids, client, plan, seen,
        list(tcfg.get("bundle_premium_folders") or []),
        list(tcfg.get("bundle_normal_folders") or []),
        list(tcfg.get("bundle_free_folders") or []), cost=cost)
    if not media_ids:
        # No tier folders configured (or all exhausted). Adaptive is the house
        # default now, and an account whose media lives in the per-rung folders
        # must not go silent — pull from the rung's own folder instead; only
        # the PHOTO SOURCE falls back, the price stays adaptive.
        r = rungs[new_idx] if isinstance(rungs[new_idx], dict) else {}
        folders, media_ids = await _pull_rung(client, tcfg=tcfg, rung=r, seen=seen,
                                              cost=cost)
        if not media_ids:
            return None
        return {"media_ids": media_ids, "price_cents": int(price),
                "is_free": price <= 0, "folders": folders, "rung": new_idx,
                "next_rung": new_idx, "convo": True, "softened": softened,
                "unbought": next_unbought}
    return {"media_ids": media_ids, "price_cents": int(price),
            "is_free": price <= 0, "folders": [], "rung": new_idx,
            "next_rung": new_idx, "convo": True, "softened": softened,
            "bundle": breakdown, "unbought": next_unbought}


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
    seen = await _seen_media(account_id, fan_id)

    # Videos ride only with the checkbox on, and only WITH a coster: `count`
    # becomes a slot budget, so a clip is charged its rate-card worth in
    # photo-slots and a $25 tip can never walk off with a $30 video. OFF (the
    # default) strips clips at the scan and never pays the mirror read.
    cost = (await _video_slot_cost(account_id, _cents_per_slot(cfg))
            if cfg.get("videos_in_rewards") else _PHOTOS_ONLY)

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
        repeat_ok={0}, never_repeat=set(matched), cost=cost)
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
