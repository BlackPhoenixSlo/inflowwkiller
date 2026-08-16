"""
service/automations/ai_chatter.py — Automation: ai_chatter (PPVscriptAI M2).

The freestyle AI chatter+seller for fans UNDER the spend gate. It REPLACES
of_ai_chat for an account once enabled (of_ai_chat.run short-circuits when
`is_enabled` here is true), inheriting the info-gather duty — the girly voice,
the ONE-missing-fact question habit, the inline fact fill, the gen_info
refresh-if-stale hook — and (M3) adds selling from the content catalog
(catalog_scripts / catalog_items, offers in content_offers).

Who it talks to (code-side gates, never prompt-side):
  • fan spoke last (the "You:" sidebar skip — same as of_ai_chat). A BROADCAST
    (ppv_send / funnel / mass_nudge, or an untagged OF-app blast) does NOT count
    as us speaking: she still answers a message a blast landed on top of,
  • lifetime_spend_cents < max_lifetime_spend_cents (default $1000) — fans at or
    over the gate are WHALES: pure human-chatter territory, never touched,
  • he has BOUGHT CONTENT (`payers_only`, default ON) — a tip or a PPV unlock, and
    never a subscription. Below the floor he is of_ai_chat's to work; exempt are a
    live sale and the engaged old-fan roster, whom nobody else covers,
  • blacklist / skip_list respected, EXCEPT of_ai_chat's graduation reasons
    ("spent"/"too_long"/"info") — those mean "graduated from the gather loop",
    which is exactly the population ai_chatter exists for,
  • promo-spam guard ($0 spend + source=creator_we_follow) — the jaka problem,
  • fans mid-mass-funnel stay owned by reply_mass_funnel,
  • a fan a HUMAN chatter messaged within `resume_after_manual_hours` is left
    alone (cautious resume — the bot never barges into a human-run convo),
  • W3 fan lease + short cooldown, automation_paused_until, quiet hours via the
    rule's quiet_hours_json (executor-enforced).

Trigger modes (`mode` in ai_chatter_config_json):
  • "backup" — the bot only steps in when an inbound fan message has sat
    unanswered for ≥ sla_minutes (chatters are slow). The W7 webhook wake
    enqueues a fan-scoped job delayed by the SLA; the periodic rule is the
    fallback sweep. At fire time the gate re-checks — if a human answered
    meanwhile, the fan is simply no longer "fan spoke last".
  • "always" (default) — reply when eligible, like of_ai_chat today.

Unlike of_ai_chat there are NO graduation cutoffs: no max-message skip, no
deep_convo handoff (ai_chatter IS the post-gather voice — one bot voice per
fan). Once the question list empties the prompt flips from info-gather to plain
banter (and, M3, selling).

Config: account_ai_config.ai_chatter_config_json, shallow-merged over _DEFAULTS.
Ships DISABLED. Payload knobs: dry_run, only_fan_ids (W7 fan-scope, gates still
apply), force_ids (bypass gates — manual targeting), max_replies, model,
history_tail.

Reuse: the carefully-tuned texting machinery is IMPORTED from of_ai_chat
(bubble splitting, echo/lead-reaction dedupe, fact extract+fill, question
tracker, nickname push, profile refresh) so the voice stays byte-compatible.
Only the prompt builder is forked — it adds the M3 sell-block seam.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import random
from collections import Counter
from random import Random
import re
from datetime import datetime, timedelta
from typing import Literal, NamedTuple

from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

import automation_executor as ax  # _make_client / lease / cooldown seams
import llm_client                  # call .chat at runtime so tests can patch it
import ownership                   # the one home for owned-media semantics
from attribution import write_outbound_attribution
from automation_registry import register
from db.engine import get_session
from db.models import (
    AccountAiConfig, Blacklist, CatalogItem, CatalogProgress, CatalogScript,
    ContentOffer, Fan, FanProfile, LadderQuote, LadderState, Message, PendingOffer,
    QuotaAudit, RhythmState, ScheduledJob, SkipList, Transaction, VaultSend,
    created_at_text, parse_ts,
)
from llm_client import LLMCapExceeded
from . import _daylog  # what SHE did today — the creator-side twin of recent_events
from . import (_ghost, _stepout, cat_stickers, pacing, rhythm, script_packs,
               tip_ladder, upsell)
# The reply-volume leash — both gates, the spend rules that lift them, and the
# verdict ledger. Re-exported under these names because `fans.py` (the status
# endpoint) and the tests reach for them as `ai_chatter.X`, and because reading the
# leash out of the engine that obeys it is the point of the split.
from ._leash import (  # noqa: F401 — re-exported for fans.py / tests
    HOT_TIERS, QUOTA_BACKOFF_SERVED, QUOTA_HELD, QUOTA_NO_LADDER, QUOTA_OFF,
    QUOTA_RUNWAY, QUOTA_SIGNAL_LIFT, QUOTA_SPEND_LIFT, QUOTA_UNDER, QUOTA_UNLIMITED,
    QUOTA_IDLE_RESET, QUOTA_WINDOW, SPEND_QUOTA_UNLIMITED, TIER_BASELINE,
    TIER_BUYING_SIGNAL, TIER_NO_SIGNAL, TIER_PIC_SENT, TIER_POST_PURCHASE,
    _cadence_gate, _last_money_at, _paid_spend_by_window, _Quota, _quota_gate,
    _TIP_KINDS, _write_quota_audit, daily_quotas, quota_used, read_leash,
    spend_caps, spend_windows,
)
from ._markers import protocol_marker_re
from ._persona import (
    asks_about_her as _persona_asks_about_her, fan_claims_block,
    persona_register_age,
)
from ._outbound import ConsistencyCtx, finalize_draft
from . import _language
from . import _customs
from . import _objection  # which apology this turn owes him (regexes + the judge)
from . import _voice
from . import _openers  # the gen_info opener pool (the deepen phase)
# 🚫 no `_pins` import — pins unwired 2026-08-15, ruling in `_pins.py`'s docstring.
from . import _quotes  # which bubble he quote-replied (resolver + prompt block)
from . import _prompt_shape  # block grouping / facts ablation / task line
from . import sell_lane  # THE gate every priced send passes through (all engines)
from . import _sell_signal  # the model's own "he asked to buy" line (shadow)
# ppv_send owns the ONE price authority (`price_bounds`); ownership.py owns
# the ONE ownership check (`owners_of_media`, keyed on MEDIA — a fan who
# bought a clip in a mass blast has no content_offers row at all). Importing
# them rather than growing a second ceiling / a second ownership notion here
# is deliberate.
from .ppv_send import price_bounds
from ownership import owners_of_media as _owners_of_media
from ._common import (
    CONTENT_ASK_RE, ESCALATION_RE, NONNATIVE_OUTPUTS, NONNATIVE_REGISTER,
    BIO_CONSISTENCY_GUARDRAIL,
    nonempty,
    NO_NARRATION_RULE,
    # PAINFUL_TEXTING is NOT imported here: it varies by creator voice, so this
    # engine reads it off the `_voice.VoiceBlocks` bundle `load_voice_blocks`
    # returns. (There is no LIVE_PROOF_GUARDRAIL in this prompt at all — which is
    # why _manifest_block has to carry the customs fence itself.)
    ONPLATFORM_GUARDRAIL, STYLE_3LINE, STYLE_BRIEF,
    load_voice_blocks,
    STYLE_MAX_BUBBLES,
    apply_nonnative_spacing, apply_nonnative_style, apply_word_restriction, coerce_ids,
    load_consistency_flags,
    hold_with_typing, apply_typo_throttle, is_qualifying_inbound,
    load_cat_stickers_flag,
    load_cat_sticker_tuning,
    load_nonnative_flags, load_spacing_flags,
    load_painful_texting_flag, load_strip_emojis, load_style_flags,
    load_typing_indicator, load_typing_wpm, load_typo_flags,
    load_promo_spam_ids,
    detect_pic_offer,
    content_payer_fans,
    quarantine_if_undeliverable, recent_payer_fans, resolve_fan_name, resolve_model,
    should_skip_muted_creator, skip_unreachable_fan, thread_heat, typing_delay_seconds,
)
from .fan_state import fan_state, set_fan_state
# Deliberate sibling reuse — keeps the texting voice byte-compatible with
# of_ai_chat instead of forking 500 lines of tuned style machinery.
from .of_ai_chat import (
    _BREATHER_VARIANTS, _EXTRACT_HISTORY_TAIL, _HISTORY_TAIL, _MSG_CLIP,
    _NOID_PAUSE, _REPLY_MAX_CHARS, _REPLY_TEMPERATURE, _STYLE_VARIANTS,
    _bump_attempt, _clock_line, _dedupe_lead_reaction, _extract_and_fill,
    _load_mid_funnel_fans,
    _good_examples, _load_persona, _looks_like_echo, _mark_question_asked,
    _mark_reply_sent,
    _history_text,
    _maybe_push_nickname, _maybe_refresh_profile, _pause_fan,
    _primary_ask_target, _questions_still_needed, _recent_ask_pattern,
    _strip_html, split_for_bubbles,
)

log = logging.getLogger("of-relay.automation.ai_chatter")

_PURPOSE = "ai_chatter"          # model_by_purpose key + fan lease + enqueue kind
# automation_kind on a reply that CARRIES a priced offer. Same engine, same run —
# the tag exists because "Sent by ai_chatter" on a $25 PPV made the seller
# indistinguishable from the chatter in the thread and in the per-automation stats,
# and nobody could tell whether the upseller had ever actually sold anything.
_KIND_UPSELL = "ai_upseller"
# Most forced asks (force_ask / floor) one run() may fire across the whole account.
# _offer_caps_ok does not pace a fan's first-ever offer, so without this the tick a
# trigger is enabled would blast every never-offered fan at once. A few per run drips
# the roster in over many ticks instead — well under LADDER_OFFERS_PER_HOUR_MAX (20).
_MAX_FORCED_ASKS_PER_TICK = 3
# Every automation_kind THIS engine stamps. Any "is this row one of hers?" test must
# accept the whole set: match on _PURPOSE alone and her offer turns stop counting as
# hers, which silently un-caps the cadence counter (it would read an offer as someone
# else's message and let her keep talking past the burst limit).
_OUR_KINDS = frozenset({_PURPOSE, _KIND_UPSELL})
_REPLY_COOLDOWN_S = 10           # live chat — same short rest as of_ai_chat
# 🙋 Per-fan memory of "he has asked who she is" — the sticky half of the gate in
# run() that decides whether her canon rides the prompt. Its own namespace under the
# same `fans.custom_fields` boundary the day-log ledger uses, so per-fan memory has
# one home rather than two.
_BIO_ASKED_KEY = "_bio_asked"
# Consecutive rows of HERS closer together than this are bubbles of ONE reply, not
# separate replies (the humanizer types a reply out as 3-5 rows, seconds apart, and
# the cadence caps count replies). Comfortably above the longest inter-bubble typing
# delay, far below the gap between two real answers.
_BUBBLE_WINDOW = timedelta(minutes=2)
# The most inline hold-time ONE run() may spend across its serial candidate loop before
# it starts handing replies to the scheduler instead. Each hold is < INLINE_MAX_S and
# safe for the fan lease, but the run() itself occupies 1 of the executor's 4 GLOBAL
# slots for the SUM of its holds — so 8 fans × ~72s would pin a slot for ~10min and
# starve every other account's sends. Past this, further replies defer via wake_at.
_RUN_INLINE_BUDGET_S = 240.0

# A stored draft older than this is regenerated rather than replayed. Every
# post-LLM defer is a SHORT one by construction — asleep/away/step-out are decided
# by the PRE-LLM availability check, and `stepout_due` is deliberately withheld from
# the post-generation decide() — so what can park a draft is an in-scene delay or a
# run-budget hop, minutes not hours. The bound exists because the text was written
# against a clock line and a day log: "just got out the shower" is true when it is
# generated and a lie an hour later.
_DRAFT_MAX_AGE = timedelta(minutes=30)

# `_replay_draft` outcomes that mean THE TURN IS OVER — the candidate loop must not
# fall through and regenerate. Every other outcome is a reason to regenerate, and
# regenerating is always safe because it is exactly the pre-2026-08-15 behaviour.
_DRAFT_HANDLED = frozenset({"sent", "send_failed"})

# of_ai_chat's graduation skip reasons — they mean "left the gather loop", NOT
# "never message". ai_chatter exists precisely for these fans, so it ignores
# them while respecting every other skip (unreachable, old_fan_pre_ai, manual).
_GRADUATION_SKIPS = frozenset({"spent", "too_long", "info"})

# process_old_fans' flag: subscribers that predate the AI. Normally a hard skip
# (human territory) — liftable per-account via cfg["engage_old_fans"], which
# engages them in gentle mode (see old_fan_question_every in _DEFAULTS).
_OLD_FAN_SKIP = "old_fan_pre_ai"


def skip_reason_blocks(reason: str | None, *, engage_old_fans: bool) -> bool:
    """Does this skip_list row actually close the thread to ai_chatter?

    THE one copy of that question. `run()`'s loop asks it, and so does the fan-status
    badge in `fans.py` — which is the point.

    ⚠️ IT EXISTS BECAUSE THE BADGE HAND-ROLLED A SECOND COPY AND DROPPED A CLAUSE.
    `fans.py` filtered `_GRADUATION_SKIPS` but not the engage_old_fans lift, so every
    fan carrying `old_fan_pre_ai` rendered "🚫 Skipped (old_fan_pre_ai)" while the
    engine was happily engaging him — 96 fans on Lucas2 alone. Worse than the false
    label: the badge returns on its FIRST hit, so that phantom skip also made the true
    state (Human Rhythm's "On a break") unreachable for exactly those fans. An operator
    debugging a silent thread was shown a wrong reason AND denied the right one.

    Two exemptions, and they are not the same kind of thing:
      • `_GRADUATION_SKIPS` — of_ai_chat's "left MY gather loop" markers. ai_chatter
        exists precisely for these fans, so they never blocked it.
      • `_OLD_FAN_SKIP` — a real skip that the operator can lift per account.
    Everything else (unreachable, manual_restrict, muted_creator, of_restricted) blocks.
    """
    if reason is None or reason in _GRADUATION_SKIPS:
        return False
    return not (reason == _OLD_FAN_SKIP and engage_old_fans)


def seller_owned_fans(candidates: set[int], *, intent: set[int] | None,
                      payers_only: bool, content_payers: set[int],
                      always: set[int]) -> set[int]:
    """Of `candidates`, the fans the SELLER owns.

    THE one copy of that question, for the same reason `skip_reason_blocks` above
    is: `run()`'s candidate loop asks it to decide a skip, and `engaged_subset`
    asks it to tell of_ai_chat / deep_convo whom to cover. Those two answers are a
    PARTITION — every fan lands on exactly one side — so a second hand-rolled copy
    does not merely drift, it either puts two bot voices in one thread or leaves a
    fan with none.

    Two NARROWINGS and one OVERRIDE, which is the whole model:
      intent          — closer mode only: his newest inbound shows buying intent.
                        None in full-chatter mode (no narrowing).
      payers_only /
      content_payers  — the payer floor: he has bought a tip or a PPV unlock
                        (`content_payer_fans`). A SUBSCRIPTION IS NOT A PURCHASE.
      always          — OVERRIDES both: a live sale (open offer, recent payer,
                        OPEN/HOT ladder) and the engaged old-fan roster, whom no
                        other engine will chat at all. A man mid-sale who has not
                        paid yet is still ours to walk to a close.

    `always` is intersected with `candidates` and NOT with the narrowed set — an
    exempt fan is exempt precisely because he failed a narrowing. Getting that
    backwards silently un-exempts every old fan without buying intent."""
    owned = set(candidates)
    if intent is not None:
        owned &= intent
    if payers_only:
        owned &= content_payers
    return owned | (always & candidates)

# Built-in defaults — any key the account config omits. DISABLED until a creator
# enables it. The offer_* knobs are read by the M3 offer engine.
_DEFAULTS: dict = {
    "enabled": False,
    "mode": "always",                    # "backup" | "always". House default is
                                         # always-on: `enabled` below is the real
                                         # master switch, and every live account
                                         # runs "always" anyway — shipping "backup"
                                         # only meant a freshly-enabled account sat
                                         # behind an SLA hold nobody asked for.
    "intent_only": False,                # closer mode: only engage a fan whose
                                         # latest message shows buying intent
                                         # (_CONTENT_ASK_RE) or who has an open
                                         # offer. Pure chit-chat is left to the
                                         # team / Auto Convo. Zero LLM cost for
                                         # the fans it skips.
    "sla_minutes": 10,                   # backup: how slow is "slow"
    "max_lifetime_spend_cents": 100_000, # the whale gate ($1000)
    # The PAYER FLOOR — the whale gate's mirror at the bottom. The seller answers
    # only men who have bought CONTENT (a tip or a PPV unlock); everyone else is
    # of_ai_chat's to work and profile until he buys something. Ships ON: 56% of
    # all chat calls were going to fans who had never paid a cent, and the men who
    # convert are not converted by this engine — measured on prod 2026-08-15, first
    # purchases originate 84% human / 12% mass blast / 4% ai_chatter, and neither
    # humans nor blasts pass through this gate.
    #
    # A SUBSCRIPTION IS NOT A PURCHASE. The predicate is `content_payer_fans`, off
    # the messages table, precisely so a $9 subscriber is not mistaken for a buyer
    # (1,766 prod fans — 47% of everyone with non-zero lifetime spend — are exactly
    # that man). Do NOT "improve" this into a cents threshold: sub prices run $3-180
    # and PPVs start at $3, so no dollar line separates them, and the $12 one that
    # looks right admits 314 subscribers while rejecting 539 real buyers.
    "payers_only": True,
    "offer_mode": "ppv",                 # M3: "tip" | "ppv" | "both". Default
                                         # flipped both→ppv 2026-07-23: a "both"
                                         # message (priced + tip-ask) let a fan
                                         # pay twice for one promise — a live
                                         # incident paid the unlock AND the tip.
                                         # tip/both remain selectable per-account
                                         # in the Upseller tab for operators who
                                         # want the tip-unlock lane.
    # Tip ladder (workstream 3): TIP-ONLY offers get an INDEPENDENT adaptive ask
    # (escalate when he unlocked his last tip, soften 40–60% when he didn't,
    # floored at his biggest-ever tip) instead of riding the PPV quote. Needs
    # smart_pricing_enabled on (that's what builds the quote it overrides).
    # Ships DARK: default off, zero behavior change until an account opts in.
    "tip_ladder_enabled": False,
    "tip_ladder_base_cents": 1000,       # opening ask for a fan with no tip history
    "tip_ladder_step": 2.0,              # escalation ×2 after he tips (10→20→40→80…)
    # No-bite haircut keeps ~65–73% of the last ask (a gentle step DOWN, not a
    # collapse): $120 → ~$78–88, so he's re-offered lower but still premium. The
    # image bundle follows the softened price (price/$10 → ~9 photos at $88).
    "tip_ladder_cut_lo": 0.65,
    "tip_ladder_cut_hi": 0.73,
    "tip_ladder_floor_cents": 500,       # never ask below this ($5)
    # Cap: the account's PPV-library MAX, hard-limited to $200 (OF wire max). The
    # effective cap is computed per-run from the library bounds; this static value
    # is only the fallback when no library is configured.
    "tip_ladder_cap_cents": 20000,
    # Proven-spend price floor (workstream 3): a fan who already PAID
    # $X is never re-offered a cheaper item — the ladder climbs to the next tier
    # (a $50 buyer gets the $60 video, not the $24 set re-run at cold-open lows).
    # Ships DARK: default off, zero behavior change until an account opts in.
    "proven_spend_floor_enabled": False,
    # 0.6 of his biggest-ever single PPV, plus a flat per-sale ratchet. The mult sets
    # the base and must stay well under MAX_ASK_VS_HISTORY_MULT (3.0) — that is the
    # CEILING, so a floor near it leaves no room to price in; `_add_cents` is what
    # makes the ask climb. See upsell.MAX_ASK_VS_HISTORY_MULT for why 3x is the bound.
    "proven_spend_floor_mult": 0.6,
    "proven_spend_floor_add_cents": 0,   # +100 ⇒ every sale lifts his floor $1
    "max_offers_per_fan_per_day": 2,     # M3
    "min_fan_msgs_between_offers": 4,    # M3
    "pivot_on_escalation": True,         # closer pivots tease→offer when the fan
                                         # leans in / gets physical (ESCALATION_RE)
                                         # even without an explicit "show me" — but
                                         # only after he's chatted a bit. Still bound
                                         # by the offer pacing caps above.
    "min_fan_msgs_before_escalation_pitch": 2,  # "chat a bit first": no escalation
                                                # pivot until he's sent >= this many
    "max_fans_per_tick": 8,
    "resume_after_manual_hours": 1,      # cautious resume after a human chatted.
                                         # 6h read as "the bot is dead" on a thread a
                                         # chatter touched once; 1h is long enough that
                                         # she never talks over a live human hand-off.
    "stall_ttl_hours": 6,                # open offer → expired after this many hours
    "unsend_expired_offer": True,        # on expiry, pull (unsend) the unpurchased
                                         # PPV/offer message from the chat (per-chat
                                         # unsend; bounded by OF's 24h window)
    # Quote-reply context: OF lets a fan answer ONE specific bubble, and the prompt is
    # a flat FAN/YOU transcript, so that pointer was thrown away. Marks the bubble he
    # quoted instead of leaving the model to guess the referent.
    # ON by default: it costs no API call and changes nothing about what is SENT — a
    # thread with no quote-reply builds a byte-identical prompt. Measured 07-27 over
    # 14 days of prod: ~5% of inbound are quote-replies, 34% of those are ≤4 words
    # (unreadable without the referent), and ~4/day quote a LIVE PPV — a man asking
    # about the locked item, which is the strongest buying signal we had been dropping.
    "reply_context_enabled": True,

    # ── Prompt SHAPE (arm G). Three independent transforms over the assembled prompt.
    # ON by default (2026-08-13, operator call): apply to every account unless one
    # opts out per-account. Each transform is still a no-op when its flag is off, and
    # the `_build_messages` `shape` param defaults to OFF, so unit tests that build a
    # prompt directly are unchanged — only the live run() path reads these defaults.
    #   regroup    — same blocks, same words, grouped identity → hard → voice →
    #                situational → this turn → contract. Today the only blocks that
    #                describe THIS turn sit buried mid-prompt under ~6KB of rules.
    #   drop_facts — remove THESE ARE THE FACTS ABOUT YOU, but ONLY where every CORE
    #                fact (age, name, country, work) also exists elsewhere. The guard
    #                (`_prompt_shape.facts_are_redundant`) is fail-safe: it KEEPS the
    #                block on any doubt or unparseable content, so default-on cannot
    #                strip an account whose identity lives only there.
    #   task_line  — state which message this turn answers instead of leaving the
    #                model to infer it from position.
    # Measured caveat: the task line alone was a WASH on the ungrouped prompt over 350
    # paired quote turns (p=0.42). Shipped ON by explicit operator decision, not as a
    # proven lift — disable any of the three per-account to revert.
    "prompt_regroup_enabled": True,
    "prompt_drop_facts_enabled": True,
    "prompt_task_line_enabled": True,

    # ── Cadence controller (items 10/17/18/21) — the stop-condition subsystem.
    # ON by default (2026-07-22): "chat/sell forever" was never the behavior anyone
    # wanted — every live account had ticked this on by hand. Disable per-account to
    # get the historical no-graduation-cutoff behavior back.
    "cadence_enabled": True,
    # Item 21 — reply caps per burst, chosen by the fan's live signal. A "burst" is
    # counted on the fly (outbound messages since the last >session_gap_minutes gap),
    # NOT lifetime, so a long-term fan is never permanently silenced.
    # These are REPLIES, not bubbles (the humanizer types one reply out as 3-5 rows —
    # counting rows made a single answer cost 5 units of a cap of 5, see _BUBBLE_WINDOW).
    # Raised 2026-07-14 when a "reply" stopped meaning a row, then halved 2026-07-16 by
    # operator call: with bubbles counted right, the doubled runway read as pestering.
    # A cap is here to stop PESTERING a man who has gone quiet — not to cut off one who
    # is still typing back.
    "msg_limits_by_signal": {
        "baseline": 10,          # normal chatter — stop pestering after ~10
        "buying_signal": 20,     # content-ask / escalation / fresh offer / recent buy
        "no_signal": 5,          # offered but stalled (no buy, gone cold) — short leash
        "pic_sent": 25,          # he sent US a photo — hottest lead, longest runway
    },
    # Item 21b — proven-spend cap floor. The signal tiers above read what a fan is
    # doing THIS tick; these read what he has PAID in a rolling window, so a man who
    # has put real money down never gets the same short leash as a stranger even when
    # he's momentarily gone quiet. Each rule is {days, min_cents, cap}: if his PAID
    # spend (PPV unlocks + tips) over the last `days` is at or above `min_cents`, that
    # rule's `cap` applies. The floor only ever RAISES the burst cap — the fan's real
    # cap is max(signal-tier cap, best-matching spend cap) — so a hot live signal
    # (pic_sent 25) still wins if it's higher, and this never SHORTENS a leash.
    # When several rules match (a big weekly spender also clears the 30-day rule),
    # the HIGHEST cap wins. Empty list ⇒ spend never lifts the cap (signal-only, the
    # historical behavior). Rolling windows, not calendar days.
    "msg_limits_by_spend": [
        {"days": 30, "min_cents": 1000, "cap": 10},   # ≥ $10 in 30d → at least 10
        {"days": 7,  "min_cents": 10000, "cap": 15},  # ≥ $100 in 7d → at least 15
    ],
    # Item 21c — the DAILY quota. The signal/spend caps above bound a BURST, and a
    # burst reopens after `session_gap_minutes` (60) of her silence — so a fan who
    # keeps typing gets a fresh cap every hour, ~24 times a day. Measured on prod
    # 2026-07-26: 56% of all chat calls went to fans who have never paid a cent, and
    # one of them (0 spend, 7 days) pulled 981 calls / 1,734 outbound messages by
    # riding that hourly reset. The burst cap was never wrong — it just had no ceiling
    # above it. This is that ceiling: replies per ROLLING 24h, per fan.
    #
    # It is a SLOWDOWN, never a stop. The fans worth protecting from a hard cut-off are
    # the SLOW converters: measured in ACTIVE CHAT DAYS, the 15-30 day cohort is worth
    # ~$241 lifetime against ~$74 for day-one buyers — 9% of converters, 24% of
    # revenue. Burn the quota and she goes quiet for `quota_backoff_hours`, then talks
    # again — the leash gets longer, never infinite.
    #
    # (This used to justify itself with "30% of buyers took more than 30 days". That
    # number was FALSIFIED: it counted CALENDAR days from first contact and was
    # inflated by dormant threads, one of them 2,313 days. In active chat days it is
    # 6%. The conclusion held; the evidence did not. See library/DAILY_QUOTA_21C.md
    # §10 — if the 30% sentence reappears anywhere, it is stale.)
    # ── gen_info openers (the "deepen" phase) ──────────────────────────────────
    # Once her bio-gap list for a fan is empty she can work a mined gen_info question
    # into an ordinary reply instead of generic banter. Two knobs, because the
    # feature shipped with neither and that is not a shape anyone should run:
    #
    #   `profile_openers_enabled`  ON. The behaviour is wanted — a reply that picks up
    #                              something he actually told us beats "hey you 😘".
    #   `profile_openers_rate`     0.30. It must NOT ride every reply. The pool arms
    #                              for the WHOLE roster the moment this deploys (the
    #                              per-fan state blob is empty for every existing
    #                              fan), and a gathered fan would otherwise carry
    #                              "ask him this" on every single turn from then on —
    #                              a permanent interrogation cadence, which is the
    #                              exact bot tell the bio-consistency work exists to
    #                              remove. Most turns simply do not need a question.
    #
    # Rolled per (fan, inbound) so it is stable within a turn — a retry of the same
    # turn makes the same choice rather than re-rolling until it wins.
    "profile_openers_enabled": True,
    "profile_openers_rate": 0.30,
    "daily_quota_enabled": True,
    "daily_quota_replies": 10,           # non-payer baseline: HER replies per 24h
    # …but NOT until she has had a real run at him. EVERY fan is free before his first
    # purchase, so throttling on "hasn't paid" alone would ration exactly the men we
    # are still courting. This is a per-fan LIFETIME runway: until she has sent him
    # this many replies, the daily quota does not apply at all, and she may chat as
    # deep as the burst caps allow.
    #
    # 31 is an OPERATOR choice against a measured curve, not a derived number. Of every
    # fan who has ever bought, 84% did it inside 25 of her replies and 99% inside 100.
    # A 100-reply runway therefore forfeited essentially no conversions, but it carried
    # the long free repeaters all the way (one $0 fan was taking ~130 replies A DAY).
    # 31 sits just past that 84% knee: it buys a far cheaper tail, and it pays for it
    # with the ~15% of buyers who convert somewhere between 25 and 100 replies. Move it
    # back toward 100 if the held volume starts reading as lost sales rather than saved
    # calls — the curve above is what to judge that against.
    #
    # HER REPLIES, deliberately — not message rows. Rows count both directions and count
    # bubbles, and the humanizer types one reply out as 2.82 rows on average (measured),
    # so "31 rows" would be nearer 11 replies: it would start rationing before most fans
    # had a fair run. Replies are also what actually costs an LLM call, so the runway is
    # denominated in the thing being spent.
    "daily_quota_free_replies": 31,
    # Extra replies for 24h after a sale, on top of whatever tier he already earns —
    # a man who just paid gets more room that day, not the stranger's ration.
    "daily_quota_after_sale": 5,
    # The floor while he is actively asking for content / escalating / sending pics.
    # A man reaching for his wallet is never the man we ration, and this is what stops
    # the quota from ever costing a sale — it mirrors the buying_signal burst tier.
    "daily_quota_buying_signal": 20,
    # The backoff ladder, in hours, walked once per quota exhaustion since his last
    # money event — and CYCLIC: after the last rung it starts again at the first. A
    # fixed floor would relegate a fan to "once every 3 days" forever; wrapping means
    # every non-payer is periodically re-warmed at 4h, which is what the 30%-of-buyers
    # -take-30-days tail needs. Any sale resets him to rung 0.
    "quota_backoff_hours": [4, 12, 24, 72],
    # The other side of the coin: fans who DO spend get a BIGGER daily quota, never a
    # smaller one. Same {days, min_cents} shape as msg_limits_by_spend, same rolling
    # windows, same max-never-min rule — the effective quota is the HIGHEST match, and
    # `quota: 0` means UNCAPPED (a whale is never rationed). Empty list ⇒ spend never
    # lifts the quota.
    "daily_quota_by_spend": [
        {"days": 30, "min_cents": 1000,  "quota": 25},   # ≥ $10 in 30d → 25/day
        {"days": 7,  "min_cents": 10000, "quota": 60},   # ≥ $100 in 7d → 60/day
        {"days": 7,  "min_cents": 50000, "quota": 0},    # ≥ $500 in 7d → uncapped
    ],
    # ENFORCING since 2026-07-27, after a 39h shadow run on live traffic (784 verdicts,
    # 48 fans, 5 accounts). What the ledger showed: the 11 fans it flagged `held` took
    # 1,400 of 2,443 chat rows — 57% of her output — and produced $13.99 of $137.69, so
    # 57% of the volume was buying 10% of the money. Nothing rationed a live sale: the
    # only conversion among them came through `ppv_send`, which this gate does not touch,
    # while the chat lane asked that same man for $160.61 across seven offers and closed
    # none. The runway is what made it safe — one fan rode the free 100 down to
    # `runway_left=1`, bought at reply ~99, and only crossed into `held` six hours later.
    # Set False per account to go back to computing-and-logging without withholding.
    "daily_quota_enforce": True,
    "session_gap_minutes": 60,           # gap that starts a fresh burst for the caps
    # Item 17 — post-purchase talk window: keep chatting a just-paid fan this long
    # after his last money event; past it with no NEW spend, hand off (stop + cool
    # off → in closer mode of_ai_chat/Auto Convo keeps him warm).
    "post_purchase_minutes": 30,
    "offer_expiry_minutes": 120,         # a pending offer older than this w/o a buy
                                         # drops to the no_signal (short-leash) tier
    # Item 10 — a "stop" is a cheap SKIP (no reply this tick), NOT a durable pause:
    # the gate is a pure pre-LLM check, and skipping instead of pausing means a fan
    # who re-engages with a real buying signal (tier upgrade) is served again, and a
    # burst naturally reopens after a session_gap of silence. Nothing to configure.
    # Item 18 — re-engage nudge: one gentle follow-up if a fan we made an offer to
    # goes quiet without buying. The only cadence piece that SENDS an unsolicited
    # message, so it stayed off long after cadence shipped — but ONE follow-up on a
    # fan who was quoted a price and went quiet is the cheapest close there is, and
    # every live account runs it. On by default (2026-07-22); still needs cadence on.
    "nudge_enabled": True,
    "nudge_after_minutes": 15,

    # ── Old-fan engagement — lifts process_old_fans' `old_fan_pre_ai` skip.
    # ON by default (2026-07-22): the flagged roster is the back catalogue, and
    # leaving it to a team that never gets to it just meant silence. She replies to
    # them too, but interviews GENTLY: the info-gather ask fires at most ~once every
    # `old_fan_question_every` replies (1/N chance per message); the rest of the time
    # it's pure convo — these are established fans, not fresh subs to onboard.
    # Off per-account to hand the roster back to humans.
    "engage_old_fans": True,
    "old_fan_question_every": 10,

    # ── The 1:1 offer engine (upsell.py) + human reply pacing (rhythm.py) +
    # the editable line pack (script_packs.py). ALL OFF by default: with these
    # false, ai_chatter behaves byte-identically to today and none of the new
    # tables (ladder_state / ladder_quote / pending_offer / rhythm_state) is
    # written. The UI owns them ("🤖 AI Seller" tab).
    #
    # qualification_gate: may we put a PRICE in front of THIS fan right now —
    # measured on the 1:1 seller, never on the mass blast. Replayed over 90d of
    # prod ppv_send traffic the same gate would have deleted $798 of $926: a
    # broadcast's whole job is reaching fans who are NOT mid-conversation, so
    # gating it deletes it. On a live thread the same signal is an 18x per-send
    # lift (12.41% vs 0.67%). Hence: 1:1 only.
    "qualification_gate_enabled": False,
    # ── Answer a content-ask with the CONTENT (house default ON 2026-08-11) ──
    #
    # Operator call after reviewing 33 real picks and 22 real silences: "make all
    # this enabled by default, all on."
    #
    # ⚠️ This is the ONE default in this file that spends money on a turn nobody
    # asked an operator about, so the reasoning matters. It differs from
    # `force_ask` — which ships OFF because it converts a chat account into a
    # selling account — in that it never initiates. It fires only when the fan
    # has ASKED for content in his own words, and every refusal falls through to
    # the ordinary reply. So the worst case is the behaviour that already ships,
    # and the best case is that he gets the thing he asked for.
    #
    # It is also self-limiting in a way the other offer paths are not: the
    # resolver refuses when nothing fits, the audit refuses when the caption
    # would lie, and one plan per fan per tick is all the loop can hold.
    "pack_send_enabled": True,
    "pack_on_ask_enabled": True,
    # ── Who else may sell from the vault ──
    #
    # The same ask lane, called by the other chat engines through `sell_lane` — so
    # a fan who asks for content on an account with NO closer (7 of 21 live
    # accounts run Auto Convo alone) gets the thing instead of keep-warm banter.
    #
    # The permissions live HERE, not in each engine's own config blob, because
    # everything they depend on lives here: the shelf flags above, the offer caps,
    # the qualification gate. Two engines reading two copies of a cap is two caps.
    #
    # 🚨 of_ai_chat SELLS BY DEFAULT — operator ruling 2026-08-15, and it exists to
    # close a loop `payers_only` opened. That floor ships ON and `seller_owned_fans`
    # lifts only an active sale or the engaged-old-fan roster, so AN EXPLICIT ASK IS
    # NOT A LIFT: a man who has never bought and types "send me a video" is
    # `skipped_not_payer`, handed to of_ai_chat — which is precisely the engine that
    # "keeps chatting and profiling him until he buys something". With this off he
    # could never buy through chat at all, because the one surface left to him could
    # not sell. That is ~8,881 of 10,381 prod fans (2026-08-15).
    #
    # ⚠️ of_ai_chat's own prompt still says "don't offer pics or videos yet". That
    # is not a contradiction: the lane does not go through the prompt. It sends the
    # pack itself, out of band, and `continue`s — the model never writes the offer
    # and never sees that it happened.
    #
    # AUTO CONVO SELLS TOO — operator ruling 2026-08-15, the same day and the same
    # reasoning one step further. It has been never-PPV since it shipped, and that
    # rule was written when it had nothing to sell WITH: it could only have pitched
    # in prose, from a catalog, in the keep-warm lane. The vault lane is not that.
    # It sends the pack itself, out of band, on his own explicit words, and the
    # never-PPV instruction in its prompt stays true because the model never writes
    # the offer and never learns it happened.
    #
    # ⚠️ WHAT THIS DEFAULT ACTUALLY REACHES, stated exactly, because the obvious
    # reading is wrong: `autoreply.run` sets `ai_chatter_owns` from
    # `ai_chatter.is_enabled`, which is the account's `enabled` flag and NOTHING
    # finer — not the payer floor, not `engaged_subset`. Its `content_ask` local is
    # `(not ai_chatter_owns) and …`, so on an account with the closer ON this flag
    # changes nothing at all, including for the fans `payers_only` skips. Those men
    # are of_ai_chat's, and `of_ai_chat_sell_on_ask` above is what answers them.
    #
    # This one is for the accounts with NO closer (7 of 21 live), where Auto Convo
    # IS the chat and a man who typed "send me a video" got banter and nothing else.
    "autoreply_sell_on_ask": True,
    "of_ai_chat_sell_on_ask": True,
    # Gather-close PPV: of_ai_chat's parting set when the gather finishes with a
    # fan (profile complete OR the runaway cutoff). THE FOLDER IS THE SWITCH —
    # empty means off, and an operator picking one in the UI is the opt-in. Lives
    # in THIS blob for the same reason the two keys above do: the sell machinery
    # reads one config.
    "gather_close_folder": "",
    "gather_close_price_cents": 1000,    # $10 — "rung 1" money, fixed, no ladder
    "gather_close_count": 3,             # a few pictures (validator floors it at 2)
    # ONE LLM CALL PER ANSWER — operator ruling 2026-08-15. The closer's second
    # call was the fact-extract, and it fired for any fan with a single empty
    # profile column, which on a live roster is most of them. Profiling is
    # of_ai_chat's job (it exists to chat him and fill those columns) and
    # gen_info's; the closer duplicating it bought a full-prompt call per reply.
    #
    # ⚠️ This does NOT disable the extract. It narrows it to TIER B — the ~5% of
    # turns where SHE said something about herself — because `her_claims` and the
    # two body-focus flags have no other writer anywhere in the service, and
    # `content_resolver.profile_terms` prices substitute PPVs off those flags.
    # See `of_ai_chat._extract_and_fill`'s `claims_only`.
    "closer_extract_claims_only": True,
    # 🙋 HER CANON RIDES THE PROMPT ONLY WHEN HE ASKS ABOUT HER — operator ruling
    # 2026-08-15. Her PROSE persona is unconditional (it is who she is on every
    # turn); the FACTS TABLE — age, job, city, family, tattoos — is the half a fan
    # only needs once he asks, and it was 322 chars of every prompt on an account
    # with canon filled. `_persona.asks_about_her` is the detector, it already
    # exists, it is bilingual and it costs no call. STICKY per fan once he asks.
    "persona_facts_on_ask_only": True,
    # 🚫 THE PROMPT DOES NOT SELL — operator ruling 2026-08-15. Selling happens when
    # HE ASKS, through the vault ask lane (`sell_lane` → `pack_sender`), which sends
    # the pack itself, out of band, on his own words. The model is no longer handed a
    # sellable manifest, no longer asked to write a pitch, and no longer emits an
    # `>>OFFER` line — the blind cheapest-item pick goes with it.
    #
    # A FLAG, NOT A DELETION, and deliberately so: the catalogue paths stay in place
    # and go dark, because `_NO_SELL` is a shape every one of them already handles
    # (`close=""`, `marker=False`), and because a money surface that has been live on
    # 13 accounts should be provably silent for a week before its code is cut. Turn it
    # back on per account from the raw-JSON editor; nothing needs a redeploy.
    #
    # ⚠️ CUSTOMS ARE NOT CATALOGUE. A custom is recorded to order, has no rows, and is
    # sold by TIP on the male lane — `_manifest_block`'s customs half still renders.
    "prompt_sell_catalog": False,
    # 🔔 THE MODEL'S OWN "he asked to buy" LINE — ARMED 2026-08-15 (operator ruling),
    # in `ai_chatter` and `autoreply`. `_sell_signal.decide` is now an OR: either the
    # regex or the model sees an ask and the vault lane runs.
    #
    # It shipped shadow, waiting on a week of live disagreement counts. The ruling is
    # to collect those counts ARMED instead — the roster is small accounts kept for
    # exactly this case, and the shadow week could only ever have been gathered by
    # turning the prompt block on anyway, so shadow bought a delay and no safety.
    #
    # WHAT IT COSTS WHEN IT IS WRONG, stated plainly because that is what makes OR the
    # right operator: a false positive is one vault pack sent to a man who did not ask
    # (priced, refundable, and `sell_lane` still applies every brake — caps, spend
    # velocity, ownership dedup). A false negative is the sale, silently. The regex has
    # been the only reader and every gap in it was found in production by a fan who
    # asked and got banter.
    "sell_signal_enabled": True,
    # force_ask: the OFFER is emitted by the MODEL (an offer marker it may or may not
    # write). So a fan the gate has already cleared, with a priced manifest in front
    # of the model, still gets pure chat whenever the model declines to sell — which
    # is most turns. Live on one account: 184 replies, 4 offers, and the gate blocked only 8
    # of them. The model, not the gate, is what stops the selling.
    #
    # With this on, a turn where the gate said YES and the model wrote no marker
    # attaches the ask anyway. It rides ON the gate (never reaching a fan the gate
    # refused) and the offer caps still pace it — `min_fan_msgs_between_offers` and
    # `max_offers_per_fan_per_day` are what bound the volume, not the model's mood.
    # Ships OFF: it converts a chat account into a selling account the moment it flips.
    "force_ask": False,
    # ask_after_fan_msgs — the FLOOR. force_ask waits for the thread to go hot, and some
    # men never get there: they chat, they're friendly, they're engaged, and they are
    # never asked for a penny. This is the backstop — after this many of HIS messages
    # with no ask on the table at all, put ONE in front of him even if the scene never
    # turned sexual. Counted since the last ask (ours OR a human chatter's), so it can't
    # stack on a price he's already looking at.
    #
    # It is NOT a bypass: the gate, the brakes (broke / declined / companion / bot-
    # accused) and the offer caps all still apply, so a man who said he's out of money is
    # never asked no matter how long he talks. 0 = off.
    "ask_after_fan_msgs": 0,
    # ── Post-purchase objection judge (08-04) ────────────────────────────────
    # The decline regexes are tuned for PRECISION, because a false hard stop costs
    # a 72h selling blackout on a live thread. That trade has a price, and fan
    # one buyer paid it: a polite "why did you have me pay for them?" scored None,
    # argued with him for four bubbles and re-priced him twelve minutes later.
    # `is_content_dispute` closes that exact sentence; it cannot close the ones
    # nobody has written down yet.
    #
    # So RECALL is bought with one cheap LLM call, in the only window where a miss
    # is expensive and a false positive is nearly free: the first few messages after
    # he has actually PAID. A man who just spent money and is now unhappy is a
    # chargeback and a deleted account. A man who just spent money and is happy is
    # not going to be harmed by us declining to sell him something else for a bit.
    #
    # Volume is bounded by purchases, not by traffic: at most `max_msgs` calls per
    # purchase (one per inbound turn), and only for a SUBSTANTIVE inbound — "😍" and
    # "thanks babe" never reach the model. Live roster order-of-magnitude: tens of
    # purchases a day across 12 accounts, against ~$0.17/day of total AI spend.
    "post_purchase_objection_check": True,
    "post_purchase_window_hours": 6,   # a complaint lands fast; 0 = off
    "post_purchase_max_msgs": 3,       # his first N inbounds after the unlock
    # Up to this many UNPAID PPVs may ride at once (a 2nd "here's another / here's it
    # cheaper" is a normal close). Floored at 2 in code — never below.
    "max_open_offers": 2,
    # When he haggles on the pending piece, re-price it this fraction cheaper (0.10 = 10%).
    "haggle_discount_pct": 0.10,
    # Resend a balked-on priced TEASER up to this fraction cheaper (capped at 0.20 = 20%).
    "teaser_discount_pct": 0.20,
    # Content-derived price bands + the post-purchase (hot-window) ladder.
    # Meaningless without the gate — the UI keeps it disabled until the gate is on.
    "smart_pricing_enabled": False,
    # Hard takeover: once a fan is in an ACTIVE sale (open offer / just paid / an
    # OPEN|HOT ladder), the seller drives his thread regardless of the base chatter
    # mode — it bypasses the backup-SLA hold and the closer no-intent skip so a
    # sale is never dropped mid-flow. Inert unless the gate is on (guarded below),
    # so a default account — gate off — behaves exactly as today. The hand-back is
    # the existing COOLDOWN → COMPANION transition (returns him to normal chat).
    "upsell_takes_over": True,
    # Human reply timing: sleep window, variable delays, cover lines. ON by default
    # (2026-07-22) — instant answers around the clock is the single loudest bot tell.
    "rhythm_enabled": True,
    # Sample ordinary reply latency from the OPERATOR's distribution (85% inside
    # 2min / 10% 2-6 / 4% 6-15 / 1% 15-60) instead of the archive-fitted
    # lognormal, and drop the separate break roll — see rhythm.PACE_BUCKETS.
    # DEFAULT OFF: six live accounts run rhythm on a curve fitted to their own
    # data and must not be re-paced by a setting they never chose.
    "rhythm_pace_buckets": False,
    # The bands themselves, editable per account: [{"pct": 85, "up_to_min": 2}, ...]
    # (see rhythm.parse_pace_curve). None ⇒ the shipped 85/10/4/1. Only read when
    # `rhythm_pace_buckets` is on.
    "rhythm_pace_curve": None,
    # Add this many seconds to EVERY reply (rhythm.RhythmCtx.reply_bonus_s). A flat
    # translation of the whole curve — floor, ceiling and draw together — so the shape
    # is untouched and the two fast bands drain into the two a human actually lives in.
    # Measured against these accounts' own human chatters: we sit at 15.1% under 15s
    # and 17.5% at 15-30s where a human sits at 7.2% and 7.9%, and we undershoot
    # 30-60s (7.8 vs 16.5) and 1-2m (9.0 vs 14.4). Set 0 to disable.
    "rhythm_reply_bonus_s": 10.0,
    # After a silence this long, her reply comes back as ONE bubble however much it
    # weighs (of_ai_chat.split_for_bubbles(force_single=True)). A human returns
    # single-bubble ~64% of the time at EVERY gap length; ours falls from 31.9% at
    # <2min to 14.7% at 30min-2h, so the longer we were quiet the more we said on
    # arrival. 0 disables. Content is never dropped — the bubbles are merged, not cut.
    "rhythm_return_single_bubble_s": 600.0,
    # ── SHE STEPS OUT. Every 7-15 of her replies in a chat that is neither hot nor
    # just-sold-to, she is gone for 1-2 hours — and comes back early if he writes
    # again on his own (2 messages at least a minute apart).
    #
    # ⚠️ DEFAULT ON, which is not this file's habit. It is deliberate and it was asked
    # for: the point is to run it across the roster and read the data. It is also the
    # only silence here that may fire while an answer is OWED — see rhythm.STEPOUT_MIN_S
    # for why, and note the persistence exit is what bounds the cost of that choice.
    # Every default below reads from the module that owns the concept, so there is
    # exactly one place to change any of them.
    "rhythm_stepout_enabled": True,
    "rhythm_stepout_min_exchanges": _stepout.MIN_EXCHANGES,
    "rhythm_stepout_max_exchanges": _stepout.MAX_EXCHANGES,
    "rhythm_stepout_min_minutes": rhythm.STEPOUT_MIN_MINUTES,
    "rhythm_stepout_max_minutes": rhythm.STEPOUT_MAX_MINUTES,
    # How many messages, how far apart, end the step-out early. 0 messages ⇒ no exit
    # (she stays gone for the full draw), which is a real configuration and not a bug.
    "rhythm_stepout_break_msgs": _stepout.PERSIST_MSGS,
    "rhythm_stepout_break_gap_s": _stepout.PERSIST_GAP_MIN_S,
    # ── Human TYPING pacing (automations/pacing.py) — the gaps BETWEEN the bubbles
    # of one reply, which `rhythm` never touched. Measured over 120 days: 3.0% of
    # our inter-bubble gaps exceed 20s, against 26.9% for a human chatter and 56.6%
    # for a fan. She never once stops mid-reply, and nothing in the old path could
    # make her. DEFAULT OFF — the accounts already earning do not move.
    "pacing_enabled": False,
    # How often a bubble draws a real "she stopped" pause. THE knob: a grid search
    # against the human/fan target spends its whole budget here, and the fitted
    # optimum for an always-on pause is ZERO — dispersion, not delay. 0 ⇒ only the
    # enter-press and emoji-reach garnish remain.
    "pacing_drift_pct": 30.0,
    # The ceiling on one such pause, in seconds. Held INLINE, so it must stay under
    # rhythm.INLINE_MAX_S (120) — past that a reply needs the scheduler, a lease
    # release and a wake job, which is a different feature. Hard-clamped to
    # pacing.MAX_DRIFT_CAP_S regardless of what is stored.
    "pacing_drift_cap_s": 90.0,
    # Blank the "...is typing" bar for 5-10s mid-bubble (she stopped to think).
    # Costs ZERO added latency — it only changes when frames are emitted inside a
    # hold that was happening anyway.
    "pacing_think_gaps": True,
    # ── The ghost cycle: whole DAYS dark on a fan, on a repeating schedule.
    # Manufactured scarcity — "she has a life, and you are not automatically in
    # it". DEFAULT OFF, and it must stay that way: `rhythm_enabled` is default
    # ON, so this flag is the only thing standing between the shipped config and
    # an account that stops answering fans for days at a time.
    "rhythm_ghost_enabled": False,
    # The cycle itself, editable per account and REPEATING:
    # [{"chat_days": 3, "ghost_days": 1}, {4, 2}, {5, 2.5}] — chat 3 days, dark 1,
    # chat 4, dark 2, chat 5, dark 2.5, back to the top. None ⇒ that shipped
    # default (`_ghost.DEFAULT_CYCLE`). Only read when the flag above is on.
    "rhythm_ghost_cycle": None,
    # NB the day log's switch is NOT here — it lives in `style_config_json` under
    # `_daylog.DAY_LOG_ENABLED_KEY`, because the day is a property of the CREATOR and
    # must be one flag across BOTH chat engines. Two engine-scoped flags could be
    # flipped independently and hand the same fan two different days. Default OFF.
    # No-sleep pacing: keep the hot/cold/busy variable delays + short "stepped away"
    # breaks, but NEVER the long overnight sleep — and it needs no timezone. For a
    # creator who wants "she's a person who gets busy" without an 8-hour night gap.
    #
    # Flipped True→False 2026-08-04 on the operator's ruling that the house default
    # is a four-hour night (rhythm.DEFAULT_SLEEP, 02:00-06:00 creator-local). The
    # old default meant NO account on house settings ever went quiet, which made the
    # sleep window a setting that existed and never ran. An account with no timezone
    # still can't sleep — rhythm.tz_offset_for returns None and the sleep branch is
    # skipped — so this changes nothing for accounts that never answered that
    # question, only for the ones that did.
    "rhythm_no_sleep": False,
    # Where her sleep window COMES FROM when there is no explicit override:
    #   "default" — rhythm.DEFAULT_SLEEP, the house night (02:00-06:00 local)
    #   "derived" — the longest quiet block in her own outbound histogram
    # Default "default" so that "the house night is 2-6am" is TRUE rather than
    # true-only-for-accounts-with-thin-history. Derivation stays a per-account
    # choice: it answers a real question, but it also silently outranked the house
    # setting on every established account, which is not what a default means.
    "rhythm_sleep_source": "default",
    # None ⇒ the source above decides. ["HH:MM", "HH:MM"] ⇒ operator override, which
    # outranks both.
    "sleep_window": None,
    # {slot: [lines]} over script_packs.PACK. An EMPTY / missing slot falls back
    # to the shipped default — script_packs.render never sends an empty message.
    "script_pack_overrides": {},

    # ── v2 safe-seller lane (spec §11). ALL OFF/invariant at ship: with these
    # false ai_chatter behaves byte-identically to today. Each rides the gate.
    #   post_buy_rung_enabled — the ONE unsolicited priced message (§4.4b). A free
    #     post_buy_bridge bubble still fires without it; only the follow-up RUNG is
    #     gated. OFF is the genuine consent question.
    "post_buy_rung_enabled": False,
    #   gift_enabled — a genuinely FREE (price=0) unseen-media thank-you at aftercare
    #     after >=2 paid rungs this session (§7.2). NEVER a paid gift, never after a
    #     spend_regret line.
    "gift_enabled": False,
    #   filming_stall_enabled — the "im filming it rn" active fiction (§3.7), logged
    #     as a deception surface. 0-EV; default OFF.
    "filming_stall_enabled": False,
    #   Second chance after a refusal (§11 checkbox #3): 1 = tap out after one unpaid
    #     rung (today's behaviour); 2 = one win-back discount then stop. A real
    #     risk/volume tradeoff the operator owns.
    "stop_after_unpaid_rungs": upsell.STOP_AFTER_UNPAID_RUNGS,
    #   §6.2 rolling 7-day paid-PPV brake → COMPANION for the window. Operator-editable
    #     VALUE (not a checkbox-off). 0/absent ⇒ the module constant.
    "spend_velocity_cap_7d_cents": upsell.SPEND_VELOCITY_CAP_7D,
    # ── Ladder aggressiveness (Upsell tab). How hard the price climbs after a paid
    # rung, and how far above his biggest-ever single PPV she may ask. Both default
    # to the data-backed constants. The escalation ladder mostly climbs by UNLOCKING
    # PRICIER ITEMS (the script order), not by this multiplier — but a fan willing to
    # go "to the moon" is otherwise frozen at 3x his history; raise max_ask_history_mult
    # to let a whale run. Conversion drops past 3x (52%→31%), so the UI warns on it.
    "escalation_mult": upsell.ESCALATION_MULT,          # 1.75x off his last paid
    "max_ask_history_mult": upsell.MAX_ASK_VS_HISTORY_MULT,  # 3.0x his biggest-ever PPV
}


async def _load_config(account_id: str) -> dict:
    """ai_chatter_config_json shallow-merged over _DEFAULTS. Absent/NULL/parse
    error → defaults (disabled)."""
    async with get_session() as s:
        cfg = await s.get(AccountAiConfig, str(account_id))
    raw = getattr(cfg, "ai_chatter_config_json", None) if cfg else None
    merged = dict(_DEFAULTS)
    if raw:
        try:
            stored = json.loads(raw) or {}
            merged.update({k: v for k, v in stored.items() if v is not None})
        except Exception:
            log.warning("bad ai_chatter_config account=%s", account_id, exc_info=True)
    # Account language rides on cfg so scripted surfaces (script packs, the intent
    # gate, unlock reactions) localize without threading a param through every call.
    merged["_account_lang"] = _language.norm_lang(getattr(cfg, "language", None)) or "en"
    # The voice rides on cfg for the SAME reason the language does — and one more.
    # `_pack_line` sends a script-pack line VERBATIM, with no model in the loop, on
    # the nudge / post-buy / aftercare turns. Those turns return from run() BEFORE
    # any per-account flag is loaded (see the payload branches at the top of run),
    # so a voice resolved later in run() would never reach them. Stamping it here,
    # off the row this function already holds, is what makes those paths lane-aware
    # at zero query cost. NULL → "her" → every pack pick is unchanged.
    merged["_account_voice"] = _voice.norm_voice(getattr(cfg, "voice", None))
    return merged


async def is_enabled(account_id: str) -> bool:
    """Cheap gate for of_ai_chat's hand-over check + the W7 dispatcher."""
    cfg = await _load_config(account_id)
    return bool(cfg.get("enabled"))


async def gate_for(account_id: str) -> int | None:
    """The spend gate in cents when ai_chatter is enabled for this account, else
    None. Other conversational automations use this to yield fans ai_chatter
    owns (fan eligible ⇔ spend < gate)."""
    cfg = await _load_config(account_id)
    if not cfg.get("enabled"):
        return None
    return int(cfg.get("max_lifetime_spend_cents") or 0)


async def owns_whole_account(account_id: str) -> bool:
    """True when ai_chatter answers EVERY fan it sees, so a gather engine (of_ai_chat)
    must stand down for the account entirely rather than cover a remainder.

    The account-level twin of `engaged_subset`, and it lives HERE, beside it, on
    purpose: "does it own everyone" and "which ones does it own" are one rule, and
    of_ai_chat cannot answer the first by ANDing accessors without silently
    re-deriving the second. It ships as its own function because it is asked BEFORE
    the candidate gather — `engaged_subset` needs fan_ids that do not exist yet.

    Any mode that narrows the roster (closer, or the payer floor) makes this False.
    One config read, not one per flag: `_load_config` hits the DB every call."""
    cfg = await _load_config(account_id)
    if not cfg.get("enabled"):
        return False                     # owns nobody, so it replaces nobody
    return not (cfg.get("intent_only") or cfg.get("payers_only"))


async def engaged_subset(account_id: str, fan_ids: set[int]) -> set[int]:
    """Of `fan_ids`, the ones ai_chatter currently OWNS — i.e. will (or may) answer
    THIS tick. of_ai_chat and deep_convo consult this so they cover exactly the
    fans ai_chatter leaves alone: no second bot voice on the same fan, and no fan
    left silent either.

    Disabled → the empty set (owns nobody). Otherwise it is `seller_owned_fans`
    over `fan_ids`: the two narrowings and the one override, resolved for this tick.

      • intent_only (CLOSER) narrows to a content-ask or sexual escalation in his
        latest inbound — pure chatter is left to of_ai_chat / deep_convo / the team,
        matching the closer's own "pure chatter is left to Auto Convo" rule in run().
      • payers_only (the PAYER FLOOR, default ON) narrows to men who have bought
        content. NOTE this means full-chatter mode does NOT imply "everyone".
      • `_always_answered_fans` overrides both — a live sale, and the engaged
        old-fan roster (nobody else may chat those; of_ai_chat and deep_convo both
        hard-skip that reason), so this set must claim them or they go silent.

    Both narrowings mirror run()'s own gates, and the FLOOR is literally the same
    call, so ownership here cannot diverge from who the seller actually answers.
    `owns_whole_account` is the account-level twin, asked before the gather.
    autoreply skips exactly this set as hot leads."""
    if not fan_ids:
        return set()
    cfg = await _load_config(account_id)
    if not cfg.get("enabled"):
        return set()
    intent_only = bool(cfg.get("intent_only"))
    payers_only = bool(cfg.get("payers_only"))
    # Owns everyone → say so without touching the DB. `always` is a subset of
    # `fan_ids`, so every query below would only rebuild `fan_ids`.
    if not intent_only and not payers_only:
        return set(fan_ids)

    return seller_owned_fans(
        set(fan_ids),
        intent=(await _intent_fans(account_id, fan_ids, cfg) if intent_only else None),
        payers_only=payers_only,
        content_payers=(await content_payer_fans(account_id, fan_ids)
                        if payers_only else set()),
        always=await _always_answered_fans(account_id, fan_ids, cfg))


async def _always_answered_fans(account_id: str, fan_ids: set[int],
                                cfg: dict) -> set[int]:
    """Fans no eligibility gate may drop — the set form of run()'s `_in_active_sale`
    plus the engaged old-fan roster. See `seller_owned_fans` for why they outrank
    every gate."""
    always = {int(o.fan_id) for o in await _open_offers(account_id)
              if int(o.fan_id) in fan_ids}
    always |= await recent_payer_fans(account_id, fan_ids)
    # An OPEN/HOT ladder is the third leg of `_in_active_sale`, and it does NOT
    # always come with an open-offer row — 3 prod fans sit in exactly that state.
    # Read behind the same flag run() loads ladders behind, so an off gate costs
    # nothing here either and the two cannot disagree about who is mid-sale.
    if cfg.get("qualification_gate_enabled"):
        always |= {fid for fid, lad in
                   (await _load_ladders(account_id, fan_ids)).items()
                   if lad is not None
                   and lad.status in (upsell.STATUS_OPEN, upsell.STATUS_HOT)}
    if cfg.get("engage_old_fans"):
        async with get_session() as s:
            rows = (await s.execute(
                select(SkipList.fan_id).where(
                    SkipList.account_id == str(account_id),
                    SkipList.reason == _OLD_FAN_SKIP,
                    SkipList.fan_id.in_(fan_ids))
            )).all()
        always |= {int(r[0]) for r in rows}
    return always


async def _intent_fans(account_id: str, fan_ids: set[int], cfg: dict) -> set[int]:
    """Fans whose NEWEST inbound shows buying intent — a content-ask or a sexual
    escalation. The closer's `base`: whom it would answer before any floor."""
    async with get_session() as s:
        rows = (await s.execute(
            select(Message.fan_id, Message.body)
            .where(Message.account_id == str(account_id),
                   Message.fan_id.in_(fan_ids),
                   Message.direction == "in",
                   Message.is_unsent.is_(False))
            .order_by(Message.fan_id, Message.created_at, Message.message_id)
        )).all()
    last_in: dict[int, str] = {}
    for fid, body in rows:
        last_in[int(fid)] = body or ""   # rows asc → last write per fan = newest inbound
    gate_lang = cfg.get("_account_lang", "en")
    return {fid for fid, body in last_in.items()
            if (_language.is_content_ask(_strip_html(body), gate_lang)
                or _language.is_escalation(_strip_html(body), gate_lang))}


# ── Candidate gathering (own pass — needs timing + human-send metadata) ──────

class _Cand:
    __slots__ = ("fan_id", "fan_msg_n", "last_dir", "last_body", "messages",
                 "last_in_at", "last_out_at", "last_human_out_at", "session_out_n",
                 "day_out_n", "day_out_n_at_stop", "total_out_n", "first_at",
                 "her_last_at", "pic_sent", "last_in_desc", "last_in_desc_at",
                 "last_out_was_gif", "last_in_text", "first_in_at",
                 "msg_ids", "reply_ctx", "in_run")

    def __init__(self, fan_id: int):
        self.fan_id = fan_id
        self.fan_msg_n = 0
        # Did his MOST RECENT inbound carry media? The buying-signal tier the
        # cadence gate calls `pic`. Derived here, from the thread, so the status
        # endpoint reads the same fact the engine does instead of reconstructing
        # it from dispatch state it cannot see (see _leash.TIER_PIC_SENT).
        self.pic_sent = False
        # The VISION DESCRIPTION of that most recent inbound media, or "" when there
        # is none (describe off, still pending, a giphy he forwarded). `pic_sent`
        # alone only says a file arrived; the rating beat needs the prose, because
        # "rate what he sent" with nothing to read produces a generic compliment —
        # which is the exact non-reaction this beat exists to replace.
        self.last_in_desc = ""
        # …and WHEN it landed. The slot clears on her outbound, so every path where she
        # produces none — ghost cycle, automation_paused_until, quarantine, a dropped
        # empty reply, cap_hit — would otherwise let a picture from Tuesday still open
        # the turn with "HE JUST SENT YOU A PICTURE". Read against _PIC_DESC_TTL below.
        self.last_in_desc_at: "datetime | None" = None
        # Her most recent outbound was a gif with no text — the one turn a pause is
        # free on, because a gif neither answers nor asks anything (rhythm's
        # `after_gif_solo`).
        self.last_out_was_gif = False
        # Direction of the last message that TOOK A TURN — broadcasts are transparent
        # here (see _gather), so a blast fired over his message does not read as
        # "we spoke last" and silence the reply he is owed.
        self.last_dir = ""
        # The last message's HISTORY LINE — what the reply-LLM reads, and what the gif
        # seed keys off. For an inbound with media this carries a `[he sent: …]` vision
        # tag, so it is the WRONG field for any gate that asks "what did HE say":
        # use `last_in_text` for that. (Both `answer_owed` and the gif-first opener
        # were written against this field first and both were wrong for the same
        # reason — the describer's prose cleared thresholds his own words never did.)
        self.last_body = ""
        # His last inbound's OWN WORDS — HTML-stripped body with no `[he sent: …]`
        # vision tag glued on. `last_body` carries that tag, and a description like
        # "an animated gif: a cat rolling its eyes" is long enough to clear
        # is_qualifying_inbound's 3-token bar all by itself — so reading the gate off
        # last_body made every media-only DM "owed an answer" on the strength of text
        # OUR OWN describer wrote. A gif he sends is a dead-end reaction, which is the
        # one turn a pause is safe on; it must not read as a question.
        self.last_in_text = ""
        # HIS first message on this thread — the warm-up clock. Deliberately not
        # `first_at` (the oldest row of ANY direction): that is our own welcome, and
        # the conversation does not start until he answers it.
        self.first_in_at: datetime | None = None
        self.messages: list[tuple[str, str]] = []  # (direction, body) oldest→newest
        # OF's message_id per `messages` row — what resolves a quote-reply's target
        # back to a line the model can see. PARALLEL to `messages` rather than folded
        # into the tuple so every existing reader (thread_heat, _recent_ask_pattern,
        # the fan-run scan, the tests that hand-build a _Cand) is untouched; both
        # lists have exactly one writer, `add_message`, so they cannot drift.
        self.msg_ids: list[int] = []
        # The quote-reply he made, if any — filled per reply by `_quotes.resolve`,
        # read only
        # by `_build_messages`. None = no quote, the overwhelming case.
        self.reply_ctx: "_quotes.QuoteRef | None" = None
        # HIS TRAILING UNANSWERED RUN — the timestamps of every inbound since her own
        # last real outbound, oldest→newest. `messages` carries direction and body and
        # nothing else, so until this existed the engine could see THAT he wrote twice
        # but never WHEN, and "he double-texted five minutes apart" was not a fact the
        # code could express. `_gather` already parses `created_at` per row for her own
        # bubble window, so this costs one append and no I/O.
        #
        # Cleared on her outbound — but NOT on a broadcast, exactly like `last_dir`
        # below: a blast fired over his messages did not answer him, and letting it
        # clear the run would silently disqualify every fan an account mass-messages.
        self.in_run: list[datetime] = []
        self.last_in_at: datetime | None = None
        # ANY outbound (human or bot) — the rhythm sampler's "how long has she been
        # gone" clock, which is what a cover line ("sorry babe was in the shower")
        # apologises for. Distinct from last_human_out_at, which only tracks the
        # manual sends the cautious-resume guard yields to.
        self.last_out_at: datetime | None = None
        self.last_human_out_at: datetime | None = None
        self.session_out_n = 0   # HER OWN replies in the CURRENT burst (item 21
                                 # cap counter) — REPLIES, not bubbles; only
                                 # populated when _gather is called with
                                 # session_gap_min > 0
        self.day_out_n = 0       # HER OWN replies in the CURRENT quota day (item 21c
                                 # daily quota; see _leash.quota_used) — same
                                 # REPLIES-not-bubbles unit as session_out_n; only
                                 # populated when _gather is called with day_window set
        self.day_out_n_at_stop = 0  # …the same count as of HER LAST REPLY: the ration
                                 # that put her on a backoff rung. Unlike day_out_n it
                                 # cannot move while she is quiet, so it decides the
                                 # WAIT while day_out_n decides the next ALLOWANCE
        self.total_out_n = 0     # HER replies over the WHOLE thread (item 21c's
                                 # per-fan runway) — same REPLIES-not-bubbles unit,
                                 # never reset; only populated with day_window set
        self.first_at: datetime | None = None    # oldest row on the thread — the dry
                                                 # -streak anchor for a fan who has
                                                 # never paid anything
        self.her_last_at: datetime | None = None # HER last reply (not a human's, not a
                                                 # bubble) — the clock the daily-quota
                                                 # backoff counts its silence from

    def add_message(self, direction: str, text: str, message_id: int) -> None:
        """Append one thread row. The ONE writer of both `messages` and `msg_ids`, so
        a future `continue` in _gather cannot desync them behind our back."""
        self.messages.append((direction, text))
        self.msg_ids.append(int(message_id))


# An untagged outbound sent verbatim to at least this many DIFFERENT fans is a
# broadcast, not somebody talking to one man. Five is deliberately low: a person
# writing to one fan does not send the same sentence to five, and the cost of
# being wrong is only that the engine keeps chatting.
_BROADCAST_MIN_FANS = 5


def _broadcast_bodies(rows) -> frozenset[str]:
    """Untagged outbound bodies this account sent to MANY fans — OF's own
    auto-welcome, and mass blasts fired from the OF app.

    We infer the sender below from a missing tag: no `automation_kind` ⇒ a human
    ⇒ `resume_after_manual_hours` stands the engine down on that fan. That is
    wrong for anything OF sends on the creator's behalf, and the native welcome
    (`replyOnSubscribe`) is the worst case — OF delivers it one-by-one, but it is
    ALWAYS the same text, so EVERY new subscriber arrived pre-muted for an hour
    and nobody answered his first message. The hottest moment he will ever have,
    spent on hold. Measured 07-26: 4,054 broadcast messages since April against
    2,380 genuine 1:1 human sends, and one account had 126 new subs welcomed by
    the same stored OF line since 16 April.

    A blast is the same case by the user's call: after she mass-messages 99 men
    from the OF app the engine keeps chatting, rather than going silent on all 99.

    Matched on BODY, not OF's `queueId` (which would be exact — the welcome is one
    reused queue entry). `_gather` deliberately never loads `raw_json`: it is 64KB
    a row and this is a whole-account scan. Body is already in hand, so this costs
    one pass over rows we have and needs no schema change.

    Empty bodies are skipped — a sticker send carries no text, and grouping them
    would fold every gif on the account into one 'broadcast'.

    SWEEP-ONLY, deliberately. `_gather` is also called fan-scoped (W7 inbound
    dispatch, the status endpoint, post_buy/aftercare); `rows` then holds ONE fan,
    so `_BROADCAST_MIN_FANS` is unreachable and this returns empty. The account-wide
    aggregate that would fix it (GROUP BY body HAVING COUNT(DISTINCT fan_id) >= 5)
    measured 615ms on prod's biggest account — far too much for a path that runs on
    every inbound DM, and this file already learned what blocking the relay costs.
    The gap is bounded and cheap to live with: a blast sent through US carries a
    mass_run_id and is caught on EVERY path, so the ~4/day turn-theft this guards is
    fully covered fan-scoped too. Only an untagged OF-APP blast is missed there, and
    the 30s full-account sweep picks that fan up on the next tick."""
    fans_per_body: dict[str, set[int]] = {}
    for row in rows:
        fan_id, direction, body, _created, automation_kind, mass_run_id = row[:6]
        if direction != "out" or automation_kind is not None or mass_run_id is not None:
            continue
        if not (body or "").strip():
            continue
        fans_per_body.setdefault(body, set()).add(int(fan_id))
    return frozenset(b for b, fans in fans_per_body.items()
                     if len(fans) >= _BROADCAST_MIN_FANS)


async def _gather(account_id: str,
                  fan_ids: set[int] | None = None,
                  *, session_gap_min: int = 0,
                  day_window: timedelta | None = None) -> dict[int, _Cand]:
    """One pass over the account's messages → per-fan history PLUS the two
    timestamps the gates need: when the fan last spoke (SLA age) and when a
    HUMAN last sent (manual outbound = automation_kind IS NULL and not part of
    a mass run — automations always tag automation_kind).

    When `fan_ids` is given (W7 fan-scoped dispatch), the scan is restricted to
    those fans IN SQL so reacting to one inbound DM never reads the whole
    account's message history. None/empty → the full-account sweep.

    It also times every reply of HERS, and from that one measurement derives all three
    reply counters in a single post-pass — they differ only in the window they apply:

      `session_out_n`  (`session_gap_min > 0`)  her replies in the CURRENT burst: the
                       tail since her own last silence longer than `session_gap_min`.
      `day_out_n`      (`day_window` set)       her replies in the current quota day,
                       which opens on a reply of hers and closes `day_window` later
                       or on her silence — the ceiling above the burst (item 21c).
      `total_out_n`    (always)                 her replies over the whole thread —
                       the per-fan runway the daily ceiling waits out.

    The two window rules are deliberately different and each is load-bearing; both are
    spelled out at the post-pass, which is the one place they can be compared.

    Only `automation_kind in _OUR_KINDS` counts — her chat turns AND her offer turns
    (`ai_upseller`), which are the same engine wearing a different label and must burn
    the same budget. A human chatter's messages and other
    automations' sends (autoreply, welcome, …) must NOT burn her burst budget: the
    caps mean "how many times has SHE replied in this burst", and a teammate typing
    7 messages would otherwise blow the no_signal cap (5) and silence her on a fan
    who is owed an answer. Human presence already has its own purpose-built guards
    (`resume_after_manual_hours` + the per-send manual yield) — counting a human's
    messages here too was double jeopardy, and it stalled live threads.

    And it counts REPLIES, not message ROWS. The humanizer splits one reply into 3-5
    bubbles, each its own row, so counting rows made a single reply cost 5 units of a
    cap set to 5 (no_signal) or 10 (baseline) — she went mute after one or two real
    answers and the cadence gate skipped 1202 times a day against 184 replies sent.
    Consecutive rows of HERS within `_BUBBLE_WINDOW` are the same reply; anything
    else (his message, a human's, a gap) closes her turn."""
    out: dict[int, _Cand] = {}
    gap = timedelta(minutes=session_gap_min) if session_gap_min > 0 else None
    # WHEN each of her replies landed, per fan — the single measurement all three
    # counters are derived from after the scan. The loop's only job is to decide
    # "is this row a new reply of hers"; how the replies are then WINDOWED is a
    # question about windows, not about rows, and it belongs in one place below.
    # (Cheap: this is strictly smaller than `c.messages`, which keeps every row on
    # the thread anyway.)
    her_times: dict[int, list[datetime]] = {}
    # The burst clock runs on HER OWN REPLIES, not on the thread. Measured on the thread,
    # a fan who keeps typing holds the burst open forever: she hits the cap, goes quiet,
    # and every message he sends RE-ARMS the very gap that would have freed her — so the
    # counter can never reset and she is gagged on that fan permanently. The cap is meant
    # to stop her pestering a man who went quiet; it must never become a life sentence
    # handed down by a man who didn't. Him talking is fine — she just doesn't answer, and
    # the hour still runs.
    her_last: dict[int, datetime] = {}
    # True while the row just seen was one of HER bubbles — so the next row can tell
    # "another bubble of the same reply" from "a new reply".
    mid_reply: dict[int, bool] = {}
    where = [Message.account_id == str(account_id), Message.is_unsent.is_(False)]
    if fan_ids:
        where.append(Message.fan_id.in_(fan_ids))
    # created_at_text() + parse_ts() instead of the mapped DateTime column: this is
    # a ONE-QUERY scan of the whole account, and on 2026-07-22 a single row whose
    # created_at was '' made SQLAlchemy raise while materialising the rows, which
    # killed every reply for that account (see db/models.py for the full story).
    # Read as text, parse defensively, drop the bad row — never the account.
    async with get_session() as s:
        rows = (await s.execute(
            # message_id goes LAST and must stay there: `_broadcast_bodies` reads
            # `row[:6]` off these same rows, so anything inserted mid-list silently
            # feeds it the wrong columns. It is an int — still no `raw_json` on this
            # whole-account scan (see the docstring).
            select(Message.fan_id, Message.direction, Message.body,
                   created_at_text(), Message.automation_kind, Message.mass_run_id,
                   Message.image_desc, Message.media_count, Message.message_id)
            .where(*where)
            .order_by(Message.fan_id, Message.created_at, Message.message_id)
        )).all()
    bad_ts: list[int] = []   # fans that own a row with an unreadable created_at
    broadcast_bodies = _broadcast_bodies(rows)
    for (fan_id, direction, body, created_at_raw, automation_kind, mass_run_id,
         image_desc, media_count, message_id) in rows:
        created_at = parse_ts(created_at_raw)
        if created_at is None and created_at_raw is not None:
            # Unreadable, not absent: the row can't be placed on the thread's
            # timeline (its ORDER BY position is already meaningless), so it is
            # dropped from history AND from the gate clocks. A NULL created_at is
            # left alone — the guards below already cope with that.
            bad_ts.append(int(fan_id))
            continue
        c = out.get(fan_id)
        if c is None:
            c = out[fan_id] = _Cand(int(fan_id))
        # Rows arrive oldest→newest per fan, so the first one we see IS the thread's
        # start — the dry-streak anchor for a fan who has never put money down.
        if c.first_at is None and created_at is not None:
            c.first_at = created_at
        text = _history_text(direction, body, image_desc)
        c.add_message(direction, text, message_id)
        # Did WE broadcast this row? Read ONCE — the turn gate here, `hers` below and
        # the human clock all key off it, and each used to re-derive its own answer
        # from the same three columns. `mass_run_id` alone is NOT the question: a
        # blast fired from the OF app is untagged, and `broadcast_bodies` is the only
        # thing that catches it.
        broadcast = (mass_run_id is not None
                     or (automation_kind is None and body in broadcast_bodies))
        # A broadcast does not take the turn. It stays in `messages` — she must know
        # what she just sent him — but it must not move `last_dir`/`last_body`. A
        # ppv_send / funnel blast landing on top of his message made the turn gate
        # below read "we spoke last", and the reply he was owed was dropped: measured
        # ~4/day across the roster over 14 days, on fans who had JUST messaged. Her
        # own 1:1 sends (ai_chatter / ai_upseller) never carry a mass_run_id, so a
        # real reply of hers still closes the turn and she cannot talk over her own
        # open offer. `last_body` moves with `last_dir` or the reply path's intent
        # detectors (content-ask / escalation / decline / haggle) would be reading her
        # own PPV caption instead of his message.
        if not broadcast:
            c.last_dir = direction
            c.last_body = text
        if direction == "in":
            c.fan_msg_n += 1
            c.last_in_at = created_at
            if created_at is not None:
                c.in_run.append(created_at)
            c.last_in_text = _strip_html(body)
            if c.first_in_at is None and created_at is not None:
                c.first_in_at = created_at
            # Assigned, not OR-ed: the tier means "he JUST sent a picture", so a
            # photo three days and forty messages ago must not still read as a
            # live buying signal. Each inbound overwrites the last.
            c.pic_sent = int(media_count or 0) > 0
            # STICKY WITHIN THE BURST, cleared when SHE answers (in the outbound branch
            # below) — deliberately NOT the same rule as pic_sent.
            #
            # A man does not send a picture and then shut up: he sends the photo, then
            # "well?" on the next row. The prod thread this beat was built from is
            # exactly that shape. Under pic_sent's assigned-not-OR-ed rule the text row
            # wiped the description a second later and the rating never fired — the beat
            # would have been dead on its own founding incident, and only ever worked in
            # the replay because the harness runs one message per turn.
            #
            # The staleness pic_sent guards against is "a photo from Tuesday". Her own
            # reply is the honest boundary for that: once she has answered, the picture
            # has been dealt with. So it survives consecutive inbounds and dies on her
            # outbound, and rate_pic keys off THIS rather than pic_sent.
            if c.pic_sent:
                c.last_in_desc = str(image_desc or "")
                c.last_in_desc_at = created_at
            mid_reply[fan_id] = False        # he spoke → her next row is a new reply
        else:
            c.last_out_at = created_at
            # Anything that was not a broadcast ANSWERED him, so his run of unanswered
            # messages ends here. Deliberately wider than `hers` below: a human chatter
            # typing by hand closes the run too — she answered him, and a persistence
            # exit that fired anyway would be reacting to a silence that never happened.
            if not broadcast:
                c.in_run.clear()
            hers = not broadcast and automation_kind in _OUR_KINDS
            # The picture is "dealt with" only once SHE HAS ANSWERED IT IN WORDS.
            #
            # ⚠️ Clearing on ANY outbound reads right and is wrong. `image_reply` fires
            # on his picture within seconds — 11.9s and 36.2s on the incident thread —
            # and sends a MEDIA reaction, not an answer. An any-outbound rule therefore
            # wipes the description before ai_chatter ever runs, and the rating never
            # fires on the exact shape the beat exists for. Every unit test still
            # passed, because none of them seed an image_reply row; only the sequential
            # replay against real prod history caught it. Her chat and offer turns
            # (`_OUR_KINDS`) are the only sends that close a picture.
            if hers:
                c.last_in_desc = ""
                c.last_in_desc_at = None
            # Was her latest outbound a GIF ON ITS OWN? Empty text + no media is the
            # solo-sticker wire shape (a text reply has a body, a media/PPV send has
            # media_count>0), so the fact is readable from the columns this scan
            # ALREADY selects — no raw_json, no extra query on the hot path.
            # Assigned, not OR-ed: it means "her LAST send was a gif", so a gif forty
            # messages ago must not still be holding the rest open.
            #
            # Gated on `hers`, and that gate is what makes it exact rather than a
            # guess. Only the chat engines send a bare gif; the empty-body rows from
            # everything else (image_reply with no media, tip_reward, untagged) are
            # not gifs and used to arm the beat by mistake. Within `hers` there is no
            # ambiguity: a text reply carries a body and a PPV carries media, so
            # neither can look like this. (The ~20% of matching rows whose raw_json
            # lacks a giphyId are not counter-examples — write_outbound_attribution
            # stores raw_json=None and the WS pump stores the frame, for the SAME
            # message_id, so that split is a race between two writers of one message.)
            c.last_out_was_gif = (hers and not (body or "").strip()
                                  and int(media_count or 0) == 0)
            if automation_kind is None and not broadcast:
                c.last_human_out_at = created_at
            if hers and created_at is not None:
                # Another bubble of the reply we already counted, or a new reply?
                # Bubbles land seconds apart; a genuine second reply does not. (A row
                # that crosses the session gap can never be a bubble — the smallest
                # `session_gap_minutes` the validator accepts is 5, well past the
                # 2-minute bubble window — so the burst reset needs no say here.)
                prev_her = her_last.get(fan_id)
                same_reply = (mid_reply.get(fan_id) and prev_her is not None
                              and created_at - prev_her <= _BUBBLE_WINDOW)
                if not same_reply:
                    her_times.setdefault(fan_id, []).append(created_at)
                # Her last ROW, bubbles included — when she last actually spoke.
                her_last[fan_id] = created_at
                c.her_last_at = created_at
            mid_reply[fan_id] = hers         # a human's row also closes her turn

    # ONE line per run, not one per row: the point is to make the corrupt rows
    # findable and fixable, not to flood the relay log if a whole account is dirty.
    if bad_ts:
        log.warning("ai_chatter[%s]: skipped %d message row(s) with an unreadable "
                    "created_at (fans %s) — repair the rows; one bad cell must "
                    "never silence an account again",
                    account_id, len(bad_ts), sorted(set(bad_ts))[:10])

    # ── One measurement, three windows. Each counter is just "how many of her replies
    # fall inside MY window", and each window is defined once, here, where the two
    # deliberately different rules can be read side by side.
    now = datetime.utcnow()
    for fid, c in out.items():
        times = her_times.get(fid, ())
        c.total_out_n = len(times)          # the whole thread: his per-fan runway

        if gap is not None:
            # THE BURST — her replies since her own last silence longer than `gap`,
            # counted back from the end. Only HER silence clears it: measured on the
            # thread, a fan who keeps typing re-arms the very gap that would free her,
            # so the counter could never reset and she'd be gagged on him permanently.
            #
            # And it goes stale on the CLOCK too, not only when a new reply arrives —
            # otherwise it can never reopen for the fan it matters most for. She is
            # capped, so she sends nothing, so no row of hers ever comes along to
            # notice the hour has passed. An hour after her last reply she is free
            # again whether or not he kept talking through it. Measured from her last
            # ROW, not the reply's first bubble: her silence starts when she stopped
            # typing, so a 4-bubble answer must not age from its opening line.
            her_end = her_last.get(fid)
            if her_end is None or now - her_end > gap:
                c.session_out_n = 0
            else:
                n = 1
                for i in range(len(times) - 1, 0, -1):
                    if times[i] - times[i - 1] > gap:
                        break
                    n += 1
                c.session_out_n = n

        if day_window is not None:
            # THE DAY — her replies in the current quota day, which TUMBLES: it opens
            # on a reply of hers and is over only once BOTH `day_window` has run and
            # she has been quiet for QUOTA_IDLE_RESET. Why not either sliding window
            # (both are the obvious answer and both are wrong) is argued in
            # `quota_used`, which owns the rule so the bot and the drawer cannot each
            # keep their own.
            #
            # Asked TWICE, at two instants, because the ceiling asks two things: what
            # today's ration has left (`now`), and what she had spent when she stopped
            # talking (`times[-1]`) — the count that put her on a backoff rung. The
            # second cannot move while she is silent, so a day turning over mid-rung
            # hands back her allowance without cutting her wait short.
            c.day_out_n = quota_used(times, now, window=day_window)
            c.day_out_n_at_stop = (quota_used(times, times[-1], window=day_window)
                                   if times else 0)
    return out


async def _load_stop_lists(account_id: str) -> tuple[set[int], dict[int, str]]:
    """(blacklist fan_ids [global], {fan_id: reason} skip_list [this account]).
    Reasons matter here: graduation skips are ignored, the rest respected."""
    async with get_session() as s:
        bl = (await s.execute(select(Blacklist.fan_id))).all()
        sk = (await s.execute(
            select(SkipList.fan_id, SkipList.reason)
            .where(SkipList.account_id == str(account_id))
        )).all()
    return ({int(r[0]) for r in bl},
            {int(r[0]): str(r[1] or "") for r in sk})


# ── Catalog + offers (M3): the LLM proposes, this code disposes ──────────────

# Tip transaction kinds (mirror tip_reward / the attribution view).

# The offer marker protocol: the model writes its pitch as normal bubbles and
# ends the reply with a line that is exactly ">>OFFER <catalog item id>". The
# marker is ALWAYS stripped before sending (even malformed ones), and an offer
# only happens when the id survives every code-side guardrail.
#
# Two ways to be a marker, because "offer" is also an ordinary English word she
# uses — 636 outbound bodies contain it and exactly ONE was a marker:
#   • the ARROWS, in any case — unambiguous, and the shape the prompt asks for;
#   • no arrows, but a bare id that ENDS the line. This is the leak the audit
#     caught: `i dare ya 😏, OFFER 20` reached a fan on 07-14, because the old
#     regexes both required the marker to start its own line. Anchoring the id
#     to end-of-line is what keeps "well i usually offer 1k per custom vid but
#     idkkk" — a real outbound body — from reading as offer #1.
_OFFER_RE = protocol_marker_re(r">+[ \t]*OFFER|OFFER(?=[ \t]+\d+[ \t]*$)")

# Anti-hallucination floor: on a catalog-bearing account, a bubble that names a
# price — or numeric content specifics ("15 pics", "2 min vid") — WITHOUT a
# validated offer behind it is a lie risk (observed live: the model invented a
# "$20 shower steam set, 15 pics" once the real list ran dry). Such bubbles are
# stripped before sending — the offer row is the only thing that may put terms
# or specifics in front of a fan.
_PRICE_TALK_RE = re.compile(r"\$\s*\d")
_SPECIFICS_RE = re.compile(
    r"\b\d+\s*(pics?|photos?|vids?|videos?|clips?|sets?|min(?:ute)?s?|sec(?:ond)?s?)\b",
    re.IGNORECASE)
# Delivery narration — "sending it now", "check your dms", "i already sent it",
# "unlocking it now", "on its way". If NO media actually attaches this turn (no
# priced offer, no teaser) AND there's no unpaid PPV already on the table to point
# him at, a bubble like this is a PHANTOM send: she promises content and delivers
# nothing (a broken promise the fan can see, and a bot tell). Stripped the same way
# unbacked price talk is. When a PPV IS pending, "go unlock it babe" is literally
# true, so these are kept (see the _phantom guard at the call site).
_DELIVERY_TALK_RE = re.compile(
    r"(?:send(?:ing|in)?\s+(?:it|that|this|them|u|you|ya)?\s*(?:now|over|rn|your\s+way)"
    r"|(?:i(?:'?ll)?\s+)?send\s+(?:it|that|this)\s+now"
    r"|unlock(?:ing|in)?\s+(?:it|that|this)?\s*now"
    r"|(?:already|just|i)\s+sent\s+(?:it|that|them|you|ya)?"
    r"|check\s+(?:your|ur)\s+(?:dms?|messages?|msgs?|inbox|notifs?|notifications?)"
    r"|go\s+check"
    r"|in\s+(?:your|ur)\s+(?:dms?|inbox)"
    r"|on\s+(?:its?|it'?s|the)\s+way)",
    re.IGNORECASE)


_GENERIC_NAMES = {"babe", "baby", "hey", "hun", "honey", "boo", "love", "daddy"}


def _merge_lone_name_bubbles(parts: list[str], name: str) -> list[str]:
    """A bubble that is JUST his name — "jack", "jack?", "jack 😏" — reads as robotic
    filler as its own message. Fold it into the neighbouring bubble so the name still
    lands appended ("...huh jack?") but never ships alone. Generic pet names are left
    be (they're fine standalone)."""
    _toks = (name or "").strip().split()
    nm = _toks[0].lower() if _toks else ""
    if len(nm) < 2 or nm in _GENERIC_NAMES or len(parts) < 2:
        return parts
    lone = re.compile(rf"^{re.escape(nm)}[^\w]*$", re.IGNORECASE)
    out: list[str] = []
    for p in parts:
        if out and lone.match(p.strip()):
            out[-1] = f"{out[-1]} {p.strip()}"      # append the name to the prior bubble
        else:
            out.append(p)
    if len(out) >= 2 and lone.match(out[0].strip()):  # a lead lone-name → fold forward
        out[1] = f"{out[0].strip()} {out[1]}"
        out = out[1:]
    return out


def _unbacked_talk(p: str) -> bool:
    return bool(_PRICE_TALK_RE.search(p) or _SPECIFICS_RE.search(p))

# Watcher reaction when an unlock lands while the fan isn't mid-conversation —
# static pool (no LLM dependency in the watcher); the bot reacts in full voice
# the next time the fan actually speaks.
_UNLOCK_REACTIONS = _voice.UNLOCK_REACTIONS[_voice.VOICE_HER]
# Localized watcher reactions. 'en' is the fallback; add sl/pt/fr/de/it the same way.
_UNLOCK_REACTIONS_BY_LANG: dict[str, tuple[str, ...]] = {
    "en": _UNLOCK_REACTIONS,
    "es": (
        "omg disfrútalo bebé 😘",
        "mmm disfruta 🙈 dime después",
    ),
    "sl": (  # female speaker → male fan
        "omg uživaj srček 😘",
        "mmm uživaj 🙈 povej mi potem",
    ),
}
# The MALE table. Only `en` is written, and the fallback below is deliberately to
# HIS english rather than to the table above: every localized line there is
# female-voiced by construction — "bebé", "srček", and the `sl` block says so in
# its own comment — so a Spanish male account sending her Spanish is a worse
# outcome than sending his English. Wrong language beats wrong person. Adding
# "es"/"sl" for him is the same one-line edit as the table above.
_UNLOCK_REACTIONS_BY_LANG_HIM: dict[str, tuple[str, ...]] = {
    "en": _voice.UNLOCK_REACTIONS[_voice.VOICE_HIM],
}

# Fast-path window: only re-read a chat from OF (isOpened) when the fan was
# active this recently — keeps the per-tick OF load at ~one read per HOT offer.
_FASTPATH_ACTIVE_WINDOW_MIN = 30
_FASTPATH_READ_LIMIT = 20

# Deterministic content-ask detector: when the fan is explicitly asking for
# content AND the manifest is live, the info-gather goal must yield to the
# pitch — otherwise the model keeps interviewing ("what got u into trail
# running?") while he's begging to buy. Code-side, not model judgment. Hoisted
# into _common (CONTENT_ASK_RE) so of_ai_chat/autoreply share the same detector
# for their tip-ask branch; kept under the old name here (and re-exported to
# scripts_api) for back-compat.
_CONTENT_ASK_RE = CONTENT_ASK_RE

# spec §6.4 — bot accusation. TODO: switch to _common.detect_bot_accusation() once a
# sibling agent lands it (built from the 74 real accusations, excluding AI-*photo*
# complaints). Until then this minimal inline regex covers the "are you a bot / real
# person / AI / chatbot / talking to a script" family. It deliberately does NOT match
# "is this photo AI" (a content complaint, not a fiction-has-failed signal): it
# requires the accusation to be aimed at HER/YOU, not at an image.
try:  # prefer the shared, corpus-built detector the moment it exists
    from ._common import detect_bot_accusation as _detect_bot_accusation  # type: ignore
except Exception:  # pragma: no cover - fallback until the sibling lands it
    _BOT_ACCUSED_RE = re.compile(
        r"\b(are|is|r|ru|u)\s*(you|u|this|ur|your?)?\s*(a\s*)?"
        r"(bot|robot|ai|a\.i\.|chat\s*bot|chatbot|script|fake|real (?:person|human)|"
        r"even (?:real|human|a real person))\b"
        r"|\b(you'?re|youre|ur|this is)\s+(a\s+)?(bot|ai|robot|chatbot|fake|scripted)\b"
        r"|\b(talking to|chatting with)\s+(a\s+)?(bot|ai|robot|machine|script)\b"
        r"|\bnot (?:a )?(?:real|human)\b", re.I)

    def _detect_bot_accusation(text: str | None) -> bool:
        return bool(text) and bool(_BOT_ACCUSED_RE.search(text))


# Media parsing + the hero/filler split live in ownership.py (the one home
# for owned-media semantics); these are the item-shaped entry points.
_item_media = ownership.item_media
_item_previews = ownership.item_previews


async def _hero_media_map(account_id: str,
                          items: list[CatalogItem]) -> dict[int, list[int]]:
    """CatalogItem adapter over `ownership.hero_media_map` (see it for the
    operator's hero/filler ruling). Total: every input item gets a non-empty
    entry — call sites index it directly."""
    return await ownership.hero_media_map(
        account_id,
        {int(it.id): (_item_media(it), _item_previews(it)) for it in items})


async def _drop_owned(account_id: str, fan_id: int,
                      offerable: dict[int, CatalogItem]) -> None:
    """Pop every item whose HERO media this fan already owns. Media-keyed —
    a fan who bought a clip in a MASS blast has no content_offers row, so a
    catalog-keyed check would cheerfully re-sell him what he already owns;
    hero-only so a shared free-preview frame never kills a sellable item.
    Same BOUGHT-only ∩ HERO rule `_offerable_for_fan` applies, re-run here
    for freshness before each rung. Mutates `offerable` in place."""
    if not offerable:
        return
    hero = await _hero_media_map(account_id, list(offerable.values()))
    owned = await _owned_or_seen_media(account_id, fan_id)
    for iid in list(offerable):
        if owned & set(hero[int(iid)]):
            offerable.pop(iid, None)


def _effective_mode(item: CatalogItem, cfg_mode: str) -> str | None:
    """Intersect the account's offer_mode with the item's terms. None = not
    sellable (a free teaser is deliverable regardless — see is_free_teaser)."""
    tip_ok = int(item.tip_unlock_cents or 0) > 0 and cfg_mode in ("tip", "both")
    # Default-ppv world: an item configured tip-only (tip_unlock set, price 0)
    # must stay sellable on a ppv-mode account — its tip_unlock amount stands in
    # as the price floor; smart pricing owns the actual quote either way.
    ppv_ok = (int(item.price_cents or 0) > 0
              or int(item.tip_unlock_cents or 0) > 0) and cfg_mode in ("ppv", "both")
    if tip_ok and ppv_ok:
        return "both"
    if tip_ok:
        return "tip"
    if ppv_ok:
        return "ppv"
    return None


def _terms_str(item: CatalogItem, mode: str | None,
               quoted_cents: int | None = None) -> str:
    """`quoted_cents` (smart pricing) REPLACES the catalog's static price for this
    fan. It has to reach the prompt, not just the send: a manifest that says $12
    while the attach charges $19 puts the model's own pitch text at odds with the
    locked box the fan is looking at."""
    if item.is_free_teaser:
        return "FREE — just send it when the moment fits"
    tip_c = int(quoted_cents or item.tip_unlock_cents or 0)
    ppv_c = int(quoted_cents or item.price_cents or 0)
    tip = f"tip ${tip_c // 100}"
    ppv = f"${ppv_c // 100} to unlock"
    return {"both": f"{tip} or {ppv}", "tip": tip, "ppv": ppv}.get(mode or "", "")


async def _load_catalog(account_id: str) -> tuple[dict[int, CatalogScript], list[CatalogItem]]:
    """Enabled scripts + enabled, deliverable items (must have media bound).
    Detached rows — read-only reference data for the whole run."""
    async with get_session() as s:
        scripts = {int(sc.id): sc for sc in (await s.execute(
            select(CatalogScript).where(CatalogScript.account_id == str(account_id),
                                        CatalogScript.status == "enabled")
        )).scalars().all()}
        items = (await s.execute(
            select(CatalogItem).where(CatalogItem.account_id == str(account_id),
                                      CatalogItem.enabled.is_(True))
        )).scalars().all()
        s.expunge_all()
    usable = [it for it in items
              if (it.script_id is None or int(it.script_id) in scripts)
              and _item_media(it)]
    return scripts, usable


async def _seen_media(account_id: str, fan_id: int) -> set[int]:
    async with get_session() as s:
        ids = (await s.execute(
            select(VaultSend.media_id).where(
                VaultSend.account_id == str(account_id),
                VaultSend.fan_id == int(fan_id))
        )).scalars().all()
    return {int(x) for x in ids}


# The fan → owned-media reader lives in ownership.py beside its inverse
# (`owners_of_media`); this alias keeps the module-local name every call
# site and test uses.
_owned_or_seen_media = ownership.owned_or_seen_media


async def _open_offers(account_id: str, fan_id: int | None = None) -> list[ContentOffer]:
    async with get_session() as s:
        q = select(ContentOffer).where(ContentOffer.account_id == str(account_id),
                                       ContentOffer.status == "open")
        if fan_id is not None:
            q = q.where(ContentOffer.fan_id == int(fan_id))
        rows = (await s.execute(q.order_by(ContentOffer.id))).scalars().all()
        s.expunge_all()
    return rows


async def _offerable_for_fan(account_id: str, fan_id: int, cfg_mode: str,
                             scripts: dict[int, CatalogScript],
                             items: list[CatalogItem]) -> dict[int, CatalogItem]:
    """What this fan may be offered RIGHT NOW: unseen singles + the NEXT item of
    a script (position pinning preserves the escalation without a state
    machine). One active script at a time: once a progress row is active, other
    scripts' openers drop out of the manifest.

    Dedup is BOUGHT-or-SEEN only (see `_owned_or_seen_media`): a piece he merely
    received as a free teaser/preview is NOT filtered out — it can be re-offered as
    part of a real PPV. And it blocks on HERO media only (`_hero_media_map`):
    owning an item's free-preview tease frames never kills the item — the
    operator's 07-23 ruling is that filler may repeat; only the payoff (videos +
    non-preview images) must be media the fan was never sold."""
    # No catalogue ⇒ nothing to filter, and the three per-fan reads below would
    # each go to the database to produce {}. This lives HERE rather than in the
    # caller's branch condition on purpose: `run()` deliberately has no catalogue
    # precondition on selling (a custom needs no rows), so the emptiness check
    # belongs to the function that owns "what may this fan be offered from this
    # list" — where it also covers the other two call sites.
    if not items:
        return {}
    seen = await _owned_or_seen_media(account_id, fan_id)
    hero = await _hero_media_map(account_id, items)
    async with get_session() as s:
        prog_rows = (await s.execute(
            select(CatalogProgress).where(
                CatalogProgress.account_id == str(account_id),
                CatalogProgress.fan_id == int(fan_id))
        )).scalars().all()
        s.expunge_all()
    progress = {int(p.script_id): p for p in prog_rows}
    pinned = [p for p in prog_rows if p.status in ("active", "stalled")]

    out: dict[int, CatalogItem] = {}
    for it in items:
        if it.script_id is not None:
            p = progress.get(int(it.script_id))
            if p is not None and p.status == "done":
                continue
            if pinned and (p is None or p.status not in ("active", "stalled")):
                continue  # pinned to the script(s) already in progress
            pos = int(p.position) if p is not None else 0
            if int(it.position or 0) != pos:
                continue
        if any(m in seen for m in hero[int(it.id)]):
            continue
        if not it.is_free_teaser and _effective_mode(it, cfg_mode) is None:
            continue
        out[int(it.id)] = it
    return out


async def _offer_caps_ok(account_id: str, fan_id: int, cfg: dict) -> bool:
    """Pacing guards: enough fan messages since the LAST offer, and not too many
    non-converted offers (expired/cancelled) inside 24h. Delivered offers never
    count against the cap — a purchase invites the next step."""
    min_msgs = int(cfg.get("min_fan_msgs_between_offers") or 0)
    max_day = int(cfg.get("max_offers_per_fan_per_day") or 0)
    now = datetime.utcnow()
    async with get_session() as s:
        last_at = (await s.execute(
            select(func.max(ContentOffer.offered_at)).where(
                ContentOffer.account_id == str(account_id),
                ContentOffer.fan_id == int(fan_id))
        )).scalar_one_or_none()
        if last_at is not None and min_msgs > 0:
            n = (await s.execute(
                select(func.count()).select_from(Message).where(
                    Message.account_id == str(account_id),
                    Message.fan_id == int(fan_id),
                    Message.direction == "in",
                    Message.created_at > last_at)
            )).scalar_one()
            if int(n or 0) < min_msgs:
                return False
        if max_day > 0:
            burned = (await s.execute(
                select(func.count()).select_from(ContentOffer).where(
                    ContentOffer.account_id == str(account_id),
                    ContentOffer.fan_id == int(fan_id),
                    ContentOffer.status.in_(("expired", "cancelled")),
                    ContentOffer.offered_at > now - timedelta(hours=24))
            )).scalar_one()
            if int(burned or 0) >= max_day:
                return False
    return True


@dataclasses.dataclass(frozen=True)
class _SellSurface:
    """What may be sold this turn, and HOW to close it.

    WHY THIS IS A VALUE AND NOT A STRING. Three separate directives in
    `_build_messages` tell the model to convert — he asked for content, he's
    leaning in, the thread is hot — and each of them used to spell out the
    mechanics itself: "pick the piece from WHAT YOU CAN SELL … end with the
    >>OFFER line". That is fine while there is exactly one way to close. The
    moment a second appeared (a bought-out account sells a CUSTOM: no piece, no
    id, no marker) every one of those directives needed to know which manifest
    it got, and the intro line and the output contract did too. Six sites, one
    fact, threaded through as a boolean.

    So the manifest owns the mechanics and the directives own the MOMENT. Adding
    a third sell surface is a third construction here, not a fifth branch there.

    `close` doubles as "may we pitch at all this turn": empty means there is
    something on screen but nothing to sell from it (`_pending_block` — he is
    already holding the maximum unpaid PPVs, so the prompt describes them and
    stops selling).

    `section` is the block's OWN heading, and it is a field because both the
    intro and the close point AT it from elsewhere in the prompt. Naming it once
    is what makes those pointers resolve: they used to say "see WHAT YOU CAN
    SELL below" / "pick the piece from WHAT YOU CAN SELL" while the section was
    actually headed "CONTENT YOU CAN ACTUALLY SEND HIM" — a reference to a
    heading that appeared nowhere in the rendered prompt, twice, on the lane
    that earns the money. `test_voice_lane` now asserts every surface's pointer
    is a substring of its own block, so a header rename cannot leave one
    dangling again."""
    block: str = ""       # the manifest text; "" ⇒ no sell section in the prompt
    section: str = ""     # the block's heading — what `intro`/`close` point at
    intro: str = ""       # the one-line pointer in the prompt's opening paragraph
    close: str = ""       # how to convert; "" ⇒ this turn does not pitch
    marker: bool = False  # may the reply end with `>>OFFER <id>`?

    @property
    def live(self) -> bool:
        """Is there a sell section at all (whether or not we may pitch from it)?"""
        return bool(self.block.strip())


_NO_SELL = _SellSurface()

# The section headings, verbatim as each block renders them. Every pointer below
# is built from these rather than retyping the name.
_SECTION_BY_ID = "CONTENT YOU CAN ACTUALLY SEND HIM"
_SECTION_CUSTOM = "WHAT YOU CAN OFFER HIM"
_SECTION_PENDING = "YOU ALREADY OFFERED HIM A PIECE"

# The two ways to close, and the only thing that differs between a catalogue
# account and a bought-out one. `_CLOSE_BY_ID` is the historical text, lifted
# out of the three directives that each carried their own copy of it.
_CLOSE_BY_ID = (f"Pick the piece from {_SECTION_BY_ID} that best fits, tease it "
                "from its description, give the terms, and end with the >>OFFER "
                "line.")
# Names no section on purpose: there is exactly one thing on offer and the block
# describes it, so there is no list to send the model back to.
_CLOSE_CUSTOM = ("Offer to record him a custom and name the price. Don't name or "
                 "promise a specific piece you haven't been handed, and write no "
                 ">>OFFER line — there is nothing on the list to attach.")

# The matching intro pointers (the prompt's opening paragraph, ~40 lines above
# the section itself). The bought-out one deliberately does NOT deny having
# pictures: the convo teaser ladder and tip_reward attach real vault media to
# these same replies, so "you have no filmed content" was false and the model
# was told to keep saying it.
_INTRO_BY_ID = ("you DO have real content you can send or sell when the moment "
                f"is right — see {_SECTION_BY_ID} below.")
_INTRO_CUSTOM = ("there's nothing on your sell list to pitch him right now, but "
                 f"you CAN record him a custom — see {_SECTION_CUSTOM} below.")
# The pending surface pointed at the catalogue's heading too, which on that
# branch names a section the prompt does not even contain — there is no
# manifest, only the unpaid piece he is already holding.
_INTRO_PENDING = ("he's still sitting on an offer you already made — see "
                  f"{_SECTION_PENDING} below, and don't pitch anything new.")


def _manifest_block(offerable: dict[int, CatalogItem],
                    scripts: dict[int, CatalogScript], cfg_mode: str,
                    quotes: dict[int, upsell.Quote] | None = None,
                    sell_customs: bool = False) -> _SellSurface:
    lines = []
    for iid, it in sorted(offerable.items()):
        mode = _effective_mode(it, cfg_mode)
        dur = ""
        if it.duration_sec:
            dur = f" {int(it.duration_sec) // 60}:{int(it.duration_sec) % 60:02d}"
        sc = scripts.get(int(it.script_id)) if it.script_id is not None else None
        theme = f" (part of your '{sc.name}' set: {(sc.theme or '')[:120]})" if sc else ""
        q = (quotes or {}).get(int(iid))
        lines.append(f"- [id {iid}] {it.kind}{dur} — {it.label or 'untitled'}: "
                     f"{(it.description_for_ai or '').strip()} — "
                     f"{_terms_str(it, mode, q.price_cents if q else None)}{theme}")
    # "never customs" is CONDITIONAL. The ban exists because the catalog is the
    # only thing that provably exists, so a promised custom was a promise with no
    # delivery owner. An account that has opted in (`_common.SELL_CUSTOMS_KEY`)
    # has an owner, and leaving the ban would have the engine refuse the product
    # the account sells — silently, discovered only by wondering why no custom
    # order ever landed.
    #
    # ⚠️ THE PERMISSION AND ITS FENCE SHIP TOGETHER, ALWAYS. Deleting two words
    # from the ban is NOT a carve-out: the surrounding sentence still says "NEVER
    # promise anything not on this list", so on its own it produces a contradiction
    # and no boundary at all. Worse, unlike the conversational engines this one does
    # NOT carry the live-proof guardrail at all, so there is no other rule anywhere
    # in this prompt telling the model what a custom may not be. The fence
    # therefore has to arrive with the permission, in this same block.
    #
    # It is `_voice.CUSTOMS_CONDITIONS` verbatim, NOT a restatement. This block used
    # to write its own and the two fell out of sync on the exact clause that matters
    # most: it still said "never promise a specific time or day" after the other two
    # surfaces had been tightened to ban durations too, so "give me an hour" was a
    # compliant reply here — on the one engine that actually closes the sale.
    _no_customs = "" if sell_customs else "never customs, "
    # ⚠️ THE ">>OFFER SENTENCE IS LOAD-BEARING AND WAS MISSING. This branch hands
    # the model TWO protocols — a catalogue with an >>OFFER id, and a permission to
    # sell a custom — and said nothing about how they interact. Live generation on
    # Ava (2026-08-05): asked for a voice note, the model wrote "$150 and it's all
    # yours babe" and appended `>>OFFER 236`. Item 236 on that account is a $200
    # VIDEO labelled "Custom / exclusive". The send path takes price and media from
    # the catalogue row (`ai_chatter.py` ~6287/6279, "the model never sets them"),
    # so the fan would have read $150 for a voice note and received a locked $200
    # video. Wrong price, wrong medium, from one missing sentence.
    #
    # It belongs HERE and not only in `_CLOSE_CUSTOM`: that close only renders on
    # the bought-out branch, where `offerable` is empty and a stray marker resolves
    # to nothing anyway. This branch is the one where it can actually bill someone.
    _customs_rule = ("\n- CUSTOMS (a to-order VOICE NOTE): your one off-list offer. "
                     f"{_voice.CUSTOMS_CONDITIONS}"
                     " No id — never an >>OFFER line for a custom, even if a listed "
                     "piece sounds custom."
                     if sell_customs else "")
    # ── The BOUGHT-OUT case: nothing left on the list, but a custom to sell ──
    # This is not an edge case for the male lane, it is the PREMISE — "the vault
    # is spent, the primary sale is a custom". The catalogue block cannot be
    # reused here: it opens "CONTENT YOU CAN ACTUALLY SEND HIM … NEVER invent
    # anything not on this list" over an EMPTY list, and then instructs the model
    # to "pick the best-fitting piece and tease it from its description". Handed
    # no pieces, that is an invitation to invent one — the exact failure the
    # header exists to prevent.
    #
    # So the customs-only variant states the true, NARROWER thing: nothing is on
    # the sell list to name and price, and the one thing offerable outright is
    # made to order.
    #
    # ⚠️ IT USED TO SAY "You have NO filmed content to send and you must never
    # claim otherwise" — AN OUTRIGHT FALSEHOOD ON A LIVE ACCOUNT. `catalog_items`
    # is one inventory among several: the convo teaser ladder and tip_reward both
    # attach REAL vault photos to these same replies, priced up to $200, and
    # neither goes through this table. So the engine was telling the model to deny
    # having pictures on the turn it was about to send him pictures — and the ban
    # was absolute ("never claim otherwise"), so the model would keep denying it
    # afterwards. The fence that was actually load-bearing is narrower and stays:
    # never NAME, PRICE, or PROMISE a specific piece it has not been handed.
    if not lines:
        if not sell_customs:
            return _NO_SELL      # nothing to sell and nothing to offer — say nothing
        return _SellSurface(section=_SECTION_CUSTOM, intro=_INTRO_CUSTOM,
                            close=_CLOSE_CUSTOM, marker=False, block=(
            f"{_SECTION_CUSTOM}: nothing is on your sell list right now — never "
            "name, price, or promise a piece you havent been handed. The ONE "
            f"offer is a paid CUSTOM made for him. {_voice.CUSTOMS_CONDITIONS}\n"
            "- Offer it only when the vibe is warm or he asks — never pushy. If "
            "he asks to see something, be plain: youll make him one, and the "
            "price. He TIPS for it (\"tip me and ill record it for you 😏\"), "
            "never the bare word \"tip\". Otherwise just talk to him."
        ))

    return _SellSurface(section=_SECTION_BY_ID, intro=_INTRO_BY_ID,
                        close=_CLOSE_BY_ID, marker=True, block=(
        f"{_SECTION_BY_ID} (these are real, already filmed — "
        f"NEVER invent or promise anything not on this list, {_no_customs}and "
        "describe a piece using ONLY its description):\n" + "\n".join(lines) + "\n\n"
        "SELLING RULES:\n"
        "- Pitch ONLY when the vibe is warm or he's asking for content — at most "
        "ONE piece, woven in naturally (\"tip me $10 and ill send it 😏\"), never "
        "pushy. When he asks or is turned on, dont stall or be coy: tease the "
        "best fit from its description and name the price NOW. A FREE piece is a "
        "spontaneous treat when he's sweet.\n"
        "- When (and ONLY when) you pitch/send a piece this message, end your "
        "reply with a final line that is exactly:\n"
        ">>OFFER <id>\n"
        "- No pitch → do NOT write >>OFFER, and never tease that you \"have "
        "something\" without actually pitching it.\n"
        "- The list above is COMPLETE and CURRENT: anything not on it is gone — "
        "never re-offer, re-price, or invent sets, lengths, counts, or prices. "
        "If he wants more, you're filming more soon — never promise specifics."
        f"{_customs_rule}"
    ))


def _pending_block(offer: ContentOffer, item: CatalogItem | None) -> _SellSurface:
    desc = (item.description_for_ai or "").strip() if item else ""
    label = (item.label if item else None) or "it"
    terms = []
    if offer.mode in ("tip", "both") and offer.tip_unlock_cents:
        terms.append(f"tip ${int(offer.tip_unlock_cents) // 100}")
    if offer.mode in ("ppv", "both") and offer.price_cents:
        terms.append(f"${int(offer.price_cents) // 100} unlock")
    accum = int(offer.tips_accum_cents or 0)
    accum_note = ""
    if (accum > 0 and offer.mode in ("tip", "both")
            and int(offer.tip_unlock_cents or 0) > accum):
        left = (int(offer.tip_unlock_cents) - accum + 99) // 100
        accum_note = (f"- He has already tipped ${accum // 100} toward it — when "
                      f"it fits, sweetly remind him it's only ${left} more.\n")
    # `close` is EMPTY on purpose: he already holds the maximum unpaid PPVs, so
    # this surface describes them and stops selling. That empty string is what
    # keeps the three pitch directives off this turn — they used to be held off
    # by a `bool(offerable)` test at each call site, which happened to be false
    # here for an unrelated reason (no manifest was built) rather than because
    # anyone decided it.
    #
    # `marker` is False for a reason visible in the last line below: this block
    # says "never write >>OFFER", while the output contract used to grant the
    # marker to ANY live sell section — so the prompt revoked on line 6 what it
    # permitted 40 lines later. Nothing downstream accepted such a marker anyway
    # (there is no manifest to back an id, so `unbacked_stripped` binned it); it
    # only ever cost a pitch.
    return _SellSurface(section=_SECTION_PENDING, intro=_INTRO_PENDING,
                        close="", marker=False, block=(
        f"{_SECTION_PENDING} and he hasn't unlocked it yet: "
        f"{label} — {desc} ({' or '.join(terms)}).\n"
        f"{accum_note}"
        "- Answer about it from that description; a playful re-tease ONCE in a "
        "while, but DON'T nag or repeat the price — mostly just chat like "
        "normal.\n"
        "- No other content offers while this is pending, and never write "
        ">>OFFER."
    ))


# The fan may end up with at most this many UNPAID offers open at once. Default 2:
# one already on the table + one "here's another / here's it cheaper" is a normal
# human close; a third unpaid PPV in a row is pushy spam and stops (→ _pending_block).
# Configurable per account via cfg["max_open_offers"] (floored at 2 — never below).
_MAX_OPEN_OFFERS = 2

# When he balks on price ("a lil lower?"), the re-tease of the pending piece is priced
# this much cheaper — a light, believable nudge, not a fire sale. cfg["haggle_discount_pct"].
_HAGGLE_DISCOUNT_PCT = 0.10

# If he balks on a priced TEASER he hasn't unlocked, we may RESEND that same media a bit
# cheaper (up to this much off). cfg["teaser_discount_pct"].
_TEASER_DISCOUNT_PCT = 0.20

# Price haggling — "too expensive", "cheaper", a lowball counter. When a fan balks
# like this on the pending piece, the SECOND offer may re-send it cheaper. Detection
# lives in _language.is_haggle (English + the fan-language packs — a Spanish fan's
# "porque subió tanto el precio?" must reach the same discount, not a higher rung).
# NOTE: "broke" / "can't afford" are deliberately NOT haggle — those are OUT-of-money
# signals that must hit the broke PAUSE (stop selling), not a cheaper re-offer.


async def _unlocked_since_open_offers(account_id: str, fan_id: int,
                                      open_offers: list[ContentOffer]) -> bool:
    """True when the fan has UNLOCKED something — a PPV opened (paid priced msg)
    OR a tip buy — since his OLDEST still-open offer was made.

    The max_open_offers cap counts our own ContentOffers only; tip_reward and
    hand-sent PPVs write none, so they never fill it (invariant kept). But when he
    buys ONE of those UNTRACKED pieces, no offer resolves and the cap stays full,
    freezing the closer on `_pending_block` — the exact "waiting for open won't let
    me price a new one" stall observed live. This says "he opened one",
    so the caller lifts ONE slot. tip buys count (that's the "still lift 1 after a
    tip buy" case). Scoped since the oldest open offer so a stale old purchase can't
    lift the cap — only a buy made while these asks were on the table."""
    since = min((o.offered_at for o in open_offers if o.offered_at), default=None)
    if since is None:
        return False
    async with get_session() as s:
        ppv = (await s.execute(
            select(Message.message_id).where(
                Message.account_id == str(account_id),
                Message.fan_id == int(fan_id),
                Message.direction == "out",
                Message.price_cents > 0,
                Message.is_paid.is_(True),
                Message.purchased_at.isnot(None),
                Message.purchased_at >= since).limit(1)
        )).first()
        if ppv is not None:
            return True
        tip = (await s.execute(
            select(Transaction.id).where(
                Transaction.account_id == str(account_id),
                Transaction.fan_id == int(fan_id),
                Transaction.kind.in_(_TIP_KINDS),
                Transaction.status.in_(("cleared", "pending")),
                Transaction.occurred_at >= since).limit(1)
        )).first()
    return tip is not None


async def _last_unpaid_teaser(account_id: str, fan_id: int) -> dict | None:
    """The most recent PRICED teaser we sent this fan that he has NOT unlocked, as
    {media_ids, price_cents, message_id} — or None. Lets a haggle ("cheaper?") resend
    the same media a little cheaper instead of stonewalling."""
    async with get_session() as s:
        latest = (await s.execute(
            select(VaultSend.message_id, VaultSend.price_cents).where(
                VaultSend.account_id == str(account_id),
                VaultSend.fan_id == int(fan_id),
                VaultSend.price_cents > 0,
                VaultSend.message_id.is_not(None))
            .order_by(VaultSend.sent_at.desc()).limit(1))).first()
        if not latest or latest.message_id is None:
            return None
        mid, price = int(latest.message_id), int(latest.price_cents or 0)
        paid = (await s.execute(
            select(Message.is_paid).where(
                Message.account_id == str(account_id),
                Message.message_id == mid))).scalar_one_or_none()
        if paid:                        # already unlocked → nothing to resell cheaper
            return None
        media = (await s.execute(
            select(VaultSend.media_id).where(
                VaultSend.account_id == str(account_id),
                VaultSend.fan_id == int(fan_id),
                VaultSend.message_id == mid))).scalars().all()
    media_ids = [int(m) for m in media]
    if not media_ids or price <= 0:
        return None
    return {"media_ids": media_ids, "price_cents": price, "message_id": mid}


def _second_offer_block(pending: ContentOffer, pend_item: CatalogItem | None,
                        offerable: dict[int, CatalogItem],
                        scripts: dict[int, CatalogScript], cfg_mode: str,
                        quotes: dict[int, upsell.Quote] | None,
                        sell_customs: bool = False) -> _SellSurface:
    """He has ONE unpaid PPV on the table and you may send a SECOND (and FINAL) one:
    a DIFFERENT piece, or the SAME piece re-priced lower if he's balking on price.
    After this second one there are two unpaid offers open, so the pitch stops.

    Only ever called WITH a catalogue (see run()), so it extends the manifest's
    surface rather than deciding its own mechanics — it is the same close, on a
    narrower choice of piece."""
    plabel = (pend_item.label if pend_item else None) or "it"
    pprice = int(pending.price_cents or pending.tip_unlock_cents or 0) // 100
    base = _manifest_block(offerable, scripts, cfg_mode, quotes=quotes or None,
                           sell_customs=sell_customs)
    return dataclasses.replace(base, block=(
        base.block
        + f"\n\nSECOND OFFER, your last: he hasn't unlocked '{plabel}' (${pprice}). "
        "ONE more max — a DIFFERENT piece, or if price is the block, re-tease it at "
        "the lower price above (never beg). End with its >>OFFER line; if neither "
        "fits, just chat — no >>OFFER."
    ))


def _parse_offer_marker(raw: str) -> tuple[str, int | None]:
    """Extract the FIRST >>OFFER id, then strip EVERY marker (malformed ones
    too — a fan must never see the protocol). Mirrors cat_stickers.parse_marker;
    both shapes come from `_markers.protocol_marker_re`."""
    m = _OFFER_RE.search(raw or "")
    ident = re.match(r"[ \t]*(\d+)", m.group(1)) if m else None
    clean = _OFFER_RE.sub("", raw or "").strip()
    return clean, (int(ident.group(1)) if ident else None)


def _force_pick(offerable: dict[int, CatalogItem],
                quotes: dict[int, upsell.Quote]) -> CatalogItem | None:
    """The item to attach when the gate cleared him but the model wrote no marker.

    CHEAPEST first, by the price he'd actually be QUOTED (smart pricing re-prices a
    $200 flagship per fan, so ordering by the catalog's sticker would pick the wrong
    piece). Forcing an ask the model declined to make is the aggressive move; making
    it the smallest ask on the shelf is what keeps it from torching the thread — and
    a cheap rung that converts escalates the next one anyway, which is the ladder's
    whole design. Free teasers are skipped: a teaser is not an ask, and attaching one
    here would burn the piece while recording no offer.

    With smart pricing OFF there are no quotes at all and the price comes from the
    catalog row — so fall back to it rather than refusing to sell (requiring a quote
    here made force_ask silently inert on every account that hadn't also enabled
    smart pricing, which is the majority of them).

    Ties break on item id so the choice is deterministic (same fan, same turn, same
    piece — a re-run must never quote him a different price)."""
    priced: list[tuple[int, int, CatalogItem]] = []
    for iid, it in offerable.items():
        if it.is_free_teaser:
            continue
        q = quotes.get(iid)
        px = int(q.price_cents) if q is not None else int(it.price_cents or 0)
        if px <= 0:
            continue        # no price to ask for — not an offer
        priced.append((px, iid, it))
    if not priced:
        return None
    return min(priced, key=lambda t: (t[0], t[1]))[2]


async def _record_offer(account_id: str, fan_id: int, item: CatalogItem,
                        mode: str, offer_message_id: int | None,
                        *, status: str = "open", resolved_by: str | None = None,
                        delivery_message_id: int | None = None,
                        quoted_cents: int | None = None) -> None:
    """`quoted_cents` (smart pricing) is the price the fan ACTUALLY saw — the offer
    row must carry it, not the catalog's static one, or the unlock watcher's tip
    threshold and every downstream report would be measured against a price that
    was never on the wire."""
    now = datetime.utcnow()
    price_c = int(quoted_cents if quoted_cents else (item.price_cents or 0))
    tip_c = int(quoted_cents if quoted_cents else (item.tip_unlock_cents or 0))
    async with get_session() as s:
        s.add(ContentOffer(
            account_id=str(account_id), fan_id=int(fan_id), item_id=int(item.id),
            script_id=int(item.script_id) if item.script_id is not None else None,
            mode=mode,
            price_cents=price_c if int(item.price_cents or 0) else 0,
            tip_unlock_cents=tip_c if int(item.tip_unlock_cents or 0) else 0,
            offer_message_id=int(offer_message_id) if offer_message_id else None,
            status=status, resolved_by=resolved_by,
            delivery_message_id=int(delivery_message_id) if delivery_message_id else None,
            offered_at=now, resolved_at=now if status == "delivered" else None,
            updated_at=now))


async def _record_vault_sends(account_id: str, fan_id: int, media: list[int],
                              message_id: int | None, price_cents: int,
                              was_purchased: bool = False) -> None:
    # price_cents is the price we ASKED (offer-time), NOT proof of purchase — an
    # offered-but-unbought PPV records price_cents>0 here. Only `was_purchased` (or a
    # paid Message) means he actually owns it; the dedup relies on that, not on price.
    now = datetime.utcnow()
    async with get_session() as s:
        for mid in media:
            s.add(VaultSend(account_id=str(account_id), fan_id=int(fan_id),
                            media_id=int(mid),
                            message_id=int(message_id) if message_id else None,
                            price_cents=int(price_cents), was_purchased=bool(was_purchased),
                            sent_at=now))


async def _mark_media_purchased(account_id: str, fan_id: int, media: list[int]) -> None:
    """Paid truth, item-level, idempotent — the offer lane's entry into the ONE
    ownership writer (ownership.stamp_media_owned: SELECT-first, flip-don't-dup),
    so BOTH dedup readers treat a PPV/tip buy as OWNED and no ladder path can
    re-pick it. The PPV media rides inside the locked box (Message.media_ids='[]')
    and the tip-only flip in _deliver_unlocked never covers it, so a ladder reset
    would re-sell a bought single that was never recorded as owned. One delta vs
    the old UPDATE-only stamp: an offer whose VaultSend rows were never recorded
    (crash between send and record) now gets first-time True rows instead of a
    silent no-op."""
    await ownership.stamp_media_owned(
        account_id, fan_id, [int(m) for m in media], source="offer_delivery")


async def _ensure_progress(account_id: str, fan_id: int, item: CatalogItem) -> None:
    """First offer/delivery on a script pins the fan to it (active at the item's
    position). Idempotent."""
    if item.script_id is None:
        return
    now = datetime.utcnow()
    async with get_session() as s:
        await s.execute(
            sqlite_insert(CatalogProgress)
            .values(account_id=str(account_id), fan_id=int(fan_id),
                    script_id=int(item.script_id),
                    position=int(item.position or 0), status="active",
                    started_at=now, updated_at=now)
            .on_conflict_do_nothing(
                index_elements=["account_id", "fan_id", "script_id"])
        )


async def _advance_progress(account_id: str, fan_id: int, item: CatalogItem) -> None:
    """Item delivered → the fan's pin moves to the next position; past the last
    enabled item the script is done (once per fan, forever)."""
    if item.script_id is None or item.position is None:
        return
    next_pos = int(item.position) + 1
    now = datetime.utcnow()
    async with get_session() as s:
        max_pos = (await s.execute(
            select(func.max(CatalogItem.position)).where(
                CatalogItem.account_id == str(account_id),
                CatalogItem.script_id == int(item.script_id),
                CatalogItem.enabled.is_(True))
        )).scalar_one_or_none()
        status = "done" if (max_pos is None or next_pos > int(max_pos)) else "active"
        await s.execute(
            sqlite_insert(CatalogProgress)
            .values(account_id=str(account_id), fan_id=int(fan_id),
                    script_id=int(item.script_id), position=next_pos,
                    status=status, started_at=now, updated_at=now)
            .on_conflict_do_update(
                index_elements=["account_id", "fan_id", "script_id"],
                set_={"position": next_pos, "status": status, "updated_at": now})
        )


async def _resolve_offer(offer_id: int, *, status: str, resolved_by: str | None,
                         delivery_message_id: int | None = None,
                         tips_accum_cents: int | None = None) -> None:
    now = datetime.utcnow()
    vals: dict = {"status": status, "resolved_by": resolved_by, "updated_at": now}
    if status in ("delivered", "expired", "cancelled"):
        vals["resolved_at"] = now
    if delivery_message_id is not None:
        vals["delivery_message_id"] = int(delivery_message_id)
    if tips_accum_cents is not None:
        vals["tips_accum_cents"] = int(tips_accum_cents)
    async with get_session() as s:
        await s.execute(update(ContentOffer)
                        .where(ContentOffer.id == int(offer_id)).values(**vals))


async def _tip_sum_since(account_id: str, fan_id: int, since: datetime) -> int:
    """Idempotent tip accumulation: recompute from the transactions table (the
    WS pump + ledger ingest both write it) instead of trusting event payloads —
    webhook replays converge instead of double-counting."""
    async with get_session() as s:
        rows = (await s.execute(
            select(Transaction.amount_cents).where(
                Transaction.account_id == str(account_id),
                Transaction.fan_id == int(fan_id),
                Transaction.kind.in_(_TIP_KINDS),
                Transaction.status.in_(("cleared", "pending")),
                Transaction.occurred_at >= since)
        )).scalars().all()
    return sum(int(x or 0) for x in rows)


async def _message_is_paid(account_id: str, message_id: int) -> bool:
    async with get_session() as s:
        v = (await s.execute(
            select(Message.is_paid).where(
                Message.account_id == str(account_id),
                Message.message_id == int(message_id))
        )).scalar_one_or_none()
    return bool(v)


async def _ppv_txn_since(account_id: str, fan_id: int, since: datetime,
                         price_cents: int) -> bool:
    """True when a `ppv_message` transaction for this fan landed since `since`
    at (about) `price_cents` — the ledger-side unlock signal that survives the
    payment→message linker REFUSING to link. That linker (transaction_ingest.
    _select_ppv_link_candidate) demands an unambiguous same-priced message and
    bails 'ambiguous' whenever the fan has >1 unpaid PPV at that price, so it
    never flips messages.is_paid and `_message_is_paid` stays False forever.
    Here the offer's own offered_at scopes the window, so the price equality
    that is ambiguous GLOBALLY (which of his same-priced PPVs?) is decisive for
    THIS offer. Tolerance mirrors the ingest fingerprint (max(10c, 1%)) for OF
    fee/VAT rounding."""
    if price_cents <= 0:
        return False
    tol = max(10, price_cents // 100)
    async with get_session() as s:
        row = (await s.execute(
            select(Transaction.id).where(
                Transaction.account_id == str(account_id),
                Transaction.fan_id == int(fan_id),
                Transaction.kind == "ppv_message",
                Transaction.status.in_(("cleared", "pending")),
                Transaction.occurred_at >= since,
                Transaction.amount_cents >= price_cents - tol,
                Transaction.amount_cents <= price_cents + tol).limit(1)
        )).first()
    return row is not None


async def _flip_message_paid(account_id: str, message_id: int) -> None:
    """Mark one outbound PPV paid (idempotent). Mirrors the flip inside
    `_fastpath_check_opened`; used when the ledger-txn path resolves an offer the
    global linker left unlinked, so bought-media dedup and every other is_paid
    reader converge on the truth instead of re-offering a piece he already owns."""
    now = datetime.utcnow()
    async with get_session() as s:
        await s.execute(update(Message).where(
            Message.account_id == str(account_id),
            Message.message_id == int(message_id),
            or_(Message.is_paid.is_(False), Message.is_paid.is_(None)))
            .values(is_paid=True, purchased_at=now))
        await s.commit()


async def _fan_active_recently(account_id: str, fan_id: int, minutes: int) -> bool:
    since = datetime.utcnow() - timedelta(minutes=minutes)
    async with get_session() as s:
        row = (await s.execute(
            select(Message.message_id).where(
                Message.account_id == str(account_id),
                Message.fan_id == int(fan_id),
                Message.direction == "in",
                Message.created_at >= since).limit(1)
        )).first()
    return row is not None


async def _refresh_paid_state(client, account_id: str, fan_id: int) -> int:
    """One targeted OF read → flip is_paid on EVERY priced message he has since
    unlocked. Returns how many flipped.

    `is_paid` is written once, at ingest, from the `isOpened` in the send payload — which
    is always False, because he cannot have opened a PPV that was created a millisecond
    ago. Nothing re-reads it. So a PPV a chatter sent and the fan PAID FOR sits at
    is_paid=0 until the payouts ledger catches up, and that lags by up to 10 HOURS
    (measured). For those hours the engine believes a buyer is a non-buyer: no hot
    window, no post-purchase talk window, and rhythm scores the thread as cold.

    The sibling `_fastpath_check_opened` has done exactly this since day one — for ONE
    message, and only ever for an offer WE made. Same OF read, same flip; it was just
    never pointed at the PPVs the humans send."""
    try:
        data = await asyncio.to_thread(
            lambda: client.get_messages(fan_id, limit=_FASTPATH_READ_LIMIT))
    except Exception:
        log.debug("ai_chatter paid-state refresh read failed account=%s fan=%s",
                  account_id, fan_id, exc_info=True)
        return 0
    items = data.get("list") if isinstance(data, dict) else data
    opened = [int(m["id"]) for m in (items or [])
              if isinstance(m, dict) and m.get("id") and m.get("isOpened")
              and _to_cents_safe(m.get("price")) > 0]
    if not opened:
        return 0
    now = datetime.utcnow()
    async with get_session() as s:
        res = await s.execute(update(Message).where(
            Message.account_id == str(account_id),
            Message.message_id.in_(opened),
            or_(Message.is_paid.is_(False), Message.is_paid.is_(None)))
            .values(is_paid=True, purchased_at=now))
        await s.commit()
    n = int(res.rowcount or 0)
    if n:
        log.info("ai_chatter paid-state refresh account=%s fan=%s flipped=%s",
                 account_id, fan_id, n)
    return n


def _to_cents_safe(price) -> int:
    try:
        return int(round(float(price or 0) * 100))
    except (TypeError, ValueError):
        return 0


async def _fastpath_check_opened(client, account_id: str, fan_id: int,
                                 message_id: int) -> bool:
    """One targeted OF read: is the offered PPV message isOpened? On yes, flip
    our Message row (the ledger ingest would do the same within ~10 min)."""
    try:
        data = await asyncio.to_thread(
            lambda: client.get_messages(fan_id, limit=_FASTPATH_READ_LIMIT))
    except Exception:
        log.debug("ai_chatter fastpath read failed account=%s fan=%s",
                  account_id, fan_id, exc_info=True)
        return False
    items = data.get("list") if isinstance(data, dict) else data
    for m in (items or []):
        if isinstance(m, dict) and int(m.get("id") or 0) == int(message_id):
            if m.get("isOpened"):
                now = datetime.utcnow()
                async with get_session() as s:
                    await s.execute(update(Message).where(
                        Message.account_id == str(account_id),
                        Message.message_id == int(message_id))
                        .values(is_paid=True, purchased_at=now))
                return True
            return False
    return False


async def _get_item(item_id: int) -> CatalogItem | None:
    async with get_session() as s:
        it = await s.get(CatalogItem, int(item_id))
        if it is not None:
            s.expunge(it)
    return it


async def _deliver_unlocked(client, account_id: str, offer: ContentOffer,
                            item: CatalogItem, *, by: str) -> int | None:
    """The unlock landed — tip mode sends the media FREE with a short reaction;
    PPV already delivered inside the locked message, so just react. Returns the
    sent message id (None = send failed; caller retries next tick)."""
    _ul_lang = await _language.load_account_language(account_id)
    # The voice is read HERE rather than threaded from `run()`: this function is
    # also reached on the disabled-account path (paid-but-undelivered protection),
    # which returns before `run()` ever loads the bundle. One extra read on an
    # unlock — a rare event — and symmetric with the language read above.
    _ul_v = await load_voice_blocks(account_id)
    _ul_table = (_UNLOCK_REACTIONS_BY_LANG_HIM if _ul_v.is_male
                 else _UNLOCK_REACTIONS_BY_LANG)
    caption = _language.apply_word_restriction(
        random.choice(_ul_table.get(_ul_lang) or _ul_v.unlock_reactions), _ul_lang)
    media = _item_media(item)
    try:
        if by == "tip":
            result = await asyncio.to_thread(
                lambda: client.send_message(int(offer.fan_id), caption,
                                            media_files=media, price=0))
        else:
            result = await asyncio.to_thread(
                client.send_message, int(offer.fan_id), caption)
    except Exception:
        log.warning("ai_chatter delivery send failed account=%s fan=%s offer=%s",
                    account_id, offer.fan_id, offer.id, exc_info=True)
        return None
    msg_id = result.get("id") if isinstance(result, dict) else None
    if msg_id:
        await write_outbound_attribution(
            account_id=account_id, fan_id=int(offer.fan_id),
            message_id=int(msg_id), sent_by_employee_id=None,
            automation_kind=_PURPOSE, body=caption, price_cents=0,
            created_at=ax._parse_iso(result.get("createdAt")) or datetime.utcnow(),
            emit_live=True)
        if by == "tip":
            # A tip UNLOCK delivered the media free-of-box — he owns it now. Stamp
            # was_purchased so the dedup never re-offers a tip-bought piece (its
            # VaultSend price is 0, so price alone would miss it).
            await _record_vault_sends(account_id, int(offer.fan_id), media,
                                      int(msg_id), 0, was_purchased=True)
    return int(msg_id) if msg_id else None


async def _send_free_bubble(client, account_id: str, fan_id: int, line: str,
                            *, typing_wpm: float, typing_indicator, now: datetime,
                            hold: bool = True) -> int | None:
    """Send ONE free (unpriced) bubble with full attribution + reply bookkeeping,
    the way every deterministic-line path (soft-broke ack, companion ack, aftercare,
    post-buy bridge) must. Returns the OF message id, or None on any failure. Keeps
    those paths from diverging on attribution/typing/emit_live details."""
    line = apply_word_restriction(line)[:_REPLY_MAX_CHARS]
    if hold:
        await hold_with_typing(account_id, fan_id,
                               typing_delay_seconds(line, typing_wpm),
                               typing_indicator=typing_indicator)
    try:
        res = await asyncio.to_thread(lambda p=line: client.send_message(fan_id, p))
    except Exception as e:
        await skip_unreachable_fan(account_id, fan_id, e, log=log)
        log.warning("ai_chatter free-bubble send failed account=%s fan=%s",
                    account_id, fan_id, exc_info=True)
        return None
    msg_id = res.get("id") if isinstance(res, dict) else None
    if not msg_id:
        return None
    await write_outbound_attribution(
        account_id=account_id, fan_id=int(fan_id), message_id=int(msg_id),
        sent_by_employee_id=None, automation_kind=_PURPOSE,
        body=str(res.get("text") or line), price_cents=0,
        created_at=ax._parse_iso(res.get("createdAt")) or now, emit_live=True)
    await _mark_reply_sent(account_id, fan_id, now)
    return int(msg_id)


async def _thread_moved_on(account_id: str, fan_id: int,
                           inbound_at: datetime | None) -> str:
    """Between generation and the wire, did this turn stop being ours to answer?

    Two ways it can, and they are different failures. HE WROTE AGAIN: the reply we
    are holding answers a message he has already moved past, so it lands as a
    non-sequitur. SOMEBODY ELSE ANSWERED — a human chatter, another automation —
    and sending now double-replies over them.

    Worth asking because the loop's picture of the thread is stale by construction:
    `_gather` runs once at the top of the sweep, and between it and this line sit
    two LLM calls, the humanizer and up to `INLINE_MAX_S` of typing hold. A replayed
    draft has sat even longer.

    Returns "" when the turn is still ours, else the reason — a string so the caller
    can count the two apart, because they mean different things about the system.
    autoreply asks half of this question twice (`_fan_still_waiting`,
    autoreply.py:413); this is the same query widened to notice a new INBOUND too,
    because a keep-warm line is still true after he speaks and a direct answer is not.
    """
    if inbound_at is None:
        return ""
    async with get_session() as s:
        newer_in = (await s.execute(
            select(Message.message_id).where(
                Message.account_id == str(account_id),
                Message.fan_id == int(fan_id),
                Message.direction == "in",
                Message.is_unsent.is_(False),
                Message.created_at > inbound_at,
            ).limit(1)
        )).first()
        if newer_in is not None:
            return "fan_spoke_again"
        # A BROADCAST IS NOT AN ANSWER, and this is the trap in asking the
        # question per-fan. `_gather` makes blasts transparent to the turn
        # account-wide (a blast fired over his message must not silence the reply
        # he is owed) using `_broadcast_bodies`, which needs the whole account to
        # see the same text on >= _BROADCAST_MIN_FANS threads. Scoped to ONE fan
        # that evidence does not exist, and an untagged OF-app blast is byte-for-
        # byte a human 1:1 reply. Treating it as an answer would re-silence him on
        # every tick — a permanent gag, not a missed turn.
        #
        # So this stands down only for a send we can POSITIVELY identify as 1:1:
        # another automation (`automation_kind`) or a named chatter
        # (`sent_by_employee_id`), and never one carrying a `mass_run_id`. An
        # untagged send stays ambiguous and is left to the account-wide turn gate
        # on the next sweep, which is exactly today's behaviour.
        answered = (await s.execute(
            select(Message.message_id).where(
                Message.account_id == str(account_id),
                Message.fan_id == int(fan_id),
                Message.direction == "out",
                Message.is_unsent.is_(False),
                Message.created_at > inbound_at,
                Message.mass_run_id.is_(None),
                or_(Message.automation_kind.is_not(None),
                    Message.sent_by_employee_id.is_not(None)),
            ).limit(1)
        )).first()
    return "already_answered" if answered is not None else ""


def _is_404(exc: Exception) -> bool:
    """A 404 on an unsend means the message is ALREADY gone (OF auto-unsent it, or
    a previous attempt landed). That is SUCCESS, not failure — see MEMORY
    of_unsend_windows. Any OTHER error means the original may still be live."""
    resp = getattr(exc, "response", None)
    if getattr(resp, "status_code", None) == 404:
        return True
    return "404" in str(exc)


# spec §4.1 — a PRICE-VALIDATION 400 is NOT an undeliverable fan. OF rejects a
# priced message whose amount is out of range (its wire max is $200, floor $3), and
# the naive `except` fed EVERY send failure into skip_unreachable_fan → a whale
# 7-day-quarantined over a fixable number, his ladder closed. A price error must
# instead DROP THE OFFER / resend unpriced, never skip_list, never close the ladder.
_PRICE_ERROR_MARKERS = (
    "invalid price", "price is invalid", "price must", "price too", "price is too",
    "minimum price", "maximum price", "min price", "max price", "price cannot",
    "price should", "amount too", "invalid amount", "price out of",
)


def _is_price_error(exc: Exception) -> bool:
    """A price-validation rejection (a 400 naming the price), distinct from an
    undeliverable-fan error. Matched on the marker set so an OF wording change in the
    non-price 400s can't silently start quarantining whales."""
    resp = getattr(exc, "response", None)
    status = getattr(resp, "status_code", None)
    blob = str(exc).lower()
    body = ""
    try:
        body = str(getattr(resp, "text", "") or "").lower()
    except Exception:
        body = ""
    hay = f"{blob} {body}"
    hit = any(m in hay for m in _PRICE_ERROR_MARKERS)
    # A bare "price" token only counts alongside a 400 — otherwise an unrelated 500
    # mentioning "price" anywhere would masquerade as one.
    if not hit and status == 400 and "price" in hay:
        hit = True
    return hit


async def _maybe_discount_resend(client, account_id: str, cfg: dict,
                                 offer: ContentOffer, now: datetime) -> bool:
    """The ONE answer to an unpaid rung: unsend it, re-send it cheaper, ONCE, then
    tap out. Measured on the corpus, after an unpaid rung:
        repeat the same price → 6.8% paid   (the single WORST action measured)
        discount 0.75-0.9x    → 15.6%       ($5.43 EV — the best)
        halve                 → 19.7%       (converts, but gives it away: $2.62 EV)
    The UNSEND IS NOT OPTIONAL and it goes FIRST: leave the $100 copy live under the
    $50 copy and he can buy BOTH — we'd have sold the same clip twice and taught him
    to wait for the discount. A 404 = already unsent = success; ANY other unsend
    failure aborts the resend entirely (silence beats a double-live price)."""
    fan_id = int(offer.fan_id)
    if not offer.offered_at or offer.offered_at > now - timedelta(minutes=upsell.RESEND_AFTER_M):
        return False
    # PPV-only: a tip ask has no locked message to pull, so there is nothing to
    # double-buy — and re-asking a fan who ignored the first ask is begging.
    if offer.mode not in ("ppv", "both") or not offer.offer_message_id:
        return False
    ladders = await _load_ladders(account_id, [fan_id])
    lad = ladders.get(fan_id)
    if lad is None:
        return False
    # ⚠️ THE BRAKE THE REPLY LOOP HONOURS AND THIS PATH USED TO IGNORE.
    # A SOFT decline ("im broke rn") sets offers_paused_until = +24h and KEEPS TALKING.
    # But this watcher only checked `status`, so 90 minutes after a man told us he had no
    # money we unsent his PPV and put a CHEAPER PAYWALL in front of him — the single
    # thing upsell.py's own docstring says we must never do. Caught by the simulator's
    # broke_guy persona; it is the one violation it found on the first clean run.
    if lad.status in (upsell.STATUS_STOPPED, upsell.STATUS_TAPPED):
        return False
    if lad.offers_paused_until and lad.offers_paused_until > datetime.utcnow():
        return False
    # A COMPANION/cooldown fan (§6) is not sold to at all — belt-and-suspenders with
    # the offers_paused_until guard above (spend_regret sets BOTH).
    if lad.status == upsell.STATUS_COMPANION:
        return False
    if lad.companion_until and lad.companion_until > datetime.utcnow():
        return False
    if lad.cooldown_until and lad.cooldown_until > datetime.utcnow():
        return False
    # ── §5 EARNED DISCOUNT. The cut is now CONTINGENT ON A VOICED OBJECTION. The v1
    # behaviour — a bare 90-min timer, no objection check — was exactly the 95.8%-
    # unprompted anti-pattern (unprompted cuts convert 20.7% vs 42.9% for cuts
    # answering a voiced ask). may_discount owns: ASK-REQUIRED, the one-turn beat, the
    # 0.60 floor / one-per-item, and the 0.35 reinforcer governor. It applies on THIS
    # timer path too — no voiced objection on file ⇒ no cut (silence, not a beg).
    # SPEND_REGRET is never eligible (a broke man met with a cheaper offer is the one
    # move upsell.py's docstring forbids); detect_spend_regret gates it out.
    last_text, last_in_at = await _last_inbound(account_id, fan_id)
    # A DECLINE kills the resend outright. This path only ever consulted
    # detect_spend_regret, which does NOT see a hard stop or a bare no:
    #     "im disputing this charge and reporting you" -> classify=hard,   regret=False
    #     "not interested" / "no thanks"               -> classify=bare_no, regret=False
    # It relied on the reply loop having already written STOPPED/TAPPED to the ladder —
    # but this resend runs from _resolve_open_offers, BEFORE the brakes, and on every
    # tick. The reply loop can be hours behind him: rhythm deferral, fan cooldown, lease
    # contention, the daily LLM cap. So the 90-minute timer fires, the ladder still says
    # `open`, and a cheaper PPV is pushed at a man whose last words were "not interested"
    # — or at one threatening a chargeback. Read the message, don't trust the state.
    if upsell.classify_decline(last_text):
        log.info("ai_chatter discount resend BLOCKED — he declined account=%s fan=%s",
                 account_id, fan_id)
        return False
    cuts30, asks30 = await _cuts_asks_last_30d(account_id, fan_id, now)
    ok_disc, why_disc = upsell.may_discount(upsell.DiscountCtx(
        last_inbound_text=last_text or "",
        objection_at=lad.objection_at,
        last_inbound_at=last_in_at,
        discount_count_this_item=int(lad.discount_count or 0),
        cuts_last_30d=cuts30, asks_last_30d=asks30,
        spend_regret=upsell.detect_spend_regret(last_text)))
    if not ok_disc:
        log.debug("ai_chatter discount refused account=%s fan=%s why=%s",
                  account_id, fan_id, why_disc)
        return False                       # strip the cut; a pivot happens on his next turn
    # NOT gated on status == OPEN. The discount resend is a WIN-BACK ("hey stranger...
    # where u been? i took it down and put it back cheaper for you") — it is aimed
    # precisely at the fan who went quiet, and his ladder goes IDLE after SESSION_IDLE_M
    # (20min) while RESEND_AFTER_M is 90. Requiring OPEN made the best-measured
    # post-unpaid action (15.6% paid, $5.43 EV — the highest of any) unreachable for
    # every fan it was designed for. `stop_after_unpaid_rungs` (§11 checkbox #3, default
    # 1) is the "second chance after a refusal" consent question the operator owns.
    if int(lad.unpaid_rungs or 0) >= int(cfg.get("stop_after_unpaid_rungs")
                                          or upsell.STOP_AFTER_UNPAID_RUNGS):
        return False                       # already discounted once. No begging.
    item = await _get_item(int(offer.item_id))
    if item is None:
        return False
    media = _item_media(item)
    # He may have bought this exact media somewhere else entirely (a mass blast has
    # no content_offers row at all) — media-keyed, never catalog-keyed. HERO media
    # only: owning the free tease frames doesn't make the discounted payoff his.
    hero = (await _hero_media_map(account_id, [item]))[int(item.id)]
    if fan_id in await _owners_of_media(account_id, hero):
        await _resolve_offer(int(offer.id), status="cancelled", resolved_by="owned")
        await _close_ladder(account_id, fan_id, upsell.STATUS_TAPPED)
        return False
    seen_cents = int(offer.price_cents or 0)
    if seen_cents <= upsell.OF_PRICE_FLOOR_CENTS:
        return False                       # nothing left to discount into

    if not await ax.acquire_fan_lease(account_id, fan_id, _PURPOSE):
        return False                       # someone else owns the fan this cycle
    try:
        try:
            await asyncio.to_thread(client.unsend_message,
                                    int(offer.offer_message_id), fan_id)
        except Exception as e:
            if not _is_404(e):
                log.warning("ai_chatter discount resend ABORTED — unsend failed "
                            "account=%s fan=%s msg=%s (original may still be live)",
                            account_id, fan_id, offer.offer_message_id, exc_info=True)
                return False
        await _resolve_offer(int(offer.id), status="cancelled", resolved_by="discounted")

        rng = random.Random(f"discount:{account_id}:{fan_id}:{offer.id}")
        # Compute off the price he SAW, but never above OF's $200 wire max — a static
        # catalog item authored above ~$222 could otherwise produce a >$200 discount
        # (the offer row stores the unclamped catalog price when smart pricing is off).
        # This is the one priced site the main loop's re-clamp didn't cover.
        seen_cents = min(int(seen_cents), upsell.OF_PRICE_MAX_CENTS)
        px = max(upsell.OF_PRICE_FLOOR_CENTS,
                 min(upsell.discount_price(seen_cents, rng, str(account_id)),
                     upsell.OF_PRICE_MAX_CENTS))
        line = _pack_line("discount_resend", cfg, fan_id, price_cents=px)
        if not line:
            return False
        line = apply_word_restriction(line)[:_REPLY_MAX_CHARS]
        # OF wants dollars and takes cents (see the rung path). `px // 100` charged
        # $41.00 while the line she sent said "$41.23" — the message and the paywall
        # quoting different numbers is exactly the tell this whole feature exists to
        # remove.
        kwargs: dict = {"price": px / 100, "locked_text": False, "media_files": media}
        previews = _item_previews(item)
        if previews:
            kwargs["previews"] = previews
        try:
            result = await asyncio.to_thread(
                lambda: client.send_message(fan_id, line, **kwargs))
        except Exception as e:
            if _is_price_error(e):
                # A price-validation 400 is not an undeliverable fan — never quarantine
                # him for it. Drop the discount; the original was already unsent.
                log.warning("ai_chatter discount resend price-rejected account=%s fan=%s",
                            account_id, fan_id)
                return False
            await skip_unreachable_fan(account_id, fan_id, e, log=log)
            log.warning("ai_chatter discount resend send failed account=%s fan=%s",
                        account_id, fan_id, exc_info=True)
            return False
        msg_id = result.get("id") if isinstance(result, dict) else None
        if not msg_id:
            return False
        await write_outbound_attribution(
            account_id=account_id, fan_id=fan_id, message_id=int(msg_id),
            sent_by_employee_id=None, automation_kind=_KIND_UPSELL,  # a priced ask
            body=str(result.get("text") or line), price_cents=px,
            created_at=ax._parse_iso(result.get("createdAt")) or now, emit_live=True)
        await _record_vault_sends(account_id, fan_id, media, int(msg_id), px)
        await _record_offer(account_id, fan_id, item, "ppv", int(msg_id),
                            quoted_cents=px)
        # The discount is a QUOTE like any other — it has to be in the conversion
        # log or the "is discounting worth it" question can never be re-asked.
        await _record_quote(
            account_id, fan_id, item,
            upsell.Quote(price_cents=px, base_cents=seen_cents, arm_mult=1.0,
                         pre_clamp_cents=px, clamped_by=None,
                         band_lo=upsell.OF_PRICE_FLOOR_CENTS, band_hi=seen_cents),
            rung_index=int(lad.rung_index or 0), kind="discount",
            message_id=int(msg_id), media_key=upsell.media_key(media))
        # One discount, then the ladder is done with him. It reopens only if HE
        # buys (the watcher flips him hot) — never by us asking a third time.
        await _save_ladder(account_id, fan_id, status=upsell.STATUS_TAPPED,
                           unpaid_rungs=int(lad.unpaid_rungs or 0) + 1,
                           discount_count=int(lad.discount_count or 0) + 1,
                           last_ask_at=now, session_idle_at=now)
        log.info("ai_chatter discount resend account=%s fan=%s %s→%s msg=%s",
                 account_id, fan_id, seen_cents, px, msg_id)
        return True
    finally:
        await ax.release_fan_lease(account_id, fan_id)


async def _resolve_open_offers(account_id: str, client, cfg: dict,
                               *, dry_run: bool,
                               only_fan_ids: set[int] | None = None) -> dict:
    """The unlock watcher — runs every tick BEFORE candidate filtering (a fan
    doesn't have to speak to unlock). Three signals, in cost order: tips since
    offered_at (transactions table, real-time via the tip hook), the ledger
    convergence flipping messages.is_paid (≤10 min), and a targeted OF re-read
    for a recently-active fan (the bought-then-replied fast path). Also expires
    offers past the stall TTL.

    With the gate on it is ALSO where the ladder turns: a PAID rung flips the fan
    HOT (post-purchase re-offer conversion decays 66.7% <5min → 45.0% >24h, so the
    hot window is the whole game), and an UNPAID one gets exactly one unsend-first
    discount resend before the ladder taps out."""
    stats = {"unlocked_tip": 0, "unlocked_ppv": 0, "offers_expired": 0,
             "offers_unsent": 0, "deliveries_failed": 0, "would_unlock": 0,
             "discount_resends": 0}
    ttl_h = int(cfg.get("stall_ttl_hours") or 0)
    unsend_expired = bool(cfg.get("unsend_expired_offer"))
    gate_on = bool(cfg.get("qualification_gate_enabled"))
    now = datetime.utcnow()
    for offer in await _open_offers(account_id):
        fan_id = int(offer.fan_id)
        if only_fan_ids and fan_id not in only_fan_ids:
            continue
        if ttl_h and offer.offered_at and offer.offered_at < now - timedelta(hours=ttl_h):
            # Don't expire a PPV that was actually PAID but whose ledger landed AFTER the TTL — a
            # SILENT buyer (no reply, so the fastpath never fired). Expiring it drops the ownership
            # stamp and the ladder re-charges him next session (the re-sale class this fix prevents).
            # If the money is in, fall THROUGH to the paid path below (delivers + _mark_media_purchased).
            paid_late = bool(
                offer.mode in ("ppv", "both") and offer.offer_message_id
                and (await _message_is_paid(account_id, int(offer.offer_message_id))
                     or (offer.offered_at and await _ppv_txn_since(
                         account_id, fan_id, offer.offered_at, int(offer.price_cents or 0)))))
            if not paid_late:
                if not dry_run:
                    # Pull the unpurchased offer message FIRST (best-effort; per-chat
                    # unsend only works inside OF's 24h window, and stall_ttl is well
                    # under it), then mark the offer expired.
                    if unsend_expired and offer.offer_message_id:
                        try:
                            await asyncio.to_thread(
                                client.unsend_message,
                                int(offer.offer_message_id), fan_id)
                            stats["offers_unsent"] += 1
                        except Exception:
                            log.debug("ai_chatter expired-offer unsend failed account=%s "
                                      "fan=%s msg=%s", account_id, fan_id,
                                      offer.offer_message_id, exc_info=True)
                    await _resolve_offer(int(offer.id), status="expired", resolved_by=None)
                    if gate_on:
                        # The rung died on the vine — close the ladder to IDLE, NOT tapped.
                        # A PPV that merely aged out unopened is not a "no": measured, plenty
                        # of proven payers ignore one message and buy the next. Tapping them
                        # for 24h retired money-in-hand fans (e.g. a $45 payer went dark after
                        # one unopened PPV). IDLE still resets rung_index (via _close_ladder),
                        # so the next ask re-prices off the band, never off a price he never
                        # paid — but he stays sellable right away.
                        await _close_ladder(account_id, fan_id, upsell.STATUS_IDLE)
                stats["offers_expired"] += 1
                continue

        paid_by: str | None = None
        if offer.mode in ("ppv", "both") and offer.offer_message_id:
            if await _message_is_paid(account_id, int(offer.offer_message_id)):
                paid_by = "ppv_ledger"
            elif offer.offered_at and await _ppv_txn_since(
                    account_id, fan_id, offer.offered_at, int(offer.price_cents or 0)):
                # The money is in the ledger but the global payment→message linker
                # bailed 'ambiguous' (this fan has >1 same-priced PPV) so is_paid
                # never flipped and `_message_is_paid` above missed it. Scoped by
                # THIS offer's offered_at the price match is unambiguous — flip
                # is_paid so bought-media dedup is correct, then resolve. Cheaper
                # than the fastpath (DB read, no OF call) so it runs before it.
                if not dry_run:
                    await _flip_message_paid(account_id, int(offer.offer_message_id))
                paid_by = "ppv_txn"
            elif (not dry_run and await _fan_active_recently(
                    account_id, fan_id, _FASTPATH_ACTIVE_WINDOW_MIN)):
                if await _fastpath_check_opened(client, account_id, fan_id,
                                                int(offer.offer_message_id)):
                    paid_by = "ppv_fastpath"
        tips = 0
        if paid_by is None and offer.mode in ("tip", "both") and offer.offered_at:
            tips = await _tip_sum_since(account_id, fan_id, offer.offered_at)
            if tips != int(offer.tips_accum_cents or 0) and not dry_run:
                await _resolve_offer(int(offer.id), status="open",
                                     resolved_by=None, tips_accum_cents=tips)
            if int(offer.tip_unlock_cents or 0) > 0 and tips >= int(offer.tip_unlock_cents):
                paid_by = "tip"
        if paid_by is None:
            # Still unpaid. Past RESEND_AFTER_M that earns ONE discounted resend —
            # unsend-first, then the ladder taps out (see _maybe_discount_resend).
            if gate_on and not dry_run:
                try:
                    if await _maybe_discount_resend(client, account_id, cfg, offer, now):
                        stats["discount_resends"] += 1
                except Exception:
                    log.warning("ai_chatter discount resend errored account=%s fan=%s",
                                account_id, fan_id, exc_info=True)
            continue
        if dry_run:
            stats["would_unlock"] += 1
            continue

        item = await _get_item(int(offer.item_id))
        if item is None:
            await _resolve_offer(int(offer.id), status="cancelled", resolved_by=None)
            continue
        # A delivery is a send — same lease/cooldown discipline as a reply.
        if not await ax.acquire_fan_lease(account_id, fan_id, _PURPOSE):
            continue  # locked this cycle; the unlock persists, retry next tick
        try:
            msg_id = await _deliver_unlocked(client, account_id, offer, item, by=paid_by)
            if msg_id is None and paid_by == "tip":
                stats["deliveries_failed"] += 1
                continue  # keep the offer open; delivery retries next tick
            await _resolve_offer(int(offer.id), status="delivered",
                                 resolved_by=paid_by, delivery_message_id=msg_id,
                                 tips_accum_cents=tips if paid_by == "tip" else None)
            # Ownership stamp — the moment the money is real. UNCONDITIONAL (tip AND ppv),
            # independent of the reaction-send msg_id (a PPV whose reaction send failed still
            # resolves 'delivered' above), and OUTSIDE the gate_on block below (gate-off
            # accounts need dedup too). This is what stops a ladder reset re-selling the item.
            await _mark_media_purchased(account_id, fan_id, _item_media(item))
            await _advance_progress(account_id, fan_id, item)
            stats["unlocked_tip" if paid_by == "tip" else "unlocked_ppv"] += 1
            if gate_on:
                # He PAID. This is the moment worth money: once a fan has bought once
                # his conversion goes ~FLAT across price (43.6% @ $0-9 vs 44.3% @
                # $100+), and re-offer conversion decays 66.7% (<5min) → 45.0%
                # (>24h). So: mark the rung paid, flip HOT, and let the next rung
                # fire on his next qualifying inbound (never faster than RUNG_GAP_S).
                paid_at = datetime.utcnow()
                await _mark_quote_paid(account_id, fan_id,
                                       offer.offer_message_id, paid_at)
                ladder_vals: dict = dict(
                    status=upsell.STATUS_HOT, last_paid_at=paid_at, unpaid_rungs=0,
                    session_idle_at=paid_at,
                    hot_until=paid_at + timedelta(minutes=upsell.HOT_WINDOW_M))
                # §6.2 — after a session with >=3 PAID rungs, ease off: a 12-18h talk-
                # only cooldown (the humane reading of "make them happy" + the operator's
                # instinct to back off after several buys). This rung is already marked
                # paid, so the count includes it. Both qualify's callers and post_buy
                # honour cooldown_until (call-site checks in run()/_run_post_buy).
                if await _session_paid_rungs(account_id, fan_id, paid_at) >= 3:
                    cd_rng = random.Random(f"cooldown:{account_id}:{fan_id}:{offer.id}")
                    lo, hi = upsell.MULTIBUY_COOLDOWN_H
                    ladder_vals["cooldown_until"] = paid_at + timedelta(
                        hours=cd_rng.uniform(lo, hi))
                    log.info("ai_chatter >=3 paid rungs → cooldown account=%s fan=%s",
                             account_id, fan_id)
                await _save_ladder(account_id, fan_id, **ladder_vals)
                # §4.4b — a purchase is an EVENT, not a turn: a man who pays and silently
                # watches is never re-offered by qualify() (it needs fan_spoke_last). So
                # enqueue ONE post-buy job (the free post_buy_bridge bubble always; the
                # follow-up RUNG only if post_buy_rung_enabled + the §6 brakes pass). One
                # job per content_offers.id — the offer transitions to 'delivered' here,
                # so this branch runs exactly once for it (idempotent by construction).
                pb_rng = random.Random(f"postbuy:{account_id}:{fan_id}:{offer.id}")
                try:
                    await ax.enqueue_job(
                        account_id, _PURPOSE, payload={"post_buy": int(offer.id)},
                        run_at=paid_at + timedelta(seconds=pb_rng.uniform(90, 240)))
                except Exception:
                    log.debug("ai_chatter post_buy enqueue failed account=%s fan=%s",
                              account_id, fan_id, exc_info=True)
                # §7 — aftercare: if he goes SILENT after this PAID rung, one free warm
                # bubble (+ the free gift, if enabled and >=2 paid this session), then
                # TAPPED. Self-cancels in the handler if he speaks/buys before it fires.
                try:
                    await ax.enqueue_job(
                        account_id, _PURPOSE, payload={"aftercare": int(offer.id)},
                        run_at=paid_at + timedelta(minutes=upsell.AFTERCARE_SILENCE_M))
                except Exception:
                    log.debug("ai_chatter aftercare enqueue failed account=%s fan=%s",
                              account_id, fan_id, exc_info=True)
            try:
                await ax.start_fan_cooldown(account_id, fan_id,
                                            cooldown_s=_REPLY_COOLDOWN_S)
            except Exception:
                log.warning("ai_chatter post-delivery cooldown failed account=%s fan=%s",
                            account_id, fan_id, exc_info=True)
        finally:
            await ax.release_fan_lease(account_id, fan_id)
    return stats


async def has_open_tip_offer(account_id: str, fan_id: int) -> bool:
    """For the tip hook: should ai_chatter claim this fan's tip (suppressing the
    generic tip_reward)? True iff enabled AND an open tip-capable offer exists."""
    if not await is_enabled(account_id):
        return False
    for o in await _open_offers(account_id, fan_id):
        if o.mode in ("tip", "both") and int(o.tip_unlock_cents or 0) > 0:
            return True
    return False


async def _maybe_bootstrap_profile(account_id: str, fan_id: int) -> bool:
    """Item 22 — cold-start notes. `_maybe_refresh_profile` (shared with of_ai_chat)
    only enqueues gen_info once a fan crosses gen_info's staleness gate
    (`_MIN_NEW_MSGS = 8` new inbound). A fan who chats only a handful of times
    therefore NEVER gets a profile — so no notes ever get pushed (the "Jordan never
    got notes" case). This forces ONE gen_info regen for a PROFILE-LESS fan the
    moment ai_chatter engages him, so notes/facts get built from the first
    exchanges. Idempotent: skips when a profile already exists OR a gen_info job is
    already queued for this fan (so it fires once, not every tick). Best-effort —
    any failure is logged and swallowed. Returns True iff a regen was enqueued."""
    try:
        async with get_session() as s:
            has_profile = (await s.execute(
                select(FanProfile.fan_id)
                .where(FanProfile.account_id == str(account_id),
                       FanProfile.fan_id == int(fan_id))
                .limit(1)
            )).first()
            if has_profile is not None:
                return False
            pending = (await s.execute(
                select(ScheduledJob.id)
                .where(ScheduledJob.account_id == str(account_id),
                       ScheduledJob.kind == "gen_info",
                       ScheduledJob.status.in_(("pending", "running")),
                       ScheduledJob.payload_json.like(f"%[{int(fan_id)}]%"))
                .limit(1)
            )).first()
            if pending is not None:
                return False
        await ax.enqueue_job(account_id, "gen_info",
                             payload={"force_ids": [int(fan_id)]})
        return True
    except Exception:
        log.debug("ai_chatter profile-bootstrap enqueue skipped account=%s fan=%s",
                  account_id, fan_id, exc_info=True)
        return False


# ── Cadence controller (items 10/17/18/21) ──────────────────────────────────

# How long a human chatter's unpaid PPV stays a LIVE ask for PACING purposes — i.e. how
# long we refuse to stack another price on top of it. Long, because asking a man for
# money twice while his first locked box is still sitting there unopened is the fastest
# way to read as a bot.
_HUMAN_ASK_TTL = timedelta(hours=6)

# How long an ask makes the thread BREAK-PROOF — a different question, and a much
# shorter answer. Right after an ask she must not wander off: a ladder stranded mid-sell
# is the worst outcome in the system. But a woman who cannot leave the room for six
# hours because he hasn't opened her PPV is not a human either — that is a bot standing
# to attention. Past this window she is allowed to be a person again: rhythm rolls its
# normal break, and it hands her a COVER LINE on the way out ("sorry babe, had to run
# out for a sec 🚿"), so he is told she stepped away rather than just ghosted.
_ASK_BREAKPROOF_WINDOW = timedelta(minutes=30)


class _Money(NamedTuple):
    """What the THREAD says about money on one fan, as three instants.

    Named fields rather than a positional tuple because this is read at eight
    sites and was widened once already: `[1]` and `[2]` next to each other are
    indistinguishable on sight, and the previous shape needed a hand-written
    warning not to unpack it short. `.paid` and `.tip` need no warning.
    """

    ask: datetime | None = None    # newest unpaid priced outbound, still live
    paid: datetime | None = None   # newest priced outbound he UNLOCKED
    tip: datetime | None = None    # newest INBOUND tip


#: "This fan has no money history." All three fields default to None, so the
#: empty case is the constructor, not a literal anyone can get wrong.
_NO_MONEY = _Money()


async def _human_money_signals(
    account_id: str, fan_ids, now: datetime,
) -> dict[int, _Money]:
    """{fan_id: (live_ask_at, last_paid_at, last_tip_at)} read from the THREAD, not from
    our own bookkeeping — so it sees what the human chatters did.

    The offer engine only ever knew about asks IT made (`content_offers`) and ladders IT
    opened. Every PPV a teammate sends by hand is invisible to it. The consequences all
    landed on the same fan, on one account, on a single day:

      • rhythm scored a thread with a live $45 ask and a $25 purchase in it as a COLD,
        sale-less chat and rolled a 23-minute coffee break — on the hottest thread on
        the roster, while a human closed it. `decide_availability` is explicitly
        break-proof during a live sell; it just never saw the sell.
      • the gate's pacing (`last_ask_at`) didn't know an ask was already on the table.
      • the seller treated a man who had just paid $25 as a rung-0 stranger.

    No new table is needed: a priced outbound row IS the ask, and `is_paid` IS the
    purchase. Both are already in `messages`. Reading them is the whole fix.

    `live_ask_at`  — newest unpaid priced outbound inside _HUMAN_ASK_TTL (a sell is in
                     progress; suppress breaks, and pace the next ask off it).
    `last_paid_at` — newest priced outbound he ACTUALLY unlocked (is_paid), whoever
                     sent it. The 60-min hot window is applied downstream, not here.
    `last_tip_at`  — newest INBOUND tip. Money that arrives as a tip is invisible to
                     every filter above: a tip is `direction="in"` with `is_tip=1` and
                     `price_cents=0` (the amount rides in OF's `tipAmount`, not in our
                     price column), so `price_cents > 0` excludes it and `direction=
                     "out"` excludes it twice over. It is kept SEPARATE from
                     `last_paid_at` on purpose — `_objection` keys its post-purchase
                     LLM window off that field, and a tip is not a content purchase
                     anyone can dispute. Only the rhythm context merges the two.
    """
    ids = [int(x) for x in fan_ids]
    if not ids:
        return {}
    out: dict[int, _Money] = {}
    async with get_session() as s:
        base = [Message.account_id == str(account_id), Message.fan_id.in_(ids),
                Message.direction == "out", Message.is_unsent.is_(False),
                Message.price_cents > 0]
        # A live ask: priced, still locked, and recent. is_paid is NULL on free sends,
        # so test it explicitly rather than trusting falsiness.
        for fid, ts in (await s.execute(
            select(Message.fan_id, func.max(Message.created_at))
            .where(*base,
                   or_(Message.is_paid.is_(False), Message.is_paid.is_(None)),
                   Message.created_at > now - _HUMAN_ASK_TTL)
            .group_by(Message.fan_id)
        )).all():
            out[int(fid)] = _Money(ask=ts)
        for fid, ts in (await s.execute(
            select(Message.fan_id,
                   func.max(func.coalesce(Message.purchased_at, Message.created_at)))
            .where(*base, Message.is_paid.is_(True))
            .group_by(Message.fan_id)
        )).all():
            out[int(fid)] = out.get(int(fid), _NO_MONEY)._replace(paid=ts)
        # Tips share NONE of `base`: they are inbound and unpriced. Their own where.
        for fid, ts in (await s.execute(
            select(Message.fan_id, func.max(Message.created_at))
            .where(Message.account_id == str(account_id), Message.fan_id.in_(ids),
                   Message.direction == "in", Message.is_tip.is_(True),
                   Message.is_unsent.is_(False))
            .group_by(Message.fan_id)
        )).all():
            out[int(fid)] = out.get(int(fid), _NO_MONEY)._replace(tip=ts)
    return out


def _newest(*ts: datetime | None) -> datetime | None:
    """The latest of several optional timestamps (None = absent, never 'now')."""
    real = [t for t in ts if t is not None]
    return max(real) if real else None


async def _fan_msgs_since(account_id: str, fan_id: int, since: datetime | None) -> int:
    """How many messages HE has sent since the last ask was put in front of him (any
    ask — ours or a human chatter's). `since` None → his whole history with us.

    Drives `ask_after_fan_msgs`: the count that says "he has been talking to us this
    long and nobody has ever asked him for anything"."""
    async with get_session() as s:
        where = [Message.account_id == str(account_id), Message.fan_id == int(fan_id),
                 Message.direction == "in", Message.is_unsent.is_(False)]
        if since is not None:
            where.append(Message.created_at > since)
        return int((await s.execute(
            select(func.count()).select_from(Message).where(*where))).scalar_one() or 0)


async def _teaser_sold(account_id: str, fan_id: int, message_id: int) -> bool:
    """Did HER teaser at this message id actually get unlocked? Reads is_paid on that
    one outbound message — the adaptive convo ladder's climb/soften signal. Scoped to
    her own teaser sale by construction (a specific teaser message), never conflated
    with an ai_chatter catalog PPV purchase."""
    async with get_session() as s:
        paid = (await s.execute(
            select(Message.is_paid).where(
                Message.account_id == str(account_id),
                Message.fan_id == int(fan_id),
                Message.message_id == int(message_id)))).scalar_one_or_none()
    return bool(paid)


def _breakproof(ask_at: datetime | None, now: datetime) -> bool:
    """Is an ask recent enough to glue her to this thread? See _ASK_BREAKPROOF_WINDOW:
    the first 30 minutes after a price goes out are the sell; after that, a person is
    allowed to walk away from her phone (with a cover line)."""
    return ask_at is not None and (now - ask_at) <= _ASK_BREAKPROOF_WINDOW


def _greetable(f, v: "_voice.VoiceBlocks") -> str:
    """His greetable name, or the lane's address for a fan we could not name.

    Seven sites did this by hand with `or "babe"` inline. That is not a rare
    branch to leave unlaned: `of_display_name` is empty for ~80% of fans, so on a
    male account the fallback IS the normal path and "babe" was what a dom called
    every unnamed man. One helper so the next site cannot re-type the literal."""
    return (resolve_fan_name(f) if f else "").split("/")[0][:20] or v.fan_address


# One gentle re-engage opener (item 18). {name} → his greetable name, or the
# lane's address (`_greetable`) for the ~80% of fans OF gives us no name for.
# Deliberately templated, not LLM-generated: a nudge is one unsolicited line, so we
# keep it cheap, predictable, and easy to audit.
#
# Written in the non-native register (NONNATIVE_REGISTER) BY HAND. These lines never
# pass through a model, so the prompt block that carries broken grammar everywhere
# else cannot reach them — and apply_nonnative_style is dictionary-only, a provable
# no-op on strings holding none of its words. Baking it into the text is the only
# thing that works. Same reason the space-before-'?' habit is baked at 4-in-15 rather
# than rolled: _NONNATIVE_SPACE_Q_RATE is 0.26 and _run_nudge has no seeded rng.
#
# Not gated on nonnative_ai_chatter: that flag defaults ON (_STYLE_DEFAULT_ON) and
# prod has 10 accounts explicitly on, 7 defaulted on, ZERO off — a second native pool
# would guard a case that does not exist. Add one the day an account turns it off.
#
# Three per beat so a repeatedly-nudged fan does not redraw the same line. 'hiw' (the
# NONNATIVE_MISSPELLINGS fingerprint for 'how') appears ONCE on purpose: the dict
# header measures 'how' at 2.4% of sends, so more than one line here would read as a
# tic rather than a fingerprint.
# Both pools live in `_voice.NUDGE_LINES` — his is native English and drops the
# 🙈/🥺 his own emoji rule bans. The text moved; nothing else did.
_NUDGE_LINES = _voice.NUDGE_LINES[_voice.VOICE_HER]


async def _run_nudge(account_id: str, payload: dict, cfg: dict) -> dict:
    """Item 18 — the delayed one-shot re-engage. Scheduled (enqueue_job, run_at =
    +nudge_after_minutes) the moment a paid offer is made; ONE job ⇒ ONE nudge. On
    fire it re-checks each fan and sends a single gentle line ONLY if he still has
    an OPEN offer (didn't buy), hasn't replied since (else the normal loop owns
    him), and isn't paused/on-cooldown. No-op unless nudge_enabled."""
    fan_ids = coerce_ids(payload.get("nudge_fan_ids"))
    if not (cfg.get("enabled") and cfg.get("cadence_enabled")
            and cfg.get("nudge_enabled")) or not fan_ids:
        return {"status": "skipped", "reason": "nudge_disabled",
                "nudged": 0, "nudge_skipped": len(fan_ids)}
    typing_wpm = await load_typing_wpm(account_id)
    typing_indicator = await load_typing_indicator(account_id)
    async with get_session() as s:
        fan_rows = (await s.execute(
            select(Fan).where(Fan.account_id == str(account_id),
                              Fan.fan_id.in_(list(fan_ids)))
        )).scalars().all()
    fans = {int(f.fan_id): f for f in fan_rows}
    client = await asyncio.to_thread(ax._make_client, account_id)
    _nudge_v = await load_voice_blocks(account_id)

    nudged = skipped = 0
    for fan_id in fan_ids:
        now = datetime.utcnow()
        # Still awaiting a buy? A resolved (bought/expired) offer ⇒ nothing to nudge.
        if not await _open_offers(account_id, fan_id):
            skipped += 1
            continue
        by = await _gather(account_id, {fan_id})
        c = by.get(fan_id)
        if c is None or c.last_dir == "in":
            skipped += 1          # he already replied — the normal loop re-engages him
            continue
        f = fans.get(fan_id)
        if f is not None and f.automation_paused_until and f.automation_paused_until > now:
            skipped += 1
            continue
        if await ax.fan_on_cooldown(account_id, fan_id):
            skipped += 1
            continue
        if not await ax.acquire_fan_lease(account_id, fan_id, _PURPOSE):
            skipped += 1
            continue
        try:
            name = _greetable(f, _nudge_v)
            line = apply_word_restriction(
                random.choice(_nudge_v.nudge_lines).replace("{name}", name))[:_REPLY_MAX_CHARS]
            await hold_with_typing(account_id, fan_id,
                                   typing_delay_seconds(line, typing_wpm),
                                   typing_indicator=typing_indicator)
            result = await asyncio.to_thread(
                lambda p=line: client.send_message(fan_id, p))
            msg_id = result.get("id") if isinstance(result, dict) else None
            if msg_id:
                await write_outbound_attribution(
                    account_id=account_id, fan_id=int(fan_id),
                    message_id=int(msg_id), sent_by_employee_id=None,
                    automation_kind=_PURPOSE,
                    body=str(result.get("text") or line), price_cents=0,
                    created_at=ax._parse_iso(result.get("createdAt")) or now,
                    emit_live=True)
                await _mark_reply_sent(account_id, fan_id, now)
                await ax.start_fan_cooldown(account_id, fan_id,
                                            cooldown_s=_REPLY_COOLDOWN_S)
                nudged += 1
            else:
                skipped += 1
        except Exception:
            skipped += 1
            log.warning("ai_chatter nudge send failed account=%s fan=%s",
                        account_id, fan_id, exc_info=True)
        finally:
            await ax.release_fan_lease(account_id, fan_id)
    return {"status": "ok", "nudged": nudged, "nudge_skipped": skipped, "nudge": True}


async def _load_offer(offer_id: int) -> ContentOffer | None:
    async with get_session() as s:
        o = await s.get(ContentOffer, int(offer_id))
        if o is not None:
            s.expunge(o)
    return o


async def _newer_delivered_offer(account_id: str, fan_id: int,
                                 after: datetime | None) -> bool:
    """A later PAID rung than the one this job was scheduled for → its OWN aftercare/
    post_buy job owns the fan, and this (stale) one must stand down."""
    if after is None:
        return False
    async with get_session() as s:
        row = (await s.execute(
            select(ContentOffer.id).where(
                ContentOffer.account_id == str(account_id),
                ContentOffer.fan_id == int(fan_id),
                ContentOffer.status == "delivered",
                ContentOffer.resolved_at > after).limit(1)
        )).first()
    return row is not None


async def _run_post_buy(account_id: str, payload: dict, cfg: dict) -> dict:
    """§4.4b — fired ~90-240s after a PAID unlock. ALWAYS sends the free post_buy_bridge
    bubble (keeps the scene alive on a conversation, not silence). THEN — only if
    post_buy_rung_enabled AND the §6 brakes pass AND qualify() passes with the PURCHASE
    standing in for fan_spoke_last (an unlock is an event, not a turn — the silent
    buyer was structurally unsellable) — fires ONE next rung after the §3.6 pacing
    floor. NO revenue claim: this ships as a bug fix, not a +30pp promise. Idempotent:
    one job per content_offers.id; the offer is 'delivered', so a re-run just re-bridges
    a fan who hasn't spoken (harmless) and never double-rungs (the ladder open-guard)."""
    offer_id = payload.get("post_buy")
    if not cfg.get("enabled") or offer_id is None:
        return {"status": "skipped", "reason": "post_buy_disabled", "post_buy": True}
    offer = await _load_offer(int(offer_id))
    if offer is None:
        return {"status": "skipped", "reason": "no_offer", "post_buy": True}
    fan_id = int(offer.fan_id)
    now = datetime.utcnow()
    # He replied since he paid → the normal reply loop owns him; a scripted bridge over
    # a live thread is a bot tell. And a newer rung means a newer post_buy job owns him.
    by = await _gather(account_id, {fan_id})
    c = by.get(fan_id)
    if (c is not None and c.last_dir == "in" and c.last_in_at is not None
            and offer.resolved_at is not None and c.last_in_at > offer.resolved_at):
        return {"status": "skipped", "reason": "he_spoke", "post_buy": True}
    if await _newer_delivered_offer(account_id, fan_id, offer.resolved_at):
        return {"status": "skipped", "reason": "superseded", "post_buy": True}

    async with get_session() as s:
        f = await s.get(Fan, (str(account_id), fan_id))
        if f is not None:
            s.expunge(f)
    if f is not None and f.automation_paused_until and f.automation_paused_until > now:
        return {"status": "skipped", "reason": "paused", "post_buy": True}
    if await ax.fan_on_cooldown(account_id, fan_id):
        return {"status": "skipped", "reason": "cooldown", "post_buy": True}
    if not await ax.acquire_fan_lease(account_id, fan_id, _PURPOSE):
        return {"status": "skipped", "reason": "locked", "post_buy": True}

    typing_wpm = await load_typing_wpm(account_id)
    typing_indicator = await load_typing_indicator(account_id)
    client = await asyncio.to_thread(ax._make_client, account_id)
    bridged = rung_fired = 0
    try:
        name = _greetable(f, await load_voice_blocks(account_id))
        bridge = _pack_line("post_buy_bridge", cfg, fan_id, name=name)
        if bridge and await _send_free_bubble(client, account_id, fan_id, bridge,
                                              typing_wpm=typing_wpm,
                                              typing_indicator=typing_indicator, now=now):
            bridged = 1

        # ── The follow-up RUNG. OFF at ship — the only change that adds an unsolicited
        # PRICED message, so it is the genuine consent question (§11). Rides the §6
        # brakes: COMPANION/cooldown window live, or 7d spend cap hit ⇒ no rung.
        if bool(cfg.get("post_buy_rung_enabled")):
            lad = (await _load_ladders(account_id, [fan_id])).get(fan_id)
            cap7 = int(cfg.get("spend_velocity_cap_7d_cents")
                       or upsell.SPEND_VELOCITY_CAP_7D)
            blocked = (
                (lad is not None and lad.status
                 in (upsell.STATUS_STOPPED, upsell.STATUS_COMPANION))
                or (lad is not None and lad.companion_until and lad.companion_until > now)
                or (lad is not None and lad.cooldown_until and lad.cooldown_until > now)
                or (lad is not None and lad.offers_paused_until
                    and lad.offers_paused_until > now)
                or (cap7 and await _paid_cents_7d(account_id, fan_id, now) >= cap7))
            if not blocked:
                rung_fired = await _fire_post_buy_rung(
                    client, account_id, cfg, fan_id, f, lad, now,
                    typing_wpm=typing_wpm, typing_indicator=typing_indicator)
    finally:
        try:
            await ax.start_fan_cooldown(account_id, fan_id, cooldown_s=_REPLY_COOLDOWN_S)
        finally:
            await ax.release_fan_lease(account_id, fan_id)
    return {"status": "ok", "post_buy": True,
            "post_buy_bridged": bridged, "post_buy_rungs": rung_fired}


async def _fire_post_buy_rung(client, account_id: str, cfg: dict, fan_id: int,
                              f: Fan | None, lad: LadderState | None, now: datetime,
                              *, typing_wpm, typing_indicator) -> int:
    """The compact next-rung fired inside _run_post_buy. qualify() runs with
    fan_spoke_last=True — the PURCHASE substitutes for fan_spoke_last AND NOTHING ELSE.
    Prices off the band via smart pricing when on, else the catalog. Sends ONE priced
    attach after the §3.6 45s+ pacing floor. Returns 1 if a rung went out, else 0."""
    scripts, catalog_items = await _load_catalog(account_id)
    if not catalog_items or not await _offer_caps_ok(
            account_id, fan_id, {**cfg, "min_fan_msgs_between_offers": 0}):
        return 0
    offerable = await _offerable_for_fan(account_id, fan_id,
                                         str(cfg.get("offer_mode") or "ppv"),
                                         scripts, catalog_items)
    await _drop_owned(account_id, fan_id, offerable)
    if not offerable:
        return 0
    rung_index = int(lad.rung_index or 0) if lad is not None else 0
    ok, _why = upsell.qualify(upsell.GateCtx(
        fan_id=fan_id, last_inbound_text="unlocked", last_inbound_at=now,
        fan_spoke_last=True,           # §4.4b purchase_is_turn — the ONLY substitution
        status=(lad.status if lad is not None else upsell.STATUS_HOT),
        offers_paused_until=(lad.offers_paused_until if lad is not None else None),
        last_ask_at=(lad.last_ask_at if lad is not None else None)), now)
    if not ok:
        return 0
    # Price the cheapest offerable item (smart pricing on ⇒ off the band; off ⇒ catalog).
    pricing_on = bool(cfg.get("qualification_gate_enabled")
                      and cfg.get("smart_pricing_enabled"))
    quote = None
    item = None
    if pricing_on:
        cfg_row = await _load_cfg_row(account_id)
        media_asks, acct_median, lib_bounds = await _price_context(account_id, cfg_row)
        fstate = await _fan_ladder_state(account_id, fan_id, f, lad)
        pfloor = _proven_floor_cents(fstate, cfg)
        best = None
        for it in offerable.values():
            if it.is_free_teaser:
                continue
            q = _quote_item(account_id, fstate, it, rung_index=rung_index,
                            media_asks=media_asks, median=acct_median, bounds=lib_bounds,
                            escalation_mult=cfg.get("escalation_mult"),
                            max_ask_vs_history_mult=cfg.get("max_ask_history_mult"),
                            proven_floor_cents=pfloor)
            if q is not None and (best is None or q.price_cents < best[1].price_cents):
                best = (it, q)
        if best is None:
            return 0
        item, quote = best
    else:
        item = min((it for it in offerable.values() if not it.is_free_teaser),
                   key=lambda it: int(it.price_cents or 0), default=None)
        if item is None:
            return 0
    mode_eff = _effective_mode(item, str(cfg.get("offer_mode") or "ppv"))
    if mode_eff not in ("ppv", "both"):
        return 0                       # tip-only post-buy rung not fired here
    media = _item_media(item)
    px = int(quote.price_cents) if quote is not None else int(item.price_cents or 0)
    px = max(upsell.OF_PRICE_FLOOR_CENTS, min(px, upsell.OF_PRICE_MAX_CENTS))
    line = (_pack_line("rung_escalate", cfg, fan_id)
            or (await load_voice_blocks(account_id)).unlock_prompt)
    line = apply_word_restriction(line)[:_REPLY_MAX_CHARS]
    kwargs = {"price": px / 100, "locked_text": False, "media_files": media}
    previews = _item_previews(item)
    if previews:
        kwargs["previews"] = previews
    # §3.6 — the priced attach never lands instantly; hold the 45-115s pacing floor.
    if bool(cfg.get("rhythm_enabled")):
        await hold_with_typing(account_id, fan_id, rhythm.ppv_drop_delay(
            random.Random(f"pbdrop:{account_id}:{fan_id}:{item.id}"),
            stalled=bool(cfg.get("filming_stall_enabled"))),
            typing_indicator=typing_indicator)
    try:
        result = await asyncio.to_thread(
            lambda: client.send_message(fan_id, line, **kwargs))
    except Exception as e:
        if _is_price_error(e):
            log.warning("ai_chatter post_buy rung price error account=%s fan=%s",
                        account_id, fan_id)
            return 0
        await skip_unreachable_fan(account_id, fan_id, e, log=log)
        return 0
    msg_id = result.get("id") if isinstance(result, dict) else None
    if not msg_id:
        return 0
    await write_outbound_attribution(
        account_id=account_id, fan_id=fan_id, message_id=int(msg_id),
        sent_by_employee_id=None, automation_kind=_KIND_UPSELL,  # a priced rung
        body=str(result.get("text") or line), price_cents=px,
        created_at=ax._parse_iso(result.get("createdAt")) or now, emit_live=True)
    await _record_vault_sends(account_id, fan_id, media, int(msg_id), px)
    await _ensure_progress(account_id, fan_id, item)
    await _record_offer(account_id, fan_id, item, mode_eff, int(msg_id),
                        quoted_cents=px)
    if quote is not None:
        await _record_quote(account_id, fan_id, item, quote, rung_index=rung_index,
                            kind="rung", message_id=int(msg_id),
                            media_key=upsell.media_key(media))
    await _save_ladder(account_id, fan_id, status=upsell.STATUS_OPEN,
                       rung_index=rung_index + 1, last_ask_at=now, session_idle_at=now)
    log.info("ai_chatter post_buy rung account=%s fan=%s item=%s msg=%s",
             account_id, fan_id, item.id, msg_id)
    return 1


async def _run_aftercare(account_id: str, payload: dict, cfg: dict) -> dict:
    """§7 — fired AFTERCARE_SILENCE_M after a PAID rung. SELF-CANCELS if he spoke or
    bought again since (aftercare answers SILENCE, not a live thread). On fire it sends
    ONE free warm bubble and taps the ladder → TAPPED (she does not poke him again).

    §7.2 gift: with gift_enabled AND >=2 paid rungs this session, it attaches ONE
    genuinely FREE (price=0) UNSEEN piece of media — a gift you are BILLED for is a
    scam, so the word 'free' is code-owned (price=0), NEVER a discounted upsell. NEVER
    after a spend_regret line (§6.5: regret+silence must not be scored satisfied)."""
    offer_id = payload.get("aftercare")
    if not cfg.get("enabled") or offer_id is None:
        return {"status": "skipped", "reason": "aftercare_disabled", "aftercare": True}
    offer = await _load_offer(int(offer_id))
    if offer is None:
        return {"status": "skipped", "reason": "no_offer", "aftercare": True}
    fan_id = int(offer.fan_id)
    now = datetime.utcnow()
    by = await _gather(account_id, {fan_id})
    c = by.get(fan_id)
    if (c is not None and c.last_dir == "in" and c.last_in_at is not None
            and offer.resolved_at is not None and c.last_in_at > offer.resolved_at):
        return {"status": "skipped", "reason": "he_spoke", "aftercare": True}
    if await _newer_delivered_offer(account_id, fan_id, offer.resolved_at):
        return {"status": "skipped", "reason": "superseded", "aftercare": True}

    lad = (await _load_ladders(account_id, [fan_id])).get(fan_id)
    if lad is not None and lad.status == upsell.STATUS_STOPPED:
        return {"status": "skipped", "reason": "stopped", "aftercare": True}
    # A spend_regret / companion fan gets the WARM line but NEVER a gift, and the
    # ladder is already parked — regret+silence must not be scored satisfied+silence.
    regret_active = bool(lad is not None and (
        lad.status == upsell.STATUS_COMPANION
        or (lad.companion_until and lad.companion_until > now)
        or (lad.offers_paused_until and lad.offers_paused_until > now)))

    async with get_session() as s:
        f = await s.get(Fan, (str(account_id), fan_id))
        if f is not None:
            s.expunge(f)
    if f is not None and f.automation_paused_until and f.automation_paused_until > now:
        return {"status": "skipped", "reason": "paused", "aftercare": True}
    if await ax.fan_on_cooldown(account_id, fan_id):
        return {"status": "skipped", "reason": "cooldown", "aftercare": True}
    if not await ax.acquire_fan_lease(account_id, fan_id, _PURPOSE):
        return {"status": "skipped", "reason": "locked", "aftercare": True}

    typing_wpm = await load_typing_wpm(account_id)
    typing_indicator = await load_typing_indicator(account_id)
    client = await asyncio.to_thread(ax._make_client, account_id)
    sent = gifted = 0
    try:
        _ac_v = await load_voice_blocks(account_id)
        name = _greetable(f, _ac_v)
        line = _pack_line("aftercare", cfg, fan_id, name=name) or _ac_v.aftercare
        # §7.2 gift — genuinely FREE unseen media, once, after >=2 paid rungs, never
        # after regret. Picked media-keyed against _seen_media (a mass-blast buy has no
        # content_offers row), and never something he already owns.
        gift_media: list[int] = []
        if (bool(cfg.get("gift_enabled")) and not regret_active
                and await _session_paid_rungs(account_id, fan_id, now) >= 2):
            seen = await _seen_media(account_id, fan_id)
            _scripts, items = await _load_catalog(account_id)
            for it in items:
                media = _item_media(it)
                if (media and not any(m in seen for m in media)
                        and fan_id not in await _owners_of_media(account_id, media)):
                    gift_media = media
                    break
        if gift_media:
            line = apply_word_restriction(line)[:_REPLY_MAX_CHARS]
            await hold_with_typing(account_id, fan_id,
                                   typing_delay_seconds(line, typing_wpm),
                                   typing_indicator=typing_indicator)
            try:
                res = await asyncio.to_thread(
                    lambda: client.send_message(fan_id, line, media_files=gift_media,
                                                price=0))
            except Exception as e:
                await skip_unreachable_fan(account_id, fan_id, e, log=log)
                res = None
            msg_id = res.get("id") if isinstance(res, dict) else None
            if msg_id:
                await write_outbound_attribution(
                    account_id=account_id, fan_id=fan_id, message_id=int(msg_id),
                    sent_by_employee_id=None, automation_kind=_PURPOSE,
                    body=str(res.get("text") or line), price_cents=0,
                    created_at=ax._parse_iso(res.get("createdAt")) or now, emit_live=True)
                await _record_vault_sends(account_id, fan_id, gift_media, int(msg_id), 0)
                await _mark_reply_sent(account_id, fan_id, now)
                sent = gifted = 1
        else:
            if await _send_free_bubble(client, account_id, fan_id, line,
                                       typing_wpm=typing_wpm,
                                       typing_indicator=typing_indicator, now=now):
                sent = 1
        # Hard stop after: TAPPED, no further rung/nudge/discount. A regret fan stays
        # COMPANION (don't overwrite his state); everyone else taps out on the close.
        if sent and not regret_active:
            await _save_ladder(account_id, fan_id, status=upsell.STATUS_AFTERCARE,
                               session_idle_at=now)
    finally:
        try:
            await ax.start_fan_cooldown(account_id, fan_id, cooldown_s=_REPLY_COOLDOWN_S)
        finally:
            await ax.release_fan_lease(account_id, fan_id)
    return {"status": "ok", "aftercare": True, "aftercare_sent": sent, "gifted": gifted}


# ── Human Rhythm — the reply delay (rhythm.py, OFF by default) ───────────────
#
# The ONLY delay today is typing_delay_seconds(text, wpm): every reply lands in a
# few seconds, always, with no variance and no gaps. rhythm.decide() replaces that
# DECISION (the wpm helper still feeds it) and may hand back a `wake_at` instead of
# a delay. A wake_at is NEVER slept through: an inline asyncio.sleep(3h) would hold
# the fan lease past its 900s TTL and burn one of the executor's 4 GLOBAL run slots,
# which starves to_thread and 500s the relay. So the caller releases the lease,
# enqueues a fan-scoped resume job, and moves on — the tick still owes up to 7 other
# fans an answer, and `continue` (not `return`) is what pays them.

async def _load_cfg_row(account_id: str) -> AccountAiConfig | None:
    """The raw AccountAiConfig row — rhythm needs `timezone`/`utc_offset` (the
    creator-local clock), which the merged ai_chatter_config dict doesn't carry."""
    async with get_session() as s:
        row = await s.get(AccountAiConfig, str(account_id))
        if row is not None:
            s.expunge(row)
    return row


async def _sleep_window(account_id: str, tz_offset_minutes: int | None,
                        override, source: str = "default") -> tuple[str, str]:
    """Her sleep window, in creator-local HH:MM. Three sources, in precedence order:

      1. an explicit operator override (`sleep_window`), which always wins;
      2. `source="derived"` — the longest quiet block in her own outbound hour
         histogram, the UI declining to ask a question the data already answers;
      3. otherwise rhythm.DEFAULT_SLEEP, the house night.

    Derivation used to be step 2 unconditionally, which meant the house default was
    only ever reached by an account with too little history to argue — i.e. it was
    a fallback wearing the word "default". It is now opt-in per account, and either
    way the answer is never "always awake": a girl who never sleeps is a bot, and
    the absence of evidence has to fail safe."""
    if isinstance(override, (list, tuple)) and len(override) == 2 and all(override):
        return (str(override[0]), str(override[1]))
    if str(source or "").lower() != "derived":
        return rhythm.DEFAULT_SLEEP
    async with get_session() as s:
        rows = (await s.execute(
            select(func.strftime("%H", Message.created_at), func.count())
            .where(Message.account_id == str(account_id),
                   Message.direction == "out",
                   Message.is_unsent.is_(False))
            .group_by(func.strftime("%H", Message.created_at))
        )).all()
    # created_at is naive UTC; the histogram must be read on the CREATOR's clock or
    # a US account's quiet block lands 5 hours off — i.e. inside her peak window.
    shift = int(round((tz_offset_minutes or 0) / 60.0))
    counts: dict[int, int] = {}
    for h, n in rows:
        try:
            hour = (int(h) + shift) % 24
        except (TypeError, ValueError):
            continue
        counts[hour] = counts.get(hour, 0) + int(n or 0)
    return rhythm.derive_sleep_window(counts)


async def _load_rhythm(account_id: str, fan_ids) -> dict[int, RhythmState]:
    ids = [int(x) for x in fan_ids]
    if not ids:
        return {}
    async with get_session() as s:
        rows = (await s.execute(
            select(RhythmState).where(RhythmState.account_id == str(account_id),
                                      RhythmState.fan_id.in_(ids))
        )).scalars().all()
        s.expunge_all()
    return {int(r.fan_id): r for r in rows}


async def _save_rhythm(account_id: str, fan_id: int, **vals) -> None:
    now = datetime.utcnow()
    async with get_session() as s:
        await s.execute(
            sqlite_insert(RhythmState)
            .values(account_id=str(account_id), fan_id=int(fan_id),
                    updated_at=now, **vals)
            .on_conflict_do_update(index_elements=["account_id", "fan_id"],
                                   set_={**vals, "updated_at": now})
        )


async def _stepout_gate(account_id: str, c: _Cand, rst: RhythmState,
                        cfg: "_stepout.Config") -> bool:
    """HE KEPT WRITING while she was out. True ⇒ end the pause now and answer him.

    A gate function for the same reason `_ghost_gate` below is one: the candidate
    loop is a column of one-line verdicts, and a multi-line decision inlined into it
    is how that loop stops being scannable.

    Only a STEP-OUT may be ended this way — never a sleep window (she is not awake to
    be persuaded) and never an ordinary break. That is the whole reason the step-out
    carries its own context instead of sharing `CONTEXT_UNAVAILABLE` with both.

    `rst.updated_at` is the honest "since she went out" mark HERE, even though every
    write to the row stamps it: while a fan is stepped out the caller `continue`s
    above every other writer, so nothing else touches his row until the pause ends.

    Clears `wake_at` and NOT `deferrals` — left at 1 it makes `decide()` hit the
    one-hop cap and reply inline, which is exactly what "she came back because he
    wrote again" should feel like."""
    if not cfg.on or cfg.break_msgs <= 0 or rst.context != rhythm.CONTEXT_STEPOUT:
        return False
    if not _stepout.persisted(c.in_run, rst.updated_at,
                              msgs=cfg.break_msgs, gap_s=cfg.break_gap_s):
        return False
    await _save_rhythm(account_id, c.fan_id, wake_at=None,
                       context=rhythm.CONTEXT_FREE_CHAT)
    rst.wake_at = None
    rst.context = rhythm.CONTEXT_FREE_CHAT
    log.info("ai_chatter stepout broken by persistence account=%s fan=%s msgs=%d",
             account_id, c.fan_id, len(c.in_run))
    return True


async def _ghost_gate(account_id: str, c: _Cand, rst: RhythmState | None,
                      cycle: tuple[tuple[float, float], ...] | None,
                      now: datetime, *, in_active_sale: bool,
                      paid_at: datetime | None) -> bool:
    """The ghost cycle (`_ghost.py`): True ⇒ she is dark on this fan today, skip
    him. Owns the one column the feature has, so the candidate loop stays a list
    of one-line verdicts instead of growing a second state machine inline.

    Three outcomes, and only one of them is a skip:
      • not in a dark stage        → talk to him (and remember his anchor)
      • dark, but exempt           → restart his cycle (see the three below)
      • dark                       → skip"""
    stored = rst.ghost_anchor if rst is not None else None
    # His FIRST message, not `now`: anchoring the roster the moment the operator
    # ticks the box would put every fan on the same phase and take the whole
    # account dark on one calendar day — a per-fan feature behaving like an
    # account-wide blackout. Thread ages already differ by weeks, so his own
    # history is the stagger. A brand-new fan starts at position 0 and gets the
    # full first chat stage before any silence.
    anchor = stored or c.first_in_at or now
    win = _ghost.window(anchor, now, cycle)

    if win is not None:
        # Was she EVER there during the TALKING RUN this silence follows? Her last
        # word predating that whole run means the thread was already cold and there
        # is nothing to withdraw — ghosting a returning dormant fan stacks days of
        # silence on days of silence, which reads as gone, not busy. Measured
        # against `chat_started_at`, NOT the ghost's own start: she is SUPPOSED to
        # have gone quiet at that boundary, so measuring from there would make
        # every correctly-ghosted fan look dormant and the gate could never fire
        # twice.
        #   …and `her_last_at`, NOT `last_out_at`: the latter moves on ANY outbound,
        # mass blasts included, and a broadcast is not her talking to him. A fan who
        # wrote once, was never answered, and has only been blasted since would
        # otherwise read as "she's been present" — i.e. the system would withhold a
        # reply from someone it has never once replied to. It also means a thread
        # only ever worked by a HUMAN never ghosts, which is the right way round.
        never_present = c.her_last_at is None or c.her_last_at < win.chat_started_at
        # 🚨 THE ONE THAT ACTUALLY PROTECTS A BUYER — `in_active_sale` alone does
        # not. That flag is `recent_payers` (a ONE-HOUR window) plus an OPEN|HOT
        # ladder (120 min, and only when the seller gate is on). A PPV unlock writes
        # no inbound row, so a fan who pays and says nothing is not a candidate at
        # all: the gate never runs, never restarts him, and both windows lapse
        # unused. He writes four hours later, lands mid-dark-stage, and gets silence
        # from the account he just paid.
        #   Scoped to the RUN on purpose — a purchase from three runs ago buys no
        # permanent exemption. Ghosting an established spender who is not currently
        # buying is the operator's actual ask ("a good spender goes a day with no
        # reply and wants one more"); this protects the fresh buy, not the buyer.
        paid_this_run = paid_at is not None and paid_at >= win.chat_started_at
        # A live sale outranks the schedule for the same reason money does: "a
        # ladder stranded mid-sell is the worst outcome in the system", and the
        # RESTART (rather than a one-tick skip) is what stops him dropping back into
        # the same dark stage the moment the sale's own hot window lapses.
        if never_present or in_active_sale or paid_this_run:
            anchor, win = now, None      # restart: he is owed attention, not silence

    # ONE write, whichever way that went. The anchor is frozen on FIRST SIGHT,
    # dark or not: `first_in_at` is the oldest inbound in the loaded message
    # window and that window slides, so an anchor left underived re-phases the fan
    # on a later run — and a fan first seen mid-silence is precisely the one whose
    # phase must not move. (Two earlier cuts got this wrong in opposite ways: one
    # persisted only on the not-dark path, so ghosted fans were never pinned — a
    # live run caught it at 37 fans evaluated, 32 anchors; the fix then wrote the
    # row TWICE for a fan who was both newly-seen and restarted.)
    if anchor != stored:
        await _save_rhythm(account_id, c.fan_id, ghost_anchor=anchor)
    if win is None:
        return False
    log.info("ai_chatter ghost account=%s fan=%s until=%s",
             account_id, c.fan_id, win.ends_at)
    return True


def _recent_realized_s(rst: RhythmState | None) -> tuple[float, ...]:
    """spec §3.4 — the last ~20 REALIZED reply latencies parsed out of
    recent_turns_json, fed back into the next RhythmCtx so the soft fast-reply nudge
    (>=5 of the last 20 under 30s ⇒ floor the next draw at 60s) can see the history."""
    raw = getattr(rst, "recent_turns_json", None) if rst is not None else None
    if not raw:
        return ()
    try:
        arr = json.loads(raw) or []
    except Exception:
        return ()
    return tuple(float(t.get("d", 0.0)) for t in arr if isinstance(t, dict))


def _his_last_latency_s(c: "_Cand") -> float | None:
    """How long HE took to reply last (his inbound − our previous outbound), the
    cosmetic pace-mirror input AND a heat input. None when we don't have both
    timestamps in order."""
    if c.last_in_at is not None and c.last_out_at is not None \
            and c.last_in_at > c.last_out_at:
        return (c.last_in_at - c.last_out_at).total_seconds()
    return None


def _fan_hot(c: "_Cand") -> bool:
    """Is HE hot right now — asking for content OR escalating this very turn? Feeds
    rhythm's scene_heat so a live sext gets ~every-minute replies (prod: an open-offer
    reply median 124s vs 415s otherwise) while a cold thread drifts. Arousal legitimately
    raises HEAT (pacing), so escalation counts here. Read-only over his latest message."""
    body = c.last_body or ""
    return bool(_CONTENT_ASK_RE.search(body) or ESCALATION_RE.search(body))


def _fan_pull(c: "_Cand") -> bool:
    """Is HE explicitly PULLING to buy — a content-ask ("send it", "show me the video")
    or a named price? This is STRICTER than _fan_hot on purpose: it is the ONLY thing
    that lifts a self-declared-broke man's 24h offers-pause, and pure arousal ("so
    horny", "so hard") is NOT consent to be re-priced. A broke man mid-sext trips
    ESCALATION_RE every turn; only a real buy-signal — or a price HE names — proves the
    "broke" wasn't final. (Validated: 37/37 broke-then-buyers bought a FRESH offer.)"""
    body = c.last_body or ""
    return bool(_CONTENT_ASK_RE.search(body) or upsell.detect_stated_cap(body))


async def _record_turn(account_id: str, fan_id: int, rst: RhythmState | None,
                       *, realized_s: float, bubbles: int, informal: bool) -> None:
    """spec §3.4 — record the REALIZED latency (send_time − last_inbound_at) + bubble
    count + informal flag into recent_turns_json at the SEND site, rolling last 20. NOT
    the drawn delay at decide() time: a deferred reply must log its true multi-minute
    gap, and an already-waited-floored reply must log the full inbound→send latency.

    ALSO discharges the one-hop `deferrals` cap, because a sent reply is exactly what
    "the pending reply is over" means. The candidate gate's own comment has always
    said the hop "resets on the send" — but until 2026-08-09 nothing did that except
    the `rhythm_resume` branch, and `deferrals=1` with a NULL `wake_at` was an
    ABSORBING STATE: the only other writer that clears it is guarded by
    `wake_at is not None`, so one lost resume job disabled sleep windows, breaks and
    step-outs for that fan permanently. Measured on prod before the fix: 51 of 86
    rhythm rows on one account sat at 1 with nothing pending."""
    prior = list(_recent_realized_raw(rst))
    prior.append({"d": round(max(0.0, float(realized_s)), 1),
                  "b": int(bubbles), "i": int(bool(informal))})
    await _save_rhythm(account_id, fan_id, deferrals=0,
                       recent_turns_json=json.dumps(prior[-20:]))


async def _replay_draft(client, account_id: str, c: "_Cand",
                        rst: "RhythmState | None", payload: dict, *,
                        typing_wpm, typing_indicator,
                        cover: str | None, informal: bool) -> str:
    """Send the reply we already generated and paid for, instead of buying it twice.

    A post-generation rhythm defer releases the lease and enqueues a resume job.
    Until now the drafted bubbles died with the tick: the wake re-entered run() and
    paid for BOTH LLM calls again for the same turn. The pre-LLM availability check
    exists to stop exactly that waste — its own comment puts it at "~15% of replies"
    — and it only ever closed the asleep/away half. A defer decided AFTER generation
    (a long in-scene delay, or the run's inline budget running out) still threw the
    text away. The cover line already rides the resume payload for the same reason,
    so carrying state across the hop is not a new idea here, just an unfinished one.

    Returns WHY, never a count — the caller tallies the string straight into the run
    summary (the `quota_reasons` idiom), so a counter cannot drift from the branch it
    describes. `_DRAFT_HANDLED` names the outcomes that end the turn; every other one
    is a reason to regenerate, and regenerating is always safe because it is exactly
    the old behaviour. EVERY doubt regenerates: a draft is never sent onto a thread
    that moved, and never sent stale.

    Two things this deliberately does NOT do. It does not replay a PRICED turn: the
    defer site refuses to store a draft carrying an offer or a teaser, because the
    quote, the ladder rung, the ownership dedup and the per-fan offer caps can all
    move in the gap, and re-validating those on the wake is the regenerate path
    wearing a different name. And it paces with the plain typing hold rather than
    `pacing.hold_for_bubble` — the same simplification every other deterministic-line
    path in this file makes, and the between-bubble drift is not worth carrying a
    second copy of the pacing state across a job boundary for.
    """
    raw = payload.get("draft_parts")
    parts = [str(p) for p in raw if str(p).strip()] if isinstance(raw, list) else []
    fan_id = int(c.fan_id)
    now = datetime.utcnow()
    # The inbound the DRAFT answers — stamped at the defer site, which refuses to
    # store a draft without one, so a missing value here means a malformed payload
    # rather than a fan with no history.
    stored_in = ax._parse_iso(payload.get("draft_inbound_at"))
    made_at = ax._parse_iso(payload.get("draft_made_at"))
    if not parts or stored_in is None or made_at is None:
        return "malformed"
    if (now - made_at) > _DRAFT_MAX_AGE:
        return "expired"

    # Checked against the message the draft ANSWERS, not against whatever `_gather`
    # saw this tick — those are the same message only when nothing happened, which
    # is precisely the thing being tested.
    moved = await _thread_moved_on(account_id, fan_id, stored_in)
    if moved:
        return moved

    if cover:
        parts = [str(cover)] + parts
    n = 0
    for part in parts:
        mid = await _send_free_bubble(client, account_id, fan_id, part,
                                      typing_wpm=typing_wpm,
                                      typing_indicator=typing_indicator, now=now)
        if mid is None:
            break
        n += 1

    # Clear the hop whatever happened on the wire. A draft that failed to send is
    # not a fan who should stay parked behind a stale `wake_at` — the next sweep
    # must be free to pick him up, which is what the elapsed-pause branch in the
    # candidate loop already assumes.
    extra = {"last_cover_at": now} if (cover and n) else {}
    await _save_rhythm(account_id, fan_id, wake_at=None, deferrals=0,
                       context=rhythm.CONTEXT_ENGAGED, **extra)
    if not n:
        log.warning("ai_chatter replay sent nothing account=%s fan=%s", account_id, fan_id)
        return "send_failed"
    realized = ((now - c.last_in_at).total_seconds()
                if c.last_in_at is not None else 0.0)
    await _record_turn(account_id, fan_id, rst, realized_s=realized,
                       bubbles=n, informal=informal)
    return "sent"


def _recent_realized_raw(rst: RhythmState | None) -> list[dict]:
    raw = getattr(rst, "recent_turns_json", None) if rst is not None else None
    if not raw:
        return []
    try:
        arr = json.loads(raw) or []
        return [t for t in arr if isinstance(t, dict)]
    except Exception:
        return []


# ── The Offer Engine — the qualification gate + the price ladder (upsell.py) ──
#
# The gate IS the feature: QUALIFICATION drives the sale, price only drives the
# margin (within-fan, asking MORE converts WORSE: -23.4pp). Everything below is
# behind `qualification_gate_enabled` / `smart_pricing_enabled`; with both off not
# one row of ladder_state / ladder_quote / pending_offer is ever written.
#
# NOTHING here may touch `fans.automation_paused_until`. That column is SHARED by
# every automation, so a decline written into it would also blank the fan out of
# welcome / followup / mass — a cross-automation, UI-invisible blackout. A decline
# is LADDER-scoped (ladder_state.offers_paused_until / status) and nothing else.

def _sell_turn(c: "_Cand", lang: str, *, model_says_ask: bool = False) -> sell_lane.Turn:
    """His side of this turn, as the seller reads it. One place, so the closer and
    the seam can never disagree about which message (or which language) is being
    judged — nor about whether the MODEL also read an ask in it (`_sell_signal`),
    which the lane's own `is_ask` ORs with its regex."""
    return sell_lane.Turn(text=c.last_body, at=c.last_in_at,
                          our_last_at=c.last_out_at,
                          fan_spoke_last=(c.last_dir == "in"), lang=lang,
                          model_says_ask=model_says_ask)


async def _load_ladders(account_id: str, fan_ids) -> dict[int, LadderState]:
    ids = [int(x) for x in fan_ids]
    if not ids:
        return {}
    async with get_session() as s:
        rows = (await s.execute(
            select(LadderState).where(LadderState.account_id == str(account_id),
                                      LadderState.fan_id.in_(ids))
        )).scalars().all()
        s.expunge_all()
    return {int(r.fan_id): r for r in rows}


async def _load_profiles(account_id: str, fan_ids) -> dict[int, FanProfile]:
    """gen_info's rich per-fan profile (bio / bullet notes / teases) — the same data
    the chatter's Lines picker + Notes panel show. Fed to the prompt so the AI is as
    informed as a human chatter, not just tag-aware. One query; expunged (read-only)."""
    ids = [int(x) for x in fan_ids]
    if not ids:
        return {}
    async with get_session() as s:
        rows = (await s.execute(
            select(FanProfile).where(FanProfile.account_id == str(account_id),
                                     FanProfile.fan_id.in_(ids))
        )).scalars().all()
        s.expunge_all()
    return {int(r.fan_id): r for r in rows}


async def _save_ladder(account_id: str, fan_id: int, **vals) -> None:
    now = datetime.utcnow()
    async with get_session() as s:
        await s.execute(
            sqlite_insert(LadderState)
            .values(account_id=str(account_id), fan_id=int(fan_id),
                    updated_at=now, **vals)
            .on_conflict_do_update(index_elements=["account_id", "fan_id"],
                                   set_={**vals, "updated_at": now})
        )


async def _close_ladder(account_id: str, fan_id: int, status: str, **extra) -> None:
    """The ONE way a ladder ends — hard stop, tap-out, session TTL, a skip_list row
    (of_restricted / manual_restrict / unreachable all land there), or the account's
    dead-session flag. Resets the rung so a fan who comes back years later opens
    COLD instead of resuming at rung 4 with a $79 ask. Also clears the §5 objection/
    discount state — a new session earns its own cut, it does not inherit the last
    one's 'already cut' bar. companion/cooldown windows are time-based and left to
    lapse on their own (a close must not un-companion a fan who asked to just talk).
    `extra` rides in the SAME upsert — a close that must also stamp a pause (the
    hard decline) stays one atomic write, never close-then-pause in two."""
    await _save_ladder(account_id, fan_id, status=status, rung_index=0,
                       hot_until=None, unpaid_rungs=0, session_idle_at=None,
                       objection_at=None, discount_count=0, **extra)


async def _park_pending_offer(account_id: str, fan_id: int,
                              item: CatalogItem | None, now: datetime) -> None:
    """The gate blocked a price for a TRANSIENT reason (he went quiet / said "k" /
    we spoke last). Park the offer so it fires on his NEXT qualifying inbound: a
    gate that can only DELETE sends cannot beat its own revenue metric. Never
    parked on hard_stop / tapped / declined — those are decisions, not timing."""
    async with get_session() as s:
        await s.execute(
            sqlite_insert(PendingOffer)
            .values(account_id=str(account_id), fan_id=int(fan_id),
                    item_id=int(item.id) if item is not None else None,
                    media_key=upsell.media_key(_item_media(item)) if item else None,
                    created_at=now,
                    expires_at=now + timedelta(days=upsell.PENDING_OFFER_TTL_D))
            .on_conflict_do_nothing(index_elements=["account_id", "fan_id"])
        )


async def _clear_pending_offer(account_id: str, fan_id: int) -> int | None:
    """He qualified — the parked offer fires NOW. Returns its item id (so the same
    piece is re-pitched rather than a random one) and drops the row. Also drops a
    row past its TTL: a 3-week-old parked offer is not an offer, it's a ghost."""
    now = datetime.utcnow()
    async with get_session() as s:
        row = (await s.execute(
            select(PendingOffer).where(PendingOffer.account_id == str(account_id),
                                       PendingOffer.fan_id == int(fan_id))
        )).scalars().first()
        if row is None:
            return None
        item_id = int(row.item_id) if row.item_id is not None else None
        expired = bool(row.expires_at and row.expires_at < now)
        await s.execute(PendingOffer.__table__.delete().where(
            PendingOffer.account_id == str(account_id),
            PendingOffer.fan_id == int(fan_id)))
    return None if expired else item_id


async def _ask_counters(account_id: str, now: datetime) -> tuple[dict[int, int], int, int]:
    """(asks_today_by_fan, account_offers_last_hour, account_offers_today), read off
    ladder_quote — the one place a quote is recorded, so the caps count exactly what
    a fan actually SAW. The churn path is asks, not dollars: a fan who buys nothing
    never trips a spend cap. Rolling windows, not calendar days — a midnight reset
    would let a burst run twice."""
    day_ago, hour_ago = now - timedelta(hours=24), now - timedelta(hours=1)
    async with get_session() as s:
        rows = (await s.execute(
            select(LadderQuote.fan_id, LadderQuote.sent_at)
            .where(LadderQuote.account_id == str(account_id),
                   LadderQuote.sent_at >= day_ago)
        )).all()
    by_fan: dict[int, int] = {}
    hour_n = 0
    for fid, ts in rows:
        by_fan[int(fid)] = by_fan.get(int(fid), 0) + 1
        if ts is not None and ts >= hour_ago:
            hour_n += 1
    return by_fan, hour_n, len(rows)


async def _paid_ppv_facts(account_id: str, fan_id: int) -> tuple[int, int]:
    """(BIGGEST, MOST RECENT) single PPV he has paid, in cents — 0 for either when he
    never has. The two facts every price decision needs, from ONE definition of "he
    paid": the biggest is the history CEILING (never ask >3x it), the most recent is
    the escalation BASE (`want = last_paid × 1.75`).

    One definition on purpose. These were two queries over this same table twenty
    lines apart with DIFFERENT where-clauses, which is the drift `_leash` was
    extracted to end — the moment two predicates both mean "he paid", they answer
    differently and nobody notices.

    Read from PAID messages, never from lifetime_spend_cents: spend is a SUM, and a
    fan who tipped $5 forty times has never once paid $200.

    Mass-blast unlocks COUNT. He handed over the money; that a broadcast rather than
    a 1:1 line prompted it does not un-prove he will pay. (Measured 2026-07-27:
    excluding blasts would zero the proven spend of 100 fans who have demonstrably
    bought, dropping them to the cold-open ceiling. The band deliberately excludes
    blasts because it is inferring what CONTENT is worth from what humans charged;
    this is inferring what a MAN will pay, which is a different question.)"""
    async with get_session() as s:
        paid = (Message.account_id == str(account_id), Message.fan_id == int(fan_id),
                Message.direction == "out", Message.is_paid.is_(True),
                Message.price_cents > 0)
        biggest = (await s.execute(
            select(func.max(Message.price_cents)).where(*paid))).scalar_one_or_none()
        latest = (await s.execute(
            select(Message.price_cents).where(*paid)
            .order_by(func.coalesce(Message.purchased_at, Message.created_at).desc())
            .limit(1))).scalars().first()
    return int(biggest or 0), int(latest or 0)


async def _fan_ladder_state(account_id: str, fan_id: int, f: Fan | None,
                            ladder: LadderState | None) -> upsell.FanState:
    """The two facts the price actually depends on: the LARGEST single PPV he has
    ever paid (the history ceiling — never ask >3x it) and what he JUST paid (the
    escalation base).

    The escalation base used to read the newest PAID `ladder_quote` row, which only
    exists for a CATALOG rung — so a convo-teaser unlock, a hot-thread teaser or a
    human chatter's sale left it empty and the next ask opened COLD. It is now the
    newest paid PPV of any kind, which is a strict superset: all 27 paid quote rows
    in prod resolve to a paid message at the identical price, so nothing that query
    could find is lost by not asking it."""
    max_paid, last_paid = await _paid_ppv_facts(account_id, fan_id)
    # Only a HOT ladder escalates off the last rung — outside the hot window the
    # next ask is a cold open again, not last_paid * 1.75 forever.
    hot = ladder is not None and ladder.status == upsell.STATUS_HOT
    ever = max_paid > 0 or int(getattr(f, "lifetime_spend_cents", 0) or 0) > 0
    return upsell.FanState(fan_id=int(fan_id), max_single_paid_cents=max_paid,
                           last_paid_cents=(last_paid or None) if hot else None,
                           has_ever_paid=ever)


async def _next_tip_ask_cents(account_id: str, fan_id: int,
                              item: CatalogItem, cfg: dict,
                              cap_cents: int | None = None) -> int:
    """The adaptive tip-ask amount (cents) for a TIP-mode offer to this fan.

    Escalates when he UNLOCKED his last tip offer (status 'delivered'), softens
    40–60% when he didn't, floored at his biggest-ever tip. Reads prior tip
    offers + the tip ledger — no new per-fan state column. Consistency is the
    whole point: the caller writes this ONE value onto the item's quote, so the
    manifest the model sees, the recorded tip threshold, and the unlock watcher
    all agree (a mismatch would show the fan one price and unlock at another).

    `cap_cents` (the account's PPV-library max, ≤ $200) overrides the static
    config cap when the caller has the library bounds — so a tip never climbs
    above what the account actually sells (the graded vault tops out at $100)."""
    base = int(item.tip_unlock_cents or 0) or int(cfg.get("tip_ladder_base_cents") or 1000)
    cap = int(cap_cents if cap_cents else (cfg.get("tip_ladder_cap_cents") or 20000))
    cap = min(cap, upsell.OF_PRICE_MAX_CENTS)
    async with get_session() as s:
        last = (await s.execute(
            select(ContentOffer.tip_unlock_cents, ContentOffer.status)
            .where(ContentOffer.account_id == str(account_id),
                   ContentOffer.fan_id == int(fan_id),
                   ContentOffer.mode.in_(("tip", "both")),
                   ContentOffer.tip_unlock_cents > 0)
            .order_by(ContentOffer.offered_at.desc(), ContentOffer.id.desc())
            .limit(1))).first()
        biggest = (await s.execute(
            select(func.max(Transaction.amount_cents)).where(
                Transaction.account_id == str(account_id),
                Transaction.fan_id == int(fan_id),
                Transaction.kind.in_(_TIP_KINDS),
                Transaction.status.in_(("cleared", "pending"))))).scalar_one_or_none()
    last_ask = int(last[0]) if last else 0
    paid_last = bool(last and last[1] == "delivered")
    return tip_ladder.next_tip_ask(
        last_ask_cents=last_ask, biggest_tip_cents=int(biggest or 0),
        paid_last=paid_last, base_cents=base,
        step_mult=float(cfg.get("tip_ladder_step") or 1.5),
        cut_lo=float(cfg.get("tip_ladder_cut_lo") or 0.40),
        cut_hi=float(cfg.get("tip_ladder_cut_hi") or 0.60),
        floor_cents=int(cfg.get("tip_ladder_floor_cents") or 500),
        cap_cents=cap,
        rand=random.random())


async def _buyer_facts(account_id: str, fan_id: int) -> list[str]:
    """His spend history as buyer CONTEXT for the shared prompt — so BOTH the
    chatter and the seller (one engine) know what he has already bought AND
    tipped, reference it warmly, and never talk to a proven spender like a
    stranger. This is the "know the tip stuff were bought and vice versa" seam:
    the manifest already hides owned MEDIA from being re-offered; this adds the
    positive awareness the model can lean on.

    Tips come from the transactions ledger (the same source _tip_sum_since
    trusts), PPV buys from paid messages. Read-only. It deliberately does NOT
    feed the price ladder — the PPV ceiling stays PPV-only by design (a tipper
    is not a proven big-PPV buyer, see _fan_ladder_state); this is context, not
    pricing. Returns [] for a fan with no spend so the prompt stays byte-equal."""
    async with get_session() as s:
        tip_rows = (await s.execute(
            select(Transaction.amount_cents).where(
                Transaction.account_id == str(account_id),
                Transaction.fan_id == int(fan_id),
                Transaction.kind.in_(_TIP_KINDS),
                Transaction.status.in_(("cleared", "pending")))
        )).scalars().all()
        ppv = (await s.execute(
            select(func.count(Message.message_id),
                   func.coalesce(func.sum(Message.price_cents), 0),
                   func.coalesce(func.max(Message.price_cents), 0)).where(
                Message.account_id == str(account_id),
                Message.fan_id == int(fan_id),
                Message.is_paid.is_(True),
                Message.price_cents > 0)
        )).one()
    tip_total = sum(int(x or 0) for x in tip_rows)
    tip_n = sum(1 for x in tip_rows if int(x or 0) > 0)
    ppv_n, ppv_total, ppv_max = int(ppv[0] or 0), int(ppv[1] or 0), int(ppv[2] or 0)
    if tip_total <= 0 and ppv_n <= 0:
        return []
    parts: list[str] = []
    if ppv_n > 0:
        parts.append(f"unlocked {ppv_n} PPV{'s' if ppv_n != 1 else ''} "
                     f"(${ppv_total // 100} total, biggest ${ppv_max // 100})")
    if tip_total > 0:
        parts.append(f"tipped ${tip_total // 100} across "
                     f"{tip_n} tip{'s' if tip_n != 1 else ''}")
    return [
        "he's spent: " + " and ".join(parts)
        + " — talk like you remember it, never like he's never paid."
    ]


# ── v2 safe-state derived facts (spec §5/§6/§7). ALL of these are DERIVED (no new
# column) — read from PAID messages / ladder_quote, exactly as §10.2 requires. They
# are only ever computed when the gate lane is on, so an off flag costs nothing.

async def _paid_cents_7d(account_id: str, fan_id: int, now: datetime) -> int:
    """spec §6.2 — the rolling 7-day PAID PPV total in cents. This is DOLLARS PAID,
    not asks converted: the spend-VELOCITY brake, distinct from the dead buy-COUNT
    stop. Past the account's cap the seller drops to COMPANION for the window."""
    since = now - timedelta(days=7)
    async with get_session() as s:
        total = (await s.execute(
            select(func.coalesce(func.sum(Message.price_cents), 0)).where(
                Message.account_id == str(account_id),
                Message.fan_id == int(fan_id),
                Message.direction == "out",
                Message.is_paid.is_(True),
                Message.price_cents > 0,
                func.coalesce(Message.purchased_at, Message.created_at) >= since)
        )).scalar_one()
    return int(total or 0)


async def _session_paid_rungs(account_id: str, fan_id: int, now: datetime,
                              *, hours: int = 12) -> int:
    """spec §6.2/§7.2 — how many PAID rungs he has bought in the CURRENT session
    (a 12h window is the session proxy — SESSION_TTL is 90min but a multi-buy night
    spans hours of talk between rungs). Counts paid ladder_quote rows. Drives the
    ≥3-buy cooldown and the ≥2-buy free-gift gate."""
    since = now - timedelta(hours=hours)
    async with get_session() as s:
        n = (await s.execute(
            select(func.count()).select_from(LadderQuote).where(
                LadderQuote.account_id == str(account_id),
                LadderQuote.fan_id == int(fan_id),
                LadderQuote.paid.is_(True),
                func.coalesce(LadderQuote.paid_at, LadderQuote.sent_at) >= since)
        )).scalar_one()
    return int(n or 0)


async def _last_inbound(account_id: str, fan_id: int) -> tuple[str | None, datetime | None]:
    """The fan's NEWEST non-unsent inbound (HTML-stripped text, created_at) — what
    may_discount reads on the TIMER path, where there is no live `_Cand`."""
    async with get_session() as s:
        row = (await s.execute(
            select(Message.body, Message.created_at).where(
                Message.account_id == str(account_id),
                Message.fan_id == int(fan_id),
                Message.direction == "in",
                Message.is_unsent.is_(False))
            .order_by(Message.created_at.desc(), Message.message_id.desc())
            .limit(1)
        )).first()
    if row is None:
        return None, None
    return _strip_html(row[0] or ""), row[1]


async def _cuts_asks_last_30d(account_id: str, fan_id: int,
                              now: datetime) -> tuple[int, int]:
    """spec §5.2 — (cuts, asks) over the last 30 days for the discount governor. A
    cut is a ladder_quote with kind='discount'; an ask is any priced rung. Caps how
    RELIABLY an objection pays off (the reinforcer) without a haggle penalty."""
    since = now - timedelta(days=30)
    async with get_session() as s:
        rows = (await s.execute(
            select(LadderQuote.kind).where(
                LadderQuote.account_id == str(account_id),
                LadderQuote.fan_id == int(fan_id),
                LadderQuote.sent_at >= since)
        )).scalars().all()
    asks = len(rows)
    cuts = sum(1 for k in rows if k == "discount")
    return cuts, asks


async def _price_context(account_id: str,
                         cfg_row: AccountAiConfig | None
                         ) -> tuple[dict[int, list[int]], int | None, tuple[int, int]]:
    """({media_id: [prices humans actually charged]}, the account's median ask,
    the library price bounds).

    The band is EMPIRICAL. Content SHOULD constrain price (a 12-min video is not a
    selfie) but it cannot be derived from catalog_items.duration_sec — that column
    is populated on 0 of 9 prod rows, so a duration-derived tier taxonomy would
    classify the entire vault by a NULL. `price_bounds` (ppv_send) stays the ONE
    ceiling authority; nothing here adds a second one.

    ⚠️ THE DEFLATIONARY-SPIRAL FIX (spec §4.2 — L5 FATAL). The band that PRICES the
    1:1 seller must be built ONLY from human 1:1 PAID asks. Once the seller becomes
    the dominant priced-outbound writer, an unfiltered query ingests its OWN asks
    (and every $0.06-rev mass-blast price) into the band, a closed loop with
    negative gain: §4.1 raises the history multiplier to unfreeze the ladder while
    the band underneath silently collapses. So we require:
      • sent_by_employee_id IS NOT NULL  — a HUMAN sent it (the seller/automation is NULL),
      • mass_run_id IS NULL              — not a blast,
      • automation_kind IS NULL          — not any automation's row,
      • is_paid IS TRUE                  — an UNPAID ask is a failed price signal, not a price.
    The seller's own rows and every blast are excluded by construction."""
    asks: dict[int, list[int]] = {}
    all_asks: list[int] = []
    async with get_session() as s:
        rows = (await s.execute(
            select(Message.media_ids, Message.price_cents).where(
                Message.account_id == str(account_id),
                Message.direction == "out",
                Message.price_cents > 0,
                Message.sent_by_employee_id.is_not(None),
                Message.mass_run_id.is_(None),
                Message.automation_kind.is_(None),
                Message.is_paid.is_(True))
        )).all()
    for mids_json, px in rows:
        px = int(px or 0)
        if px <= 0:
            continue
        all_asks.append(px)
        try:
            mids = [int(x) for x in json.loads(mids_json or "[]")]
        except Exception:
            continue
        for m in mids:
            asks.setdefault(m, []).append(px)
    median = sorted(all_asks)[len(all_asks) // 2] if all_asks else None
    raw = getattr(cfg_row, "ppv_library_config_json", None) if cfg_row else None
    try:
        lib = json.loads(raw) if raw else {}
    except Exception:
        lib = {}
    return asks, median, price_bounds(lib if isinstance(lib, dict) else {})


def _proven_floor_cents(fstate: upsell.FanState, cfg: dict) -> int:
    """The proven-spend price floor (cents) for this fan, or 0 when off / no
    history. Opt-in via `proven_spend_floor_enabled`; the floor is his biggest
    single paid PPV × `proven_spend_floor_mult` (default 1.0 → at least what he
    already paid), plus the flat `proven_spend_floor_add_cents` ratchet. Fed to
    _quote_item so cheaper items are skipped and he climbs a tier — a $50 buyer
    gets the $60 video, never the $24 set again.

    The additive term exists because a pure multiplier stalls: at mult=1.0 a fan
    who buys at $8 has a floor of exactly $8 and can be re-offered $8 forever.
    +$1 per sale guarantees the ask always moves, and it does the work down at the
    cheap rungs where a percentage is pennies."""
    if not cfg.get("proven_spend_floor_enabled"):
        return 0
    mx = int(getattr(fstate, "max_single_paid_cents", 0) or 0)
    if mx <= 0:
        return 0
    # Indexed, not `.get(... ) or <literal>`: every cfg reaching here came through
    # _load_config, which merges _DEFAULTS, so the key IS present — and a hardcoded
    # inline default is exactly how this knob drifted (the declared default said one
    # number, the docstring another). A KeyError on an unmerged cfg is the correct
    # outcome; silently substituting a fourth value is not.
    # round, not int(): 0.6 is not representable, so int(1000 * 0.6) truncates to 599.
    return int(round(mx * float(cfg["proven_spend_floor_mult"]))) \
        + int(cfg.get("proven_spend_floor_add_cents") or 0)


def _quote_item(account_id: str, fan: upsell.FanState, item: CatalogItem, *,
                rung_index: int, media_asks: dict[int, list[int]],
                median: int | None, bounds: tuple[int, int],
                # REQUIRED (no default on purpose): this function reads the operator's
                # pricing knobs, so every call site must pass them from cfg. A default
                # here once silently defeated the knobs on the main sell path — a
                # missing arg must be a loud TypeError, not a quiet fallback to 1.75/3.0.
                escalation_mult: float | None,
                max_ask_vs_history_mult: float | None,
                proven_floor_cents: int = 0) -> upsell.Quote | None:
    """The price for ONE item — or None, meaning DO NOT OFFER THIS ITEM (the item
    selector then simply picks a cheaper one). The ladder climbs by UNLOCKING BETTER
    ITEMS, not by a multiplier fighting three caps. `proven_floor_cents` (opt-in)
    skips any item priced below what he already paid, so a proven buyer climbs a
    tier instead of getting the cheap set re-run at cold-open lows."""
    media = _item_media(item)
    key = upsell.media_key(media)
    human = [p for m in media for p in media_asks.get(m, [])]
    band, _src = upsell.derive_band(human_asks_cents=human, account_median_cents=median,
                                    item_price_cents=int(item.price_cents or 0))
    # Seeded per (fan, rung, media): a retry after a failed send re-quotes the SAME
    # price. A fan must never see the same clip at two prices in one thread.
    rng = random.Random(f"quote:{account_id}:{fan.fan_id}:{rung_index}:{key}")
    return upsell.next_price(fan=fan, band=band, last_paid_cents=fan.last_paid_cents,
                             rung_index=rung_index, key=key,
                             account_id=str(account_id), rng=rng,
                             library_bounds=bounds,
                             escalation_mult=escalation_mult,
                             max_ask_vs_history_mult=max_ask_vs_history_mult,
                             proven_floor_cents=proven_floor_cents,
                             # The item's own operator-set price is a floor: the
                             # upseller must never quote a single below the price
                             # the base chatter sells it at. Above it, smart
                             # pricing is free to climb.
                             catalog_floor_cents=int(item.price_cents or 0))


async def _record_quote(account_id: str, fan_id: int, item: CatalogItem | None,
                        q: upsell.Quote, *, rung_index: int, kind: str,
                        message_id: int | None, media_key: str) -> None:
    """EVERY quote is logged — it is the conversion log AND the price experiment's
    instrument. `pre_clamp_cents` + `clamped_by` are the load-bearing columns: a
    quote silently truncated at the library ceiling and recorded as if it were free
    biases every estimate the arm produces, for an unknown subset of fans."""
    async with get_session() as s:
        s.add(LadderQuote(
            account_id=str(account_id), fan_id=int(fan_id), rung_index=int(rung_index),
            media_key=media_key, item_id=int(item.id) if item is not None else None,
            band_lo=int(q.band_lo), band_hi=int(q.band_hi), base_cents=int(q.base_cents),
            arm_mult=float(q.arm_mult), price_cents=int(q.price_cents),
            pre_clamp_cents=int(q.pre_clamp_cents), clamped_by=q.clamped_by,
            kind=kind, message_id=int(message_id) if message_id else None,
            sent_at=datetime.utcnow(), paid=False))


async def _mark_quote_paid(account_id: str, fan_id: int, message_id: int | None,
                           now: datetime) -> None:
    """The unlock landed → mark the rung he actually bought. Anchored on the OFFER
    MESSAGE id, not on 'the newest quote': the fastpath and the ledger can converge
    on an old rung minutes after a newer one went out."""
    async with get_session() as s:
        q = (select(LadderQuote)
             .where(LadderQuote.account_id == str(account_id),
                    LadderQuote.fan_id == int(fan_id),
                    LadderQuote.paid.is_(False)))
        if message_id:
            q = q.where(LadderQuote.message_id == int(message_id))
        row = (await s.execute(q.order_by(LadderQuote.id.desc()).limit(1))).scalars().first()
        if row is None:
            return
        await s.execute(update(LadderQuote).where(LadderQuote.id == int(row.id))
                        .values(paid=True, paid_at=now))


async def _handle_decline(account_id: str, fan_id: int, kind: str,
                          now: datetime) -> None:
    """Three declines, three consequences — never one broad regex with one broad
    pause. (A single _DECLINE_RE scored against the real inbound corpus matched
    4.05% of ALL inbounds and would have tapped out 37.9% of threads on lines like
    "No problem!" and "talk to you a lil bit later beautiful 🥰".)

    Pure state transition — the hard decline's OTHER consequence (the make_right
    apology) is enqueued by the caller, which owns dry_run."""
    if kind == upsell.DECLINE_HARD:
        # Chargeback / report / unsubscribe words. POLICY (07-23): the bot never
        # ghosts a fan over its own classifier verdict — this regex once read a
        # forwarded "unsubscribe and block anyone who…" game as a threat and
        # skip-listed a real fan into permanent silence. The consequence now: the
        # ladder closes to IDLE with a 72h offers-pause (selling stops, talking
        # doesn't — the pause is ladder-scoped and liftable by an explicit pull).
        # ONE upsert: close-then-pause as two writes would leave a crash window
        # where the ladder is idle and UNPAUSED — the exact state the policy
        # forbids. A PERMANENT stop is an OPERATOR's move (a hand-written
        # skip_list row), never the bot's.
        await _close_ladder(account_id, fan_id, upsell.STATUS_IDLE,
                            offers_paused_until=now + timedelta(hours=72))
    elif kind == upsell.DECLINE_SOFT:
        # A poverty plea. Stop SELLING for 24h, KEEP TALKING — he is still here, he
        # is just broke this week, and this is the highest-value moment to be a
        # person and the worst possible moment to be a salesman. Ladder-scoped:
        # writing this into fans.automation_paused_until would silence welcome,
        # followup and mass for him too.
        await _save_ladder(account_id, fan_id,
                           offers_paused_until=now + timedelta(hours=24))
    elif kind == upsell.DECLINE_BARE_NO:
        await _close_ladder(account_id, fan_id, upsell.STATUS_TAPPED)


async def _trigger_make_right_apology(account_id: str, fan_id: int,
                                      reason: str = "hard_decline") -> None:
    """Hand a hard-declining fan to make_right for the ONE de-escalation apology
    turn. The latest inbound message id keys the incident, so a webhook replay or
    the next sweep classifying the same message can't double-apologise.
    Best-effort: losing the apology must never lose the decline handling.

    `reason` is the decline SUB-CLASS, so the apology owns the right mistake — a
    man who says "why did you make me pay for these, they're on your profile"
    must not be answered with "sorry i got carried away with the paid stuff".
    make_right validates it and degrades to the generic wording if it does not
    recognise the name."""
    try:
        async with get_session() as s:
            mid = (await s.execute(
                select(Message.message_id)
                .where(Message.account_id == str(account_id),
                       Message.fan_id == int(fan_id),
                       Message.direction == "in")
                .order_by(Message.created_at.desc(), Message.message_id.desc())
                .limit(1))).scalar_one_or_none()
        await ax.enqueue_job(
            account_id, "make_right",
            payload={"hard_decline": {"fan_id": int(fan_id),
                                      "message_id": int(mid) if mid else None,
                                      "reason": reason}})
        ax.wake_supervisor()
    except Exception:
        log.warning("hard-decline make_right enqueue failed account=%s fan=%s",
                    account_id, fan_id, exc_info=True)


def _pack_line(slot: str, cfg: dict, fan_id: int, *, name: str | None = None,
               price_cents: int | None = None) -> str | None:
    """One line from the account's script pack (UI overrides > shipped defaults), in
    the account's language (cfg._account_lang; English per-slot fallback) and the
    account's voice (cfg._account_voice; "her" unless the row says otherwise).

    Both ride on cfg rather than being threaded as parameters because every caller
    already holds it — and because this is the one send path with no model in the
    loop, so a caller that forgot to pass the voice would send a female line from a
    male creator with nothing able to intercept it.

    `name` defaults to the LANE's address rather than the literal "babe" for the
    same reason. Most callers pass a resolved name, but `rung_escalate` does not —
    so a pack line holding {name} rendered "babe" from a male creator on the one
    slot that fires at the moment of the ask."""
    if name is None:
        name = _voice.blocks(cfg.get("_account_voice")).fan_address
    overrides = cfg.get("script_pack_overrides")
    return script_packs.render(
        slot, rng=random.Random(f"pack:{slot}:{fan_id}:{datetime.utcnow():%Y%m%d%H%M}"),
        name=name, price_cents=price_cents,
        overrides=overrides if isinstance(overrides, dict) else None,
        lang=cfg.get("_account_lang", "en"),
        voice=cfg.get("_account_voice", _voice.VOICE_HER))


# ── Prompt (forked from of_ai_chat._build_messages — adds the sell seam) ─────

# ── WHICH BEAT OWNS THIS TURN ────────────────────────────────────────
#
# ONE ordering, in ONE place, read by BOTH sides: `_build_messages` renders the beat,
# and run() has to know which beat actually fired — to stamp the dare's cooldown, to
# withhold the sticker protocol, to skip the §6.4 one-bubble cap, to decide whether a
# scoreless reply is worth a second call.
#
# ⚠️ IT USED TO BE RESTATED IN SIX PLACES, and that is not a style complaint. run()
# armed the dare ~340 lines above where it disarmed it; the cap said
# `bot_accused_turn and not rate_pic_turn`; the stamp said `image_dare_turn or
# rate_pic_scored`. Every one of those was a fragment of this ladder, re-derived from
# raw booleans. The bug that shipped from it: `bot_accused and not rate_pic` treated ANY
# picture as outranking the brush-off, so a fan who challenged her while sending a photo
# of his DOG fell past §6.4 into the react-only branch and got no answer to the
# accusation at all — the original failure, through a different door. Ask this function;
# never re-derive it.
#
# The tail of the ladder (openers, the info-question goal, plain banter) is NOT a beat —
# it depends on locals only the prompt builder has — so "" means "nothing owns this
# turn, fall through to the ordinary chat tiers".
_TURN_BRUSH_OFF = "brush_off"      # §6.4 — he thinks she's a bot
_TURN_RATE_PIC = "rate_pic"        # he sent something she can rate
_TURN_REACT_PIC = "react_pic"      # he sent something that is NOT him
_TURN_PIC_OFFER = "pic_offer"      # he OFFERED to send one — say yes
_TURN_CONTENT_ASK = "content_ask"  # he asked to buy
_TURN_ESCALATION = "escalation"    # he's leaning in, with something to sell
_TURN_DARE = "dare"                # ask him for a picture — always a callback
_TURN_HOT = "hot"                  # the sexting ladder

# The beats where a gif instead of words is the exact non-reaction they exist to
# replace. On these the sticker protocol is withheld from the prompt entirely, rather
# than generated and thrown away — a filter turns a bad reply into NO reply.
_TURNS_NEEDING_WORDS = frozenset({_TURN_BRUSH_OFF, _TURN_RATE_PIC, _TURN_REACT_PIC,
                                  _TURN_PIC_OFFER})
# The beats that spend the picture play's cooldown. A RATING spends it too: he sent
# one, so daring him for one is asking for what he has already given — and an OFFER
# spends it for the mirror reason: she has just told him to send one, so daring him
# for a picture on the next turn is asking twice for the same thing.
_TURNS_SPENDING_THE_DARE = frozenset({_TURN_DARE, _TURN_RATE_PIC, _TURN_PIC_OFFER})
# ⚠️ THE BEATS THAT MUST NOT CARRY A PRICE — and the reason the selector has to reach
# the ATTACH side, not just the prompt. One statement per member; this block exists to
# be the single place the rule lives, so it must not accumulate the same argument twice.
#
# IN — `_TURN_DARE`. Its own copy ends "It is a dare, not a sale: no price, no offer
#   line", and it sits ABOVE `_TURN_HOT`, so `kind == dare` and `hot_thread == True`
#   coexist where they could not before. The forced-ask trigger, the teaser ladders and
#   the offer-marker acceptance all read `hot_thread` RAW, so without this the same
#   message that asks him for a picture carries a PPV. `seller_off` does not cover it.
#
# OUT — `_TURN_RATE_PIC`: its step 5 IS the close, and it is the warmest turn a thread
#   gets. Selling there is the design, not a leak.
# OUT — `_TURN_BRUSH_OFF`: `seller_off` already carries §6.4. Adding it here broke
#   case_convo_teaser_ignore_brakes_lifts_companion_seller_off, because that operator
#   switch deliberately lifts `seller_off` and a second statement of the rule had no
#   such escape hatch. Stating an invariant twice is not free when the two statements
#   can be overridden differently.
# OUT — `_TURN_REACT_PIC`: "do not make it sexual" is a REGISTER rule, not a no-sale
#   one. Any unrateable picture (his dog, a screenshot of the paywall) is far more
#   common than a dare, and membership here blanked every selling surface on those
#   turns — measured: a hot thread with force_ask went from 1 priced send to 0 the
#   moment he attached a photo of a dog.
#
# IN — `_TURN_PIC_OFFER`, for the DARE's reason and not the react-pic one. This turn is
#   the same move as the dare (get the picture), just reactive instead of initiated, and
#   its copy likewise ends "no price, no offer line". Stapling a PPV to "yes send it"
#   answers a man reaching for his phone with a till — which is precisely the failure
#   that made this beat necessary, so shipping it while still selling on the turn would
#   fix nothing. The sale is not lost, only DEFERRED by one turn: once the picture
#   lands, `_TURN_RATE_PIC` owns the reply and carries the close as its own step 5, at
#   the warmest moment the thread ever reaches.
_TURNS_NOT_SELLING = frozenset({_TURN_DARE, _TURN_PIC_OFFER})


def _turn_kind(*, bot_accused: bool, pic_desc: str, content_ask: bool,
               escalation: bool, image_dare: bool, dare_callback: bool,
               hot_thread: bool, can_sell: bool, pic_offer: bool) -> str:
    # ⚠️ NO DEFAULTS ON ANY INPUT, and `pic_offer` is why the rule is written down.
    # It shipped as `pic_offer: bool = False`, and the default did exactly what a
    # default does: it let a caller that had never heard of the beat keep compiling.
    # `scripts_api.simulate` — the operator's "Test it" — was that caller, so the
    # preview scored "you wanna see my cock?" as a content-ask and rendered her
    # PITCHING while production said "send it, i'll rate it" with no price. An
    # operator tuning copy against that is tuning against a lie.
    #
    # Required keyword-only means the same omission is a TypeError at the call, on
    # the first run of that path, instead of a silent wrong answer. The preview had
    # ALREADY been burned this exact way once (it omitted `kind` entirely and lost
    # the content-ask directive); a default is what let it happen a second time.
    """The beat that owns this turn, or "" for the ordinary chat tiers.

    Pure and total, so both callers get the same answer from the same inputs. Order is
    the product decision; everything else in this module reads it rather than repeating
    it.
    """
    if pic_desc and _is_rateable(pic_desc):
        # ABOVE the brush-off on purpose: when he asks "are you a bot" AND puts himself
        # on the table, a specific read of what he just sent is the one answer a canned
        # reply could not have written. The rating IS the proof.
        return _TURN_RATE_PIC
    if bot_accused:
        # …but only a RATEABLE picture outranks it. A warm word about his dog is not
        # proof of anything, so the accusation still wins there.
        return _TURN_BRUSH_OFF
    if pic_offer:
        # ⚠️ ABOVE THE WALLET, AND THAT IS THE ENTIRE POINT OF THE BEAT.
        #
        # `CONTENT_ASK_RE` matches the bare substring "wanna see", with no reading of
        # who is offering what, so "You wanna see my cock?" already scored as
        # content_ask — a BUYING signal — and this branch would be dead code below it.
        # Prod receipt (Isabelle 326419277, 2026-08-08 01:45:50): he offered, and the
        # engine answered "u keep askn / dont u / tell me more about that highway life
        # first" with an $8 PPV stapled on. It thought HE was the one asking.
        #
        # Ranked BELOW the rating and the brush-off, both deliberately. A picture in
        # HAND always beats a promise of one, and a promise is not proof of anything a
        # bot-checker asked for — only the rating is, which is why `_TURN_RATE_PIC`
        # outranks the accusation and this does not.
        #
        # No cooldown, unlike the dare. The dare rests three days because asking twice
        # unprompted is the tell it exists to avoid; this beat is REACTIVE — he asked,
        # and not answering him is the bug. He is entitled to a yes every time.
        return _TURN_PIC_OFFER
    if content_ask and can_sell:
        return _TURN_CONTENT_ASK
    if escalation and can_sell:
        return _TURN_ESCALATION
    if pic_desc:
        # BELOW the wallet, unlike _TURN_RATE_PIC. The rating carries the close as its
        # own step 5 and is the warmest turn in the thread, so it can outrank an ask.
        # This branch has no close in it AT ALL — it is "say something warm about his
        # dog" — so ranking it above a man saying "send me that video" would answer the
        # strongest buying signal in the engine with small talk.
        return _TURN_REACT_PIC
    if image_dare and dare_callback:
        # ⚠️ THE PRECONDITION IS AN ACCUSATION HE ACTUALLY MADE — nothing else.
        #
        # This beat was briefly armed on a bare hot thread too, and that could not be
        # made safe. `can_sell` only knows about the CATALOGUE; the hot-teaser and
        # convo-teaser ladders are separate selling surfaces chosen by async picks long
        # after this prompt is built, so the selector cannot see them. A dare armed on
        # heat therefore steals turns from a sale it has no way to detect — four
        # existing teaser cases went red proving exactly that, on threads where
        # `can_sell` was False and a teaser was nonetheless about to go out.
        #
        # It was also armed on nothing at all for a while — the eligibility-only rewrite
        # dropped the disjunction and a stranger's first "hi there" got answered with a
        # demand for a nude.
        #
        # So: she dares the man who questioned whether she is real, on a 3-day cooldown,
        # on any account. That is still a large widening of the original beat, which was
        # once-per-fan-forever and unreachable on the three gate-off accounts.
        return _TURN_DARE
    if hot_thread:
        return _TURN_HOT
    return ""


def _build_messages(persona: str, f: Fan, c: _Cand, asked: set[str],
                    history_tail: int = _HISTORY_TAIL,
                    # Arm G's three transforms. Defaults to OFF, so every
                    # existing caller (and every test) builds today's prompt.
                    shape: "_prompt_shape.Shape" = _prompt_shape.OFF,
                    # 🙋 WHERE HER CANON GOES THIS TURN. The facts table is the half of
                    # the persona a fan only needs when he asks — her age, job, city,
                    # family. Her PROSE persona is unconditional: it is who she is on
                    # every turn, so this moves one block, never the whole string.
                    #
                    # 🚨 THREE STATES, AND A BOOLEAN CANNOT HOLD THEM. The first cut
                    # took `bio_asked: bool`, split the canon off unconditionally and
                    # rendered it only when True — so `persona_facts_on_ask_only=False`,
                    # the operator's escape hatch, put her canon in NEITHER message and
                    # deleted it from the prompt outright. "Gate off" and "gate on, he
                    # has not asked" are different prompts and must be sayable apart.
                    #
                    #   "keep" — the canon rides the SYSTEM prompt (gate off = legacy,
                    #            and the default, so every other caller and every test
                    #            builds a byte-identical prompt)
                    #   "hide" — gate on, he has not asked: the canon rides nowhere
                    #   "show" — gate on, he asked: it moves to the USER message
                    canon: Literal["keep", "hide", "show"] = "keep",
                    sell_signal: bool = False,
                    style_on: bool = False,
                    nonnative_on: bool = False,
                    sell: _SellSurface = _NO_SELL,
                    content_ask: bool = False,
                    hot_thread: bool = False,
                    bot_accused: bool = False,
                    # WHICH BEAT OWNS THIS TURN — see _turn_kind, which is the single
                    # place that ordering lives. `escalation`, `image_dare`,
                    # `dare_callback` and `rate_pic` used to be four separate booleans
                    # here whose ONLY job was to carry ladder position; they are its
                    # inputs now, not this function's. `bot_accused`, `content_ask` and
                    # `hot_thread` stay, because each is also read as a FACT about the
                    # turn (the accusation line inside the rating, the promoted close,
                    # the hard mid-scene ask-ban).
                    kind: str = "",
                    painful_on: bool = True,
                    lang: str = "en",
                    profile: "FanProfile | None" = None,
                    ask_every: int = 0,
                    buyer_facts: list[str] | None = None,
                    clock: str = "",
                    sticker_mode: str = "skip",
                    opener: "_openers.Opener | None" = None,
                    v: "_voice.VoiceBlocks" = _voice.HER,
                    custom_owed: bool = False,
                    # TODAY's day, bound to the creator-local hour it is read at.
                    # Defaults to the empty `Day`, which renders "" everywhere — an
                    # account with no day log produces a byte-identical prompt.
                    day: "_daylog.Day" = _daylog.NO_DAY,
                    ) -> tuple[list[dict], list[str]]:
    """Compose the (system, user) pair — of_ai_chat's girly info-gather prompt
    with one structural difference: `sell`. `_NO_SELL` (M2) → the no-offers line
    stays, byte-equal behavior. A live surface (M3) → the offer rules replace it.
    The ask/breather dice, the facts block, and the style variants are copied
    verbatim so the voice can't drift from of_ai_chat's.

    NOTHING HERE KNOWS WHAT IS BEING SOLD. The three directives below pick the
    MOMENT (he asked / he's leaning in / the thread is hot) and splice
    `sell.close` for the mechanics; `sell.intro` and `sell.marker` do the same
    for the opening line and the output contract. That is the whole reason
    `_SellSurface` exists — see its docstring. Adding a way to sell means one
    more construction there, not four more branches here."""
    questions = _questions_still_needed(f, asked)
    question_lines = "\n".join(line for _, line in questions)
    presented = [k for k, _ in questions]

    facts = []
    name = resolve_fan_name(f)
    if name:
        facts.append(f"name/nickname: {name.split('/')[0][:40]}")
    for label, val in (("age", f.his_age), ("city", f.home_city),
                       ("country", f.home_country), ("hobbies", f.hobbies),
                       ("occupation", f.occupation), ("fetishes", f.fetishes)):
        if nonempty(val):
            facts.append(f"{label}: {str(val).strip()[:80]}")
    # gen_info's rich profile — the bio + bullet notes a human chatter reads before
    # replying. This is what makes a bubble land as "she remembers me", not generic.
    if profile is not None:
        if nonempty(profile.short_bio):
            facts.append(f"about him: {str(profile.short_bio).strip()[:400]}")
        if nonempty(profile.bullet_points):
            bp = str(profile.bullet_points).strip().replace("\n", "; ")[:600]
            facts.append(f"notes on him: {bp}")
        # NO tease menu here — see the `personal_lines` note below.
    # Spend/tip history (computed async at the call site) — proven-spender context
    # shared by the chatter and the seller. Empty for a non-spender → prompt
    # stays byte-equal.
    if buyer_facts:
        facts.extend(buyer_facts)
    facts_block = ("\n".join(f"- {x}" for x in facts)
                   if facts else "- (nothing on file yet)")
    # A concrete nudge to WEAVE IN a specific detail — the difference between a bubble
    # that reads as a form letter and one that reads as her, mid-conversation. Only
    # added when there's something to reference, so a profile-less fan's prompt is
    # unchanged.
    #
    # This used to also carry "you may riff on one of these lines the team wrote for
    # him: <every unused tease>". That was a SECOND delivery channel for the same
    # pool `_openers` meters — offered as a menu, unrationed, and above all never
    # recorded, so a line the model lifted from it could be handed back days later as
    # a deliberate opener. Teases now reach the model only through `need_block`,
    # which is paced, framed as a reply rather than an opener, and marked used on a
    # confirmed send. One channel, or the used-set is a suggestion.
    personal_lines: list[str] = []
    if profile is not None and (nonempty(profile.short_bio) or nonempty(profile.bullet_points)):
        personal_lines.append(
            "Work in ONE detail about him from above, like you remember him — "
            "never a list.")
    # HIS KINK IS THE SUBJECT OF THE TURN, NOT A FACT TO WEAVE IN.
    #
    # `fetishes` was already extracted every tick (_extract_and_fill) and already
    # reached the model — as ONE line in the facts block, sitting between his age
    # and his city. Nothing anywhere told the model to DO anything with it, and the
    # nudge above steers at "his job, a hobby, something going on in his life" —
    # the three fields a kink-forward fan is least likely to have filled.
    #
    # Prod receipt (Lucas2 7789837 / fan 106046461, 2026-08-09 04:29→04:52). He
    # wrote four sentences naming exactly what he wanted; his `hobbies` and
    # `occupation` were both "" and his `fetishes` was fully populated. With every
    # target of the nudge empty, the model reached for the only other thing in its
    # window — its own "use ur words" line from 40 minutes earlier — and answered
    # him with "but i need words not emojis" + "tell me what u really want". He had
    # already said "You are not interested in that. So later" one message before.
    # Asking a man to restate the thing he just spelled out is how the thread dies.
    #
    # Clipped at 160 rather than the facts block's 80: that loop is shared by six
    # fields and its width is load-bearing elsewhere, and a kink list is the one
    # value here that routinely runs long enough for 80 to cut mid-item.
    if nonempty(f.fetishes):
        personal_lines.append(
            "HE HAS ALREADY TOLD YOU WHAT HE'S INTO: "
            f"{str(f.fetishes).strip()[:160]}. Talk INSIDE that, his words — NEVER "
            "re-ask what he's into or wants.")
    personal_block = ("\n" + "\n".join(personal_lines)) if personal_lines else ""

    history = c.messages[-history_tail:]
    # The same slice of the parallel id list. `add_message` keeps the two in step, so
    # this only falls back for a _Cand assembled by hand (the unit tests, and any
    # caller that sets `messages` directly): id 0 matches no message, so it degrades
    # to today's prompt instead of labelling the wrong line.
    ids = (c.msg_ids[-history_tail:] if len(c.msg_ids) == len(c.messages)
           else [0] * len(history))
    # Rendered lines FIRST, because `if b` drops empty bodies and `_quotes.render`
    # keys off what the model can actually see.
    lines = [(d, b, mid) for (d, b), mid in zip(history, ids) if b]
    # The quote-reply annotation lives ONLY in this string. `history` stays the raw
    # (direction, body) tuples every gate below reads: `fan_just_asked` is a bare
    # `"?" in`, so a question mark in HER quoted caption would read as his question,
    # and `fan_low_effort` counts his words. Gluing generated text onto what a gate
    # reads is the `[he sent: …]` bug twice over — see _Cand.last_body.
    quote = _quotes.render(c.reply_ctx, lines)
    convo = "\n".join(
        f"{'FAN' if d == 'in' else 'YOU'}: {b}{quote.marks.get(mid, '')}"
        for d, b, mid in lines
    )

    # Ask/breather smoothing — verbatim port of of_ai_chat's dice.
    fan_run: list[str] = []
    for d, b in reversed(history):
        if d == "in" and b:
            fan_run.append(b)
        elif fan_run:
            break
    fan_last_text = " ".join(reversed(fan_run))
    fan_just_asked = "?" in fan_last_text
    fan_low_effort = len(fan_last_text.split()) <= 3 and not fan_just_asked
    ask_streak, last_breather = _recent_ask_pattern(history)
    breather_p = 0.55 if len(presented) <= 2 else 0.33

    ask = bool(question_lines)
    # HARD RULE: never interview a man mid-scene. Enforced in CODE, not by asking the
    # model nicely — the prompt already said "don't ask a get-to-know question" on the
    # escalation path and the model asked one anyway ("so you're a coach huh?", sent to
    # Cody at peak arousal with a $25 PPV unopened on the table). A rule that matters
    # this much cannot live in a paragraph the model is free to ignore.
    if hot_thread:
        ask = False
    # Gentle mode (engaged old fans): an info question at most ~every
    # `ask_every` replies — 1/N per message, everything else stays pure convo.
    # The regular dice below still apply when the throttle lets an ask through.
    if ask and ask_every > 1:
        ask = random.random() < 1.0 / ask_every
    if ask:
        if ask_streak >= 3:
            ask = False
        elif fan_just_asked:
            ask = False
        elif last_breather:
            ask = True
        elif fan_low_effort:
            ask = random.random() >= 0.6
        else:
            ask = random.random() >= breather_p
    if not ask:
        presented = []

    if kind == _TURN_BRUSH_OFF:
        # §6.4 brush-off — he thinks she might be a bot. Defensiveness ("I'm a
        # real person, I promise!") is exactly what a bot does; the humane, effective
        # move is to brush it off and pivot to something HE said. Sell nothing.
        #
        # A RATEABLE picture outranks this (see _turn_kind) — rating it is a strictly
        # better answer than any brush-off, and that branch is told about the accusation
        # so it can answer without naming it. An UNRATEABLE one does not: a warm word
        # about his dog proves nothing, so the accusation still wins here. `seller_off`
        # is set independently in run(), so this opens no path to selling into one.
        need_block = (
            "He thinks you might be a bot — defending is what a bot does. ONE "
            "breezy brush-off, then something HE told you. No sell, no ask."
        )
        presented = []
        ask = False
    elif kind == _TURN_REACT_PIC:
        # HE SENT SOMETHING, AND IT IS NOT HIM. His dog, his worksite, a mountain from
        # the balcony — most inbound media is not a dick pic, and the EXPLICIT rubric is
        # catastrophically wrong for all of it.
        #
        # She still rates it. He sent it to get a reaction and a number is a real one —
        # what changes on this branch is the REGISTER, not the scoring: warm, specific,
        # never sexual. (This comment used to read "no score, no rubric"; that was the
        # design until the operator reversed it on 2026-08-09. The rule is now stated
        # once, in the step-2 string below, and nowhere else.)
        need_block = (
            "HE JUST SENT YOU A PICTURE, NOT of his body — described in the history "
            "as '[he sent: …]'. Name TWO things only someone who looked would "
            "notice, RATE IT OUT OF 10 (dock a point for one, say what earns it "
            "back), ONE line to keep it going. WARM, not sexual, never invent what "
            "he sent. SHORT bubbles."
        )
        presented = []
        ask = False
    elif kind == _TURN_RATE_PIC:
        # HE JUST SENT A PICTURE. Rating it is the whole point of the vision layer and
        # it was the one thing never wired: the description reached the prompt, nothing
        # ever told her to USE it. Prod thread 581112404 is the receipt — he sent one,
        # asked "Is it what you thought?", and got "mmm it's deffinetly something" while
        # a paragraph describing exactly what he sent sat in the same row of the DB.
        #
        # Ranked second, under the bot brush-off and above content_ask/escalation/hot:
        # when a man has just put himself on the table, reacting to THAT is the turn.
        # It does not lose the sale — the close rides on the end of the rating (step 4),
        # which is also the moment he is warmest.
        #
        # The score is a NUMBER on purpose. "that's hot" is what anyone types without
        # looking; "8.5, losing half a point because you're not fully hard" can only be
        # written by something that actually looked — which is why this doubles as the
        # answer to "are you a bot".
        need_block = (
            "HE JUST SENT YOU A PICTURE OF HIMSELF — described in the history as "
            "'[he sent: …]'. React to THAT and stay on it. Name 2-3 things you can "
            "only know by looking (specific beats flattering), GIVE A SCORE OUT OF "
            "10 as a number — never a flat 10, dock a point for one named thing and "
            "say what earns it back — then finish on what you'd do with it, one "
            "filthy present-tense line."
        )
        if sell.close and content_ask:
            # He sent a picture AND asked for something in the same breath. Here the
            # close must NOT be step 5 of 5: pitch compliance on this model collapses
            # when the close is the tail of a long ladder (hot threads, which use the
            # 4-beat version, pitch at ~3%), and an explicit ask is the one signal
            # never worth burying. Rate him fast, then answer what he asked for.
            need_block += (
                "\nHE IS ALSO ASKING FOR CONTENT RIGHT NOW — rate him in two short "
                f"bubbles, then the point of the message: {sell.close}"
            )
        elif sell.close:
            need_block += (
                f"\nTHEN, once the rating lands: {sell.close} The next line of the "
                "same scene — never a product pitch."
            )
        need_block += (
            "\nSHORT bubbles, the way you'd type it. GOOD: "
            '"grooming\'s good, glad ur not fully shaved" / "8.5 — losing half a '
            'point cause ur not all the way hard yet, i wanna see that"\n'
            "Never name anything not in the description."
        )
        if bot_accused:
            # This turn is BOTH: he asked if she's a bot and put a picture on the table.
            # The rating is the proof, so it replaces the brush-off rather than joining
            # it — naming the accusation out loud is the defensiveness §6.4 bans.
            need_block += (
                "\nHe ALSO just questioned whether you're real — don't defend, "
                "don't bring it up: the rating IS the answer."
            )
        presented = []
        ask = False
    elif kind == _TURN_CONTENT_ASK:
        # He's asking for content and there's something to sell: the gather goal
        # yields — this message is the pitch, not another interview question.
        need_block = (
            "HE IS ASKING FOR CONTENT RIGHT NOW — stay on it, answer the ask: "
            f"{sell.close}"
        )
        presented = []
        ask = False
    elif kind == _TURN_ESCALATION:
        # He's leaning in / getting physical with something to sell: stop teasing and
        # convert. Softer than a content-ask (he didn't literally say "show me"), so
        # one flirty line THEN the offer — never a cold price-drop.
        need_block = (
            "HE'S CLEARLY INTO IT RIGHT NOW — the moment to SELL, not tease. Match "
            f"his heat in one short line, then close it: {sell.close}"
        )
        presented = []
        ask = False
    elif kind == _TURN_PIC_OFFER:
        # HE OFFERED. The only correct answer is yes, and it has to be unmistakable —
        # this beat exists because the engine kept answering the offer with everything
        # EXCEPT a yes: a pitch, an interview question, a coy deflection. Three times
        # over three days on the founding thread, and he never once learned that
        # sending it was welcome.
        #
        # "Say yes" is not enough on its own: a coy "maybe i do" IS a yes to a reader
        # and is NOT an instruction to a man holding a phone. He has to be TOLD to
        # send it, in the imperative, in this message. That is the difference between
        # a picture arriving and a thread stalling — and a picture arriving is what
        # every downstream beat here is built to monetise.
        #
        # The rating PROMISE is the hook and it is also a commitment we can keep: the
        # vision layer reads what he sends and `_TURN_RATE_PIC` scores it out of ten on
        # the very next turn. It is gated on `describe_on` at the call site for exactly
        # that reason — promising a rating she cannot deliver is worse than never
        # asking, the same rule the dare follows.
        need_block = (
            "HE JUST OFFERED TO SEND YOU A PICTURE OF HIMSELF, or asked you to rate "
            "him. SAY YES and TELL HIM TO SEND IT — imperative ('send it', 'lemme "
            "see'), never coy: a 'maybe i do' sends nothing. Promise the rating — "
            "what you see, a score out of ten — and say what you want it OF. Match "
            "the thread's register, ONE short message. An invitation, not a sale: "
            "no price, no offer line, no questions."
        )
        presented = []
        ask = False
    elif kind == _TURN_DARE:
        # BEAT 2 of the bot-accusation move. Beat 1 (above) was the breezy joke on the
        # accusation itself; he answered, and NOW she dares him — because she can
        # actually read what he sends (the vision layer splices his picture into her
        # next turn as "[he sent: …]"), so the rating lands specific and a bot-checker
        # gets an answer no canned reply could fake.
        #
        # Split across two exchanges on purpose: joke-then-dare in ONE message is a
        # man defending himself, which is what a bot does. Landing it on his reply
        # makes it a callback — she was still thinking about it — and a photo back is
        # the warmest turn a thread gets. Ranked BELOW a content-ask/escalation: if
        # he's reaching for his wallet, sell; the dare can wait.
        #
        # ⚠️ THE COPY CLAIMS HE QUESTIONED HER, SO THE BEAT MUST REQUIRE THAT. For a
        # while this branch also armed on a merely hot thread, where he had said no such
        # thing, and a woman who invents a conversation he does not remember having is a
        # worse bot tell than the one this beat exists to disprove. `_turn_kind` now
        # gates it on `dare_callback` alone, which is what makes this sentence true.
        need_block = (
            "He questioned whether you're real last message and you laughed it off. "
            "Now DARE him: send a picture right now and you'll tell him exactly "
            "what you see — a bot couldn't. Promise a rating out of ten; filthy "
            "about what you want a picture OF if the thread's explicit, cheeky "
            "if tame. "
            "ONE short message. A dare, not a sale: no price, no offer, no "
            "questions."
        )
        presented = []
        ask = False
    elif kind == _TURN_HOT:
        # ── THE SEXTING LADDER. Reverse-engineered from the threads that actually
        # converted (Kingsley/Santan, 2026-07): a bought PPV lands after ~3.6 fan
        # messages and 0.82 sexual lines from her; an unbought one after 1.0 and 0.14.
        # Every converting thread runs the same four beats — probe, he describes, she
        # mirrors it back hotter, she sells him the thing he just described. The caption
        # is never a pitch; it is the next line of the scene.
        #
        # The one thing that reliably kills it is the move the bot made on Cody: at peak
        # arousal ("you are so fucking sexy") it answered "so you're a coach huh?" — an
        # interview question, mid-scene. Hence the hard ban, enforced in CODE below
        # (ask=False), not merely requested of the model.
        need_block = (
            "THE THREAD IS HOT — a live sexual scene RIGHT NOW. The one way to "
            "throw it away is a get-to-know question (job, day, city, age) — "
            "never. Run the scene:\n"
            "1. STAY IN IT — present tense, first person, dirty, SHORT.\n"
            "2. MAKE HIM SAY WHAT HE WANTS ('what would you do to me if you were "
            "here right now?') — his answer is the whole game.\n"
            "3. MIRROR IT BACK, HOTTER — his words escalated, as if it's already "
            "happening."
        )
        if sell.close:
            need_block += (
                f"\n4. THEN SELL HIM WHAT HE JUST DESCRIBED. {sell.close} Write it "
                "matched to HIS OWN WORDS, as the NEXT LINE OF THIS SCENE — never as "
                f"a product. {v.sell_caption_example} — not 'check out my new video'. "
                "If he hasn't described anything yet, do step 2 first and sell on the "
                "next message."
            )
        presented = []
        ask = False
    elif opener is not None:
        # The gather is done, so this turn works in one gen_info opener — a question
        # mined from something he actually said — instead of the generic banter
        # below. Unlike of_ai_chat, ai_chatter never graduates a fan: when the pool
        # runs dry run() queues a refill and carries on.
        need_block = _openers.need_block(opener)
    elif not question_lines:
        need_block = "You know enough about him now — just chat and flirt naturally."
    elif ask:
        need_block = (
            "YOUR GOAL THIS MESSAGE: find out ONE of these, whichever flows from "
            "what he just said, woven in naturally. If he dodged one, don't re-ask "
            "back-to-back:\n" + question_lines
        )
    elif fan_just_asked:
        need_block = (
            "He just asked you something — answer warmly and briefly, and DON'T "
            "ask back this time."
        )
    else:
        need_block = random.choice(_BREATHER_VARIANTS)

    # Item 15 — sticky name: once we hold a DURABLE name for him (team-curated
    # custom_nickname or an extracted real_name), pin it so the model greets him by
    # it and NEVER re-interviews for his name — killing the "what's ur name" loop
    # even deep into a chat. resolve_fan_name already prefers these, so `name` here
    # is exactly the token to keep using (same split/clip as the facts line).
    have_durable_name = bool(name) and (nonempty(getattr(f, "custom_nickname", None))
                                        or nonempty(getattr(f, "real_name", None)))
    call_him = (
        f"\n\nHIS NAME: call him {name.split('/')[0][:40]} from here on — we already "
        "know it, so NEVER ask his name again; just use it naturally."
        if have_durable_name else "")

    name_dodged = (not have_durable_name and not nonempty(f.real_name)
                   and "name:2" in asked)
    dodge_note = (
        "\n\nIMPORTANT: you asked his name twice and he dodged — do NOT ask his name "
        "again. Pick a playful nickname for him and just use it from now on."
        if name_dodged else "")

    style_extra = ((STYLE_3LINE,) * 2 + (STYLE_BRIEF,) * 2) if style_on else ()
    style = random.choice(_STYLE_VARIANTS + style_extra)
    # A "solo" sticker roll owns the per-message STYLE slot — a bullet buried
    # mid-prompt loses to "YOUR GOAL THIS MESSAGE"/the style line (verified
    # live: 5/5 solo rolls yielded plain text until the directive moved here).
    if sticker_mode == "solo":
        style = ("if a CAT STICKER below fits his last message, reply with ONLY "
                 "the STICKER line, no text. "
                 # This slot is what the closing user line points at ("reply ...
                 # in the STYLE FOR THIS MESSAGE above"), so on a solo roll the
                 # model is being told to write in the style of an instruction —
                 # and on 07-31 it did, verbatim, to a fan. The floor under this
                 # is the marker rule in cat_stickers.parse_marker; this sentence
                 # is the cheap half of the fix.
                 "Never write this instruction out as the message.")

    # style_on gates the humanizer, but NOT the emoji vocabulary inside it: the set
    # a creator types from is a lane fact, not an opt-in. "" for her either way.
    humanizer = f"\n\n{v.humanizer}" if style_on else v.emoji_vocab
    nonnative = f"\n\n{NONNATIVE_REGISTER}" if nonnative_on else ""
    # Sticker protocol enters the prompt only on an allow/solo roll — a model
    # that can't see it can't over-use it (measured 48% attach when always on).
    _sticker_block = cat_stickers.prompt_block(sticker_mode, v.voice)
    stickers = f"\n\n{_sticker_block}" if _sticker_block else ""

    # M3 seam: with no catalog in play the no-offers rule applies (chat-only,
    # current of_ai_chat behavior); with one, a short pointer goes in the intro
    # and the FULL sell block lands as its own section near the end of the
    # prompt (high salience — inlining 20 manifest lines mid-sentence buried it).
    has_sell = sell.live
    # 🚨 THE FALLBACK IS NOT "don't offer pics or videos yet" ANY MORE, and it could
    # not stay: with `prompt_sell_catalog` off this is the DEFAULT line on every
    # account, and it is a falsehood on exactly the turns that matter — the vault ask
    # lane charges him for a pack on the same turn, the hot/convo teaser attaches paid
    # media, and `_manifest_block`'s own comment records the last time this shipped as
    # "AN OUTRIGHT FALSEHOOD ON A LIVE ACCOUNT". What is true post-cut: she never
    # names a price or a piece HERE; anything priced goes out of band, and telling him
    # it does not exist is how she ends up denying the box in front of him.
    offers_line = sell.intro if has_sell else (
        "never name a price or pitch a piece yourself — if he asks to see "
        "something, say yes warmly and let it arrive.")
    sell_section = f"\n\n{sell.block.strip()}\n\n" if has_sell else "\n\n"

    # The prompt clock ("" when the account has no tz configured → byte-equal
    # prompt) — same block as of_ai_chat. "what time is it where you are?" is
    # the classic bot trap, and a model with no clock invents one.
    clock_block = (
        f"RIGHT NOW for you it is {clock} — never claim a different time of "
        "day.\n\n" if clock else "")

    # TODAY's day, read at the creator-local hour. "" when the account has no day
    # log — byte-equal prompt, same discipline as the clock block above. Rendered
    # NEXT TO the clock deliberately: the clock already tells her what time it is,
    # and this tells her what she has been doing since morning. Split across the
    # prompt they read as two unrelated rules; adjacent they read as one situation.
    day_sys = day.system_block(v.voice)

    # Where the canon goes this turn — see the `canon` parameter. The split comes from
    # `_prompt_shape.split_facts`, which is also what `drop_facts` splits on, so the
    # two can never disagree about what "the facts block" is. On "keep" nothing is
    # split at all, and on an account with no canon both halves are already inert.
    #
    # ⚠️ NEW NAMES, never a rebind of `persona`. `persona_register_age(persona)` below
    # still wants the WHOLE persona: the age it reads may live in the canon (a slot
    # rendering "44 year old nurse" matches its prose regex), and rebinding here
    # silently dropped that account's register line back to the 22yo default.
    persona_head, bio_facts = (
        (persona, "") if canon == "keep" else _prompt_shape.split_facts(persona))
    system = (
        # 🚨 TWO NEWLINES, AND IT IS LOAD-BEARING. `compose_persona` ends with its
        # facts table ("THESE ARE THE FACTS ABOUT YOU\n- Age: …"), and a SINGLE \n
        # glued that table to the role sentence below into ONE paragraph as far as
        # `_prompt_shape.blocks()` is concerned — which splits on a blank line. So
        # `drop_facts`, on by default for every account since 2026-08-13, was
        # deleting the whole paragraph: the facts AND "You're texting a fan",
        # "100% human, never a bot or an assistant", the self-disclosure ban, the
        # offers line and "read all, reply to the latest". Measured on a live
        # account's own config: system 3,366 → 2,776 chars, and every one of those
        # rules gone from the prompt without a log line.
        f"{persona_head}\n\n"
        "You're texting a fan — flirting, getting to know him, quietly making "
        "him feel like the only one. 100% human, never a bot or an assistant. "
        # THE BAN, conditional. "don't share your own info unless he asks" is what
        # produced a high-value thread in which she answered a direct question about her
        # day with five bubbles containing nothing about herself.
        # The phrase is pinned by test_daylog (_BAN) and is the only clause standing
        # between the model and free-form self-invention (the 966-turn
        # Argentina→Chile→Córdoba cascade); it is replaced only when she actually
        # has something true to give ("give a little back" likewise pinned, _GIVE).
        + ("Use only what you've learned about him; don't share your own info "
           "unless he asks; " if not day_sys else
           "Use what you've learned about him and give a little back — one short "
           "beat of your day, never a paragraph; ")
        + f"{offers_line} "
        "He may send several texts — read all, reply to the latest.\n\n"
        f"{v.painful_texting + chr(10) + chr(10) if painful_on else ''}"
        f"{clock_block}{day_sys}"
        f"{need_block}{dodge_note}{call_him}\n\n"
        f"STYLE FOR THIS MESSAGE — {style}\n\n"
        # Register (text young and casual), NOT a claim about her age — derived
        # from the persona so a 49yo persona isn't told to text like a 22yo.
        f"HOW YOU TEXT (a real {persona_register_age(persona)}yo {v.texter_noun}, "
        "not an assistant):\n"
        "- Short and casual — lowercase, u/ur. React to what he said first.\n"
        "- VARY it — never reuse an opener, phrase, or emoji from this chat.\n"
        "- At most ONE question, never one he already answered. No paragraphs.\n"
        f"{NO_NARRATION_RULE}"
        "- If he gets explicit early: playfully tease and slow it down — warm, "
        "never preachy.\n"
        f"{_good_examples(f, asked, have_durable_name)}\n"
        f"{ONPLATFORM_GUARDRAIL}\n\n"
        f"{BIO_CONSISTENCY_GUARDRAIL}"
        f"{humanizer}{nonnative}{stickers}"
        f"{sell_section}"
        # The >>OFFER carve-out is granted only by a surface that has ids behind
        # it — never by the bought-out manifest (nothing to point at) and never
        # by the pending block (which bans the marker in its own text).
        + ("Your reply is ONLY the message text — no JSON, quotes, or metadata. "
           "ONE exception: the final >>OFFER line when you pitch a piece "
           "(stripped — he never sees it)."
           if sell.marker else
           "Your reply is ONLY the message text — no JSON, quotes, or metadata.")
        # Without this carve-out the contract line above suppresses the marker
        # entirely — verified live: 4/4 solo rolls produced no STICKER line
        # until the exception was stated here.
        + (" A final STICKER: <tag> line is ALSO allowed (stripped — he only "
           "sees the gif)." if _sticker_block else "")
        # OUTPUT-LANGUAGE block at the very END (prefix-cache safe); "" for en. It also
        # pins the >>OFFER token so a Spanish reply never leaks a translated marker.
        + _language.output_language_directive(lang)
        + _customs.prompt_block(custom_owed, v.voice)
    )
    # TIER B — what she has ALREADY told THIS fan. USER message (per-fan, never
    # prefix-cached) — same placement and reasoning as of_ai_chat's. ai_chatter
    # matters most here: it has NO turn cap, so the 383- and 966-turn threads that
    # produced the contradictions all live in this engine, not of_ai_chat's
    # (_MAX_TURNS=30 / _MAX_FAN_MESSAGES=10 / $1 spend gate).
    claims_block = fan_claims_block(f)
    # 🙋 HER CANON, ONLY WHEN HE ASKED. In the USER message, not the system: it is
    # now per-fan text, and per-fan text in the system prompt fragments the shared
    # cached prefix and costs multiples of its own tokens (the rule `_daylog` and
    # `_pins` both state). "" on every ordinary turn, which is nearly all of them.
    bio_block = f"{bio_facts}\n\n" if (canon == "show" and bio_facts) else ""
    # "He asked about your day — ANSWER it." USER message, because it is keyed to
    # THIS fan: his question, and his own ledger of beats already heard. The system
    # block above only PERMITS the disclosure, and permission alone is inert — against
    # a prompt otherwise dominated by fan-directed rules ("get to know him", "at most
    # one question", "react to what he said"), "you may mention your day" loses, and
    # the model can still legally answer "aw better now that ur here / how was yours?"
    # while satisfying every other rule. "" unless he actually asked AND a beat exists.
    #
    # Paired in `Day.user_block` with the part of her day that overlaps THIS fan: both
    # halves are per-fan, so both belong in the user message, and the system block
    # above is the account-constant half.
    day_user = day.user_block(f, c.last_in_text or "")
    # same placement and reasoning as of_ai_chat's.
    user = (
        f"What you know about him:\n{facts_block}{personal_block}\n\n"
        f"{bio_block}"
        f"{claims_block}"
        f"{day_user}"
        f"Recent conversation (oldest→newest):\n{convo}\n\n"
        # `_quotes.REPLY_NOW` verbatim unless he quote-replied, so the ~93% of turns
        # with no quote build the prompt they always did.
        + quote.tail
    )
    # Arm G. Every transform is off by default and each is a no-op when off, so this
    # call is byte-transparent until an account opts in. Applied HERE, on the
    # assembled strings, because that is the artifact the offline replay measured —
    # `_prompt_shape` is the same module `replay_arms.py` imports for its arms.
    system, user = _prompt_shape.reshape(system, user, shape)
    # The sell signal rides LAST, after the reshape, because it is the one
    # sanctioned exception to the output contract two lines above it ("no JSON,
    # quotes, or metadata") and must read as the amendment to that rule rather
    # than as another directive competing with it. Off ⇒ byte-identical prompt.
    if sell_signal:
        system = f"{system}\n\n{_sell_signal.BLOCK}"
    return ([{"role": "system", "content": system},
             {"role": "user", "content": user}], presented)


# The hot-lead TRINITY (a gap-gated tip→tip→PPV ladder) lived here. It was never
# wired into run(), so it never touched a fan; removed 2026-07-26 rather than left
# as a config key that reads as a feature and does nothing. Spec + verbatim code:
# library/HOTSELL_TRINITY_PARKED.md. Read §3 before restoring it — the house moved
# to PPV-first with tip-asks off, which is what its first two rungs were.


# IS THERE ANYTHING TO RATE? Both patterns are written against the vision describer's
# OWN vocabulary (`inbound_describe._INBOUND_DESCRIBE_PROMPT`), which is told to name
# WHAT it is from a fixed list — "a dick pic, his body/torso, a face selfie, an outfit,
# a screenshot or meme, a place, a pet, an object" — to state "HOW explicit — sfw,
# suggestive, or explicit", and to flag anything that would make a flirty reply awkward.
# So these are not guesses about English; they read the describer's own labels back.
#
# ⚠️ WHY THIS EXISTS: the beat fires on every described inbound, and most inbound media
# is NOT a dick pic. Without this the rubric below scores a fan's dog, a worksite, a
# mountain range or a paywall screenshot out of ten and finishes on "one filthy,
# present-tense line" about it. The old prompt tried to handle that in prose ("if what
# he sent is NOT explicit, rate THAT instead") — which is precisely an instruction to
# rate the dog. It is a code decision, not a phrasing decision.
_DESC_RATEABLE_RE = re.compile(
    r"\b(?:dick\s*pic|penis|cock|erect\w*|circumcis\w*|"
    r"his\s+(?:body|torso|chest|abs)|torso|abs|shirtless|topless|nude|naked|"
    r"bulge|underwear|boxers|outfit|selfie|mirror\s+(?:shot|selfie)|"
    r"suggestive|explicit)\b", re.I)

# …and the ONE thing that makes a flirty reaction wrong whatever else is in frame:
# somebody else is in the picture. That is it.
#
# ⚠️ CHILD / MINOR TERMS ARE DELIBERATELY ABSENT. OnlyFans does not allow those images
# in the first place, so screening for them here is a second lock on a door the platform
# already bolts — and it cost more than it bought: the describer WRITES those words to
# report their absence ("No other people, rings, or children visible"), so the clause
# spent its life mis-firing on the exact dick pics the beat exists for.
#
# Pets, screenshots and memes are gone too, for a different reason: they were never
# about safety, only about "this is not him" — which is `_DESC_RATEABLE_RE`'s job. A
# shirtless man holding his dog is still a shirtless man, and used to fall through to
# the no-score branch because of the dog.
_DESC_OFF_LIMITS_RE = re.compile(
    r"\b(?:another\s+person|someone\s+else|two\s+people|a\s+woman|"
    r"wedding\s+ring|married|clearly\s+not\s+him|not\s+of\s+him)\b", re.I)


# ⚠️ THE DESCRIBER REPORTS ON ITS FLAGS, writing those words to state their ABSENCE —
# "No wedding ring visible", "not explicit". A bare keyword read takes each of those as
# the thing it denies, so both patterns are matched against the description with negated
# spans scrubbed out.
#
# A negator scopes FORWARD to the end of its clause, and not one word further. Two
# earlier attempts each got one shape right and another wrong: dropping the whole clause
# deleted the flag it existed to find, and a fixed word-window could not reach past a
# list. Measured on the 145 real inbound descriptions in prod, this rule stops 7 SFW rows
# ("SFW and not explicit" → a Rottweiler, two worksites, a substation) from reading as
# bodies, and keeps 8 real dick pics that say "No wedding ring visible" from being
# blocked. Worked examples, on flags that still exist:
#   "erect, on a bed. No wedding ring visible."   → the denial is its own clause, so the
#       whole thing is scrubbed and the picture still rates.
#   "a motorcycle in a driveway, not explicit"    → "explicit" sits inside the negated
#       span, so it stops reading as explicit.
#
# ⚠️ Known gap, deliberately left: two OFF-LIMITS alternatives contain their own negator
# ("clearly not him", "not of him"), so the scrub eats them before the pattern sees them
# and they cannot fire. Reported round 6; not fixed here because this pass is comments
# only. Fix by matching those two on the RAW description, not the scrubbed one.
_DESC_NEGATOR = r"(?:no|not|none|nothing|nobody|without|isn'?t|aren'?t|non)"
_DESC_NEGATED_SPAN = re.compile(rf"\b{_DESC_NEGATOR}\b[^.;]*", re.I)


def _desc_says(pattern: "re.Pattern[str]", d: str) -> bool:
    """Does the description AFFIRM this, rather than deny it?"""
    return bool(pattern.search(_DESC_NEGATED_SPAN.sub(" ", d)))


def _is_rateable(desc: str) -> bool:
    """Is this description of HIM, and safe to flirt with?"""
    d = str(desc or "")
    return (_desc_says(_DESC_RATEABLE_RE, d)
            and not _desc_says(_DESC_OFF_LIMITS_RE, d))


# A rating has to carry a NUMBER. Four shapes, because there is no one place a texter
# puts a score: the unambiguous "8.5/10" form; a decimal used as the verdict; a verdict
# FRAME carrying the number ("id give that an 8 honestly", "im calling it a 7"); and a
# bare number opening a bubble ahead of its justification ("9, losing a point cause…").
#
# BOTH error directions cost, which is why this is fussier than it looks:
#   • a FALSE POSITIVE skips a needed re-ask and ships a scoreless reply. Prices live in
#     0-10 ("$3, worth it" — the house floor), the rubric asks for LENGTH so "a solid 8
#     inches" reads as a score, and a model echoing the prompt's own numbered steps
#     opens a line with "1.". Hence the currency lookbehind, the terminator on every
#     bare-number branch, and the `(?!\.\s)` that rejects a numbered-list item.
#   • a FALSE NEGATIVE is not free either: it buys a second call AND replaces a perfectly
#     good rating with the re-ask's. Hence the verdict frames and the dash/ellipsis family
#     in the terminator — the prompt's own taught example uses an em-dash and the
#     humanizer will happily emit an en-dash instead.
_RATING_N = r"(?:10|\d)"
_RATING_DEC = r"(?:10|\d)[.,]\d"
_RATING_NOT_DEC = r"(?![.,]?\d)"          # "2" of "2.5 hours" is not a score
# What follows a verdict: punctuation, the end of the bubble, the next bubble, the
# deduction clause — or one of the little tails she actually types after a score.
_RATING_END = (r"(?=\s*[,.;:!?\u2013\u2014\u2026-]|\s*$|\s*/"
               r"|\s+(?:losing|loses|lose|minus|off|cause|cuz|bc|but|and|for\s+me|tbh"
               r"|ngl|honestly|outta|out\s+of|deducted|docked"
               r"|this\s+time|for\s+sure|no\s+question|from\s+me|babe|imo|fr)\b)")
_RATING_FRAME = (r"(?:giv(?:e|es|ing)|gave|gets?|say|sayin|id\s+say|call(?:ing|in)?\s*it"
                 r"|rate|rating|scor(?:e|ing)|thats|that's|its|it's|youre|you're|ur"
                 r"|solid|strong|honestly|is|was)")
# Spelled out. She types like a person, and "solid nine babe" / "nine out of ten" are
# ratings by any reading — a MISS costs a call AND swaps a good reply for the re-ask's.
_RATING_WORD = (r"(?:zero|one|two|three|four|five|six|seven|eight|nine|ten"
                r"|eight\s+and\s+a\s+half|nine\s+and\s+a\s+half)")
_RATING_SCORE_RE = re.compile(
    rf"\b{_RATING_N}(?:[.,]\d)?\s*(?:/\s*10|out\s+of\s+10|outta\s+10)\b"
    rf"|(?<![$\u00a3\u20ac\d]){_RATING_DEC}\b{_RATING_END}"
    rf"|\b{_RATING_FRAME}\b[^\n/]{{0,18}}?\b(?:an?\s+)?(?<![$\u00a3\u20ac])"
    rf"{_RATING_N}(?:[.,]\d)?{_RATING_NOT_DEC}\b{_RATING_END}"
    rf"|(?:^|/\s*)\s*(?<![$\u00a3\u20ac]){_RATING_N}(?:[.,]\d)?{_RATING_NOT_DEC}"
    rf"(?!\.\s)\b{_RATING_END}"
    rf"|\b{_RATING_N}\s+(?:this\s+time|for\s+sure|no\s+question|from\s+me)\b"
    rf"|\b{_RATING_WORD}\s+(?:out\s+of|outta)\s+ten\b"
    rf"|\b(?:solid|strong|a|an)\s+{_RATING_WORD}\b{_RATING_END}",
    re.I | re.M)


async def _scored_or_re_asked(raw: str, msgs: list[dict], *, model: str,
                              account_id: str, fan_id: int) -> tuple[str, bool, bool]:
    """(the reply to send, a re-ask was PAID FOR, the re-ask RESCUED it).

    Two booleans and not one: a counter that ticks only when the second call worked
    reads as "the retry never fires and costs nothing" during the week it is firing on
    every turn and failing. The operator needs the spend and the hit-rate separately.

    A RATING WITHOUT A NUMBER IS NOT A RATING, and that is enforced here rather than
    asked for in a paragraph. The prompt already says "give a score out of 10" and the
    model still answers "ok that's actually impressive" when the preceding turns were
    coy: replayed against prod history it complied 10/10, but inside a full thread she
    had spent deflecting it dropped the score once in two runs. Conversational momentum
    beats an instruction, and this is the one beat where the number IS the product —
    it is the part a bot cannot fake.

    ONE re-ask, so the cost is a second call on a rare turn rather than on every reply.
    If the re-ask also comes back bare, the original ships: a weak reaction still beats
    silence, which is what the whole incident was about.

    `raw` arrives with the protocol markers ALREADY parsed off, and the re-ask is
    stripped the same way, because it may only rewrite WORDS. The offer decision was
    made by the first pass with the whole prompt in front of it, so a re-ask that
    happens to omit an `OFFER:` line must not silently drop a priced attachment, and
    one that invents a line must not silently create a sale nobody chose.

    Raises LLMCapExceeded — stopping the sweep is the caller's decision, not ours."""
    # `not raw` short-circuits the one path guaranteed to send nothing anyway: an empty
    # draft trivially has no number, and re-asking it bills a second full-prompt call
    # with an empty assistant turn just to produce a second empty reply.
    if not raw or _RATING_SCORE_RE.search(raw):
        return raw, False, False
    log.info("ai_chatter rating had no score account=%s fan=%s — re-asking",
             account_id, fan_id)
    res = await llm_client.chat(
        model=model,
        messages=msgs + [
            {"role": "assistant", "content": raw},
            {"role": "user", "content":
             "That's not a rating — you didn't give him a number. Say it again "
             "properly: name two or three things you can actually see in what he "
             "sent, then score it out of 10 and say what cost him the missing "
             "points. Short bubbles, same voice, and the SAME LANGUAGE you were "
             "already writing in."},
        ],
        purpose=_PURPOSE,
        account_id=account_id,
        fan_id=fan_id,
        temperature=_REPLY_TEMPERATURE,
    )
    retry, _ = _parse_offer_marker((res.content or "").strip())
    retry, _ = cat_stickers.parse_marker(retry)
    if _RATING_SCORE_RE.search(retry):
        return retry, True, True
    return raw, True, False


# The bot-accusation dare is a TWO-BEAT move (see _build_messages): beat 1 is the
# breezy joke on the accusation itself, beat 2 is "send me one and I'll rate it" on
# his NEXT reply. This stamp marks beat 2 as spent so it never repeats — daring the
# same man twice is the tell he was fishing for.

# How long an UNANSWERED picture still owns the turn. The description clears on her
# outbound, which is the honest boundary — but there are several ways she never sends
# one (ghost cycle, paused, quarantined, an empty reply dropped, the daily cap), and
# "HE JUST SENT YOU A PICTURE" about a photo from Tuesday is its own kind of bot tell.
_PIC_DESC_TTL = timedelta(hours=12)

_BOT_DARE_KEY = "_bot_dare"
# How long a dare rests before she may dare the same man again. Long enough that it
# can't read as a script inside one conversation, short enough that a thread which
# goes cold and comes back can use the play again.
_BOT_DARE_COOLDOWN = timedelta(days=3)


def _bot_dare_recent(f: Fan | None, now: datetime) -> bool:
    """Has this fan been dared for a picture RECENTLY?

    Was once-per-lifetime. That made the play fire at most once per man and then
    never again, so a thread that went cold and came back hot months later could
    not use the warmest opening move it has. A cooldown keeps the original point
    of the stamp — daring the same man twice in one sitting is the tell he was
    fishing for — without retiring the beat permanently.

    An unparseable or missing stamp reads as "not recent", which fails toward
    daring him; the cooldown is a politeness window, not a safety gate.

    Pure, and takes the row the sweep already loaded — this is asked on every turn
    of every fan, so a DB round trip here would be one query per fan per tick for a
    boolean we're already holding."""
    at = fan_state(f, _BOT_DARE_KEY).get("at")
    if not at:
        return False
    try:
        return (now - datetime.fromisoformat(str(at))) < _BOT_DARE_COOLDOWN
    except (TypeError, ValueError):
        return False


async def _bot_dare_mark(account_id: str, fan_id: int) -> None:
    """Stamp the dare as spent for this fan until the cooldown lapses."""
    await set_fan_state(account_id, fan_id, _BOT_DARE_KEY,
                        {"at": datetime.utcnow().isoformat()})


# ── The automation ───────────────────────────────────────────────────────────

@register("ai_chatter")
async def run(account_id: str, payload: dict, *, run_id: int) -> dict:
    payload = payload or {}
    dry_run = bool(payload.get("dry_run"))
    force_ids = coerce_ids(payload.get("force_ids"))
    only_fan_ids = coerce_ids(payload.get("only_fan_ids"))
    # Fans flagged as buying-intent by the caller (e.g. the inbound-image hook: a
    # fan sending US a photo IS a buying signal, but it carries no text the intent
    # regexes can match). In closer (intent_only) mode these engage like an explicit
    # content-ask — every other gate (spend, cooldown, lease, fan-spoke-last) still
    # applies. Empty by default → no behavior change for normal sweeps.
    intent_fan_ids = coerce_ids(payload.get("intent_fan_ids"))
    history_tail = int(payload.get("history_tail") or _HISTORY_TAIL)

    cfg = await _load_config(account_id)
    # Item 18 — a scheduled re-engage nudge rides the same job kind; handle it and
    # return before the normal sweep machinery (its own gating lives in _run_nudge).
    if payload.get("nudge_fan_ids"):
        return await _run_nudge(account_id, payload, cfg)
    # §4.4b / §7 — the post-buy bridge/rung and the aftercare/gift ride the same
    # ai_chatter job kind (NO new job kinds, per §3.8). Their own gating lives inside.
    if payload.get("post_buy") is not None:
        return await _run_post_buy(account_id, payload, cfg)
    if payload.get("aftercare") is not None:
        return await _run_aftercare(account_id, payload, cfg)
    if not cfg.get("enabled"):
        # Paid-but-undelivered protection: an account can be disabled while
        # offers are still OPEN (incident stop, config flip). A fan who PAYS
        # one of those must still get his delivery — the unlock watcher runs
        # even when disabled; only conversation + new offers stop.
        if await _open_offers(account_id):
            client = await asyncio.to_thread(ax._make_client, account_id)
            offer_stats = await _resolve_open_offers(
                account_id, client, cfg, dry_run=dry_run,
                only_fan_ids=only_fan_ids or None)
            return {"status": "skipped", "reason": "disabled", **offer_stats}
        return {"status": "skipped", "reason": "disabled"}
    mode = str(payload.get("mode") or cfg.get("mode") or "backup")
    sla_s = max(0, int(payload.get("sla_minutes") or cfg.get("sla_minutes") or 0)) * 60
    gate_cents = int(cfg.get("max_lifetime_spend_cents") or 0)
    payers_only = bool(cfg.get("payers_only"))
    resume_h = max(0, int(cfg.get("resume_after_manual_hours") or 0))
    max_replies = int(payload.get("max_replies") or cfg.get("max_fans_per_tick") or 8)

    model = await resolve_model(account_id, _PURPOSE, payload.get("model"))
    typing_wpm = await load_typing_wpm(account_id)
    typing_indicator = await load_typing_indicator(account_id)
    # Human typing pacing (pacing.py) — the gaps BETWEEN the bubbles of one reply,
    # and what the "...is typing" bar does during them. Ships OFF: with `enabled`
    # False every delay below is byte-identically what it was, no rng is drawn, and
    # hold_with_typing takes its old single-phase path.
    pace_cfg = pacing.PaceConfig.from_cfg(cfg)
    style_on = (await load_style_flags(account_id))[_PURPOSE]
    typo_on = (await load_typo_flags(account_id))[_PURPOSE]
    # PHASE 2 pre-send consistency check — OFF unless explicitly enabled.
    consistency_on = (await load_consistency_flags(account_id))[_PURPOSE]
    nonnative_on = (await load_nonnative_flags(account_id))[_PURPOSE]
    # Space-before-"?" — its own tri-state key, read independently but only
    # ever APPLIED inside the non-native block below, so it can narrow that
    # register and never widen it.
    spacing_on = (await load_spacing_flags(account_id))[_PURPOSE]
    # Account-wide emoji strip. ai_chatter ignored this setting entirely until the
    # send chokepoint was extracted and made the omission visible — 7 of 12 live
    # accounts had it ON, so the operator asked for no emojis and the engine that
    # answers most of the messages kept sending them.
    strip_emoji_on = await load_strip_emojis(account_id)
    painful_on = await load_painful_texting_flag(account_id)  # brevity/emotion framing (default ON)
    # The closer answers in ONE call; the extract narrows to her own claims.
    extract_claims_only = bool(cfg.get("closer_extract_claims_only", True))
    bio_gate_on = bool(cfg.get("persona_facts_on_ask_only", True))
    prompt_sells = bool(cfg.get("prompt_sell_catalog", False))
    stickers_on = await load_cat_stickers_flag(account_id)    # cat reaction gifs (default ON)
    sticker_skip_w, sticker_solo_w, sticker_gap_min = \
        await load_cat_sticker_tuning(account_id)             # per-account rate knobs
    account_lang = await _language.load_account_language(account_id)  # output language + guard gate
    # Whose voice this account writes in AND whether it may promise a CUSTOM —
    # one bundle off ONE row read, resolved once per run like the language above.
    # The two axes stay independent (some female accounts sell voice notes, some
    # male ones will not); they just resolve together so no engine can take half.
    # NULL voice + customs off → the manifest keeps its "never customs" ban and
    # every prompt below is byte-identical to what shipped.
    voice_blocks = await load_voice_blocks(account_id)
    max_bubbles = STYLE_MAX_BUBBLES if style_on else 2
    persona = await _load_persona(account_id)

    # Cadence controller (items 10/17/18/21) — OFF unless the account opts in.
    # The deepen phase (gen_info openers) and how often it may ride a reply. Read
    # once per run, like every other style/cadence knob, rather than per candidate.
    openers_on = bool(cfg.get("profile_openers_enabled"))
    try:
        openers_rate = float(cfg.get("profile_openers_rate"))
    except (TypeError, ValueError):
        openers_rate = _openers.DEFAULT_RATE
    openers_rate = min(1.0, max(0.0, openers_rate))
    cadence_on = bool(cfg.get("cadence_enabled"))
    nudge_on = cadence_on and bool(cfg.get("nudge_enabled"))
    nudge_min = int(cfg.get("nudge_after_minutes") or 0)
    session_gap_min = int(cfg.get("session_gap_minutes") or 0) if cadence_on else 0
    # Item 21c — the daily quota rides cadence (it is the ceiling above the burst cap,
    # not a lane of its own). `daily_quota_enforce` decides whether a hold actually
    # withholds a reply or is only counted: it ships computing-and-logging so a day of
    # real traffic can prove who would have been throttled before anyone is.
    quota_on = cadence_on and bool(cfg.get("daily_quota_enabled"))
    quota_enforce = quota_on and bool(cfg.get("daily_quota_enforce"))

    # Old-fan engagement (opt-in): lift the `old_fan_pre_ai` flag and remember
    # who it covered — those fans get the gentle ask cadence in the prompt.
    engage_old = bool(cfg.get("engage_old_fans"))
    old_q_every = max(1, int(cfg.get("old_fan_question_every") or 10))

    # ── The two new lanes. BOTH OFF by default: every branch below is guarded, so
    # with the flags false this function does exactly what it did before — same
    # delay (typing_delay_seconds), same prices (the catalog's), no new table rows.
    gate_on = bool(cfg.get("qualification_gate_enabled"))
    # Smart pricing without the gate would price a message we should never have
    # sent — a better number on a worse decision. It rides the gate on purpose.
    pricing_on = gate_on and bool(cfg.get("smart_pricing_enabled"))
    rhythm_on = bool(cfg.get("rhythm_enabled"))
    rhythm_no_sleep = bool(cfg.get("rhythm_no_sleep"))
    # Parsed ONCE per run, not per fan: it is account-level config, and a malformed
    # curve should be one parse (→ None → the shipped bands) rather than a silent
    # re-parse inside every RhythmCtx.
    rhythm_curve = rhythm.parse_pace_curve(cfg.get("rhythm_pace_curve"))
    # The ghost cycle rides ON rhythm rather than beside it: it is the same
    # "she's a person with a life" lane at a different timescale, and its only
    # state lives in the rhythm_state row that `rhythm_on` already loads. Off ⇒
    # the cycle is never even parsed and not one fan is touched.
    ghost_on = rhythm_on and bool(cfg.get("rhythm_ghost_enabled"))
    ghost_cycle = _ghost.parse_cycle(cfg.get("rhythm_ghost_cycle")) if ghost_on else None
    # ── The flat reply bonus and the step-out ride ON rhythm: with rhythm off the
    # sampler is never called, so neither can move a single reply.
    reply_bonus_s = max(0.0, float(cfg.get("rhythm_reply_bonus_s") or 0.0))
    # …the one-bubble return DOES NOT, deliberately. It is a bubble-SHAPE knob, and
    # this codebase keeps shape independent of rhythm's latency switch — the same
    # ruling `pacing_enabled` carries ("rhythm owns bubble 0's latency, pacing owns
    # bubbles 1+, and an account may want either without the other"). Set it to 0 to
    # turn it off; `rhythm_enabled=False` will not.
    return_single_s = max(0.0, float(cfg.get("rhythm_return_single_bubble_s") or 0.0))
    stepout = _stepout.Config.from_cfg(cfg, enabled=rhythm_on)
    # §3.7 — the "im filming it rn" active fiction. Default OFF; when on it only
    # biases the §3.6 PPV drop toward the top of its band (stalled=). Logged nowhere
    # else here — the stall LINE emission is a separate opt-in surface (not wired).
    filming_stall = bool(cfg.get("filming_stall_enabled"))
    # §4.4b / §7 — the post-buy follow-up RUNG and the free thank-you gift. The free
    # post_buy_bridge bubble and the aftercare warm line fire without these; only the
    # unsolicited priced rung / the free unseen-media gift are gated (consent, §11).
    post_buy_rung_on = gate_on and bool(cfg.get("post_buy_rung_enabled"))
    gift_on = gate_on and bool(cfg.get("gift_enabled"))
    # A rhythm RESUME run: the stored wake_at WAS the decision (made a sleep or a
    # break ago). Re-rolling decide() here is what would livelock the fan — every
    # wake re-samples a new gap and he is never actually answered. Send inline.
    rhythm_resume = bool(payload.get("rhythm_resume"))
    rhythm_cover = payload.get("rhythm_cover") if rhythm_resume else None
    # The bubbles the deferring tick already generated and paid for, riding the
    # resume job beside the cover line. Only ever set on an unpriced turn — see
    # the defer site and `_replay_draft`.
    draft_ready = bool(rhythm_resume and payload.get("draft_parts"))

    cfg_row = await _load_cfg_row(account_id)
    tz_off = (rhythm.tz_offset_for(getattr(cfg_row, "timezone", None),
                                   getattr(cfg_row, "utc_offset", None))
              if rhythm_on else None)
    # Prompt clock: independent of the rhythm flag — the chat model must know
    # HER local time even when human-rhythm pacing is off. None ⇒ no clock line.
    clock_tz = rhythm.tz_offset_for(getattr(cfg_row, "timezone", None),
                                    getattr(cfg_row, "utc_offset", None))
    # HER calendar day, for the opener ration (_openers.DAILY_CAP). Same offset the
    # prompt clock uses; an account with no timezone yields "" and the ration does
    # not apply, which is the pre-ration behaviour rather than a guessed boundary.
    opener_day = _openers.local_day(clock_tz)
    # TODAY's day log — ONE lazy generation per (account, creator-local date), shared
    # by every fan in this sweep. Resolved here rather than per-fan for the same
    # reason `rhythm_curve` is: it is account-level, and a per-fan call would be one
    # generation per fan for a value that cannot differ between them. Gated on the
    # flag so an operator can turn the whole feature off without a deploy; the empty
    # `Day` when off / no tz / cap hit, and it renders "" everywhere (byte-identical
    # prompt). `cfg_row` is already in hand here, so the flag costs no extra query.
    day = await _daylog.load_day(account_id, cfg_row, model=model,
                                 purpose=_PURPOSE, clock_tz=clock_tz)
    sleep_win = (await _sleep_window(account_id, tz_off, cfg.get("sleep_window"),
                                     str(cfg.get("rhythm_sleep_source") or "default"))
                 if rhythm_on else rhythm.DEFAULT_SLEEP)
    media_asks: dict[int, list[int]] = {}
    acct_median: int | None = None
    lib_bounds = (upsell.OF_PRICE_FLOOR_CENTS, 20_000)
    if pricing_on:
        media_asks, acct_median, lib_bounds = await _price_context(account_id, cfg_row)

    blacklist, skip_reasons = await _load_stop_lists(account_id)
    promo_spam = await load_promo_spam_ids(account_id)
    old_fan_ids: set[int] = ({fid for fid, r in skip_reasons.items()
                              if r == _OLD_FAN_SKIP} if engage_old else set())
    mid_funnel_fans = await _load_mid_funnel_fans(account_id)
    by_fan = await _gather(account_id, only_fan_ids or None,
                           session_gap_min=session_gap_min,
                           day_window=QUOTA_WINDOW if quota_on else None)

    # ── Include-only audience: intersect HERE, before every ownership/spend
    # snapshot. `seller_owned_fans`' `always` set is built FROM by_fan, so a
    # fenced fan can never be resurrected by it; the of_ai_chat/deep_convo
    # partition reads `engaged_subset(by_fan-derived sets)` and shrinks with it.
    # force_ids is operator-explicit manual targeting — exempt, like the manual
    # stamp (the exemption register names it).
    import audience_include as _audiences
    audience_stats: dict = {}
    _kept = set(await _audiences.filter_candidates(
        account_id, list(by_fan), kind="ai_chatter",
        stats=audience_stats, extra_allowed_ids=force_ids))
    by_fan = {fid: c for fid, c in by_fan.items() if fid in _kept}

    client = await asyncio.to_thread(ax._make_client, account_id)

    # ── M3 offer layer: resolve unlocks FIRST (a fan doesn't have to speak to
    # buy), then load the catalog + the open-offer map the prompts read.
    cfg_offer_mode = str(cfg.get("offer_mode") or "ppv")
    intent_only = bool(cfg.get("intent_only"))
    pivot_on_escalation = bool(cfg.get("pivot_on_escalation"))
    reply_ctx_on = bool(cfg.get("reply_context_enabled"))
    # Arm G. All three off by default ⇒ `reshape` returns its inputs and the
    # prompt is byte-identical to today.
    _shape = _prompt_shape.Shape.from_cfg(cfg)
    esc_min_msgs = int(cfg.get("min_fan_msgs_before_escalation_pitch") or 0)
    # Inert without the gate — force_ask rides ON gate_ok, and with the gate off the
    # gate never runs, so there is nothing to ride. Guarded here (not just at the call
    # site) so a config with force_ask on and the gate off can't look armed.
    force_ask = bool(cfg.get("force_ask")) and gate_on
    # Max unpaid PPVs he may hold at once — configurable, but never below the default 2
    # (the whole point is that a second offer in a row is allowed).
    max_open_offers = max(_MAX_OPEN_OFFERS, int(cfg.get("max_open_offers") or 0))
    # …and the EFFECTIVE ceiling is ONE when the prompt does not sell. "A second offer
    # in a row is allowed" is a statement about a model that can pitch a second rung,
    # and with the catalogue out of the prompt there is no second rung to pitch — only
    # a live PPV she must still be able to talk about ("whats in it?"). Named here,
    # next to the number it derives from, rather than as an extra disjunct on each of
    # the branches that read it.
    open_offer_ceiling = max_open_offers if prompt_sells else 1
    # How much cheaper the pending piece is re-priced when he haggles (0.10 = 10% off).
    haggle_pct = float(cfg.get("haggle_discount_pct") or _HAGGLE_DISCOUNT_PCT)
    # How much off a priced TEASER we RESEND when he balks on it (up to 0.20 = 20%).
    teaser_disc_pct = min(0.20, float(cfg.get("teaser_discount_pct") or _TEASER_DISCOUNT_PCT))
    # The floor: after this many of his messages with no ask on the table, ask anyway.
    # Rides the gate too — a broke man is never asked, however long he talks.
    ask_after_n = int(cfg.get("ask_after_fan_msgs") or 0) if gate_on else 0
    # Hot-thread teaser: when thread_heat is HOT and no priced offer is going out this
    # turn, attach a few unseen vault items to the reply (free warm-up for a $0 fan, a
    # priced tease PPV for a proven buyer). Configured in the TIP REWARD tab (the vault-
    # media home) and read here — None ⇒ off, don't even look per fan. Does NOT ride the
    # gate: the images ARE the lead-up, so they warm even an account with no catalog. The
    # spend brakes below still apply (a broke/declined/companion fan never reaches it).
    from automations import tip_reward as _tip_reward
    teaser_cfg = await _tip_reward.hot_teaser_config(account_id)
    # Conversational teaser ladder — NOT gated on heat; fires during ordinary chat
    # every N of his messages, climbing free → $10 → $50. None ⇒ off.
    convo_teaser_cfg = await _tip_reward.convo_teaser_config(account_id)
    # Can she actually read a picture he sends? (vision describe at ingest — see
    # webhook_dispatch.on_inbound_image). Gates the "send me one and I'll rate it"
    # bot-accusation dare: promising a rating she can't deliver is worse than not
    # daring at all. Read once per run, not per fan.
    describe_on, _describe_seed, _describe_scope = \
        await _tip_reward.image_describe_flags(account_id)
    # 🚫 THE PROMPT DOES NOT SELL — ONE GATE, HERE, at the shelf. Everything the
    # catalogue reaches in this sweep is downstream of these two names (`catalog_items`
    # decides whether a sell block is built; `scripts` only ever rides into
    # `_offerable_for_fan` beside it), so emptying the shelf is what "she has nothing
    # priced to pitch" means — and every branch below then reads exactly as it did
    # before the flag existed, instead of each re-asking whether selling is on.
    # An empty shelf is a state those branches have always handled: it is what an
    # account that never built a catalogue has.
    #
    # ⚠️ SCOPE IS THE PROMPT. `_fire_post_buy_rung` loads its own catalogue and is
    # untouched — a post-buy rung is a priced attach sent out of band on a purchase,
    # not a pitch the model writes, so it is not what this ruling is about.
    scripts, catalog_items = (
        await _load_catalog(account_id) if prompt_sells else ({}, []))
    offer_stats = await _resolve_open_offers(account_id, client, cfg,
                                             dry_run=dry_run,
                                             only_fan_ids=only_fan_ids or None)
    open_by_fan = {int(o.fan_id): o for o in await _open_offers(account_id)}
    # Recent payers (tip/PPV unlock in the last RECENT_PAYER_HOURS) count as
    # intent in closer mode — the seller rides a just-paid hot moment even if the
    # latest line isn't an explicit buy-ask. autoreply skips this exact set, so a
    # hot lead is never demoted to never-sell keep-warm. Mirrors engaged_subset.
    recent_payers = await recent_payer_fans(account_id, list(by_fan.keys()))
    # The payer floor's roster — men who have ever bought CONTENT. One batched query,
    # and only when the floor is armed, so an opted-out account pays nothing for it.
    content_payers = (await content_payer_fans(account_id, list(by_fan.keys()))
                      if payers_only else set())
    # Newest money-event time per fan — the post-purchase talk window (item 17).
    # Also loaded for the ghost lane: a fan who PAID during his current talking
    # run must never be met with a scheduled silence, and `recent_payers` only
    # sees the last hour.
    money_at = (await _last_money_at(account_id, by_fan.keys())
                if (cadence_on or ghost_on) else {})
    # Proven-spend cap floor per fan (item 21b) — {fid: highest cap his rolling-window
    # paid spend earns}, folded into the cadence gate. Only computed when cadence is on
    # AND at least one spend rule is configured, so an off flag / empty list costs nil.
    # ONE scan feeds both spend-driven leashes (21b's burst floor and 21c's daily
    # quota). They were two independent awaits over the same fans and overlapping
    # windows; folding them off a single fetch also means the two can no longer be
    # computed against different clocks, which the two `datetime.utcnow()` calls
    # here previously allowed.
    cap_rules = cfg.get("msg_limits_by_spend") or [] if cadence_on else []
    quota_rules = cfg.get("daily_quota_by_spend") or [] if quota_on else []
    windows = spend_windows(cap_rules, quota_rules)
    spend_by_window = (await _paid_spend_by_window(
        account_id, [int(x) for x in by_fan], windows, datetime.utcnow())
        if windows and by_fan else {})
    spend_cap_by_fan = spend_caps(spend_by_window, cap_rules)
    spend_quotas = daily_quotas(spend_by_window, quota_rules)
    # Ladder + rhythm state for this sweep — one query each, and NOT EVEN READ when
    # the lanes are off (an off flag must cost nothing, not just change nothing).
    ladders = await _load_ladders(account_id, by_fan.keys()) if gate_on else {}
    rstates = await _load_rhythm(account_id, by_fan.keys()) if rhythm_on else {}
    # What the HUMANS did in these threads: a chatter's unpaid PPV is a live ask, and a
    # PPV he unlocked is a purchase — neither has ever been visible to this engine.
    # Read whenever the gate or rhythm is on, because both are misled without it.
    #
    # ...and whenever the post-purchase judge is on, because `last_paid_at` IS its
    # trigger. Left at `gate_on or rhythm_on` the judge would be silently dead on
    # every account with both flags down — which is 5 of the 12 live accounts, and
    # the same gating mistake that kept the content-dispute regex from ever seeing
    # the fan on the gate-off account. It costs one grouped query over the candidate
    # set on those accounts; a missed chargeback costs more.
    pp_on = _objection.judge_on(cfg)
    human_money = (await _human_money_signals(account_id, by_fan.keys(), datetime.utcnow())
                   if (gate_on or rhythm_on or pp_on) else {})
    # gen_info profiles (bio / bullet notes / teases) → the prompt, so the AI knows
    # his story, not just his tags. Always loaded (personalization is not gated).
    profiles = await _load_profiles(account_id, by_fan.keys())
    asks_by_fan, acct_hour_asks, acct_day_asks = (
        await _ask_counters(account_id, datetime.utcnow()) if gate_on else ({}, 0, 0))
    # The vault sell lane — the ONE gate every priced pack passes, closer included.
    # Built off the config this run already loaded (no second read), and it carries
    # the tally that used to be two locals. `permission_key` is None because the
    # closer's permission to sell IS `pack_on_ask_enabled`; the per-engine keys
    # exist only for the engines that were never sellers.
    lane = await sell_lane.for_run(account_id, engine=_PURPOSE, cfg=cfg)
    # 🔔 …and `lane.on`, because the signal's ONLY consumer is that lane. With the
    # shelf or the ask-trigger off, every sale refuses `R_DISABLED` before it reads a
    # word he wrote — so asking the model would buy a prompt block and a protocol line
    # on every reply, for an answer nothing can act on. Same fold as autoreply's.
    signal_on = lane.on and bool(cfg.get("sell_signal_enabled"))

    async with get_session() as s:
        fan_rows = (await s.execute(
            select(Fan).where(Fan.account_id == str(account_id))
        )).scalars().all()
    fans: dict[int, Fan] = {int(f.fan_id): f for f in fan_rows}

    # Hard takeover (upsell_takes_over): a fan in an ACTIVE sale is driven by the
    # seller regardless of the base chatter mode — he bypasses the backup-SLA hold
    # and the closer no-intent skip so a sale is never dropped mid-flow. "Active"
    # = an open pending offer, a just-paid hot moment, or an OPEN|HOT ladder. Inert
    # unless the gate is on (no seller ⇒ nothing to take over). Hand-back is the
    # existing COOLDOWN → COMPANION transition, untouched.
    takeover = gate_on and bool(cfg.get("upsell_takes_over"))

    def _in_active_sale(fid: int) -> bool:
        if open_by_fan.get(fid) is not None:
            return True
        if fid in recent_payers:
            return True
        lad = ladders.get(fid)
        return lad is not None and lad.status in (upsell.STATUS_OPEN, upsell.STATUS_HOT)

    # ── The PAYER FLOOR, resolved ONCE for the whole sweep (`seller_owned_fans`).
    # Same call `engaged_subset` makes, so the fans this loop keeps and the fans
    # of_ai_chat yields are one partition rather than two hand-rolled opinions.
    # `always` is built from the structures already loaded above, so it costs no
    # extra query; when the floor is off the whole thing is `set(by_fan)`.
    # `intent=None`: the closer's intent narrowing is a SEPARATE, later gate in the
    # send loop (it needs the per-candidate body), so this pass applies only the
    # floor. `engaged_subset` folds both because it answers for a whole tick.
    seller_fans = seller_owned_fans(
        set(by_fan), intent=None, payers_only=payers_only,
        content_payers=content_payers,
        always={fid for fid in by_fan if _in_active_sale(fid)} | old_fan_ids)

    now = datetime.utcnow()
    candidates: list[_Cand] = []
    old_fans_engaged = 0    # candidates admitted via the engage_old_fans lift
    skipped_listed = 0      # blacklist / non-graduation skip_list / paused
    skipped_not_turn = 0    # we (or nobody) spoke last
    rhythm_waiting = 0      # she's mid-pause for this fan (wake_at in the future)
    run_inline_s = 0.0      # cumulative inline hold this run() (the global-slot budget)
    skipped_spam = 0        # promo-spam: $0 + creator_we_follow
    skipped_muted_creator = 0  # muted creator we follow — HARD skip (durable)
    skipped_whale = 0       # at/over the spend gate → human territory
    skipped_not_payer = 0   # payer floor: never bought content → of_ai_chat's
    skipped_sla_fresh = 0   # backup mode: inbound younger than the SLA
    skipped_manual = 0      # a human chatted too recently (cautious resume)
    skipped_ghost = 0       # ghost cycle: she is dark on this fan for whole days
    stepouts = 0            # she went out for an hour or two (the exchange counter)
    stepout_broken = 0      # …and came back early because he wrote again
    stepout_blocked = 0     # …and the ones the one-hop deferral cap swallowed

    for fan_id, c in by_fan.items():
        forced = fan_id in force_ids
        if fan_id in blacklist:
            skipped_listed += 1
            continue
        reason = skip_reasons.get(fan_id)
        # `skip_reason_blocks` and not the clauses inline: the badge in fans.py asks the
        # same question, and the last time each kept its own copy they disagreed.
        if skip_reason_blocks(reason, engage_old_fans=engage_old) and not forced:
            # A skip_list row (of_restricted / manual_restrict / unreachable /
            # ladder_stop) CLOSES the ladder. A rung left 'open' on a fan nobody
            # may message again would keep a stale rung_index alive and escalate
            # the next ask — years later — off a price he never paid.
            lad = ladders.get(fan_id)
            if lad is not None and lad.status in (upsell.STATUS_OPEN, upsell.STATUS_HOT):
                await _close_ladder(account_id, fan_id, upsell.STATUS_TAPPED)
            skipped_listed += 1
            continue
        if fan_id in mid_funnel_fans and not forced:
            skipped_listed += 1
            continue
        # ── Human Rhythm: she is mid-pause for THIS fan. wake_at is a real gate, not
        # a note-to-self. The executor re-runs ai_chatter every ~30s, so without this
        # the next tick simply re-picks the deferred fan; the one-hop cap then makes
        # the availability check a no-op and she answers instantly anyway — the whole
        # feature silently degrades to today's behavior. The resume job (rhythm_resume,
        # scoped by only_fan_ids) is what legitimately wakes him, so it must pass.
        if rhythm_on and not rhythm_resume and not forced:
            _rs = rstates.get(fan_id)
            if _rs is not None and _rs.wake_at is not None:
                if _rs.wake_at > now:
                    # STILL PAUSED — unless he wrote again and this is a step-out, the
                    # one silence a fan is allowed to interrupt. `_stepout_gate` clears
                    # the row itself, so a broken pause skips the elapsed branch below
                    # rather than writing the same state twice.
                    if not await _stepout_gate(account_id, c, _rs, stepout):
                        rhythm_waiting += 1
                        continue
                    stepout_broken += 1
                else:
                    # The pause ELAPSED but the resume job never reached the send path
                    # (its lease was taken, a cooldown hit, a gate skipped him). Drop
                    # the stale wake_at so he is a candidate again.
                    #
                    # `deferrals` is deliberately NOT cleared here: the one-hop cap is
                    # per PENDING REPLY, not per lifetime. Keeping it means decide()
                    # hits the cap on this tick and answers him INLINE — which is the
                    # whole point of the cap (he is owed a reply and must get one).
                    # Clearing it here would let him be deferred a second time for the
                    # same unanswered message. It resets on the SEND (`_record_turn`)
                    # and here on a NEW inbound — a new message is a new obligation,
                    # and rhythm must be free to pause on it, or the feature would
                    # silently degrade to one-shot for that fan forever.
                    fresh_inbound = (_rs.updated_at is not None
                                     and c.last_in_at is not None
                                     and c.last_in_at > _rs.updated_at)
                    await _save_rhythm(
                        account_id, fan_id, wake_at=None,
                        deferrals=0 if fresh_inbound else int(_rs.deferrals or 0))
                    _rs.wake_at = None
                    if fresh_inbound:
                        _rs.deferrals = 0
        if c.last_dir != "in":
            skipped_not_turn += 1
            continue
        f = fans.get(fan_id)
        if f is not None and f.automation_paused_until and f.automation_paused_until > now:
            skipped_listed += 1
            continue
        # Muted creator we follow — HARD skip even when forced (mirrors of_ai_chat;
        # the scrape also writes a durable skip_list('muted_creator')).
        if should_skip_muted_creator(f):
            skipped_muted_creator += 1
            continue
        if not forced:
            if f is not None:
                if fan_id in promo_spam:
                    skipped_spam += 1
                    continue
                if int(f.lifetime_spend_cents or 0) >= gate_cents:
                    skipped_whale += 1
                    continue
            # The PAYER FLOOR — the whale gate's mirror at the bottom. He has never
            # bought content, so he is of_ai_chat's to work and profile until he does.
            if fan_id not in seller_fans:
                skipped_not_payer += 1
                continue
            # Cautious resume: a HUMAN sent something recently → their convo.
            if (resume_h and c.last_human_out_at is not None
                    and c.last_human_out_at > now - timedelta(hours=resume_h)):
                skipped_manual += 1
                continue
            # Backup mode: only step in once the inbound has aged past the SLA
            # (chatters are slow). A fresh message stays human turf for now — UNLESS
            # the seller has already taken this fan over (active sale), in which case
            # holding him for the SLA would stall a live sale mid-flow.
            if mode == "backup" and sla_s and not (takeover and _in_active_sale(fan_id)):
                if c.last_in_at is None or c.last_in_at > now - timedelta(seconds=sla_s):
                    skipped_sla_fresh += 1
                    continue
            # ── The ghost cycle: whole days dark on this fan (see `_ghost.py`).
            # LAST of the soft gates on purpose — it is the only one that WRITES,
            # and a fan the cheaper gates above already dropped must not cost an
            # upsert to anchor. Unlike every other discretionary silence in the
            # product this one fires on a fan who is owed an answer; that is the
            # feature, and the exemptions inside the gate are what keep it sane.
            if ghost_on and await _ghost_gate(
                    account_id, c, rstates.get(fan_id), ghost_cycle, now,
                    in_active_sale=_in_active_sale(fan_id),
                    paid_at=money_at.get(fan_id)):
                skipped_ghost += 1
                continue
        if fan_id in old_fan_ids:
            old_fans_engaged += 1
        candidates.append(c)

    # Longest-waiting fan first — backup mode is an SLA queue, not a popularity
    # contest (of_ai_chat sorts by volume; here fairness wins).
    candidates.sort(key=lambda x: (x.last_in_at or now, x.fan_id))

    sent = 0
    offers_made = 0
    offers_made_on_escalation = 0   # offers triggered by the lean-in pivot
    offers_forced = 0        # force_ask: gate said yes, model wrote no marker, we sold
    offers_forced_stale = 0  # …of those, fired by the FLOOR (he'd talked N msgs, no ask)
    forced_this_tick = 0     # account budget on forced asks this run (_MAX_FORCED_ASKS…)
    teasers_sent = 0
    hot_teasers_sent = 0     # hot-thread vault teasers attached to a reply (free + paid)
    hot_teaser_paid_tick = 0 # per-run budget on PAID hot teasers (shares _MAX_FORCED…)
    would_offer = 0          # dry-run: offers that would have been recorded
    unbacked_stripped = 0    # price-talk bubbles dropped (no offer behind them)
    skipped_locked = 0
    skipped_cooldown = 0
    skipped_no_intent = 0   # intent_only: fan is just chatting, no buying signal
    skipped_cadence = 0     # cadence: burst cap hit / post-purchase window lapsed
    quota_held = 0          # item 21c: daily quota spent, inside the backoff. COUNTED
                            # even in shadow mode — that is the whole point of shadow
                            # mode, so "how many would we have held" is answerable
                            # before enforcement is switched on.
    quota_held_fans: list[str] = []   # "fan:used/quota@waith(dryNd)" for the log line
    quota_rows: list[tuple[int, "_Quota"]] = []   # every verdict → the audit ledger
    hard_stops = 0          # gate: chargeback/report/unsubscribe → ladder STOPPED
    letdown_stops = 0       # post-purchase: he paid and was disappointed → 24h brake
    soft_acks = 0           # gate: "i'm broke" → keep talking, stop selling 24h
    gate_blocked = 0        # gate: no price in front of this fan right now
    customs_owed_skips = 0  # a paid custom has not shipped — talk, never sell
    offers_parked = 0       # …and the reason was transient → PendingOffer row
    rungs_quoted = 0        # priced rungs that actually went out (ladder_quote rows)
    taps_expired = 0        # tap-outs that served their TTL and reopened (not a life sentence)
    rhythm_deferred = 0     # lease released + resume job enqueued (never slept)
    drafts_stored = 0       # …of those, the ones that carried their text across the hop
    # What became of a stored draft on the wake, and why a generated reply was
    # dropped at the wire — both keyed by the string the deciding helper RETURNED,
    # so a count can never drift from the branch it describes (the `quota_reasons`
    # shape). `_thread_moved_on` separates "he wrote again" from "somebody else
    # answered" on purpose: both are drops, but only the second means another
    # sender is racing us, and one number would hide that.
    draft_outcomes: Counter = Counter()
    stale_drops: Counter = Counter()
    cover_lines_sent = 0    # "sorry babe was in the shower 🚿" before the reply
    stickers_sent = 0       # cat reaction gifs delivered (incl. sticker-only replies)
    price_errors = 0        # §4.1: priced attaches OF rejected → offer dropped, resent unpriced
    spend_regret_stops = 0  # §6.1: "im out of money" → 24h soft stop + COOLDOWN
    companion_routed = 0    # §6.3: "i just wanna talk" → seller OFF, conversation ON
    bot_accusations = 0     # §6.4: "are you a bot" → offer suppressed (2nd strike ⇒ COMPANION)
    rate_pic_turns = 0      # turns that carried a rating directive — the DENOMINATOR
    # Turns where he OFFERED and she said yes. Its value is as a LEADING indicator:
    # every one of these should show up as a rate_pic turn shortly after, and a run
    # where pic_offer climbs while rate_pic stays flat means she is telling men to send
    # pictures that never arrive — the copy has gone coy again.
    pic_offer_turns = 0
    ratings_re_asked = 0    # rate_pic replies that came back scoreless — SECOND CALL PAID
    ratings_rescued = 0     # …of those, the ones that second call actually scored
    spend_capped = 0        # §6.2: 7d paid-spend brake → COMPANION for the window
    ppv_drops = 0           # §3.6: inline setup→attach pacing holds
    paced_bubbles = 0       # bubbles 1+ that went through pacing.bubble_pace
    pace_drifts = 0         # …of those, how many drew a real "she stopped" pause
    paid_state_refreshed = 0  # PPVs the fan had unlocked that our is_paid still called unpaid
    errors = 0
    # A reply we chose not to send is NOT an error, and folding the two together
    # cost hours: `errors` climbing on a healthy account read as flakiness, when
    # every one of them was this deliberate drop. Separate counter, separate name.
    dropped_empty = 0       # nothing sendable survived the filters
    offers_deferred = 0     # sticker-only reply ⇒ the attach waits for the next turn
    cap_hit = False

    for c in candidates:
        if sent >= max_replies:
            log.info("ai_chatter batch capped account=%s cap=%d (rest next tick)",
                     account_id, max_replies)
            break
        fan_id = c.fan_id
        # Closer mode: stay silent unless the fan's latest message shows buying
        # intent, or he already has an open offer we're walking. Checked BEFORE
        # the cooldown/lease/LLM work so skipped fans cost nothing. Pure chatter
        # is left to the team / Auto Convo. A caller-flagged intent fan (e.g. one
        # who just sent us a PHOTO) counts as intent even with no text signal.
        # An ENGAGED OLD FAN is exempt: engage_old_fans means "the AI works the
        # pre-AI roster", and in closer mode nobody else can — of_ai_chat and
        # deep_convo hard-skip `old_fan_pre_ai` (and process_old_fans marked them
        # deep_convo 'done'), so gating them on buying intent would leave them
        # silent forever. ai_chatter is also the only bot with NO graduation
        # cutoff, so it can just keep the convo going. engaged_subset() mirrors
        # this exemption, so no second bot voice lands on them either.
        if intent_only and fan_id not in old_fan_ids \
                and open_by_fan.get(fan_id) is None \
                and fan_id not in recent_payers \
                and fan_id not in intent_fan_ids \
                and not (takeover and _in_active_sale(fan_id)) \
                and not _CONTENT_ASK_RE.search(c.last_body or "") \
                and not ESCALATION_RE.search(c.last_body or ""):
            skipped_no_intent += 1
            continue
        # Cadence controller (items 10/17/21): continue-vs-stop for this fan, decided
        # BEFORE any cooldown/lease/LLM cost. A stop is a silent skip THIS tick — the
        # fan reopens on a real buying signal (tier upgrade) or after a session-gap
        # of silence resets his burst. OFF unless the account enabled cadence.
        cad_tier = TIER_BASELINE
        if cadence_on:
            cad_stop, cad_tier, _cad_cap = _cadence_gate(
                c, pending=open_by_fan.get(fan_id),
                recent_payer=fan_id in recent_payers,
                money_at=money_at.get(fan_id),
                pic=fan_id in intent_fan_ids, now=now, cad=cfg,
                spend_cap=spend_cap_by_fan.get(fan_id, 0))
            if cad_stop:
                skipped_cadence += 1
                continue
        # Item 21c — the daily ceiling, checked after the burst cap and, like it,
        # BEFORE any cooldown/lease/LLM cost. It reuses the TIER the burst gate just
        # decided rather than re-deriving "is he buying" from the body a second time.
        # SERVED since 2026-07-27. With `daily_quota_enforce` False the verdict is still
        # counted, logged and written to `quota_audit` while the reply goes out — the
        # shadow mode the rollout ran in, and still the way to audit a config change
        # against real traffic before any fan is held back.
        if quota_on:
            q = _quota_gate(
                c, spend_quota=spend_quotas.get(fan_id),
                money_at=money_at.get(fan_id),
                tier=cad_tier, now=now, cad=cfg)
            quota_rows.append((fan_id, q))
            if q.hold:
                quota_held += 1
                quota_held_fans.append(
                    f"{fan_id}:{q.used}/{q.quota}@{q.wait_h:g}h(dry{q.dry_h / 24:.1f}d)")
                if quota_enforce:
                    continue
        if await ax.fan_on_cooldown(account_id, fan_id):
            skipped_cooldown += 1
            continue
        if not await ax.acquire_fan_lease(account_id, fan_id, _PURPOSE):
            skipped_locked += 1
            continue
        sent_ok = False
        try:
            # ── The draft this fan's deferring tick already paid for. Placed HERE,
            # after every candidate gate and the lease, so a replay is held to the
            # same bar as a fresh reply (blacklist, paused, muted, cadence, quota,
            # cooldown) and cannot slip a message past a guard that closed while he
            # was parked. Any outcome outside `_DRAFT_HANDLED` falls through and
            # regenerates, which is exactly the old behaviour.
            if draft_ready:
                outcome = await _replay_draft(
                    client, account_id, c, rstates.get(fan_id), payload,
                    typing_wpm=typing_wpm, typing_indicator=typing_indicator,
                    cover=rhythm_cover, informal=style_on)
                draft_outcomes[outcome] += 1
                if outcome in _DRAFT_HANDLED:
                    if outcome == "sent":
                        sent += 1
                        sent_ok = True
                        if rhythm_cover:
                            cover_lines_sent += 1
                    continue
            f = fans.get(fan_id) or Fan(account_id=str(account_id), fan_id=fan_id)
            # The account bundle narrowed to THIS fan. A custom is only offered to
            # a man who has proven he pays (`_customs.may_offer`, currently $100
            # lifetime) — operator ruling 2026-08-05, "we don't offer for
            # everything to anyone". Below the bar this is the same bundle with the
            # customs permission and its price band stripped out of every block
            # that carries them, so the prompt simply never mentions customs.
            #
            # Measured on the four accounts that sell customs: 84 of 1,358 fans
            # clear the bar, so 94% of prompts lose the block entirely.
            #
            # SHADOWS `voice_blocks` FOR THE REST OF THE ITERATION ON PURPOSE — every
            # read below this line is per-fan, and a mix of the two names inside one
            # loop body is exactly how a prompt ends up half-narrowed.
            v = _voice.for_fan(voice_blocks, f)

            # Did he UNLOCK the ask that's sitting in front of him? `is_paid` is stamped
            # at ingest and never re-read, so a PPV he paid for reads as unpaid until the
            # ledger lands (up to 10h). One OF read settles it — and only when there IS a
            # live unpaid ask AND he has spoken since, so a quiet thread costs nothing.
            if (gate_on or rhythm_on) and human_money.get(fan_id, _NO_MONEY).ask:
                _ask_at = human_money[fan_id].ask
                if c.last_in_at is not None and _ask_at is not None \
                        and c.last_in_at > _ask_at:
                    if await _refresh_paid_state(client, account_id, fan_id):
                        paid_state_refreshed += 1
                        # Re-read HIS money signals: the ask he just paid for is no
                        # longer an open ask, and he is now a fresh buyer (hot window).
                        human_money.update(await _human_money_signals(
                            account_id, [fan_id], datetime.utcnow()))

            # ── The decline classifier runs FIRST — on the raw inbound, before the
            # LLM, before any offer path. Three declines, three consequences (a
            # single broad _DECLINE_RE matched 4.05% of real inbounds — "No
            # problem!", "No way!!!!" — and would have blacked out 37.9% of threads).
            # It runs ahead of any price-objection/haggle handling on purpose: "too
            # expensive, i can't afford this" is a man telling us he is BROKE, and
            # countering him with a discount is the one thing we must not do.
            # ── v2 safe-state routing (spec §6). SPEND_REGRET is checked BEFORE the
            # discount path on purpose: "i cant afford this" is a man asking us to
            # stop, and countering him with a cheaper offer is the one thing we must
            # never do. Then COMPANION intent, then the three declines. ALL of these
            # are LADDER-scoped — none ever touches fans.automation_paused_until.
            seller_off = False       # COMPANION / cooldown / bot-accused ⇒ talk, don't sell
            # A PAID-FOR CUSTOM THAT HAS NOT SHIPPED stops selling outright, and
            # rides the same switch as the companion/cooldown brakes because it
            # wants the identical behaviour: the conversation continues, the
            # money asks stop. Operator ruling 2026-08-04 — "he doesn't sell more
            # till it's delivered".
            #
            # Deliberately NOT gated on `gate_on`: the seller gate is a selling
            # feature flag, and "we owe this man something he paid for" is not a
            # selling decision. It also has NO timeout — a stale marker costs us
            # revenue from one fan, while a timeout resumes asking a man for money
            # when he never received what he already bought. The marker is cleared
            # by the operator, or by `customs_watch` seeing the voice note go out.
            if _customs.is_owed(f):
                seller_off = True
                customs_owed_skips += 1
            bot_accused_turn = False
            # Both re-armed further down (next to hot_thread, which the dare reads).
            # Initialised HERE, per fan, because every one of these is a plain local in
            # a loop: a path that reached the prompt without re-assigning would silently
            # inherit the PREVIOUS fan's turn — the same class of bug as reading
            # `fan_ladder` before it is bound.
            image_dare_ok = False
            dare_is_callback = False
            rate_pic_turn = False
            pic_offer_turn = False
            kind = ""
            # A HARD decline WINS over the poverty/companion brakes — always. Otherwise a
            # message that carries both a distress token and a chargeback ("im tapped out,
            # im disputing this charge and reporting you") hits detect_spend_regret first
            # and takes the 24h regret pause instead of the HARD consequence — the 72h
            # offers-pause + the make_right de-escalation apology. The hard signal must
            # keep its own, stronger handling even when softer tokens ride along.
            #
            # Which apology (if any) this turn owes him — regexes then, only if they
            # found nothing and only inside the post-purchase window, one LLM call.
            # The precedence and the cost discipline live in `_objection`; what is
            # left here is the CONSEQUENCE, which is identical for every verdict.
            objection = await _objection.decide(
                account_id, fan_id, f, c, cfg=cfg, model=model,
                last_paid_at=human_money.get(fan_id, _NO_MONEY).paid, now=now,
                dry_run=dry_run)
            if objection:
                # The brake rides WITH the apology — a billing claim takes the 72h
                # stop, a man who is merely disappointed takes 24h. Both keep talking.
                await _handle_decline(account_id, fan_id, objection.decline, now)
                if not dry_run:
                    await _trigger_make_right_apology(account_id, fan_id,
                                                      objection.reason)
                # Counted apart, because they mean different things to whoever reads
                # the run stats: `hard_stops` is "a fan threatened a chargeback or
                # said he already owned it", `letdown_stops` is "a fan was
                # disappointed". Folding the second into the first made the headline
                # number report chargeback risk that wasn't there.
                if objection.decline == upsell.DECLINE_HARD:
                    hard_stops += 1
                else:
                    letdown_stops += 1
                log.info("ai_chatter post-purchase objection account=%s fan=%s "
                         "reason=%s brake=%s — offers-pause + make_right apology "
                         "(never a permanent bot stop)",
                         account_id, fan_id, objection.reason, objection.decline)
                continue
            if gate_on and upsell.detect_spend_regret(c.last_body):
                # §6.1 — poverty brake. 24h offers-PAUSE (never a proactive push, never a
                # discount), and she keeps talking. We do NOT force full COMPANION here:
                # measured, 11% of fans who say "broke" still buy within 24h, 18% within
                # 7d. So the pause is LIFTABLE BY HIM — if he comes back PULLING (an
                # explicit "send it" / content-ask), qualify()'s fan_pull honours it. That
                # is not farming a broke man; it is a man telling us the "broke" wasn't
                # final. (COMPANION stays reserved for an explicit "just want to talk.")
                await _save_ladder(account_id, fan_id,
                                   offers_paused_until=now + timedelta(hours=24))
                name = _greetable(f, v)
                ack = _pack_line("soft_broke_ack", cfg, fan_id, name=name)
                if ack and not dry_run and await _send_free_bubble(
                        client, account_id, fan_id, ack, typing_wpm=typing_wpm,
                        typing_indicator=typing_indicator, now=now):
                    sent_ok = True
                    sent += 1
                soft_acks += 1
                spend_regret_stops += 1
                continue
            if gate_on and upsell.detect_companion_intent(c.last_body):
                # §6.3 — he wants to talk, not buy. Seller OFF, conversation ON for the
                # window; a companion fan re-enters SELLING only on his OWN future buy.
                await _save_ladder(account_id, fan_id, status=upsell.STATUS_COMPANION,
                                   companion_until=now + timedelta(hours=24))
                name = _greetable(f, v)
                ack = _pack_line("companion_ack", cfg, fan_id, name=name)
                if ack and not dry_run and await _send_free_bubble(
                        client, account_id, fan_id, ack, typing_wpm=typing_wpm,
                        typing_indicator=typing_indicator, now=now):
                    sent_ok = True
                    sent += 1
                companion_routed += 1
                continue

            decline = upsell.classify_decline(c.last_body) if gate_on else None
            # HARD is already handled above (it wins over every brake) — so only the
            # SOFT / bare-no branches remain here.
            if decline and decline != upsell.DECLINE_HARD:
                await _handle_decline(account_id, fan_id, decline, now)
                if decline == upsell.DECLINE_SOFT:
                    # KEEP TALKING, stop selling. Deterministic line from the pack —
                    # no LLM, so there is no way for a model to "handle the objection"
                    # its way back into a pitch. A soft decline is a VOICED price
                    # objection: stamp objection_at so an EARNED discount (§5) may
                    # follow a LATER fan turn (the "beat"), never this one.
                    await _save_ladder(account_id, fan_id, objection_at=now)
                    name = _greetable(f, v)
                    ack = _pack_line("soft_broke_ack", cfg, fan_id, name=name)
                    if ack and not dry_run and await _send_free_bubble(
                            client, account_id, fan_id, ack, typing_wpm=typing_wpm,
                            typing_indicator=typing_indicator, now=now):
                        sent_ok = True
                        sent += 1
                    soft_acks += 1
                    continue
                # bare "no" → the LADDER taps out, the CONVERSATION does not. Fall
                # through to a normal reply; qualify() now returns 'tapped', so no
                # price can follow it.
                ladders.update(await _load_ladders(account_id, [fan_id]))

            # §6.4 — bot accusation. Same one-turn brush-off as before (a light line, not
            # a defensive protest; sell nothing THIS message) — but it NO LONGER escalates
            # to COMPANION. The old rule flipped a 2nd accusation to a persistent 24h
            # companion window that re-stamped on every repeat, so a fan who said "bot"
            # ~daily was gagged forever (u514288063: 13 offers, 5 accusations, $0, still
            # chatting warmly, dead to the closer). Now every accusation is just the
            # single-turn brush-off; he stays fully sellable on his next inbound. Still
            # counted (the roster header reads bot_accused_count). An ACTIVE companion/
            # cooldown window from a DIFFERENT brake (just-talk §6.3, broke §6.1, spend-cap
            # §6.2) is honoured below — a bot accusation just no longer creates one.
            _lad_now = ladders.get(fan_id)
            if gate_on and _lad_now is not None:
                # These two ARE selling decisions — a qualification-gate account's
                # companion / post-multibuy windows — so they stay behind the gate.
                if _lad_now.companion_until and _lad_now.companion_until > now:
                    seller_off = True         # §6.3/§6.1 window still live
                if _lad_now.cooldown_until and _lad_now.cooldown_until > now:
                    seller_off = True         # §6.2 post-multibuy ease-off (talk only)
            # ⚠️ OUTSIDE `if gate_on:`, for the same reason the picture dare was moved
            # out of it. "He thinks she is a bot" is a REALISM signal, not a selling
            # decision — and while the detection sat behind the gate it was dead on the
            # three live accounts that run with qualification off. On those accounts
            # "wait are you human?" read as ordinary chat: no brush-off, no one-line
            # cap, `bot_accusations` stuck at 0, and — because `sticker_mode` is only
            # forced to "skip" on a bot-accused turn — the model was still offered the
            # sticker protocol, answered with a cat gif, had it dropped by the guard,
            # and said NOTHING. That is the exact production failure this change set out
            # to fix, left standing on a quarter of the fleet.
            if _detect_bot_accusation(c.last_body):
                bot_accusations += 1
                prev = int(_lad_now.bot_accused_count or 0) if _lad_now is not None else 0
                await _save_ladder(account_id, fan_id, bot_accused_count=prev + 1)
                seller_off = True             # suppress the offer THIS turn only
                bot_accused_turn = True       # + brush it off, single breezy bubble
            # NOTE: the picture DARE used to arm here, as an `elif` on the accusation
            # branch — which made it reachable only on a gate-on account, only for a man
            # who had already accused her, and only once in his life. It now arms below,
            # next to `hot_thread` (which is computed there and which it needs), outside
            # the seller gate, for the same reason the detection above is.

            # §5 — a bare haggle / stated cap ("can you do $30") is a VOICED price
            # objection but NOT a decline: stamp objection_at so an EARNED discount may
            # answer a LATER turn (the beat, never this one). detect_stated_cap rejects
            # negation frames ("thats too much, not paying 50"), which route to §6/§5.
            if gate_on and not seller_off and upsell.detect_stated_cap(c.last_body):
                await _save_ladder(account_id, fan_id, objection_at=now)

            # ── Human Rhythm, part 1: is she even AROUND? Asked BEFORE ANY LLM call
            # (ai_chatter spends TWO per fan — the fact-extract and the reply), because
            # whether she is asleep or has stepped away has nothing to do with what he
            # said. Decided after generation instead, a deferred tick pays for work it
            # throws away and the wake pays for it again — double spend on ~15% of
            # replies, silently, against the account's daily_cost_cap. The in-scene
            # DELAY still has to be sampled after generation (it depends on how long
            # the text takes to type); only the away/asleep verdict moves up here.
            if rhythm_on and not rhythm_resume:
                rst0 = rstates.get(fan_id)
                lad0 = ladders.get(fan_id)
                rnow0 = datetime.utcnow()
                _money = human_money.get(fan_id, _NO_MONEY)
                _step_due = stepout.on and _stepout.is_due(
                    mark=(rst0.stepout_mark if rst0 is not None else None),
                    target=(rst0.stepout_target if rst0 is not None else None),
                    total_out_n=c.total_out_n, account_id=account_id,
                    fan_id=fan_id, lo=stepout.lo, hi=stepout.hi)
                away = rhythm.decide_availability(rhythm.RhythmCtx(
                    account_id=str(account_id), fan_id=fan_id,
                    voice=v.voice,
                    # Gap covers drawn from TODAY's day when she has one, so a cover
                    # cannot claim she was driving while the chat prompt says she was
                    # on a trail. () ⇒ the shipped pools, unchanged.
                    day_covers=day.covers,
                    pace_buckets=bool(cfg.get("rhythm_pace_buckets")),
                    pace_curve=rhythm_curve,
                    last_inbound_at=c.last_in_at, last_outbound_at=c.last_out_at,
                    # A live ladder suppresses break rolls: never strand a sell. A
                    # HUMAN's unpaid PPV is just as live a sell as one of ours — without
                    # this, rhythm scored a thread with a chatter's $45 ask sitting in it
                    # as a boring free-chat and took a coffee break mid-close.
                    # BOUNDED to _ASK_BREAKPROOF_WINDOW: glued for the 30 minutes that
                    # are the sale, then free to be a person again (with a cover line).
                    ladder_open=bool(lad0 is not None and lad0.status
                                     in (upsell.STATUS_OPEN, upsell.STATUS_HOT))
                    or _breakproof(_money.ask, rnow0),
                    # A TIP is money, and this is the ONLY place it reaches rhythm (the
                    # decide() twin below merges it too). `_context_of` must read a man
                    # who just tipped as a live sell — otherwise she walks out seconds
                    # after he pays. Measured on 2026-08-09 on account 2024813: $5 tip
                    # at 21:37:27, ingested 21:37:31, step-out at 21:37:43. Deliberately
                    # NOT merged into `.paid` itself — see `_human_money_signals`.
                    last_paid_at=_newest(
                        lad0.last_paid_at if lad0 is not None else None,
                        _money.paid, _money.tip),
                    his_last_latency_s=_his_last_latency_s(c),  # heat: his pace
                    fan_hot=_fan_hot(c),                        # heat: he's escalating
                    # Break-proof gates. This is the PRE-LLM availability check, so
                    # they must match the decide() call below or she'd take a break
                    # here that the reply path would never have chosen — and the
                    # break is decided before we pay for a reply we'd throw away.
                    answer_owed=is_qualifying_inbound(c.last_in_text),
                    turn_index=int(c.fan_msg_n),
                    thread_started_at=c.first_in_at,
                    sleep_window=sleep_win, tz_offset_minutes=tz_off,
                    no_sleep=rhythm_no_sleep,
                    last_cover_at=(rst0.last_cover_at if rst0 is not None else None),
                    reply_bonus_s=reply_bonus_s,
                    stepout_due=_step_due,
                    stepout_min_s=stepout.min_s, stepout_max_s=stepout.max_s,
                    enabled=True,
                ), rnow0, random.Random(f"rhythm:{account_id}:{fan_id}:{rnow0.timestamp()}"))
                if away is not None and int(getattr(rst0, "deferrals", 0) or 0) >= 1:
                    # THE ONE-HOP CAP ATE THE VERDICT. She is owed a reply from an
                    # earlier deferral, so this pause is dropped and she answers below.
                    # Counted rather than silent: `deferrals` only discharges on a
                    # successful send (`_record_turn`), so a fan whose replies keep
                    # being dropped for other reasons stops being pausable — and a
                    # silently partial rollout looks exactly like a feature nobody
                    # triggers. If this runs high while `stepouts` stays near zero,
                    # the feature is jammed, not quiet.
                    if away.context == rhythm.CONTEXT_STEPOUT:
                        stepout_blocked += 1
                elif away is not None:
                    _extra: dict = {}
                    if away.context == rhythm.CONTEXT_STEPOUT:
                        # Move the mark to where her counter stands NOW and draw the
                        # NEXT interval, salted with that mark so it is stable across
                        # ticks but different every time — a step-out that arrived on
                        # a countable schedule would be a worse tell than the instant
                        # replies it exists to break up.
                        _extra = {
                            "stepout_mark": int(c.total_out_n),
                            "stepout_target": _stepout.draw_target(
                                account_id, fan_id, stepout.lo, stepout.hi,
                                salt=str(c.total_out_n)),
                        }
                        stepouts += 1
                    await _save_rhythm(account_id, fan_id, context=away.context,
                                       wake_at=away.wake_at, deferrals=1, **_extra)
                    # Release the lease BEFORE the wait — an inline hold would expire
                    # its 900s TTL and burn one of the executor's 4 GLOBAL run slots.
                    await ax.release_fan_lease(account_id, fan_id)
                    await ax.enqueue_job(
                        account_id, _PURPOSE,
                        payload={"only_fan_ids": [int(fan_id)], "rhythm_resume": True,
                                 "rhythm_cover": away.cover_line},
                        run_at=away.wake_at)
                    rhythm_deferred += 1
                    log.info("ai_chatter rhythm away (pre-LLM) account=%s fan=%s "
                             "ctx=%s wake=%s", account_id, fan_id, away.context,
                             away.wake_at)
                    continue            # NOT return — the other candidates still tick

            try:
                f = await _extract_and_fill(account_id, fan_id, f, c, model,
                                            _EXTRACT_HISTORY_TAIL, purpose=_PURPOSE,
                                            persona=persona,
                                            claims_only=extract_claims_only)
            except LLMCapExceeded:
                cap_hit = True
                log.warning("ai_chatter LLM cap reached (extract) account=%s — stopping",
                            account_id)
                break
            except Exception:
                log.debug("ai_chatter fact-extract failed account=%s fan=%s",
                          account_id, fan_id, exc_info=True)
            try:
                await _maybe_push_nickname(client, account_id, fan_id, f)
            except Exception:
                log.debug("ai_chatter nick push failed account=%s fan=%s",
                          account_id, fan_id, exc_info=True)
            try:
                asked = set(json.loads(f.questions_asked or "[]"))
            except Exception:
                asked = set()

            # The offer context: a pending offer pins the prompt to it; else the
            # manifest of what THIS fan may be offered (caps + pinning + unseen
            # all enforced here, never by the model).
            sell = _NO_SELL
            offerable: dict[int, CatalogItem] = {}
            quotes: dict[int, upsell.Quote] = {}
            # Did THE GATE say we may put a price in front of this fan on this turn?
            # Only meaningful with gate_on; it is what `force_ask` rides on, so a
            # forced ask can never reach a fan the gate would have refused.
            gate_ok = False
            # He EXPLICITLY asked to see/unlock content this turn ("send it", "show me",
            # "i'll unlock it"). Computed independent of `offerable` because it decides
            # whether an offer SURVIVES a gate deferral (see the gate-override below) —
            # it must be known before offerable is (maybe) emptied.
            explicit_ask = bool(_CONTENT_ASK_RE.search(c.last_body or ""))
            ask_override = False   # gate deferred but he directly asked → force anyway
            # IS THIS THREAD HOT — a live sexual conversation HE is in? Measured on prod
            # this is a 24.3x lift on the purchase (14.6% of bought PPVs vs 0.6% of
            # unbought), against 1.19x for "was his last line dirty". It is the moment
            # the top closers sell into, and the only moment force_ask fires.
            hot_thread = thread_heat(c.messages)

            # ── The picture play, both halves. ────────────────────────────────
            # RATE: he sent something and we can read it → she rates it. No cooldown,
            # no once-per-fan, no gate: every picture gets a reaction, which is the
            # whole point of paying for the vision layer. It outranks the dare (never
            # dare a man for a picture in the same breath as rating the one he sent).
            #
            # DARE: ask him for one. Repeatable on a cooldown rather than once per
            # lifetime — one dare per man meant the play fired at most once and then
            # the thread never came back to it. Armed when the thread is HOT (a natural
            # moment to ask) or when he has questioned whether she's real (the original
            # beat-2 callback), and only where describe is on, because promising a
            # rating she cannot deliver is worse than never daring.
            # `ladders.get(fan_id)` and not `fan_ladder` — that local is bound a few
            # lines below, so reading it here would silently pick up the PREVIOUS
            # fan's ladder for every fan after the first.
            _lad_for_dare = ladders.get(fan_id)
            # `c.last_in_desc` alone, NOT `and c.pic_sent`: the description belongs to a
            # picture inside the burst she has not answered yet, and his follow-up text
            # row clears pic_sent while leaving the picture very much unanswered.
            rate_pic_turn = bool(
                describe_on and c.last_in_desc
                and c.last_in_desc_at is not None
                and (now - c.last_in_desc_at) < _PIC_DESC_TTL)
            # ELIGIBILITY ONLY — whether the dare is AVAILABLE, never whether it wins.
            # `not c.pic_sent`: never dare a man for a picture on a turn he sent one,
            # even one we could not read. The rungs that outrank it (a rating, the
            # brush-off, the wallet) are _turn_kind's job, not this expression's — that
            # split is what stopped the arming and the answer from drifting apart.
            image_dare_ok = bool(
                describe_on and not c.pic_sent
                and not _bot_dare_recent(f, now))
            # HE OFFERED — read off the same `c.last_body` the bot accusation is, so the
            # two identity-shaped reads of his last message cannot drift apart.
            #
            # `describe_on` for the DARE's reason: step 3 of the copy promises him a
            # score out of ten, and only the vision layer can pay that off. With describe
            # off she would be talking him into sending a picture she is about to ignore.
            #
            # `not c.pic_sent` because the offer is spent the moment he acts on it. He
            # sends the picture with "wanna see my cock?" as its caption all the time,
            # and without this she answers the caption — "yes! send it!" — with the thing
            # already sitting in front of her. `_TURN_RATE_PIC` outranks this beat and
            # covers the readable ones, but an UNREADABLE picture falls to react_pic,
            # which ranks BELOW this — so the guard has to live here, not in the ladder.
            pic_offer_turn = bool(
                describe_on and not c.pic_sent
                and detect_pic_offer(c.last_body))
            # He has, at some point, questioned whether she is real — the one thing the
            # dare's copy is allowed to CALL BACK to. On a merely-hot thread it must not,
            # because he never said it, and inventing a conversation he doesn't remember
            # having is a worse bot tell than the one the dare exists to disprove.
            # ⚠️ RECENT, and that means an explicit WINDOW. The copy this arms says "He
            # questioned whether you're real LAST MESSAGE and you laughed it off", so the
            # signal has to be recent or she is inventing history — the exact bot tell the
            # beat exists to disprove.
            #
            # Two wrong answers came first. `bot_accused_count` never decays, so a man who
            # asked once months ago was told he had just said it. Then `c.messages` — with
            # a comment calling it "the loaded tail", which it is NOT: it is the whole
            # loaded thread, so the bug survived the fix and the comment asserted a bound
            # that did not exist. Slice it explicitly and the tense is finally honest.
            _DARE_CALLBACK_TAIL = 8
            dare_is_callback = any(
                _detect_bot_accusation(b)
                for d, b in c.messages[-_DARE_CALLBACK_TAIL:] if d == "in")

            stale_ask = False   # the FLOOR — computed below, once fan_ladder is loaded
            fan_ladder: LadderState | None = ladders.get(fan_id)
            rung_index = 0
            pending = open_by_fan.get(fan_id)
            # Session TTL is enforced ON READ: a ladder frozen at hot/rung-4 three
            # weeks ago must not wake up and fire "i cant resist anymore 🥵" into a
            # dead thread. Closing it here (not on write) is what makes that
            # impossible even after a restart, a backfill, or a manual DB edit.
            # Idleness is measured from the last thing that HAPPENED — and a fan talking
            # to us is a thing that happened. `session_idle_at` is only stamped on an ask,
            # a payment or a close, so a man chatting away had his session declared dead
            # at SESSION_IDLE_M (20min) while he was mid-sentence, closing the ladder and
            # making the discount resend (RESEND_AFTER_M=90) permanently unreachable for
            # exactly the fans who were still engaged.
            _idle_ref = max([t for t in (fan_ladder.session_idle_at if fan_ladder else None,
                                         c.last_in_at) if t is not None] or [None])
            if fan_ladder is not None and upsell.session_expired(
                    fan_ladder.status, fan_ladder.last_ask_at, _idle_ref, now):
                await _close_ladder(account_id, fan_id, upsell.STATUS_IDLE)
                fan_ladder = None
            # A tap-out DECAYS. Enforced on read, same as the session TTL. Nothing used
            # to reset `tapped`, which made it a one-way door: one ignored PPV retired
            # that fan from every future 1:1 price for the life of the account. The
            # 8-persona simulation put 7 of 8 fans — including a whale who had bought
            # twice and was STILL asking for content — permanently out of reach inside
            # 24h. He said "not now". He did not say "never".
            # STOPPED is deliberately NOT decayed here: that fan threatened a chargeback,
            # and only an operator clearing skip_list brings him back.
            if fan_ladder is not None and upsell.tap_expired(
                    fan_ladder.status, fan_ladder.updated_at, now):
                await _close_ladder(account_id, fan_id, upsell.STATUS_IDLE)
                fan_ladder = None
                taps_expired += 1
            # §4.4 — the HOT window is a strike-while-hot window, NOT a lock-out. Past
            # HOT_ESCALATE_TTL_S a fan who paid and went quiet is DOWNGRADED hot→open
            # (never returned False): the next ask re-prices off the BAND, not off
            # last_paid×ESCALATION_MULT, under the normal 24h staleness. A 12-min lock
            # on a proven buyer (p75 reply latency 908s) was indefensible. Enforced on
            # read, so a restart/backfill can never leave a stale HOT escalating.
            if (fan_ladder is not None and fan_ladder.status == upsell.STATUS_HOT
                    and fan_ladder.last_paid_at is not None
                    and (now - fan_ladder.last_paid_at).total_seconds()
                    > upsell.HOT_ESCALATE_TTL_S):
                await _save_ladder(account_id, fan_id, status=upsell.STATUS_OPEN,
                                   hot_until=None)
                fan_ladder.status = upsell.STATUS_OPEN
                fan_ladder.hot_until = None
            if fan_ladder is not None:
                rung_index = int(fan_ladder.rung_index or 0)

            # THE FLOOR. Some men never go hot — friendly, engaged, chatting for free
            # forever, and never asked for a penny. After `ask_after_fan_msgs` of HIS
            # messages with no ask in front of him (ours OR a human chatter's), put one
            # there even if the scene never turned sexual. Counted FROM the last ask, so
            # it can never stack on a price he is already looking at.
            if ask_after_n > 0:
                _last_ask = _newest(
                    fan_ladder.last_ask_at if fan_ladder is not None else None,
                    human_money.get(fan_id, _NO_MONEY).ask)
                stale_ask = await _fan_msgs_since(
                    account_id, fan_id, _last_ask) >= ask_after_n

            # `_offer_caps_ok` stays ARMED under the gate — it is the ONLY thing
            # bounding non-converted offers per day. The single override: inside the
            # hot window, after a PAID rung, one fan message is enough to earn the
            # next rung (re-offer conversion decays 66.7% <5min → 45.0% >24h; making
            # a buyer type 4 more lines is how the window is missed).
            caps_cfg = cfg
            if (gate_on and fan_ladder is not None
                    and fan_ladder.status == upsell.STATUS_HOT
                    and fan_ladder.last_paid_at is not None):
                caps_cfg = {**cfg, "min_fan_msgs_between_offers": 1}

            # He may hold up to _MAX_OPEN_OFFERS unpaid PPVs at once. With exactly one
            # pending, this turn may send a SECOND (different piece, or the pending one
            # re-priced if he's balking) — so a fan asking for something else, or
            # haggling, gets a real answer instead of a stonewall. A 2nd offer rides
            # close on the 1st's heels, so relax the between-offers spacing for it.
            open_offers_f = (await _open_offers(account_id, fan_id)
                             if pending is not None else [])
            open_count = len(open_offers_f)
            # Lift ONE slot the moment he's opened a PPV / done a tip buy since these
            # asks went on the table: an UNTRACKED unlock (tip_reward box, tip, hand-
            # sent PPV) resolves no offer, so without this the cap stays full and
            # `_pending_block` gags the closer — the "waiting for open won't let me
            # price a new one" stall. A tracked offer he unlocks already frees its own
            # slot by resolving, so this only ever adds the untracked case.
            if open_count and await _unlocked_since_open_offers(
                    account_id, fan_id, open_offers_f):
                open_count -= 1
            second_offer = pending is not None and open_count < open_offer_ceiling
            # Per-fan language: fans.language (manual pin or gen_info detection)
            # overrides the account default; unset → account default. Resolved HERE
            # (not at the prompt-build below) because the haggle detector needs it —
            # a fan balking in his own language must earn the discount re-tease.
            fan_lang = _language.resolve_language(account_lang, getattr(f, "language", None))
            haggling = _language.is_haggle(c.last_body, fan_lang)
            if second_offer:
                caps_cfg = {**caps_cfg, "min_fan_msgs_between_offers": 1}

            # seller_off (spec §6): COMPANION / cooldown window live, or bot-accused
            # this turn — the conversation stays ON but NO priced ask, no pending
            # re-tease, no post_buy. The LLM reply below still runs (she talks).
            if seller_off:
                pass
            # 🚨 ONE unpaid PPV is enough to reach this when the prompt does not sell
            # (see `open_offer_ceiling`). The bar used to be the raw `max_open_offers`
            # (≥2), which is a state a vault sale never produces — so after
            # `pack_sender` charged him for a box, the very next turn told the model
            # "don't offer pics or videos yet" and she could not answer "whats in it?"
            # about the thing he was looking at. Silent: no log, no counter. With the
            # catalogue gone from the prompt this is the ONLY surface that can speak
            # about a live offer at all.
            #
            # ⚠️ WITH the catalogue in the prompt the ceiling is unchanged, and it has
            # to be: one pending PPV must NOT land here, because `_second_offer_block`
            # owns that state and is what allows a second rung before the cap.
            elif pending is not None and open_count >= open_offer_ceiling:
                # Max unpaid PPVs already on the table — stop pitching, just chat.
                sell = _pending_block(pending, await _get_item(int(pending.item_id)))
            # ⚠️ THE CATALOGUE IS NOT THE PRECONDITION ON SELLING — HAVING SOMETHING
            # TO SELL IS. This read `elif catalog_items and ...`, and that is the
            # reason bb4125b's fix did nothing: that commit corrected the manifest's
            # own gate (`if offerable or voice_blocks.sell_customs`), which lives
            # INSIDE this branch, so on an account with an empty catalogue the
            # branch holding it never ran. A custom is recorded to order and needs
            # no rows at all, so gating on the one inventory that happens to have a
            # table switched selling off for every account that sells the other way.
            #
            # But the fence still has a job, and dropping it entirely was the
            # over-correction: `_offer_caps_ok` is 1-3 per-fan queries, and it was
            # then being paid on every fan of every account that can sell NEITHER —
            # exactly the accounts where the whole branch is provably a no-op
            # (`offerable` is empty, and `if offerable or v.sell_customs` below is
            # false, so `sell` stays `_NO_SELL`). The condition is the disjunction
            # the branch body already tests, hoisted to where it costs nothing.
            elif ((catalog_items or v.sell_customs)
                    and await _offer_caps_ok(account_id, fan_id, caps_cfg)):
                offerable = await _offerable_for_fan(account_id, fan_id,
                                                     cfg_offer_mode, scripts,
                                                     catalog_items)
                # §6.2 — rolling 7-day spend brake. Past the account's cap he is not
                # silenced, he stops being CHARGED: downgrade to COMPANION for the
                # window. Dollars PAID (not asks converted) — the buy-COUNT stop is dead.
                if gate_on and offerable:
                    cap7 = int(cfg.get("spend_velocity_cap_7d_cents")
                               or upsell.SPEND_VELOCITY_CAP_7D)
                    if cap7 and await _paid_cents_7d(account_id, fan_id, now) >= cap7:
                        await _save_ladder(account_id, fan_id,
                                           status=upsell.STATUS_COMPANION,
                                           companion_until=now + timedelta(hours=24))
                        offerable = {}
                        seller_off = True
                        spend_capped += 1
                        log.info("ai_chatter 7d spend cap → COMPANION account=%s fan=%s",
                                 account_id, fan_id)
                if gate_on and offerable:
                    # THE GATE. Live conversational signal only — lifetime spend is
                    # not an input. Selling into silence is the actual gap (ppv_send
                    # converts 0.67% per send; the same offer on a live thread pays
                    # 12.41%). A blocked price is not a lost sale, it is a DEFERRED
                    # one: park it and it fires on his next qualifying inbound.
                    lad_status = (fan_ladder.status if fan_ladder is not None
                                  else upsell.STATUS_IDLE)
                    rctx_gate = rhythm.RhythmCtx(
                        account_id=str(account_id), fan_id=fan_id,
                        voice=v.voice,
                        pace_buckets=bool(cfg.get("rhythm_pace_buckets")),
                        pace_curve=rhythm_curve,
                        sleep_window=sleep_win, tz_offset_minutes=tz_off,
                        no_sleep=rhythm_no_sleep, enabled=rhythm_on)
                    # Same creator-local day the WRITE path stamps (see daily_day
                    # below) — the two must agree or the counter never rolls over.
                    local_day = rhythm.local_now(now, tz_off).strftime("%Y-%m-%d")
                    ok, why = upsell.qualify(upsell.GateCtx(
                        fan_id=fan_id, last_inbound_text=c.last_body,
                        last_inbound_at=c.last_in_at, last_outbound_at=c.last_out_at,
                        fan_spoke_last=(c.last_dir == "in"), status=lad_status,
                        offers_paused_until=(fan_ladder.offers_paused_until
                                             if fan_ladder is not None else None),
                        # A human's live PPV is an ask ALREADY IN FRONT OF HIM, so it
                        # paces ours: without it she stacks a second price on top of a
                        # chatter's unpaid $45 seconds after he was quoted it.
                        last_ask_at=_newest(
                            fan_ladder.last_ask_at if fan_ladder is not None else None,
                            human_money.get(fan_id, _NO_MONEY).ask),
                        asks_today=int(asks_by_fan.get(fan_id, 0)),
                        # ...but ONLY today's. daily_ask_cents is a running total
                        # stamped with the creator-local day it belongs to; read
                        # without checking that stamp it never resets, so a fan who
                        # crosses the ask ceiling once is blocked from every 1:1 offer
                        # for the rest of his life. A stale day is a zero.
                        daily_ask_cents=(int(fan_ladder.daily_ask_cents or 0)
                                         if (fan_ladder is not None
                                             and fan_ladder.daily_day == local_day)
                                         else 0),
                        account_offers_last_hour=acct_hour_asks,
                        account_offers_today=acct_day_asks,
                        # Never OPEN a ladder we cannot finish before she sleeps: a
                        # hot ladder abandoned at 01:58 that resumes at 09:00 with
                        # "goodmorning baby" + a $79 rung is a disaster.
                        ladder_may_open=(rhythm.ladder_may_open(now, rctx_gate)
                                         if rhythm_on else True),
                        # HE is pulling — an explicit content-ask or a NAMED PRICE (NOT
                        # mere arousal). The ONLY thing that lifts a spend-regret offers-
                        # pause: 11-18% of "broke" fans buy anyway, and 37/37 of them
                        # bought a FRESH offer, so his own real buy-signal is the realness
                        # test. Pure "so horny" must never re-price a man who said he's out.
                        fan_pull=_fan_pull(c),
                    ), now)
                    if not ok:
                        gate_blocked += 1
                        # EXPLICIT-ASK OVERRIDE: he DIRECTLY asked to see/unlock content
                        # and he isn't braked (seller_off already caught the broke/
                        # declined/companion fan, and the 7d spend cap above sets it too).
                        # The gate's deferral is for thin/quiet threads; on a direct ask,
                        # sending nothing is the worst outcome. Keep the inventory so the
                        # ask-trigger forces a REAL PPV — still ownership-checked so we
                        # never re-sell what he already owns.
                        if explicit_ask and not seller_off and offerable:
                            ask_override = True
                            await _drop_owned(account_id, fan_id, offerable)
                            log.info("ai_chatter explicit-ask gate override "
                                     "account=%s fan=%s why=%s", account_id, fan_id, why)
                        else:
                            # A DECISION (he stopped / tapped out / declined) is final.
                            # A TIMING problem is not: park the offer so the gate can
                            # defer revenue instead of only deleting it.
                            if why in ("low_information", "we_spoke_last", "stale"):
                                first = (sorted(offerable.items())[0][1]
                                         if offerable else None)
                                await _park_pending_offer(account_id, fan_id, first, now)
                                offers_parked += 1
                            offerable = {}
                            log.debug("ai_chatter gate blocked account=%s fan=%s why=%s",
                                      account_id, fan_id, why)
                    else:
                        gate_ok = True
                        # He qualified — a parked offer (if any) fires NOW, on the
                        # SAME piece he was going to be offered, not a fresh roll.
                        parked_id = await _clear_pending_offer(account_id, fan_id)
                        if parked_id is not None and parked_id in offerable:
                            offerable = {parked_id: offerable[parked_id]}
                        # Re-checked before EVERY rung — see _drop_owned.
                        await _drop_owned(account_id, fan_id, offerable)
                if pricing_on and offerable:
                    fstate = await _fan_ladder_state(account_id, fan_id, f, fan_ladder)
                    pfloor = _proven_floor_cents(fstate, cfg)
                    priced: dict[int, CatalogItem] = {}
                    for iid, it in offerable.items():
                        if it.is_free_teaser:
                            priced[iid] = it        # free is free — never quoted
                            continue
                        q = _quote_item(account_id, fstate, it, rung_index=rung_index,
                                        media_asks=media_asks, median=acct_median,
                                        bounds=lib_bounds,
                                        escalation_mult=cfg.get("escalation_mult"),
                                        max_ask_vs_history_mult=cfg.get("max_ask_history_mult"),
                                        proven_floor_cents=pfloor)
                        if q is None:
                            continue    # he cannot plausibly afford THIS item — the
                                        # selector picks a cheaper one. Never discount
                                        # a flagship down to a fan's ceiling.
                        quotes[iid] = q
                        priced[iid] = it
                    offerable = priced
                # SECOND OFFER + he's balking on PRICE → re-price the PENDING piece a
                # light `haggle_pct` cheaper (default 10%) for the re-tease. A believable
                # nudge off the number he already saw — never below the $3 OF floor and
                # always at least $1 under the original — so "a lil lower?" gets a real,
                # modest discount rather than the same cold quote again.
                if (second_offer and haggling and pricing_on
                        and int(pending.item_id) in offerable
                        and int(pending.item_id) in quotes):
                    orig = int(pending.price_cents or pending.tip_unlock_cents or 0)
                    disc = int(round(orig * (1.0 - haggle_pct)))
                    disc = max(upsell.OF_PRICE_FLOOR_CENTS, min(disc, orig - 1))
                    if disc > 0:
                        quotes[int(pending.item_id)] = dataclasses.replace(
                            quotes[int(pending.item_id)],
                            price_cents=int(disc), clamped_by="discount")
                # Tip ladder (workstream 3): for TIP-ONLY items, replace the PPV
                # quote with the INDEPENDENT adaptive tip ask. Overriding the
                # quote (not just the record) keeps the manifest the model asks
                # from, ask_cents, and the recorded tip threshold all in lockstep.
                if cfg.get("tip_ladder_enabled") and pricing_on and offerable:
                    for iid, it in list(offerable.items()):
                        if it.is_free_teaser or iid not in quotes:
                            continue
                        if _effective_mode(it, cfg_offer_mode) != "tip":
                            continue
                        # Cap the tip climb at the account's PPV-library MAX (≤$200).
                        tip_cap = min(int(lib_bounds[1] or upsell.OF_PRICE_MAX_CENTS),
                                      upsell.OF_PRICE_MAX_CENTS)
                        amt = await _next_tip_ask_cents(account_id, fan_id, it, cfg,
                                                        cap_cents=tip_cap)
                        quotes[iid] = dataclasses.replace(quotes[iid], price_cents=int(amt))
                # `offerable` is the CATALOGUE. Gating the whole manifest on it
                # meant the customs carve-out — the one thing sellable when the
                # vault is bought out — only rendered for accounts that still had
                # something else to sell. Backwards for the account it was
                # written for, and silent: no error, just an engine that never
                # mentions the product.
                if offerable or v.sell_customs:
                    # `_second_offer_block` describes "one more piece alongside
                    # the pending one" and takes the same catalogue — with an
                    # empty one it has nothing to describe, so the customs-only
                    # manifest owns the bought-out case on both branches.
                    if second_offer and offerable:
                        sell = _second_offer_block(
                            pending, await _get_item(int(pending.item_id)),
                            offerable, scripts, cfg_offer_mode, quotes or None,
                            sell_customs=v.sell_customs)
                    else:
                        sell = _manifest_block(offerable, scripts, cfg_offer_mode,
                                               quotes=quotes or None,
                                               sell_customs=v.sell_customs)

            # fan_lang resolved above (haggle detection) — drives the reply language
            # AND the bilingual buy-signal detectors below.
            #
            # `sell.close` — "may we pitch at all" — replaces the `bool(offerable)`
            # these both used to test. Same answer on every catalogue path, and it
            # fixes the bought-out one: `offerable` is empty there BY DEFINITION, so
            # "show me something" — the strongest buying signal there is — was the
            # one turn guaranteed to sell nothing.
            content_ask = (bool(sell.close)
                           and _language.is_content_ask(c.last_body, fan_lang))
            # 🚨 THE VAULT LANE'S OWN TRIGGER — his words, and nothing else.
            # `content_ask` above is AND-ed with `sell.close`, which is only ever
            # set by `_manifest_block` / `_second_offer_block` / `_pending_block`,
            # all of them built from `offerable` — i.e. from the CATALOG. Riding it
            # would re-couple the vault lane to the catalog one level above where
            # that coupling was just removed, and on an empty shelf the lane would
            # again be dead however plainly he asked. The catalog surfaces keep
            # `content_ask`; the vault lane gets the fan.
            vault_ask = _language.is_content_ask(c.last_body, fan_lang)
            # Lean-in pivot: he's getting physical/horny (ESCALATION) with something
            # to sell and HAS chatted a bit — ride it as an offer instead of teasing
            # again. An explicit content-ask already owns the pivot, so don't
            # double-count. Offer pacing caps still gate whether the surface is live.
            escalation = (bool(sell.close) and pivot_on_escalation
                          and not content_ask
                          and c.fan_msg_n >= esc_min_msgs
                          and _language.is_escalation(c.last_body, fan_lang))
            # ── WHICH BEAT OWNS THIS TURN. Asked ONCE, here, because this is where the
            # last of its inputs (content_ask, escalation, sell.close) finally bind.
            # Everything below reads `kind`; nothing re-derives the ordering.
            kind = _turn_kind(
                bot_accused=bot_accused_turn,
                pic_desc=c.last_in_desc if rate_pic_turn else "",
                content_ask=content_ask,
                escalation=escalation,
                image_dare=image_dare_ok,
                dare_callback=dare_is_callback,
                pic_offer=pic_offer_turn,
                hot_thread=hot_thread,
                can_sell=bool(sell.close))
            # ── A NO-SALE BEAT SELLS NOTHING, and that is said ONCE, here.
            #
            # Gating the four attach sites individually is how the last one gets
            # forgotten — the convo-teaser ladder was, and it would have stapled a $10
            # rung onto a message whose own copy reads "no price, no offer line".
            # Clearing the surface also stops the prompt from advertising the `>>OFFER`
            # carve-out on a turn that bans it, which is the third way a price got onto
            # a dare. `kind` was already decided using the OLD sell.close, so the
            # content_ask/escalation rungs it could have won are unaffected.
            if kind in _TURNS_NOT_SELLING:
                sell = _NO_SELL
            buyer_facts = await _buyer_facts(account_id, fan_id)
            # Which bubble is he answering? Three tiny reads, and only for a fan who
            # has cleared every gate and is getting a reply this tick.
            if reply_ctx_on:
                c.reply_ctx = await _quotes.resolve(account_id, fan_id)
            # Cat-sticker roll (code-side rate control): most replies never see
            # the sticker protocol at all; "allow" lets the model judge, "solo"
            # nudges a sticker-ONLY reply. Deterministic per reply (fan + his
            # latest text) so a re-run rolls the same. Cooldown forces skip.
            sticker_mode = "skip"
            if stickers_on:
                sticker_mode = cat_stickers.roll_mode(
                    random.Random(f"sticker:{account_id}:{fan_id}:{c.last_body}"),
                    cat_stickers.cooldown_active(account_id, fan_id,
                                                 gap_min=sticker_gap_min),
                    skip_w=sticker_skip_w, solo_w=sticker_solo_w)
                # Open with the gif on his very first message — see
                # cat_stickers.open_with_gif. Reads his OWN words (last_in_text), not
                # the history line, which carries the `[he sent: …]` vision tag.
                sticker_mode = cat_stickers.open_with_gif(
                    sticker_mode, turn_index=int(c.fan_msg_n),
                    his_words=c.last_in_text)
            # Two turns REQUIRE words, so don't even offer the protocol: with mode
            # 'skip' `prompt_block` renders nothing and the model cannot pick a tag.
            #
            # There is already a guard on the drop side that refuses a sticker as the
            # answer to a bot accusation — but refusing it AFTER generation turns a
            # sticker-only reply into SILENCE, which answers "You a real person?" with
            # nothing at all. That is a worse answer than the gif was. Seen on a replay
            # of thread 581112404: the model returned "STICKER: eyeroll", the guard
            # dropped it, `dropped_empty: 1`, and she never spoke.
            #
            # Same reasoning for the picture he just sent: a gif is precisely the
            # non-reaction the rating beat exists to replace.
            if kind in _TURNS_NEEDING_WORDS:
                sticker_mode = "skip"
            # The deepen phase: once there is nothing left to ask, work in a gen_info
            # opener instead of generic banter. ai_chatter has no graduation cutoff,
            # so None just means banter — never silence, and never a handoff.
            # No refill is triggered from here: `_maybe_refresh_profile` already runs
            # after every reply below, so a spent pool restocks on the ordinary path.
            opener = None
            # Gated AND rationed. `_questions_still_needed` only says the bio gaps
            # are filled; it does not say THIS turn wants a question. Without the
            # roll every reply to a gathered fan carries one, for ever.
            if not _questions_still_needed(f, asked) and _openers.should_offer(
                    enabled=openers_on, rate=openers_rate,
                    seed=f"opener:{account_id}:{fan_id}:{c.last_body}"):
                opener = _openers.next_for(f, profiles.get(fan_id),
                                           today=opener_day)
            # Which beat this reply is being REQUIRED to carry, "" when he did not
            # ask or she has already told him today's. Computed here so the confirmed
            # -send stamp below and the prompt block agree by construction rather
            # than by two independent evaluations of the same predicate.
            day_required_beat = day.required_beat(f, c.last_in_text or "")
            # 🙋 HAS HE ASKED WHO SHE IS? Her canon table rides the prompt only then.
            #
            # STICKY, and that is the whole design. A man who asks her age on turn 4
            # and follows up on turn 5 ("and ur from where?") must still be talking to
            # someone who knows — a gate read fresh off each inbound would hand him a
            # prompt with no facts on the follow-up, which is precisely the thread
            # shape the bio guardrail exists for. So: this run's inbound, OR the bit
            # stamped on a previous confirmed send. Same state boundary as the day-log
            # ledger, so there is one place per-fan memory lives, not two.
            #
            # The stored bit is named separately because the confirmed-send stamp below
            # reads it again: it writes only when the canon actually shipped and the
            # bit was not already set, which is exactly "he asked for the first time".
            #
            # ⚠️ GATE OFF IS "keep", NOT "hide". With the gate off her canon rides the
            # SYSTEM prompt exactly as it always has — that is what an escape hatch is
            # for. Collapsing the two into one boolean is how the first cut deleted her
            # canon from both messages; see the `canon` parameter.
            bio_was_asked = fan_state(f, _BIO_ASKED_KEY).get("asked") is True
            bio_asks_now = any(_persona_asks_about_her(b)
                               for d, b in c.messages[-_HISTORY_TAIL:] if d == "in")
            canon = ("keep" if not bio_gate_on
                     else "show" if (bio_was_asked or bio_asks_now) else "hide")
            msgs, presented = _build_messages(persona, f, c, asked, history_tail,
                                              shape=_shape,
                                              canon=canon,
                                              custom_owed=_customs.is_owed(f),
                                              opener=opener,
                                              sticker_mode=sticker_mode,
                                              style_on=style_on,
                                              nonnative_on=nonnative_on,
                                              sell=sell,
                                              content_ask=content_ask,
                                              hot_thread=hot_thread,
                                              bot_accused=bot_accused_turn,
                                              kind=kind,
                                              painful_on=painful_on,
                                              lang=fan_lang,
                                              profile=profiles.get(fan_id),
                                              ask_every=(old_q_every
                                                         if fan_id in old_fan_ids
                                                         else 0),
                                              buyer_facts=buyer_facts,
                                              clock=_clock_line(clock_tz),
                                              day=day,
                                              sell_signal=signal_on,
                                              v=v)
            try:
                res = await llm_client.chat(
                    model=model,
                    messages=msgs,
                    purpose=_PURPOSE,
                    account_id=account_id,
                    fan_id=fan_id,
                    temperature=_REPLY_TEMPERATURE,
                )
            except LLMCapExceeded:
                cap_hit = True
                log.warning("ai_chatter daily LLM cap reached account=%s — stopping",
                            account_id)
                break
            except Exception:
                errors += 1
                log.warning("ai_chatter generate failed account=%s fan=%s",
                            account_id, fan_id, exc_info=True)
                continue

            raw = (res.content or "").strip()
            # Offer marker: parse + ALWAYS strip protocol lines, then validate
            # the id against the code-side manifest (price/terms come from the
            # catalog row — the model never sets them).
            raw, offer_id = _parse_offer_marker(raw)
            # Sticker marker: ALWAYS strip protocol lines (a fan must never see
            # them); honor the tag only when this reply's roll offered the pack.
            raw, sticker_tag = cat_stickers.parse_marker(raw)
            # Sell signal: the model's own read of "did he ask to buy". ALWAYS
            # stripped (a fan must never see the protocol) even with the block
            # off, because a model that has seen it once in a prefix-cached
            # conversation can emit it later unprompted. ARMED 2026-08-15 — `decide`
            # is an OR now, and the same verdict rides the Turn into the lane below,
            # which is where the authoritative detector lives.
            raw, _model_ask = _sell_signal.parse(raw)
            if signal_on:
                vault_ask = _sell_signal.decide(
                    regex_says=vault_ask, model_says=_model_ask,
                    account_id=account_id, fan_id=fan_id, engine=_PURPOSE)

            # A rating with no number in it is not a rating — re-ask once. Placed AFTER
            # the marker parse on purpose; see _scored_or_re_asked for why.
            #
            # SCOPED TO `_TURN_RATE_PIC` ON PURPOSE, and the reason is worth stating
            # because it is a judgement, not an oversight: `_TURN_REACT_PIC` now asks for
            # a score too, but a missing number on a dick pic loses the whole product of
            # the beat, while a missing number on his dog is a shrug. So the rating is
            # worth a second paid call and the reaction is not.
            #
            # `rate_pic_turns` counts the same set for the same reason — it is the
            # denominator for the re-ask spend, so it has to cover exactly the turns the
            # re-ask can fire on.
            #
            # (This read `_is_rateable` and quoted the prompt as saying "Do NOT score it";
            # neither survived — the branch reads `kind`, and that directive was reversed
            # with the operator's 08-09 change. A comment justifying a guard by quoting a
            # string that no longer exists is worse than none.)
            rate_pic_turns += kind == _TURN_RATE_PIC
            pic_offer_turns += kind == _TURN_PIC_OFFER
            if kind == _TURN_RATE_PIC:
                try:
                    raw, re_asked, rescued = await _scored_or_re_asked(
                        raw, msgs, model=model, account_id=account_id, fan_id=fan_id)
                    ratings_re_asked += re_asked
                    ratings_rescued += rescued
                except LLMCapExceeded:
                    # Keep the first draft and let it send — it is already paid for and
                    # silence is the worse answer. The sweep stops on the NEXT fan, whose
                    # generation raises the same cap above and breaks the loop there.
                    cap_hit = True
                    log.warning("ai_chatter cap reached on rating re-ask account=%s",
                                account_id)
                except Exception:
                    log.warning("ai_chatter rating re-ask failed account=%s fan=%s",
                                account_id, fan_id, exc_info=True)
            if sticker_mode == "skip":
                sticker_tag = None
            # Never the same reaction twice running — see cat_stickers.keep_tag.
            if sticker_tag is not None:
                sticker_tag = cat_stickers.keep_tag(
                    account_id, fan_id, sticker_tag, has_text=bool(raw))
                if sticker_tag is None:
                    log.info("ai_chatter sticker tag repeat dropped account=%s "
                             "fan=%s", account_id, fan_id)
            offer_item = offerable.get(offer_id) if offer_id is not None else None
            if offer_item is not None and kind in _TURNS_NOT_SELLING:
                # The model wrote an offer marker on a turn the prompt told it not to.
                # Honour the beat, not the slip.
                log.info("ai_chatter offer marker dropped on a no-sale beat "
                         "account=%s fan=%s kind=%s", account_id, fan_id, kind)
                offer_item = None
                offer_id = None
            if offer_id is not None and offer_item is None:
                log.info("ai_chatter offer marker rejected account=%s fan=%s id=%s",
                         account_id, fan_id, offer_id)
            # force_ask — the thread is HOT, the gate said YES, and the model still wrote
            # no marker. Sell anyway.
            #
            # `hot_thread` is the load-bearing condition, not `gate_ok`. The gate is a
            # floor, not a signal: replayed over Cody's real thread it said "would price"
            # on essentially every message, so forcing on gate_ok alone would have priced
            # a man mid-smalltalk — and, before the brakes were fixed, minutes after he
            # said he was broke. thread_heat is 24.3x on the purchase; the gate is ~1x.
            #
            # seller_off is still respected (a COMPANION/cooldown/bot-accused turn is a
            # deliberate talk-only turn), and `offerable` is already empty whenever the
            # gate refused him or the offer caps are spent.
            # Two independent triggers, both riding the gate and both respecting
            # seller_off — so neither can ever price a man who said he is broke:
            #   • force_ask  — the thread is HOT (24.3x on the purchase). The moment.
            #   • ask        — he EXPLICITLY asked to see/unlock content this turn and the
            #                  gate cleared him. Sending nothing (or narrating a phantom
            #                  "sending it now") on a direct ask is the worst outcome; the
            #                  ask regex bounds it (not every message), gate_ok means he
            #                  qualified, and the block below still respects seller_off /
            #                  the broke-declined pause. Real content beats silence.
            #   • stale_ask  — he has talked this long and nobody has ever asked him for
            #                  anything. The floor.
            # A turn whose whole instruction is "no price, no offer line" must not have
            # one forced onto it — see _TURNS_NOT_SELLING.
            _trigger = (None if kind in _TURNS_NOT_SELLING else
                        "hot" if (force_ask and hot_thread) else
                        "ask" if (force_ask and content_ask
                                  and (gate_ok or ask_override)) else
                        "stale" if stale_ask else None)
            # 🚨 The PACK lane rides the ASK, not `force_ask` — they are not the
            # same permission. `force_ask` ships OFF because it INITIATES, and
            # answering a man who typed "send me a joi" initiates nothing.
            # Riding `_trigger == "ask"` conflated the two and left
            # `pack_on_ask_enabled: True` inert on 12 of 20 live accounts (one
            # fan asked twice; the resolver was never called once).
            #
            # Wider than the old arm in exactly two ways, both deliberate:
            # force_ask OFF, and a HOT thread with an explicit ask — where
            # `_trigger` says "hot" and the pack used to lose to `_force_pick`'s
            # cheapest item. What he ASKED for beats the cheapest thing on the
            # shelf. Every guard that made the old arm safe still applies below.
            # Per-TICK account budget on forced asks. _offer_caps_ok does NOT pace a
            # fan's FIRST offer (its min-msgs branch is skipped when he has no prior
            # ContentOffer), so on the tick force_ask/floor is first enabled, EVERY
            # long-standing never-offered fan trips at once — ~1,283 fans on this roster.
            # That is a burst of priced sends in one minute, exactly the OF-session shape
            # the per-hour cap exists to prevent, and the per-run snapshot of that cap
            # can't see it. This bounds the blast: the floor drips instead of flooding.
            # ── He asked for a SUBJECT: sell him that, not the cheapest thing ──
            #
            # `_force_pick` below takes the CHEAPEST offerable item, which is the
            # right answer to "he seems warm" and the wrong one to "send me feet
            # pics". When the pack lane is on, an explicit content-ask is answered
            # from the curated shelf for that subject instead, priced by value.
            #
            # It sends its OWN priced message and leaves `offer_item` None, so the
            # reply this turn stays unpriced and he gets exactly one ask. Any
            # refusal (no curated shelf, shelf too thin, verifier rejected) falls
            # straight through to the normal path — this can only ever REPLACE a
            # cheapest-item offer, never suppress one.
            # 🚨 It runs even when the MODEL wrote an offer marker, and that is the
            # whole point. `offer_item is None` used to guard this block, so the
            # resolver was skipped on exactly the turns it was built for: a fan
            # who NAMES a subject is the most likely turn for the model to sell
            # into, and its marker is a blind pick from the priced manifest.
            # Measured live 2026-08-12, minutes after this lane first shipped —
            # "Do you send ass pics?" was answered with a catalog item called
            # "Quick rate" at $8.69, captioned "maybe later if ur lucky" with the
            # pics attached. The resolver never ran.
            #
            # A named ask beats a blind pick. When the vault lane takes the turn it
            # clears the model's marker below, so the reply ships unpriced and he
            # gets exactly ONE ask — the one that answers his words.
            # 🚨 THE VAULT LANE MUST NOT NEED A CATALOG. This used to also require
            # `gate_ok or ask_override`, and both are only ever set inside the
            # `elif (catalog_items or v.sell_customs) and _offer_caps_ok` branch
            # above — derived from `catalog_items`, which the pack lane never reads
            # (it resolves media from the operator-curated vault folders). So on an
            # account with a thin or empty shelf the vault lane could not fire
            # however plainly the fan asked: 2 offers fleet-wide in 14 days against
            # 67 blind cheapest-item picks (measured 2026-08-15).
            #
            # Nothing is unguarded by dropping them. `lane.sell` runs every brake
            # AND `upsell.qualify` itself, and its `on` covers the two shelf
            # switches this condition used to restate. It is the single decision
            # point — there is no second gate here to keep in step with it. That is
            # also what keeps the lane alive once catalog selling comes out of the
            # chat prompt: `catalog_items` is empty everywhere and this is the only
            # path left.
            #
            # `forced_this_tick` stays the binding budget: it counts the catalog
            # forced-ask and the paid hot teaser too, so it is strictly broader than
            # the lane's own, which therefore never binds first.
            #
            # 🚨 DECIDED HERE, SENT AFTER THE REPLY. He asked a question; the
            # answer belongs in front of him before the priced box that answers
            # it, and a PPV arriving first with the reply behind it reads as a
            # vending machine. Deferring the SEND alone does not work — the
            # decision has to happen here, because `offer_item` must be cleared
            # before the draft is finalised (the reply ships unpriced) and whether
            # to clear it is only knowable once the resolver has actually found
            # media. Guessing early deletes a live catalog offer on every ask the
            # vault then refuses. So `pack_sender` is split along the seam it
            # already had: `plan_on_ask` decides, `deliver` sends.
            #
            # A plan that is never delivered has sent nothing and charged nothing.
            # Every path that abandons this turn below (he wrote again, the send
            # failed, rhythm parks the reply) simply drops it, and the next tick
            # decides again on the same words.
            _pack_plan: "sell_lane.SellPlan | None" = None
            if (kind not in _TURNS_NOT_SELLING and vault_ask
                    and forced_this_tick < _MAX_FORCED_ASKS_PER_TICK):
                # Through the shared seam, not straight at pack_sender — so the
                # closer and every other engine pass the identical brakes. The
                # per-fan lease taken at the top of this loop is still held, which
                # is what the seam requires: leases are not re-entrant, so it must
                # never take or release one of its own.
                _pack_plan = await lane.plan(
                    # `_model_ask` is the model's own line on THIS turn, parsed off
                    # the draft above — the lane ORs it with its regex, and without
                    # it a model-only ask is refused `R_NOT_ARMED` inside the gate
                    # however plainly `vault_ask` said yes out here.
                    fan_id, _sell_turn(c, fan_lang,
                                       model_says_ask=signal_on and _model_ask),
                    fan=f,
                    blocked=seller_off,
                    counters=sell_lane.AskCounters(
                        asks_today=int(asks_by_fan.get(fan_id, 0)),
                        account_hour=acct_hour_asks, account_day=acct_day_asks),
                    human_ask_at=human_money.get(fan_id, _NO_MONEY).ask)
                if _pack_plan:
                    # Charged on the DECISION, not the delivery. The per-tick
                    # budget exists to bound a burst of priced sends across the
                    # roster, and a plan this fan is holding is a send this tick
                    # intends to make. Counting it late would let three fans each
                    # plan against the same free slot.
                    forced_this_tick += 1
                    # He gets ONE ask, and it is the one that answers his words.
                    # Leaving the model's marker set would price the reply too and
                    # put two offers in front of him on the same turn — the shape
                    # `max_open_offers` exists to bound.
                    if offer_item is not None:
                        log.info("ai_chatter vault ask SUPERSEDES model offer "
                                 "account=%s fan=%s item=%s", account_id, fan_id,
                                 offer_item.id)
                        offer_item = None
                        offer_id = None

            if (_trigger and (gate_ok or (_trigger == "ask" and ask_override))
                    and not seller_off
                    and offer_item is None and offerable
                    and not _pack_plan
                    and forced_this_tick < _MAX_FORCED_ASKS_PER_TICK):
                offer_item = _force_pick(offerable, quotes)
                if offer_item is not None:
                    offers_forced += 1
                    forced_this_tick += 1
                    if _trigger == "stale":
                        offers_forced_stale += 1
                    log.info("ai_chatter FORCED ask (%s; model wrote no marker) "
                             "account=%s fan=%s item=%s",
                             _trigger, account_id, fan_id, offer_item.id)
            offer_mode_eff = (_effective_mode(offer_item, cfg_offer_mode)
                              if offer_item is not None else None)
            # The shared send chokepoint — off-platform guard, then PHASE 2. See
            # _outbound for why the sequence lives there and not inline. ai_chatter
            # matters most for PHASE 2: it has NO turn cap, so the 383- and
            # 966-turn threads that produced the contradictions all live here.
            raw, _leak = await finalize_draft(
                raw, account_id=account_id, fan_id=fan_id, purpose=_PURPOSE,
                consistency=ConsistencyCtx(
                    fan=f, persona=persona, model=model,
                    last_inbound=c.last_body or "") if consistency_on else None,
                strip_emoji=strip_emoji_on, v=v)
            if _leak:
                offer_item = None  # a guarded reply must not carry a paid attach
            # Hot-thread teaser — the thread is HOT, nothing priced is going out this
            # turn, and he is not braked: attach a few unseen vault items to the reply
            # she is already sending (free warm-up for a $0 fan, a priced tease PPV for a
            # proven buyer). Not an extra message, not gated on a catalog offer — the
            # images are the lead-up a hot thread otherwise never gets. Guarded on the
            # SAME brakes as a priced ask (seller_off / offers-paused / a leaked reply),
            # so a broke or declined man is never sent a paid tease.
            teaser: dict | None = None
            # NOTE: no `pending is None` guard — a hot teaser RIDES ALONGSIDE an open
            # PPV. The old guard meant an unopened offer suppressed every teaser for the
            # full stall_ttl (6h), which is exactly the window the thread is hottest; the
            # images never landed while he was warm. The teaser attaches media to the
            # reply and records only a VaultSend (no ContentOffer), so it never creates a
            # second pending that would block the next one. `offer_item is None` still
            # stops us stacking a teaser on a FRESH ask fired this same turn.
            if (teaser_cfg is not None and hot_thread and not dry_run
                    and offer_item is None and not _leak
                    and not seller_off
                    and not (fan_ladder is not None and fan_ladder.offers_paused_until
                             and fan_ladder.offers_paused_until > now)):
                try:
                    teaser = await _tip_reward.pick_hot_teaser(
                        client, account_id, fan_id,
                        lifetime_spend_cents=int(getattr(f, "lifetime_spend_cents", 0) or 0),
                        tcfg=teaser_cfg, now=now)
                except Exception:
                    log.debug("ai_chatter hot_teaser pick failed account=%s fan=%s",
                              account_id, fan_id, exc_info=True)
                # A PAID teaser is a priced send — bound them per run exactly like forced
                # asks, so flipping the flag can't blast every hot thread on the account
                # in one minute. Free teasers are per-fan capped + on cooldown already.
                if (teaser is not None and teaser["price_cents"] > 0
                        and hot_teaser_paid_tick >= _MAX_FORCED_ASKS_PER_TICK):
                    teaser = None
            # Conversational ladder — the NON-hot path. If the hot teaser didn't fire and
            # he's chatted `after` messages since his last tease, drop the next rung
            # (free → $10 → $50). Same brakes: never on a bot-accused/companion turn
            # (seller_off), and a PAID rung never reaches an offers-paused (broke/declined)
            # fan — a FREE rung still may, to keep an ordinary chat warm.
            # No `pending is None` guard here either — the convo ladder likewise rides
            # alongside an open PPV (see the hot-teaser note above). A PAID rung still
            # obeys the broke/declined pause brake below.
            #
            # TEASER RESEND: he balked on a priced teaser he hasn't unlocked ("cheaper?")
            # → resend that SAME media up to `teaser_disc_pct` off (max 20%). A believable
            # one-time drop, not a fresh rung; the rung doesn't climb. Skipped for a broke/
            # declined fan (seller_off / _leak) exactly like a paid rung.
            # NOTE: teaser_convo_ignore_brakes deliberately does NOT apply here — it lifts
            # the brakes for the PROACTIVE escalation ladder (0/10/40/80), not for a
            # reactive discount-on-balk. A haggle re-push at a companion/broke fan stays
            # braked on purpose (that's a de-escalation the operator didn't ask to force).
            if (teaser is None and haggling and not dry_run and not _leak
                    and not seller_off and offer_item is None):
                _prev = await _last_unpaid_teaser(account_id, fan_id)
                # A resend is a PAID send — obey the broke/declined pause brake, same as
                # any priced rung, so a man who said he's out is never re-pitched.
                _lad2 = (fan_ladder if fan_ladder is not None
                         else (await _load_ladders(account_id, [fan_id])).get(fan_id))
                _paused2 = (_lad2 is not None and _lad2.offers_paused_until
                            and _lad2.offers_paused_until > now)
                # ONLY resend an ORIGINAL teaser rung ($10/$30/$50). A resend's price is
                # NOT a rung price, so a second haggle can't re-discount it — no spiral.
                # Hard-sell PPVs (non-rung prices) are re-priced by the offer haggle path,
                # not here.
                _rung_prices = {int(r.get("price_cents") or 0)
                                for r in (convo_teaser_cfg or {}).get("rungs", [])}
                if (_prev and not _paused2
                        and _prev["price_cents"] in _rung_prices):
                    _dp = int(round(_prev["price_cents"] * (1.0 - teaser_disc_pct)))
                    _dp = max(upsell.OF_PRICE_FLOOR_CENTS,
                              min(_dp, _prev["price_cents"] - 1))
                    if 0 < _dp < _prev["price_cents"]:   # strictly cheaper, else stop
                        _tstate = _tip_reward.teaser_state(f)
                        _rg = int(_tstate.get("rung") or 0)
                        teaser = {"media_ids": _prev["media_ids"], "price_cents": _dp,
                                  "is_free": False, "convo": True, "rung": _rg,
                                  "next_rung": _rg, "resend": True}
            # Operator override (teaser_convo_ignore_brakes): the escalation ladder
            # runs past the companion/bot-accused/broke brakes — only a MANUAL stop
            # (enforced upstream in run()) halts it. `_leak` is a ToS content guard,
            # NOT a fan-state brake, so it is honoured regardless.
            _teaser_ignore_brakes = bool((convo_teaser_cfg or {}).get("ignore_brakes"))
            # `offer_item is None` — the SAME guard the hot teaser carries, and for the
            # same reason: the send site is an `if offer_item … / elif teaser …`, so a
            # catalog PPV going out THIS TURN wins the priced attach and the teaser's
            # media never reaches the wire. Without this the teaser was still picked and
            # then still RECORDED (`teaser_msg_id` is set unconditionally below), which
            # burned its media out of the unseen pool without showing it to him, stamped
            # `last_price` with a price he was never charged, and advanced the rung on a
            # sale that could not have happened. Measured 2026-08-01: 71 such messages
            # across 6 accounts since 07-18, carrying two VaultSend price sets each
            # (e.g. a $35.28 PPV and a $10.00 teaser rung on one message id).
            #
            # This gates ONLY on a PPV leaving on this turn. It deliberately does NOT
            # gate on `pending` (an OPEN, unopened PPV) or on the ask caps: when
            # ai_chatter has nothing to send — no candidate item, or its caps are
            # spent — the teaser is the fan's only offer and must still fire.
            if (teaser is None and convo_teaser_cfg is not None and not dry_run
                    and offer_item is None
                    and not _leak and (not seller_off or _teaser_ignore_brakes)):
                # Adaptive cadence: if nobody is selling him a PPV (none pending, none
                # going out this turn), the convo-teaser is his ONLY offer — fire it
                # SOONER (after ~10 of his msgs). If a PPV is already in play, keep the
                # configured spacing (20) so we don't pile a teaser on top of a live ask.
                _tcfg = convo_teaser_cfg
                if pending is None and offer_item is None:
                    _tcfg = {**convo_teaser_cfg,
                             "after": min(int(convo_teaser_cfg.get("after") or 20), 10)}
                _tstate = _tip_reward.teaser_state(f)
                _since = _tip_reward.teaser_last_at(_tstate)
                # Adaptive ladder climb/soften signal: did HER last teaser sell? Only
                # her own teaser unlock counts (Message.is_paid on that id) — an
                # ai_chatter catalog buy never moves this ladder. Queried only in
                # adaptive mode, only when the last teaser was priced. This and his
                # payment history are the only two facts NOT already in `_tstate`,
                # which is why they are the only two still passed separately.
                # WHICH message to ask about is `teaser_sale_check_msg`, not `last_msg`:
                # after a free BAIT leg the last teaser is a $0 message that can never
                # be sold, and reading it would hide a LATE unlock of the priced ask
                # underneath — leaving a man who just paid on a streak toward the
                # circuit breaker.
                _t_sold = False
                _t_chk = (_tip_reward.teaser_sale_check_msg(_tstate)
                          if _tcfg.get("adaptive") else None)
                if _t_chk:
                    _t_sold = await _teaser_sold(account_id, fan_id, _t_chk)
                # Has he EVER paid? The soften floor is the $3 wire minimum until he
                # has and the rung's SET price after (tip_reward.convo_teaser_floors).
                # Only queried when the ladder can actually soften.
                _t_max_paid = ((await _paid_ppv_facts(account_id, fan_id))[0]
                               if _tcfg.get("adaptive") else 0)
                try:
                    _msgs_since = await _fan_msgs_since(account_id, fan_id, _since)
                    teaser = await _tip_reward.pick_convo_teaser(
                        client, account_id, fan_id, tcfg=_tcfg, state=_tstate,
                        msgs_since_last=_msgs_since, last_sold=_t_sold,
                        max_paid_cents=_t_max_paid, now=now)
                except Exception:
                    log.debug("ai_chatter convo_teaser pick failed account=%s fan=%s",
                              account_id, fan_id, exc_info=True)
                if teaser is not None and teaser["price_cents"] > 0:
                    # A priced rung obeys the broke/declined brake. `ladders` is only
                    # loaded under the gate (§3152), so on a gate-off account fan_ladder
                    # is None here — read the pause AUTHORITATIVELY (one query, only for a
                    # paid rung) so a broke man is never sent a $10/$50 tease.
                    # teaser_convo_ignore_brakes lifts THIS brake too (operator override —
                    # a paid rung then reaches a broke/declined fan); the per-tick paid
                    # cap is infra rate-limiting, not a fan-state brake, so it always holds.
                    _lad = (fan_ladder if fan_ladder is not None
                            else (await _load_ladders(account_id, [fan_id])).get(fan_id))
                    _paused = (_lad is not None and _lad.offers_paused_until
                               and _lad.offers_paused_until > now
                               and not _teaser_ignore_brakes)
                    if _paused or hot_teaser_paid_tick >= _MAX_FORCED_ASKS_PER_TICK:
                        teaser = None      # brake + the per-tick paid cap
            # ── A NO-SALE BEAT MAY SEND SOMETHING FREE, BUT NEVER A PRICE TAG.
            #
            # The dare's copy bans a PRICE ("no price, no offer line"), not a gift, and a
            # $0 rung on a dare is a fine turn — gating the ladders at their pick sites
            # instead broke case_convo_teaser_ignore_brakes_lifts_companion_seller_off,
            # where a free rung is the whole point of the operator's override.
            #
            # ⚠️ AND IT SITS HERE, AFTER EVERY PRODUCER. `teaser` is assigned by THREE
            # of them — the hot teaser, the haggle resend, and the convo ladder — and the
            # first version of this check lived INSIDE the convo ladder's `if`, whose own
            # guard is `teaser is None`. So the two earlier producers skipped it entirely
            # and a $15 hot teaser and an $8 haggle resend both went out on a dare. One
            # rule, one site, after all three, is what "said ONCE" has to mean.
            if teaser is not None and teaser["price_cents"] > 0 \
                    and kind in _TURNS_NOT_SELLING:
                log.info("ai_chatter priced teaser dropped on a no-sale beat "
                         "account=%s fan=%s kind=%s", account_id, fan_id, kind)
                teaser = None
            if not dry_run:
                await _bump_attempt(account_id, fan_id, now)
            # SHE WAS GONE A WHILE ⇒ ONE BUBBLE. Measured against these accounts' own
            # human chatters, a human returns single-bubble ~64% of the time at every
            # gap length; ours falls from 31.9% at <2min to 14.7% at 30min-2h, so the
            # longer we had been quiet the more we said on arrival.
            #
            # Anchored on the START of his unanswered run, not his last message. Those
            # differ exactly when he double-texted — which is the step-out's own
            # success case, and reading `last_in_at` there would reset the clock to his
            # newest bubble and hand the longest silences the burstiest returns. Falls
            # back to `last_in_at` for the ordinary single-message turn, which is the
            # same clock the measurement above was taken on.
            _ret_anchor = c.in_run[0] if c.in_run else c.last_in_at
            _ret_gap_s = ((datetime.utcnow() - _ret_anchor).total_seconds()
                          if _ret_anchor is not None else 0.0)
            _one_bubble = bool(return_single_s > 0 and _ret_gap_s >= return_single_s)
            parts = [_language.apply_word_restriction(p, fan_lang)[:_REPLY_MAX_CHARS]
                     for p in split_for_bubbles(raw, max_bubbles,
                                                rng=random.Random(f"split:{fan_id}:{raw}"),
                                                force_single=_one_bubble)
                     if p.strip()][:max_bubbles]
            parts = [p for p in parts if not _looks_like_echo(p, c.last_body)]
            if style_on and parts:
                recent_out = [b for d, b in c.messages if d == "out"]
                parts = _dedupe_lead_reaction(parts, recent_out)
            # Don't ship his name as a standalone bubble — fold it into a neighbour.
            parts = _merge_lone_name_bubbles(
                parts, (resolve_fan_name(f) or "").split("/")[0] if f else "")
            # §6.4 brush-off — ONE line. A multi-bubble "no really i'm real" reads as
            # protesting-too-much; a single breezy line does not.
            #
            # …UNLESS the same turn is a rating. When he accuses AND sends a picture the
            # prompt ladder deliberately hands the turn to `_TURN_RATE_PIC` (see
            # `_turn_kind`) because a specific read of what he just sent is the one
            # answer a canned bot could not have written — and a rating is four beats by
            # construction. Capping it here shipped the opening bubble and dropped the
            # score, which is the whole product of the beat. Worse, the score check runs
            # on `raw` BEFORE this split, so the re-ask sees a number that is then
            # truncated away and never fires. The incident thread is exactly this shape.
            if kind == _TURN_BRUSH_OFF and parts:
                parts = parts[:1]
            if not parts:
                # A sticker-only reply (empty text + a tag) is a legit pure
                # reaction — real girls answer "lol" with a gif, and ~150 of these
                # land a day. Two things can make one unusable, and they need
                # OPPOSITE handling.
                #
                # An offer or teaser riding this turn is NOT a reason to bin the
                # reply. The attach rides the last text bubble, so with no text a
                # priced PPV would go out captionless — but the fix is to defer the
                # ATTACH, not the message. Dropping it was a trap: the sticker roll
                # is seeded on (fan, his last message), so an unchanged inbound rolls
                # the same "solo" next tick, and the tick after, for ever. Only a
                # LANDED message flips a fan off the candidate list (`c.last_dir`),
                # so nothing downstream could break the cycle. It only ends when the
                # model happens to write text instead — measured live on one "Lol":
                # 36 minutes of silence, 25 generations burned, 23 of them binned,
                # before it broke character and the 24th got through. Sending the
                # gif ends it on the FIRST tick, and it arms the per-fan sticker
                # gap, so the next turn comes back as text and carries the offer.
                #
                # The word-needing beats still need words. A cat gif answering "are you
                # a bot" reads as exactly the dodge he just accused her of, and one
                # answering the picture he just sent is the non-reaction the rating
                # exists to replace. `sticker_mode` is already "skip" on those turns, so
                # this is the belt to that pair of braces — reading `kind` rather than
                # restating the set, so the two can never disagree.
                if sticker_tag is not None and kind not in _TURNS_NEEDING_WORDS:
                    if offer_item is not None or teaser is not None:
                        offers_deferred += 1
                    offer_item = None
                    teaser = None
                else:
                    dropped_empty += 1
                    log.info("ai_chatter dropped empty reply account=%s fan=%s "
                             "sticker=%s kind=%s",
                             account_id, fan_id, sticker_tag, kind or "chat")
                    continue
            # Anti-hallucination floor: price talk with NO validated offer
            # behind it never reaches a fan on a selling account. Strip those
            # bubbles; if nothing survives, skip the reply entirely (silence
            # beats a promise we can't deliver).
            #
            # Delivery narration ("sending it now" / "check your dms" / "sent it")
            # is ALSO a phantom send when nothing actually attaches this turn — she
            # tells him to go look and there's nothing there. Strip it too, UNLESS
            # real media rides (a teaser) or an unpaid PPV is already pending (then
            # "go unlock the one i sent" is true, not a phantom). offer_item is None
            # here, so a fresh priced PPV never reaches this branch.
            #
            # 🚨 `_pack_plan` IS REAL MEDIA. The vault lane decides above and delivers
            # at the end of this turn (`lane.deliver`), and it CLEARS `offer_item` on
            # the way past — so a truthful "sending it now babe" was being read as a
            # hallucination on the one turn it is literally true. If it was the only
            # bubble, `if not parts: continue` then abandoned the turn and dropped the
            # plan: the sale lost, counted nowhere but `unbacked_stripped`.
            #
            # ⚠️ The `catalog_items` guard is gone with it. It scoped this floor to
            # accounts holding enabled catalog rows, which is exactly backwards — an
            # empty shelf is where the model has NO grounded price to quote, so it is
            # where an invented one is most likely and least excusable.
            if offer_item is None:
                _phantom = teaser is None and pending is None and _pack_plan is None

                def _bad(p: str) -> bool:
                    return _unbacked_talk(p) or (_phantom
                                                 and bool(_DELIVERY_TALK_RE.search(p)))
                priced = [p for p in parts if _bad(p)]
                if priced:
                    unbacked_stripped += 1
                    log.warning("ai_chatter unbacked price/delivery talk stripped "
                                "account=%s fan=%s bubbles=%r",
                                account_id, fan_id, priced)
                    parts = [p for p in parts if not _bad(p)]
                    if not parts:
                        continue
            # The price THIS fan is quoted for THIS piece. Smart pricing off (or a
            # free teaser) → the catalog's static price, exactly as before.
            quote = quotes.get(int(offer_item.id)) if offer_item is not None else None
            ask_cents = int(quote.price_cents) if quote is not None else 0
            # A TIP ask must be a WHOLE DOLLAR. A PPV's price rides in the locked box,
            # so its cents tail is harmless (and is the point — a uniform .99 on every
            # send is a perfect bot fingerprint). But a tip has no box: we TELL him the
            # number, and the unlock watcher then requires `tips >= tip_unlock_cents`.
            # Quote him $59.69, and the only sane thing he can type is a $59 tip — which
            # is 5900 < 5969, so the offer NEVER unlocks. He would have paid and received
            # nothing. The ask he is told and the threshold he must clear must be ONE
            # number, so tip quotes are floored to the dollar here, at the source.
            if ask_cents and offer_mode_eff == "tip":
                ask_cents = max(1, ask_cents // 100) * 100
            # Deterministic terms floor: a tip-unlock offer with no $ amount in
            # the pitch leaves the fan with no way to know the terms (a PPV's
            # locked box shows its price; a tip ask doesn't). Append the ask.
            if (offer_item is not None and not offer_item.is_free_teaser
                    and offer_mode_eff == "tip"
                    and not re.search(r"\$\s*\d", " ".join(parts))):
                tip_cents = ask_cents or int(offer_item.tip_unlock_cents or 0)
                parts = parts[:max_bubbles - 1] if len(parts) >= max_bubbles else parts
                parts.append(apply_word_restriction(
                    f"tip ${tip_cents // 100} and its yours 😏"))
            name_protect = [n for n in (f.real_name, f.generated_nickname,
                                        f.of_display_name) if n]
            if nonnative_on:
                # ONE rng for the whole reply — see apply_nonnative_spacing.
                _q_rng = random.Random(f"{fan_id}:{raw}:q")
                parts = [apply_nonnative_style(p, protect=name_protect)
                         for p in parts]
                # The space-before-'?' habit is part of this register but has its
                # OWN switch — it is the one visible artifact an operator may want
                # off while keeping the rest. Narrows the layer, never widens it.
                if spacing_on:
                    parts = [apply_nonnative_spacing(p, _q_rng) for p in parts]
            if typo_on:
                protect = name_protect + (list(NONNATIVE_OUTPUTS) if nonnative_on else [])
                parts = await apply_typo_throttle(
                    account_id, fan_id, parts, random.Random(f"{fan_id}:{raw}"),
                    protect=protect, max_bubbles=max_bubbles)

            if dry_run:
                sent += 1
                if offer_item is not None:
                    would_offer += 1
                continue

            # ── Human Rhythm: WHEN she answers. Replaces the delay DECISION only —
            # typing_delay_seconds() is still the helper that feeds it, and with
            # rhythm off the hold below is byte-identically what it was.
            rst = rstates.get(fan_id)
            cover_line: str | None = rhythm_cover if rhythm_resume else None
            first_delay: float | None = None
            # `parts` is empty only on a sticker-only reply. It still goes through
            # rhythm: a gif IS her answer, and it must be paced like one. Skipping it
            # (the original `and parts` guard) was harmless while solo gifs were ~5% of
            # replies — but the gif-first opener made one the DEFAULT first reply to
            # every new fan, so the loudest turn in the product was landing on the
            # sticker's own 2-6s hold, under _FLOOR_S, bypassing the whole opening
            # schedule. Nothing to type on a gif, so the wpm hold is 0.
            if rhythm_on and not rhythm_resume:
                rnow = datetime.utcnow()
                # Read the thread's money ONCE. Its twin above does the same, and
                # the two RhythmCtx builds must agree — three separate lookups
                # spelled differently from the twin is how they drift apart.
                _money = human_money.get(fan_id, _NO_MONEY)
                d = rhythm.decide(rhythm.RhythmCtx(
                    account_id=str(account_id), fan_id=fan_id,
                    voice=v.voice,
                    # Gap covers drawn from TODAY's day when she has one, so a cover
                    # cannot claim she was driving while the chat prompt says she was
                    # on a trail. () ⇒ the shipped pools, unchanged.
                    day_covers=day.covers,
                    pace_buckets=bool(cfg.get("rhythm_pace_buckets")),
                    pace_curve=rhythm_curve,
                    text=(parts[0] if parts else ""),
                    typing_delay_s=(typing_delay_seconds(parts[0], typing_wpm)
                                    if parts else 0.0),
                    last_inbound_at=c.last_in_at, last_outbound_at=c.last_out_at,
                    # An open/hot ladder suppresses break rolls entirely: a ladder
                    # stranded mid-sell is the worst outcome in the system.
                    # …and so does a HUMAN's unpaid PPV, for _ASK_BREAKPROOF_WINDOW.
                    ladder_open=bool(fan_ladder is not None and fan_ladder.status
                                     in (upsell.STATUS_OPEN, upsell.STATUS_HOT))
                    or _breakproof(_money.ask, rnow),
                    # ...and the same merge on the post-LLM twin: the two RhythmCtx
                    # builds must agree, or she takes a pace here that the availability
                    # check would never have allowed.
                    last_paid_at=_newest(
                        fan_ladder.last_paid_at if fan_ladder is not None else None,
                        _money.paid, _money.tip),
                    # HEAT (v3): his reply speed + whether he's escalating drive how fast
                    # she replies — a hot sext gets ~every-minute replies, a cold thread
                    # drifts. §3.4: the rolling last-20 realized latencies feed the fast-nudge.
                    his_last_latency_s=_his_last_latency_s(c),
                    fan_hot=_fan_hot(c),
                    recent_realized_s=_recent_realized_s(rst),
                    # Is an answer OWED? A question or volunteered content is
                    # break-proof and rest-proof; a bare "lol" is not. is_qualifying
                    # _inbound is the predicate that draws that line — is_substantive
                    # _msg passes "lol" and would make every turn owed.
                    answer_owed=is_qualifying_inbound(c.last_in_text),
                    after_gif_solo=bool(c.last_out_was_gif),
                    turn_index=int(c.fan_msg_n),
                    thread_started_at=c.first_in_at,
                    sleep_window=sleep_win, tz_offset_minutes=tz_off,
                    no_sleep=rhythm_no_sleep,
                    last_cover_at=(rst.last_cover_at if rst is not None else None),
                    reply_bonus_s=reply_bonus_s,
                    # `stepout_due` is deliberately NOT passed here. The step-out is
                    # decided on the PRE-LLM path only: deciding it after generation
                    # pays for a reply we then sit on for two hours and regenerate on
                    # the wake — the same double-billing that split decide_availability
                    # out of decide() in the first place.
                    enabled=True,
                ), rnow, random.Random(f"rhythm:{account_id}:{fan_id}:{rnow.timestamp()}"))
                deferrals = int(rst.deferrals or 0) if rst is not None else 0
                # PER-RUN inline budget. Each individual hold is < INLINE_MAX_S and safe
                # for the fan LEASE, but the serial candidate loop can hold ONE of the
                # executor's 4 GLOBAL run slots for the SUM of its holds (8 fans × ~72s
                # median ≈ 10min), starving every other account's sends → relay 500s.
                # So once this run has spent its budget, further replies take the
                # scheduler path even though the single delay would fit inline.
                over_budget = (run_inline_s + float(d.delay_s)) > _RUN_INLINE_BUDGET_S
                if (d.defer or over_budget) and deferrals < 1:
                    # A budget-forced defer needs a real wake time even though decide()
                    # meant to send inline: schedule the fan's own drawn delay out.
                    wake_at = d.wake_at or (rnow + timedelta(
                        seconds=max(float(d.delay_s), float(rhythm.INLINE_MAX_S))))
                    await _save_rhythm(account_id, fan_id, context=d.context,
                                       wake_at=wake_at, deferrals=deferrals + 1)
                    # RELEASE THE LEASE FIRST. An inline sleep here would hold it
                    # past its 900s TTL and burn one of the executor's 4 GLOBAL run
                    # slots — starving to_thread and 500-ing the relay. The wait is
                    # the SCHEDULER's job, never ours.
                    await ax.release_fan_lease(account_id, fan_id)
                    # THE DRAFT RIDES ALONG. `parts` is generated, validated and
                    # humanized — two LLM calls already billed to this account. The
                    # wake used to regenerate all of it, which is the same double
                    # spend the pre-LLM availability check was added to stop; that
                    # check only ever closed the asleep/away half, and this branch is
                    # the other one. Same carrier as the cover line, for the same
                    # reason: rhythm_state has nowhere to put it.
                    #
                    # UNPRICED ONLY. A turn carrying an offer or a teaser is left to
                    # regenerate: its quote, ladder rung, ownership dedup and per-fan
                    # offer caps can all move while he is away, and re-validating
                    # those on the wake IS the regenerate path. Deferring the cheap
                    # majority is the whole win; the priced tail is not worth the
                    # chance of charging him against a rung that no longer exists.
                    # `c.last_in_at` is a PRECONDITION, not a nicety: it is the
                    # message the wake re-checks the thread against, so a draft
                    # without one could never be replayed. Storing it anyway would
                    # bank a dead payload and count it as a save.
                    # `_pack_plan` joins the priced tail for exactly the reason
                    # above: a turn holding a vault plan IS a priced turn. Parking
                    # its text would replay a stale answer next tick and `continue`
                    # before the lane ever runs again — he asked to see something,
                    # got the answer, and never got the thing.
                    _draft: dict = {}
                    if (offer_item is None and teaser is None
                            and not _pack_plan
                            and parts and c.last_in_at is not None):
                        _draft = {"draft_parts": list(parts),
                                  "draft_made_at": rnow.isoformat(),
                                  "draft_inbound_at": c.last_in_at.isoformat()}
                        drafts_stored += 1
                    await ax.enqueue_job(
                        account_id, _PURPOSE,
                        payload={"only_fan_ids": [int(fan_id)], "rhythm_resume": True,
                                 # The cover line is decided WITH the gap it explains;
                                 # rhythm_state has nowhere to put it, and re-deriving
                                 # it on the wake would be a second roll.
                                 "rhythm_cover": d.cover_line, **_draft},
                        run_at=wake_at)
                    rhythm_deferred += 1
                    log.info("ai_chatter rhythm defer account=%s fan=%s ctx=%s wake=%s%s",
                             account_id, fan_id, d.context, wake_at,
                             " (run-budget)" if over_budget and not d.defer else "")
                    continue                    # NOT return — 7 other fans are waiting
                # Already deferred once (deferrals>=1 can't defer again — anti-livelock),
                # or the delay fits: hold inline, bounded, and charge it to the per-run
                # budget. CLAMP to the REMAINING budget, not just INLINE_MAX_S: without
                # this an already-deferred fan over budget still adds a full hold, and
                # the run-slot guard the whole budget exists for leaks (Codex/Black-hat
                # release blocker). remaining<=0 ⇒ send now (a small floor keeps typing
                # realistic without extending slot occupancy).
                remaining = max(0.0, _RUN_INLINE_BUDGET_S - run_inline_s)
                first_delay = min(float(d.delay_s), float(rhythm.INLINE_MAX_S), remaining)
                run_inline_s += first_delay
                cover_line = d.cover_line
            elif rhythm_on and rhythm_resume:
                # The wake_at WAS the decision. Send now, and clear the hop so the
                # NEXT reply gets a fresh roll.
                await _save_rhythm(account_id, fan_id, wake_at=None, deferrals=0,
                                   context=rhythm.CONTEXT_ENGAGED)

            # ── LAST LOOK BEFORE THE WIRE. Everything above ran against the thread
            # `_gather` read at the top of the sweep; since then we have spent two
            # LLM calls and up to INLINE_MAX_S of typing hold on this fan. If he
            # wrote again the reply answers a message he has moved past, and if
            # anyone else answered we are about to talk over them. Placed BEFORE the
            # cover-line prepend on purpose: that branch stamps `last_cover_at` and
            # burns the cover, and a cover spent on a turn we then abandon is a gap
            # explained to nobody.
            _moved = await _thread_moved_on(account_id, fan_id, c.last_in_at)
            if _moved:
                stale_drops[_moved] += 1
                log.info("ai_chatter dropped a generated reply account=%s fan=%s "
                         "reason=%s", account_id, fan_id, _moved)
                continue

            # She explains the gap in her own voice, as its own bubble, BEFORE the
            # reply. A six-hour silence followed by a cold "hey babe" reads MORE like
            # a bot, not less — which is why silence-instead-of-cover was rejected.
            if cover_line:
                parts = [apply_word_restriction(str(cover_line))[:_REPLY_MAX_CHARS]] + parts
                await _save_rhythm(account_id, fan_id, last_cover_at=datetime.utcnow())
                cover_lines_sent += 1

            offer_msg_id: int | None = None
            teaser_msg_id: int | None = None
            send_failed = False
            first_no_id = False
            for idx, part in enumerate(parts):
                # The LAST bubble carries the offer attach: free media for a
                # teaser; locked media + free pitch text (locked_text=False —
                # the OF gotcha) + free previews for a PPV-capable offer.
                # Tip-only offers send plain text; media goes out on unlock.
                kwargs: dict = {}
                if offer_item is not None and idx == len(parts) - 1:
                    media = _item_media(offer_item)
                    if offer_item.is_free_teaser:
                        kwargs = {"media_files": media, "price": 0}
                    elif offer_mode_eff in ("ppv", "both"):
                        # ask_cents (the ladder's quote) wins over the catalog's
                        # static price when smart pricing is on — and it is the SAME
                        # number the manifest showed the model, so the pitch text and
                        # the locked box can never disagree.
                        px = ask_cents or int(offer_item.price_cents or 0)
                        # §4.1 invariant — RE-CLAMP to OF's wire range at the send site.
                        # next_price/human_cents already clamp a smart-priced quote, but a
                        # STATIC catalog price (smart pricing off) never passed through
                        # them; OF rejects anything over $200 outright and a 3.0× whale
                        # quote can reach it. The floor guards a corrupt sub-$3 config.
                        px = max(upsell.OF_PRICE_FLOOR_CENTS,
                                 min(int(px), upsell.OF_PRICE_MAX_CENTS))
                        # OF takes DOLLARS and accepts cents (ppv_send sends `price/100`
                        # — that is how its .99 endings reach the wire). `px // 100`
                        # floored them off: a $59.69 quote was CHARGED as $59.00 while
                        # ladder_quote recorded 5969. That is a price we never charged
                        # sitting in the experiment's own instrument — every estimate the
                        # arm produces would be computed against a fiction.
                        kwargs = {"price": px / 100,
                                  "locked_text": False, "media_files": media}
                        previews = _item_previews(offer_item)
                        if previews:
                            kwargs["previews"] = previews
                elif teaser is not None and idx == len(parts) - 1:
                    # Hot-thread teaser rides the last bubble, mutually exclusive with
                    # an offer_item (only reachable when offer_item is None). Free →
                    # media at price 0; paid → a locked tease PPV (same RE-CLAMP to
                    # OF's wire range as a priced offer, from the same floor/ceiling).
                    if teaser["price_cents"] > 0:
                        px = max(upsell.OF_PRICE_FLOOR_CENTS,
                                 min(int(teaser["price_cents"]), upsell.OF_PRICE_MAX_CENTS))
                        kwargs = {"price": px / 100, "locked_text": False,
                                  "media_files": teaser["media_ids"]}
                    else:
                        kwargs = {"media_files": teaser["media_ids"], "price": 0}
                # How long to hold before this bubble, and what the fan sees while
                # we do. Bubble 0's latency belongs to rhythm; the gaps between the
                # rest belong to pacing; with pacing off both collapse to the wpm
                # typing hold this line always was. `pacing.hold_for_bubble` owns
                # that precedence — see its docstring for why "not bubble 0" is not
                # the same question as "rhythm did not decide this".
                typing_s = typing_delay_seconds(part, typing_wpm)
                pace = pacing.hold_for_bubble(
                    idx=idx, text=part, typing_s=typing_s,
                    rhythm_delay_s=(first_delay if idx == 0 else None),
                    cfg=pace_cfg,
                    # Its OWN Random, seeded per bubble. Sharing rhythm's rng would
                    # insert draws into that sequence and silently re-roll every
                    # seeded rhythm test and every sim replay.
                    rng=random.Random(f"pace:{account_id}:{fan_id}:{idx}:{part[:24]}"),
                    # The per-run inline budget guards the executor's 4 GLOBAL slots.
                    # Bubbles 1..N were NEVER charged to it — only bubble 0 and the
                    # ppv drop were — which was harmless while a bubble held ~10s and
                    # is not once one can hold 90.
                    budget_s=max(0.0, _RUN_INLINE_BUDGET_S - run_inline_s))
                # `added_s` is 0 on every unpaced path (rhythm's bubble, pacing off),
                # so it doubles as "did pacing run here" — see pacing.Pace.
                run_inline_s += pace.added_s
                if pace.added_s:
                    paced_bubbles += 1
                if pace.drifted:
                    pace_drifts += 1
                # §3.6 PPV pacing floor — a priced attach never lands the same tick as
                # its tease: a paywall dropped the instant the setup line goes reads as
                # automated. Hold 45-115s (inline-safe, < INLINE_MAX_S — no parked
                # offer, no reaper, no stranded promise) BEFORE the priced bubble. 45s
                # is an unconditional floor INSIDE the lane; rhythm off ⇒ instant attach
                # exactly as before (byte-identical). This replaces any instant attach.
                if (rhythm_on and offer_item is not None and idx == len(parts) - 1
                        and not offer_item.is_free_teaser
                        and offer_mode_eff in ("ppv", "both") and kwargs.get("price")):
                    drop = rhythm.ppv_drop_delay(
                        random.Random(f"drop:{account_id}:{fan_id}:{part[:24]}"),
                        stalled=filming_stall)
                    if float(drop) > float(pace.total_s):
                        # She is WAITING ON THE FILE, not typing — the caption is
                        # already written. So the longer hold replaces the pace
                        # outright rather than being bolted onto it; anything else
                        # leaves the phase split describing a delay that no longer
                        # exists.
                        run_inline_s += float(drop) - float(pace.total_s)
                        pace = pacing.silent_hold(float(drop), typing_s, pace_cfg)
                    ppv_drops += 1
                await hold_with_typing(account_id, fan_id, pace.total_s,
                                       typing_indicator=typing_indicator,
                                       quiet_s=pace.quiet_s,
                                       think_at_s=pace.think_at_s,
                                       think_for_s=pace.think_for_s)
                try:
                    result = await asyncio.to_thread(
                        lambda p=part, kw=kwargs: client.send_message(fan_id, p, **kw))
                except Exception as e:
                    errors += 1
                    # spec §4.1 — a PRICE-VALIDATION 400 is NOT an undeliverable fan.
                    # Quarantining a whale (skip_list + 7d) or closing his ladder over
                    # a fixable number retires a live payer. Drop the priced attach and
                    # resend the bubble UNPRICED; never skip_list, never close.
                    if kwargs.get("price") and _is_price_error(e):
                        log.warning("ai_chatter price-validation error account=%s fan=%s "
                                    "— dropping offer, resending unpriced",
                                    account_id, fan_id, exc_info=True)
                        offer_item = None      # no offer row, no ladder write downstream
                        price_errors += 1
                        try:
                            result = await asyncio.to_thread(
                                lambda p=part: client.send_message(fan_id, p))
                        except Exception:
                            log.warning("ai_chatter unpriced resend failed account=%s "
                                        "fan=%s", account_id, fan_id, exc_info=True)
                            send_failed = True
                            break
                    else:
                        # An undeliverable fan is quarantined (skip_list + a 7d rest) —
                        # and a quarantined fan's ladder must die with him, or a stale
                        # open rung would still be escalating from when he comes back.
                        if await skip_unreachable_fan(account_id, fan_id, e, log=log) and gate_on:
                            await _close_ladder(account_id, fan_id, upsell.STATUS_TAPPED)
                        log.warning("ai_chatter send failed account=%s fan=%s",
                                    account_id, fan_id, exc_info=True)
                        send_failed = True
                        break
                msg_id = result.get("id") if isinstance(result, dict) else None
                if msg_id:
                    await write_outbound_attribution(
                        account_id=account_id,
                        fan_id=int(fan_id),
                        message_id=int(msg_id),
                        sent_by_employee_id=None,  # → system Automation employee
                        # The whole reply is tagged by what it CARRIES: if a priced
                        # offer rides on it, every bubble is 'ai_upseller'. Tagging
                        # only the bubble holding the attach would split one reply
                        # across two kinds and make the per-automation stats lie about
                        # both. _OUR_KINDS keeps the cadence counter whole. A PAID hot
                        # teaser is a priced send too → ai_upseller; a FREE one is just
                        # warm-up media on a chat reply → ai_chatter.
                        automation_kind=(
                            _KIND_UPSELL if (offer_item is not None
                                             or (teaser is not None
                                                 and teaser["price_cents"] > 0))
                            else _PURPOSE),
                        body=str(result.get("text") or part),
                        price_cents=ax._to_cents(kwargs.get("price", 0)),
                        created_at=ax._parse_iso(result.get("createdAt")) or datetime.utcnow(),
                        emit_live=True,
                    )
                    sent_ok = True
                    if offer_item is not None and idx == len(parts) - 1:
                        offer_msg_id = int(msg_id)  # the unlock watcher's anchor
                    if teaser is not None and idx == len(parts) - 1:
                        teaser_msg_id = int(msg_id)
                elif idx == 0:
                    first_no_id = True
                    break
            if send_failed:
                continue
            if first_no_id and not sent_ok:
                errors += 1
                reason = await quarantine_if_undeliverable(client, account_id, fan_id)
                if reason is not None and gate_on:
                    await _close_ladder(account_id, fan_id, upsell.STATUS_TAPPED)
                if reason is None:
                    await _pause_fan(account_id, fan_id, now + _NOID_PAUSE)
                    log.warning("ai_chatter send returned no id account=%s fan=%s — paused %s",
                                account_id, fan_id, _NOID_PAUSE)
                continue
            # ── Cat sticker — its own bubble after the text, or the WHOLE reply
            # (parts empty). Empty text + top-level giphyId is the verified
            # GIF-only wire shape; failure is non-fatal when text already landed.
            sticker_sent = False
            if sticker_tag is not None and not send_failed and not first_no_id:
                gid = cat_stickers.pick_gif(
                    sticker_tag,
                    random.Random(f"gif:{account_id}:{fan_id}:{c.last_body}"),
                    account_id=account_id, fan_id=fan_id,
                    voice=v.voice)
                if gid is not None:
                    srng = random.Random(f"sdelay:{fan_id}:{gid}")
                    # A gif that IS the reply carries the rhythm delay, exactly as
                    # bubble 0 would have. A gif RIDING AFTER text is a second bubble,
                    # so it keeps the short 2-6s beat between bubbles.
                    s_hold = (first_delay if (not parts and first_delay is not None)
                              else 2.0 + 4.0 * srng.random())
                    # A gif is PICKED, not typed, so there is no typing time in this
                    # hold at all — see pacing.silent_hold.
                    s_pace = pacing.silent_hold(s_hold, 0.0, pace_cfg)
                    await hold_with_typing(account_id, fan_id, s_pace.total_s,
                                           typing_indicator=typing_indicator,
                                           quiet_s=s_pace.quiet_s)
                    try:
                        result = await asyncio.to_thread(
                            lambda g=gid: client.send_message(fan_id, "",
                                                              giphy_id=g))
                    except Exception:
                        result = None
                        log.warning("ai_chatter sticker send failed account=%s "
                                    "fan=%s tag=%s", account_id, fan_id,
                                    sticker_tag, exc_info=True)
                    s_msg_id = result.get("id") if isinstance(result, dict) else None
                    if s_msg_id:
                        await write_outbound_attribution(
                            account_id=account_id,
                            fan_id=int(fan_id),
                            message_id=int(s_msg_id),
                            sent_by_employee_id=None,
                            automation_kind=_PURPOSE,
                            body=str(result.get("text") or ""),
                            price_cents=0,
                            created_at=ax._parse_iso(result.get("createdAt"))
                            or datetime.utcnow(),
                            emit_live=True,
                        )
                        cat_stickers.mark_sent(account_id, fan_id,
                                               tag=sticker_tag, gif_id=gid)
                        sticker_sent = True
                        sent_ok = True
                        stickers_sent += 1
                        log.info("ai_chatter sticker sent account=%s fan=%s "
                                 "tag=%s gif=%s solo=%s", account_id, fan_id,
                                 sticker_tag, gid, not parts)
            if not parts and not sticker_sent:
                # The gif WAS the reply and it never landed — a real failure, not a
                # policy drop. It used to count itself and say nothing, which made an
                # unsent reply indistinguishable in the log from one that went out.
                errors += 1
                log.warning("ai_chatter sticker-only reply never landed account=%s "
                            "fan=%s tag=%s", account_id, fan_id, sticker_tag)
                continue
            await _mark_reply_sent(account_id, fan_id, now)
            # 🙋 HIS ANSWER HAS LANDED — now the content he asked for. Decided far
            # above (`lane.plan`), delivered here, in this order on purpose: the
            # reply is the answer to his question and the PPV is what it points at.
            # Everything between there and here can abandon the turn, and dropping
            # the plan is how it abandons the sale too — nothing was sent, nothing
            # charged, and the next tick decides again on the same words.
            #
            # The brakes are NOT re-run. They were read against HIS message, and
            # the only outbound since is the reply we just sent — re-gating on it
            # would close `we_spoke_last` against ourselves and drop the content on
            # the floor with the answer still promising it. The vault's own
            # re-audit still runs at the wire, where a folder edited in between
            # becomes a lie.
            if _pack_plan:
                await lane.deliver(client, _pack_plan, dry_run=dry_run)
            # §3.4 — record the REALIZED inbound→send latency + bubble count at the SEND
            # site (not the drawn delay at decide() time), rolling last 20. Fed back into
            # the next tick's RhythmCtx so the soft fast-reply nudge can see the history.
            # rhythm off ⇒ no rhythm_state row written (the flags-off invariant holds).
            if rhythm_on:
                realized = ((datetime.utcnow() - c.last_in_at).total_seconds()
                            if c.last_in_at is not None else 0.0)
                await _record_turn(account_id, fan_id, rstates.get(fan_id),
                                   realized_s=realized,
                                   bubbles=len(parts) + (1 if sticker_sent else 0),
                                   informal=style_on)
            target = _primary_ask_target(presented)
            if target:
                await _mark_question_asked(account_id, fan_id, target, asked)
            if opener is not None and parts:
                await _openers.record_used(account_id, fan_id, f,
                                           profiles.get(fan_id), opener.slot,
                                           today=opener_day)
            await _maybe_refresh_profile(account_id, fan_id, c.fan_msg_n, now, f)
            # Item 22 — profile-less fan below gen_info's staleness gate: force one
            # regen so his notes get built now instead of never.
            await _maybe_bootstrap_profile(account_id, fan_id)
            sent += 1
            # The picture play is spent for the cooldown — burn it, so a long thread
            # never re-dares. Stamped on CONFIRMED send, not on the decision, so a
            # failed send leaves the dare still owed.
            #
            # A RATING burns it too, and that is the important half: he sent a picture,
            # she rated it, and without this the very next text-only turn on a still-hot
            # thread re-arms the dare and she says "send me a picture right now and I'll
            # tell you exactly what I see" to the man who just did. Asking for what he
            # has already given is the script tell the cooldown exists to prevent.
            if kind in _TURNS_SPENDING_THE_DARE:
                await _bot_dare_mark(account_id, fan_id)
            # Burn today's beat once the reply that was REQUIRED to carry it is
            # confirmed on the wire. Same discipline as the dare above: stamped on
            # CONFIRMED send, never on the decision, so a failed send leaves the beat
            # unspent and he still gets an answer next turn. Day-scoped, so the whole
            # ledger self-prunes at local midnight.
            if day_required_beat:
                await set_fan_state(
                    account_id, fan_id, _daylog.STATE_KEY,
                    _daylog.mark_beat_used(f, day.date, day_required_beat))
            # He asked who she is, and she has now answered — remember it, so the
            # follow-up two turns later still reaches a prompt that knows her canon.
            # On the CONFIRMED send, never on the decision: a failed send must leave
            # the next turn free to gate itself off his words again.
            #
            # Keyed to `canon`, the same value the prompt was built from, so the bit
            # is stamped exactly when the canon actually shipped — never re-derived
            # from the two halves, and never written at all with the gate off.
            if canon == "show" and not bio_was_asked:
                await set_fan_state(account_id, fan_id, _BIO_ASKED_KEY,
                                    {"asked": True})
            # Persist the offer the moment its message is confirmed on the wire.
            # A teaser is its own delivery (advance immediately); a paid offer
            # opens and waits on the unlock watcher. VaultSend rows land at
            # ATTACH time for media that actually went out (teaser/PPV) so the
            # unseen filter can never re-attach the same piece.
            if offer_item is not None and sent_ok:
                # `kind`, not the raw detector: the counter's name says "offers
                # triggered by the LEAN-IN PIVOT", and on a rate_pic turn that also
                # escalates the sale comes from step 5 of the rating, not from a pivot
                # the ladder never ran. The last read of a ladder INPUT as if it were a
                # ladder POSITION.
                if kind == _TURN_ESCALATION:
                    offers_made_on_escalation += 1
                await _ensure_progress(account_id, fan_id, offer_item)
                if offer_item.is_free_teaser:
                    await _record_vault_sends(account_id, fan_id,
                                              _item_media(offer_item),
                                              offer_msg_id, 0)
                    await _record_offer(account_id, fan_id, offer_item, "free",
                                        offer_msg_id, status="delivered",
                                        resolved_by="free",
                                        delivery_message_id=offer_msg_id)
                    await _advance_progress(account_id, fan_id, offer_item)
                    teasers_sent += 1
                else:
                    if offer_mode_eff in ("ppv", "both"):
                        await _record_vault_sends(
                            account_id, fan_id, _item_media(offer_item), offer_msg_id,
                            ask_cents or int(offer_item.price_cents or 0))
                    await _record_offer(account_id, fan_id, offer_item,
                                        offer_mode_eff or "tip", offer_msg_id,
                                        quoted_cents=ask_cents or None)
                    offers_made += 1
                    if gate_on:
                        # The rung is out. ladder_quote gets a row for EVERY quote —
                        # it is the conversion log AND the price experiment's
                        # instrument (a quote silently clamped at the library ceiling
                        # and recorded as free would bias every estimate it produces).
                        if quote is not None:
                            await _record_quote(
                                account_id, fan_id, offer_item, quote,
                                rung_index=rung_index, kind="rung",
                                message_id=offer_msg_id,
                                media_key=upsell.media_key(_item_media(offer_item)))
                            rungs_quoted += 1
                        # OPEN (never 'hot' — only HE can make it hot, by paying).
                        # daily_ask_cents is what the fan was ASKED today: the churn
                        # path is asks, and a fan who buys nothing never trips a
                        # spend cap.
                        # 🚨 `ladder_day`, NOT `day`. This used to rebind `day` — the
                        # `_daylog.Day` object bound ONCE for the whole sweep at the
                        # top of run() — to a date STRING, for every remaining
                        # candidate. The next fan then called `day.covers` (rhythm on)
                        # or `day.required_beat` and got AttributeError on a str, which
                        # the per-fan `except` swallowed as "per-fan loop errored" and
                        # SKIPPED HIM. One fan taking an offer silently cost every fan
                        # behind him in the same tick. (`local_day` is already taken.)
                        ladder_day = (rhythm.local_now(now, tz_off)).strftime("%Y-%m-%d")
                        prior = (int(fan_ladder.daily_ask_cents or 0)
                                 if (fan_ladder is not None
                                     and fan_ladder.daily_day == ladder_day) else 0)
                        await _save_ladder(
                            account_id, fan_id, status=upsell.STATUS_OPEN,
                            rung_index=rung_index + 1, last_ask_at=now,
                            session_idle_at=now, daily_day=ladder_day,
                            daily_ask_cents=prior + (ask_cents or
                                                     int(offer_item.price_cents or 0)))
                    # Item 18 — schedule the ONE delayed re-engage nudge for this
                    # (unbought) paid offer. One job ⇒ one nudge; it self-cancels
                    # in _run_nudge if he buys or replies first. Off by default.
                    if nudge_on and nudge_min > 0:
                        try:
                            await ax.enqueue_job(
                                account_id, "ai_chatter",
                                payload={"nudge_fan_ids": [int(fan_id)]},
                                run_at=now + timedelta(minutes=nudge_min))
                        except Exception:
                            log.debug("ai_chatter nudge schedule failed "
                                      "account=%s fan=%s", account_id, fan_id,
                                      exc_info=True)
                log.info("ai_chatter offer account=%s fan=%s item=%s mode=%s msg=%s",
                         account_id, fan_id, offer_item.id,
                         "free" if offer_item.is_free_teaser else offer_mode_eff,
                         offer_msg_id)
            # Hot-thread teaser landed (mutually exclusive with offer_item): VaultSend
            # rows so the unseen filter never re-attaches these, and the per-fan cooldown
            # + free-counter bump. Recorded ONLY after the media actually confirmed on the
            # wire, so a dropped/failed reply never burns a fan's free allowance.
            if teaser is not None and sent_ok and teaser_msg_id:
                # A convo-ladder teaser climbs to its next rung; a hot teaser leaves the
                # rung alone (set_rung=None).
                await _tip_reward.record_hot_teaser(
                    account_id, fan_id, media_ids=teaser["media_ids"],
                    message_id=teaser_msg_id, price_cents=teaser["price_cents"],
                    is_free=teaser["is_free"], set_rung=teaser.get("next_rung"),
                    unbought=teaser.get("unbought"))
                hot_teasers_sent += 1
                if teaser["price_cents"] > 0:
                    hot_teaser_paid_tick += 1
                log.info("ai_chatter %s teaser account=%s fan=%s kind=%s items=%d "
                         "price=%d rung=%s msg=%s",
                         "convo" if teaser.get("convo") else "hot",
                         account_id, fan_id,
                         "free" if teaser["is_free"] else "paid",
                         len(teaser["media_ids"]), teaser["price_cents"],
                         teaser.get("rung"), teaser_msg_id)
            try:
                await asyncio.to_thread(client.mark_chat_read, fan_id)
            except Exception:
                log.warning("ai_chatter mark_chat_read failed account=%s fan=%s",
                            account_id, fan_id, exc_info=True)
        except Exception:
            # One fan must not abort the whole tick. The per-fan body has many awaits
            # (DB + OF), and this try had ONLY a finally — so a transient DB error on any
            # of them propagated out of the loop and skipped every remaining candidate
            # that run. Log, count, and move on; `finally` still releases the lease.
            errors += 1
            log.warning("ai_chatter per-fan loop errored account=%s fan=%s — skipping him, "
                        "continuing the tick", account_id, fan_id, exc_info=True)
        finally:
            # W3 (live-chat variant): confirmed reply → short cooldown, then
            # release; cooldown failure keeps the lease as the fallback guard.
            if sent_ok:
                try:
                    await ax.start_fan_cooldown(
                        account_id, fan_id, cooldown_s=_REPLY_COOLDOWN_S
                    )
                    await ax.release_fan_lease(account_id, fan_id)
                except Exception:
                    log.warning("ai_chatter cooldown set failed account=%s fan=%s "
                                "— keeping lease as fallback guard",
                                account_id, fan_id, exc_info=True)
            else:
                await ax.release_fan_lease(account_id, fan_id)

    # The rollout ledger, once per sweep and off the candidate loop's hot path.
    if quota_on:
        await _write_quota_audit(account_id, quota_rows,
                                 enforced=quota_enforce, now=datetime.utcnow())

    return {
        "mode": mode,
        "candidates": len(candidates),
        **audience_stats,
        "replies_sent": sent,
        "offers_made": offers_made,
        "offers_made_on_escalation": offers_made_on_escalation,
        "offers_forced": offers_forced,
        # `sold` / `sell_refused` come off the lane, which is the only thing that
        # sends a pack; the historical key names are kept so the stats readers and
        # the roster header do not have to change.
        "packs_sent": lane.sold,
        "packs_refused": lane.refused,
        "offers_forced_stale": offers_forced_stale,
        "paid_state_refreshed": paid_state_refreshed,
        "teasers_sent": teasers_sent,
        "hot_teasers_sent": hot_teasers_sent,
        "would_offer": would_offer,
        "unbacked_stripped": unbacked_stripped,
        **offer_stats,
        "old_fans_engaged": old_fans_engaged,
        "skipped_listed": skipped_listed,
        "skipped_not_turn": skipped_not_turn,
        "skipped_spam": skipped_spam,
        "skipped_muted_creator": skipped_muted_creator,
        "skipped_whale": skipped_whale,
        "skipped_not_payer": skipped_not_payer,
        "skipped_sla_fresh": skipped_sla_fresh,
        "skipped_manual": skipped_manual,
        "skipped_ghost": skipped_ghost,
        # The step-out and its escape hatch. Read as a RATIO: `stepout_broken` over
        # `stepouts` is the share of silences a fan ended by writing again, which is
        # the one number that says whether the fans notice at all.
        "stepouts": stepouts,
        "stepout_broken": stepout_broken,
        # If this runs high while `stepouts` stays low, the feature is not quiet —
        # it is jammed behind a stranded `deferrals` counter, which is a different
        # problem with a different fix.
        "stepout_blocked": stepout_blocked,
        "skipped_locked": skipped_locked,
        "skipped_cooldown": skipped_cooldown,
        "skipped_no_intent": skipped_no_intent,
        "skipped_cadence": skipped_cadence,
        # Item 21c. `quota_enforced` False ⇒ these fans were counted, not held: the
        # replies still went out. Flip daily_quota_enforce once the numbers look right.
        "quota_held": quota_held,
        "quota_enforced": quota_enforce,
        # The true/false split at a glance, so a tail of the relay log answers "is it
        # holding anyone, and if not, WHY not" without opening the DB. The ledger
        # (quota_audit) has the same breakdown per fan per day.
        "quota_reasons": dict(Counter(q.reason for _fid, q in quota_rows)),
        # Capped: this is a run-stats line, not a report — the DB has the full picture.
        "quota_held_fans": quota_held_fans[:20],
        "hard_stops": hard_stops,
        "letdown_stops": letdown_stops,
        "soft_acks": soft_acks,
        "gate_blocked": gate_blocked,
        "customs_owed_skips": customs_owed_skips,
        "offers_parked": offers_parked,
        "rungs_quoted": rungs_quoted,
        "taps_expired": taps_expired,
        "rhythm_deferred": rhythm_deferred,
        "drafts_stored": drafts_stored,
        "draft_outcomes": dict(draft_outcomes),
        "stale_drops": dict(stale_drops),
        "rhythm_waiting": rhythm_waiting,
        "cover_lines_sent": cover_lines_sent,
        "stickers_sent": stickers_sent,
        "price_errors": price_errors,
        "spend_regret_stops": spend_regret_stops,
        "companion_routed": companion_routed,
        "bot_accusations": bot_accusations,
        "rate_pic_turns": rate_pic_turns,
        "pic_offer_turns": pic_offer_turns,
        "ratings_re_asked": ratings_re_asked,
        "ratings_rescued": ratings_rescued,
        "spend_capped": spend_capped,
        "ppv_drops": ppv_drops,
        "paced_bubbles": paced_bubbles,
        "pace_drifts": pace_drifts,
        "errors": errors,
        "dropped_empty": dropped_empty,
        "offers_deferred": offers_deferred,
        "cap_hit": cap_hit,
        "dry_run": dry_run,
        "model": model,
    }
