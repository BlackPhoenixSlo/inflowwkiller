"""service/automations/tip_reward_config.py — the tip_reward config contract.

_DEFAULTS + _load_config in their own tiny module so BOTH halves of the split
feature import them at top level with no cycle: tip_reward (reward delivery,
the @register plugin) and teaser_select (teaser selection, split out of it
07-23) each consume this; neither imports the other for config anymore.

Same logger name as tip_reward — this module was carved out of it by pure code
motion and every emitted log record must stay byte-identical.
"""
from __future__ import annotations

import json
import logging

from db.engine import get_session
from db.models import AccountAiConfig

from ._common import DEFAULT_TIP_ASK_ENABLED
from ._vault_pick import folder_list

log = logging.getLogger("of-relay.automation.tip_reward")


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
    # ── The human pause before a media reaction lands ────────────────────────
    # ON: the picture is DEFERRED to 30-90s after his photo (drawn per-photo, see
    # pacing.picture_back_target) instead of firing the instant the job is claimed,
    # and the "…is typing" bar stays DARK for it — she is in her camera roll, not
    # typing. The vision describe is counted INSIDE that target, not added on top.
    # OFF restores the instant send. Governs the tip-bundle lane too when that
    # lands. Default ON: the pause is the point, and image_reply itself ships OFF,
    # so no live account moves until an operator turns the lane on.
    "media_reply_pace_enabled": True,
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
    # clips and turns the tease's count into a SLOT budget (`SlotCost`, the same
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


def rung_folder_slot(rung: dict):
    """PUBLIC seam: the RAW value of a rung's folder slot — the `folders` key when the
    rung has it, else the single `folder` string it used to be. Both the validator
    that WRITES a rung and the engine that READS one go through here, so they can
    never disagree about which key wins (`folders: []` alongside a stale `folder`
    means the operator cleared the rung, not that the old string is back)."""
    return rung.get("folders") if "folders" in rung else rung.get("folder")


def tier_folders(cfg: dict, *tier_names: str) -> list[str]:
    """The folder names for the named tier(s) in the `tiers` config, in the order
    given and deduped ACROSS them.

    A read of the config SHAPE, so it belongs beside the defaults that define that
    shape. It used to sit in `teaser_select`, which meant `make_right` imported the
    teaser module to ask a question about the tip config.

    It goes through `folder_list` — the same coercion the config API validates a
    SAVE through — so the read and the write can never disagree about what a folder
    slot means, which is the whole reason `rung_folder_slot` above exists. Hand
    normalising here instead skipped the dedupe `folder_list` is explicit about
    ("a folder listed twice must not make the scan read it twice"), and callers
    asking for two tiers at once used to concatenate the results, which is exactly
    the case it warns about: a name in both tiers made the pull scan that folder —
    a live OF round trip — twice for one send. Ask for both here instead."""
    # The RAW slot per tier — a list, a bare string, or absent. Not `by_name`:
    # that spelling means folder-name → vault-list-id everywhere else here.
    slot_by_tier: dict[str, object] = {}
    for t in cfg.get("tiers") or []:
        if isinstance(t, dict):
            slot_by_tier.setdefault(str(t.get("name") or "").strip().lower(),
                                    t.get("folders"))
    # Coerce each slot BEFORE flattening — a bare string would otherwise iterate
    # one character at a time — then once more over the union, to dedupe across
    # the tiers asked for.
    return folder_list([f for n in (x.strip().lower() for x in tier_names)
                        for f in folder_list(slot_by_tier.get(n))])


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
    tip `enabled` master switch. scope is "all" | "paid" (paid → only fans who spent).

    Lives HERE, with the config contract it reads, and not in `tip_reward` — these are
    pure accessors over `_load_config`, and filing them inside the 1.1k-line automation
    forced every consumer (webhook_dispatch, inbound_describe, ai_chatter) to import an
    automation to read a flag, two of them behind a lazy "avoid cycle" import."""
    cfg = await _load_config(account_id)
    scope = str(cfg.get("image_describe_scope") or "all").strip().lower()
    if scope not in ("all", "paid"):
        scope = "all"
    return (bool(cfg.get("image_describe_enabled")),
            str(cfg.get("image_describe_prompt") or ""), scope)
