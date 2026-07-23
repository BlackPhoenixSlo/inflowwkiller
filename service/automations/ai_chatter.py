"""
service/automations/ai_chatter.py — Automation: ai_chatter (PPVscriptAI M2).

The freestyle AI chatter+seller for fans UNDER the spend gate. It REPLACES
of_ai_chat for an account once enabled (of_ai_chat.run short-circuits when
`is_enabled` here is true), inheriting the info-gather duty — the girly voice,
the ONE-missing-fact question habit, the inline fact fill, the gen_info
refresh-if-stale hook — and (M3) adds selling from the content catalog
(catalog_scripts / catalog_items, offers in content_offers).

Who it talks to (code-side gates, never prompt-side):
  • fan spoke last (the "You:" sidebar skip — same as of_ai_chat),
  • lifetime_spend_cents < max_lifetime_spend_cents (default $1000) — fans at or
    over the gate are WHALES: pure human-chatter territory, never touched,
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
import re
from datetime import datetime, timedelta

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
    RhythmState, ScheduledJob, SkipList, Transaction, VaultSend,
    created_at_text, parse_ts,
)
from llm_client import LLMCapExceeded
from . import cat_stickers, rhythm, script_packs, tip_ladder, upsell
from . import _language
# ppv_send owns the ONE price authority (`price_bounds`); ownership.py owns
# the ONE ownership check (`owners_of_media`, keyed on MEDIA — a fan who
# bought a clip in a mass blast has no content_offers row at all). Importing
# them rather than growing a second ceiling / a second ownership notion here
# is deliberate.
from .ppv_send import price_bounds
from ownership import owners_of_media as _owners_of_media
from ._common import (
    CONTENT_ASK_RE, ESCALATION_RE, NONNATIVE_OUTPUTS, NONNATIVE_REGISTER,
    ONPLATFORM_GUARDRAIL, PAINFUL_TEXTING, STYLE_3LINE, STYLE_BRIEF, STYLE_HUMANIZER,
    STYLE_MAX_BUBBLES,
    apply_nonnative_style, apply_word_restriction, coerce_ids, guard_offplatform,
    hold_with_typing, apply_typo_throttle, load_cat_stickers_flag,
    load_cat_sticker_tuning,
    load_nonnative_flags,
    load_painful_texting_flag, load_style_flags,
    load_typing_indicator, load_typing_wpm, load_typo_flags,
    quarantine_if_undeliverable, recent_payer_fans, resolve_fan_name, resolve_model,
    should_skip_muted_creator, skip_unreachable_fan, thread_heat, typing_delay_seconds,
)
# Deliberate sibling reuse — keeps the texting voice byte-compatible with
# of_ai_chat instead of forking 500 lines of tuned style machinery.
from .of_ai_chat import (
    _BREATHER_VARIANTS, _EXTRACT_HISTORY_TAIL, _HISTORY_TAIL, _MSG_CLIP,
    _NOID_PAUSE, _REPLY_MAX_CHARS, _REPLY_TEMPERATURE, _STYLE_VARIANTS,
    _bump_attempt, _clock_line, _dedupe_lead_reaction, _extract_and_fill,
    _load_mid_funnel_fans,
    _good_examples, _load_persona, _looks_like_echo, _mark_question_asked,
    _mark_reply_sent,
    _maybe_push_nickname, _maybe_refresh_profile, _nonempty, _pause_fan,
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

# of_ai_chat's graduation skip reasons — they mean "left the gather loop", NOT
# "never message". ai_chatter exists precisely for these fans, so it ignores
# them while respecting every other skip (unreachable, old_fan_pre_ai, manual).
_GRADUATION_SKIPS = frozenset({"spent", "too_long", "info"})

# process_old_fans' flag: subscribers that predate the AI. Normally a hard skip
# (human territory) — liftable per-account via cfg["engage_old_fans"], which
# engages them in gentle mode (see old_fan_question_every in _DEFAULTS).
_OLD_FAN_SKIP = "old_fan_pre_ai"

# Built-in defaults — any key the account config omits. DISABLED until a creator
# enables it. The offer_* knobs are read by the M3 offer engine.
_DEFAULTS: dict = {
    "enabled": False,
    "hotsell_trinity_enabled": False,    # hot-lead tip→tip→PPV ladder (S2→S1→S3).
                                         # Ships DARK: default off, zero behavior
                                         # change until an account opts in.
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
    # Proven-spend price floor (workstream 3 / Dirk fix): a fan who already PAID
    # $X is never re-offered a cheaper item — the ladder climbs to the next tier
    # (a $50 buyer gets the $60 video, not the $24 set re-run at cold-open lows).
    # Ships DARK: default off, zero behavior change until an account opts in.
    "proven_spend_floor_enabled": False,
    "proven_spend_floor_mult": 0.38,     # floor = biggest single paid × this.
                                         # 0.38 skips the cheapest sets (Dirk: no
                                         # more $24 ask → cheapest ≈ $30) WITHOUT
                                         # clamping every ask up to his ceiling —
                                         # keeps mid-tier room to convert.
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
    "session_gap_minutes": 60,           # gap that starts a fresh burst for the caps
    # Item 17 — post-purchase talk window: keep chatting a just-paid fan this long
    # after his last money event; past it with no NEW spend, hand off (stop + cool
    # off → in closer mode of_ai_chat/Auto Convo keeps him warm).
    "post_purchase_minutes": 25,
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
    # No-sleep pacing: keep the hot/cold/busy variable delays + short "stepped away"
    # breaks, but NEVER the long overnight sleep — and it needs no timezone. For a
    # creator who wants "she's a person who gets busy" without an 8-hour night gap.
    # Default ON alongside rhythm: it is the variant that needs no timezone, so it is
    # the only one that behaves correctly on an account nobody has set a tz for.
    "rhythm_no_sleep": True,
    # None ⇒ DERIVED from the account's own outbound hour histogram
    # (rhythm.derive_sleep_window). ["HH:MM", "HH:MM"] ⇒ operator override.
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


async def is_intent_only(account_id: str) -> bool:
    """True when ai_chatter is the CLOSER — it engages only fans showing buying
    intent (or with an open offer) and stays silent on pure chatter. The opener /
    drill automations read this to know they must COVER everyone else instead of
    standing down for the whole account."""
    cfg = await _load_config(account_id)
    return bool(cfg.get("enabled") and cfg.get("intent_only"))


async def engaged_subset(account_id: str, fan_ids: set[int]) -> set[int]:
    """Of `fan_ids`, the ones ai_chatter currently OWNS — i.e. will (or may) answer
    THIS tick. of_ai_chat and deep_convo consult this so they cover exactly the
    fans ai_chatter leaves alone: no second bot voice on the same fan, and no fan
    left silent either.

      • disabled                  → empty set (owns nobody)
      • full chatter              → all of `fan_ids` (it replies to everyone it sees)
      • closer mode (intent_only) → fans with an OPEN OFFER we're walking, a
                                    content-ask OR sexual escalation in their latest
                                    inbound, or a RECENT PAYER (hot lead). Pure
                                    chatter is deliberately excluded — left to
                                    of_ai_chat / deep_convo / the team, matching the
                                    closer's own "pure chatter is left to Auto Convo"
                                    rule in run(). PLUS, when engage_old_fans is on,
                                    the `old_fan_pre_ai` roster: run() exempts those
                                    from the intent gate (nobody else may chat them —
                                    of_ai_chat/deep_convo hard-skip that reason), so
                                    the closer owns them and this set must say so.

    The intent test mirrors run() exactly (open offer OR _CONTENT_ASK_RE OR
    ESCALATION_RE over the HTML-stripped latest inbound OR recent payer), so
    ownership here can never diverge from who the closer actually answers — and
    autoreply skips exactly this set as hot leads."""
    if not fan_ids:
        return set()
    cfg = await _load_config(account_id)
    if not cfg.get("enabled"):
        return set()
    if not cfg.get("intent_only"):
        return set(fan_ids)
    # Closer mode: open offers ∪ content-ask/escalation intent ∪ recent payers
    # ∪ the engaged old-fan roster (run() chats those regardless of intent).
    owned = {int(o.fan_id) for o in await _open_offers(account_id)
             if int(o.fan_id) in fan_ids}
    owned |= await recent_payer_fans(account_id, fan_ids)
    if cfg.get("engage_old_fans"):
        async with get_session() as s:
            rows = (await s.execute(
                select(SkipList.fan_id).where(
                    SkipList.account_id == str(account_id),
                    SkipList.reason == _OLD_FAN_SKIP,
                    SkipList.fan_id.in_(fan_ids))
            )).all()
        owned |= {int(r[0]) for r in rows}
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
    for fid, body in last_in.items():
        stripped = _strip_html(body)
        if (_language.is_content_ask(stripped, gate_lang)
                or _language.is_escalation(stripped, gate_lang)):
            owned.add(fid)
    return owned


# ── Candidate gathering (own pass — needs timing + human-send metadata) ──────

class _Cand:
    __slots__ = ("fan_id", "fan_msg_n", "last_dir", "last_body", "messages",
                 "last_in_at", "last_out_at", "last_human_out_at", "session_out_n")

    def __init__(self, fan_id: int):
        self.fan_id = fan_id
        self.fan_msg_n = 0
        self.last_dir = ""
        self.last_body = ""
        self.messages: list[tuple[str, str]] = []  # (direction, body) oldest→newest
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


async def _gather(account_id: str,
                  fan_ids: set[int] | None = None,
                  *, session_gap_min: int = 0) -> dict[int, _Cand]:
    """One pass over the account's messages → per-fan history PLUS the two
    timestamps the gates need: when the fan last spoke (SLA age) and when a
    HUMAN last sent (manual outbound = automation_kind IS NULL and not part of
    a mass run — automations always tag automation_kind).

    When `fan_ids` is given (W7 fan-scoped dispatch), the scan is restricted to
    those fans IN SQL so reacting to one inbound DM never reads the whole
    account's message history. None/empty → the full-account sweep.

    `session_gap_min > 0` additionally counts each fan's `session_out_n` — HER OWN
    (ai_chatter) replies in the CURRENT burst, where a burst ends once SHE has been
    silent for longer than `session_gap_min`. HIS messages do not hold the burst open:
    measured on the thread, a fan who keeps typing would re-arm the gap forever and the
    cap would become a permanent gag instead of a pause. The cadence caps (item 21) read
    this so they bound a conversation, not a fan's lifetime.

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
            select(Message.fan_id, Message.direction, Message.body,
                   created_at_text(), Message.automation_kind, Message.mass_run_id)
            .where(*where)
            .order_by(Message.fan_id, Message.created_at, Message.message_id)
        )).all()
    bad_ts: list[int] = []   # fans that own a row with an unreadable created_at
    for fan_id, direction, body, created_at_raw, automation_kind, mass_run_id in rows:
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
        text = _strip_html(body)[:_MSG_CLIP]
        c.messages.append((direction, text))
        c.last_dir = direction
        c.last_body = text
        if direction == "in":
            c.fan_msg_n += 1
            c.last_in_at = created_at
            mid_reply[fan_id] = False        # he spoke → her next row is a new reply
        else:
            c.last_out_at = created_at
            hers = mass_run_id is None and automation_kind in _OUR_KINDS
            if automation_kind is None and mass_run_id is None:
                c.last_human_out_at = created_at
            if gap is not None and hers and created_at is not None:
                prev_her = her_last.get(fan_id)
                # A gap since HER last reply opens a fresh burst — the counter is about
                # how much SHE has said, so only her own silence can clear it.
                if prev_her is not None and created_at - prev_her > gap:
                    c.session_out_n = 0
                    mid_reply[fan_id] = False
                # Another bubble of the reply we already counted, or a new reply?
                # Bubbles land seconds apart; a genuine second reply does not.
                same_reply = (mid_reply.get(fan_id) and prev_her is not None
                              and created_at - prev_her <= _BUBBLE_WINDOW)
                if not same_reply:
                    c.session_out_n += 1   # HER reply — not a human's, not a bubble
                her_last[fan_id] = created_at
            mid_reply[fan_id] = hers         # a human's row also closes her turn

    # ONE line per run, not one per row: the point is to make the corrupt rows
    # findable and fixable, not to flood the relay log if a whole account is dirty.
    if bad_ts:
        log.warning("ai_chatter[%s]: skipped %d message row(s) with an unreadable "
                    "created_at (fans %s) — repair the rows; one bad cell must "
                    "never silence an account again",
                    account_id, len(bad_ts), sorted(set(bad_ts))[:10])

    # The burst also goes stale on the CLOCK, not only when a new reply of hers arrives.
    # Without this the reset above can never fire for the fan it matters most for: she is
    # capped, so she sends nothing, so no row of hers ever comes to notice the gap has
    # passed — and she stays mute on him forever. An hour after her last reply, she is
    # free again whether or not he kept talking through it.
    if gap is not None:
        now = datetime.utcnow()
        for fid, c in out.items():
            h = her_last.get(fid)
            if h is None or now - h > gap:
                c.session_out_n = 0
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
_TIP_KINDS = ("tip", "tip_post", "tip_stream")

# The offer marker protocol: the model writes its pitch as normal bubbles and
# ends the reply with a line that is exactly ">>OFFER <catalog item id>". The
# marker line is ALWAYS stripped before sending (even malformed ones), and an
# offer only happens when the id survives every code-side guardrail.
_OFFER_MARKER_RE = re.compile(r"^\s*>{1,}\s*OFFER\s+(\d+)\s*$",
                              re.IGNORECASE | re.MULTILINE)
_OFFER_LINE_RE = re.compile(r"^.*>>\s*OFFER.*$", re.IGNORECASE | re.MULTILINE)

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
_UNLOCK_REACTIONS = (
    "omg enjoy babe 😘",
    "mmm enjoy 🙈 tell me what u think after",
    "ur the best 😏 enjoy",
    "eeek ok enjoy 💕 dont be shy after",
)
# Localized watcher reactions. 'en' is the fallback; add sl/pt/fr/de/it the same way.
_UNLOCK_REACTIONS_BY_LANG: dict[str, tuple[str, ...]] = {
    "en": _UNLOCK_REACTIONS,
    "es": (
        "omg disfrútalo bebé 😘",
        "mmm disfruta 🙈 dime qué te pareció después",
        "eres el mejor 😏 disfrútalo",
        "eeek ok disfruta 💕 no seas tímido después",
    ),
    "sl": (  # female speaker → male fan
        "omg uživaj srček 😘",
        "mmm uživaj 🙈 povej mi kako ti je bilo potem",
        "najboljši si 😏 uživaj",
        "iii ok uživaj 💕 ne bodi sramežljiv potem",
    ),
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


def _manifest_block(offerable: dict[int, CatalogItem],
                    scripts: dict[int, CatalogScript], cfg_mode: str,
                    quotes: dict[int, upsell.Quote] | None = None) -> str:
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
    return (
        "CONTENT YOU CAN ACTUALLY SEND HIM (these are real, already filmed — "
        "NEVER invent or promise anything not on this list, never customs, and "
        "describe a piece using ONLY its description):\n" + "\n".join(lines) + "\n\n"
        "SELLING RULES:\n"
        "- Selling is a side effect of good chat, not the goal of every message. "
        "Pitch ONLY when the vibe is warm or he's asking for content — at most "
        "ONE piece, woven naturally into your reply (e.g. \"tip me $10 and ill "
        "send it 😏\" or \"unlock it babe\"). Never pushy, never apologize for "
        "the price.\n"
        "- If he's ASKING for content or clearly turned on, don't stall or be "
        "coy about whether you have something — this IS the moment: pick the "
        "best-fitting piece, tease it from its description, name the price, and "
        "pitch it NOW.\n"
        "- A FREE piece is a treat you spontaneously send when he's sweet — "
        "tease it, don't oversell it.\n"
        "- When (and ONLY when) you decided to pitch/send a piece this message: "
        "end your reply with a final line that is exactly:\n"
        ">>OFFER <id>\n"
        "  Example shape (use your own words):\n"
        "  \"mmm i filmed smth in the kitchen earlier 😏 tip me $7 and ill send it\"\n"
        "  \">>OFFER 12\"\n"
        "- No pitch this message → do NOT write >>OFFER at all. Casual chat "
        "messages should NOT pitch — but never tease that you \"have something\" "
        "without actually pitching it (that's a stall; pitch it properly instead).\n"
        "- The list above is COMPLETE and CURRENT. Anything you mentioned or "
        "sold earlier that is NOT on it is gone — never re-offer, re-price, or "
        "re-describe it. Never invent new sets, lengths, counts, or prices. If "
        "he wants more and the list is empty-ish, tell him you're filming more "
        "soon — never promise specifics."
    )


def _pending_block(offer: ContentOffer, item: CatalogItem | None) -> str:
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
    return (
        f"YOU ALREADY OFFERED HIM A PIECE and he hasn't unlocked it yet: "
        f"{label} — {desc} ({' or '.join(terms)}).\n"
        f"{accum_note}"
        "- If he asks about it, answer from that description. A light, playful "
        "re-tease is fine ONCE in a while, but DON'T nag about it or repeat the "
        "price every message — mostly just keep chatting like normal.\n"
        "- DON'T offer or promise any other content while this one is pending, "
        "and never write >>OFFER."
    )


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


async def _open_offer_count(account_id: str, fan_id: int) -> int:
    return len(await _open_offers(account_id, fan_id))


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
                        quotes: dict[int, upsell.Quote] | None) -> str:
    """He has ONE unpaid PPV on the table and you may send a SECOND (and FINAL) one:
    a DIFFERENT piece, or the SAME piece re-priced lower if he's balking on price.
    After this second one there are two unpaid offers open, so the pitch stops."""
    plabel = (pend_item.label if pend_item else None) or "it"
    pprice = int(pending.price_cents or pending.tip_unlock_cents or 0) // 100
    return (
        _manifest_block(offerable, scripts, cfg_mode, quotes=quotes or None)
        + "\n\nSECOND OFFER — you already sent him "
        f"'{plabel}' for ${pprice} and he hasn't unlocked it yet. You MAY send ONE "
        "more piece THIS message (your second and last unpaid offer):\n"
        "- If he wants something different, pitch a DIFFERENT piece from the list "
        "and end with its >>OFFER line.\n"
        f"- If he's balking on PRICE, re-tease '{plabel}' at the lower price shown "
        "for it above and end with its >>OFFER line (a genuine drop, framed as a "
        "one-time thing for him — never beg).\n"
        "- If neither fits, DON'T pitch — just keep chatting; never write >>OFFER.\n"
        "- Do NOT stack a THIRD: only one new offer this message."
    )


def _parse_offer_marker(raw: str) -> tuple[str, int | None]:
    """Extract the FIRST well-formed >>OFFER id, then strip EVERY marker-ish
    line (malformed ones too — a fan must never see the protocol)."""
    m = _OFFER_MARKER_RE.search(raw or "")
    offer_id = int(m.group(1)) if m else None
    clean = _OFFER_LINE_RE.sub("", raw or "").strip()
    return clean, offer_id


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
    caption = _language.apply_word_restriction(
        random.choice(_UNLOCK_REACTIONS_BY_LANG.get(_ul_lang, _UNLOCK_REACTIONS)), _ul_lang)
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

async def _last_money_at(account_id: str, fan_ids) -> dict[int, datetime]:
    """{fan_id: newest money-event time} — the later of an inbound tip (is_tip, so
    the event time is created_at) and a PPV unlock (purchased_at). Drives the
    post-purchase talk window (item 17). Fans with no money event are absent."""
    ids = [int(x) for x in fan_ids]
    if not ids:
        return {}
    async with get_session() as s:
        rows = (await s.execute(
            select(Message.fan_id,
                   func.max(func.coalesce(Message.purchased_at, Message.created_at)))
            .where(Message.account_id == str(account_id),
                   Message.fan_id.in_(ids),
                   Message.is_unsent.is_(False),
                   or_(Message.is_tip.is_(True), Message.purchased_at.isnot(None)))
            .group_by(Message.fan_id)
        )).all()
    return {int(fid): ts for fid, ts in rows if ts is not None}


async def _spend_caps(account_id: str, fan_ids, rules: list[dict],
                      now: datetime) -> dict[int, int]:
    """{fan_id: the highest burst cap his ROLLING-WINDOW paid spend earns} for the
    item-21b spend floor. `rules` is msg_limits_by_spend — [{days, min_cents, cap}].
    Only fans that clear at least one rule appear; the rest keep their signal cap.

    Spend = PPV unlocks (paid outbound messages, priced by purchased_at/created_at)
    PLUS tips (the transactions ledger, by occurred_at) — the same two sources
    _buyer_facts and _paid_cents_7d already trust, so 'spend' here means the same
    thing it does everywhere else. One window is opened per DISTINCT `days` value in
    the rules (usually 2), each a single batched group-by, so cost is flat in fans.

    A rule with no positive cap or a non-positive window/threshold is inert — an
    operator zeroing a rung turns it off rather than capping anyone at 0."""
    ids = [int(x) for x in fan_ids]
    rules = [r for r in (rules or [])
             if int(r.get("cap") or 0) > 0
             and int(r.get("days") or 0) > 0
             and int(r.get("min_cents") or 0) > 0]
    if not ids or not rules:
        return {}
    # Sum each fan's paid spend once per distinct window, then let every rule that
    # shares that window read the same total — n distinct windows, not n rules.
    windows = sorted({int(r["days"]) for r in rules})
    spend_by_window: dict[int, dict[int, int]] = {}
    async with get_session() as s:
        for days in windows:
            since = now - timedelta(days=days)
            ppv = (await s.execute(
                select(Message.fan_id,
                       func.coalesce(func.sum(Message.price_cents), 0))
                .where(Message.account_id == str(account_id),
                       Message.fan_id.in_(ids),
                       Message.direction == "out",
                       Message.is_paid.is_(True),
                       Message.price_cents > 0,
                       func.coalesce(Message.purchased_at, Message.created_at) >= since)
                .group_by(Message.fan_id)
            )).all()
            tips = (await s.execute(
                select(Transaction.fan_id,
                       func.coalesce(func.sum(Transaction.amount_cents), 0))
                .where(Transaction.account_id == str(account_id),
                       Transaction.fan_id.in_(ids),
                       Transaction.kind.in_(_TIP_KINDS),
                       Transaction.status.in_(("cleared", "pending")),
                       Transaction.occurred_at >= since)
                .group_by(Transaction.fan_id)
            )).all()
            tot: dict[int, int] = {}
            for fid, cents in ppv:
                tot[int(fid)] = tot.get(int(fid), 0) + int(cents or 0)
            for fid, cents in tips:
                tot[int(fid)] = tot.get(int(fid), 0) + int(cents or 0)
            spend_by_window[days] = tot
    out: dict[int, int] = {}
    for fid in ids:
        best = 0
        for r in rules:
            spent = spend_by_window[int(r["days"])].get(fid, 0)
            if spent >= int(r["min_cents"]):
                best = max(best, int(r["cap"]))
        if best > 0:
            out[fid] = best
    return out


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


async def _human_money_signals(
    account_id: str, fan_ids, now: datetime,
) -> dict[int, tuple[datetime | None, datetime | None]]:
    """{fan_id: (live_ask_at, last_paid_at)} read from the THREAD, not from our own
    bookkeeping — so it sees what the human chatters did.

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
    """
    ids = [int(x) for x in fan_ids]
    if not ids:
        return {}
    out: dict[int, tuple[datetime | None, datetime | None]] = {}
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
            out[int(fid)] = (ts, None)
        for fid, ts in (await s.execute(
            select(Message.fan_id,
                   func.max(func.coalesce(Message.purchased_at, Message.created_at)))
            .where(*base, Message.is_paid.is_(True))
            .group_by(Message.fan_id)
        )).all():
            ask, _ = out.get(int(fid), (None, None))
            out[int(fid)] = (ask, ts)
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


def _cadence_gate(c: "_Cand", *, pending: ContentOffer | None, recent_payer: bool,
                  money_at: datetime | None, pic: bool,
                  now: datetime, cad: dict,
                  spend_cap: int = 0) -> tuple[bool, str, int]:
    """Decide whether ai_chatter should keep engaging this fan THIS tick, and under
    which signal tier. Returns (stop, tier, cap) — a pure function of the fan's live
    state; every limit comes from `cad` (the config). `cap` is the EFFECTIVE reply
    cap that decided `stop` (0 = uncapped, e.g. the post-purchase window); it is the
    single source of truth for "how many replies is she allowed here", so the status
    endpoint shows the same number the bot ran on instead of recomputing it. A "stop"
    means skip the reply this tick (no LLM, no pause): the fan reopens on a real
    buying signal (tier upgrade) or after a session-gap of silence resets his burst.

      • Post-purchase window (item 17): a fan who paid within post_purchase_minutes
        stays engaged with no cap; once that window lapses with no newer money event
        AND no live buying signal, stop and hand off (of_ai_chat keeps him warm in
        closer mode).
      • Otherwise classify the signal (item 21) and stop once the burst reply count
        (`session_out_n`) reaches that tier's cap (item 10 / "selling stops").

    `spend_cap` (item 21b) is the floor his rolling-window PAID spend earns, precomputed
    by _spend_caps. It only ever RAISES the leash: the effective cap is the MAX of the
    live signal tier's cap and the spend cap, so a proven spender is never cut off as
    short as a stranger, but a hotter live signal still wins if it's higher."""
    body = c.last_body or ""
    live_signal = bool(pic or _CONTENT_ASK_RE.search(body) or ESCALATION_RE.search(body))

    # Item 17 — post-purchase talk window. Only a RECENT money event opens it (an
    # ancient purchase must not stop a fan chatting again today), so bound it to the
    # recent-payer horizon.
    ppm = int(cad.get("post_purchase_minutes") or 0)
    if money_at is not None and ppm and (now - money_at) < timedelta(hours=1):
        if now - money_at <= timedelta(minutes=ppm):
            return (False, "post_purchase", 0)        # still warm — keep talking, no cap
        if not live_signal:
            return (True, "post_purchase_done", 0)    # quiet after the window → hand off
        # he's asking for more AFTER the window → a fresh sale opportunity, keep going

    limits = cad.get("msg_limits_by_signal") or {}
    if pic:
        tier = "pic_sent"
    elif recent_payer or live_signal:
        tier = "buying_signal"
    elif pending is not None:
        # An offer is on the table. Fresh → keep working it (buying_signal); stale
        # (older than offer_expiry_minutes, still unbought) → short-leash no_signal.
        oem = int(cad.get("offer_expiry_minutes") or 0)
        stale = bool(oem and pending.offered_at
                     and (now - pending.offered_at) > timedelta(minutes=oem))
        tier = "no_signal" if stale else "buying_signal"
    else:
        tier = "baseline"

    cap = int(limits.get(tier) or 0)
    # A proven spender's floor lifts the leash but never lowers it: take whichever
    # cap is larger. (A spend_cap of 0 — no rule matched — leaves the signal cap as
    # is; a signal cap of 0, i.e. an unconfigured tier, means "no cap" and must stay
    # uncapped, so only fold in spend_cap when there IS a signal cap to raise.)
    if cap:
        cap = max(cap, int(spend_cap or 0))
    return (bool(cap and c.session_out_n >= cap), tier, cap)


# One gentle re-engage opener (item 18). {name} → his greetable name (or "babe").
# Deliberately templated, not LLM-generated: a nudge is one unsolicited line, so we
# keep it cheap, predictable, and easy to audit.
_NUDGE_LINES = (
    "hey {name} u still there? 🙈",
    "miss talking to u {name} 🥺 what are u up to",
    "u went quiet on me {name}.. everything ok?",
    "cant stop thinking about our chat 😏 u around {name}?",
    "hey u 👀 dont leave me hanging {name}",
)


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
            name = (resolve_fan_name(f) if f else "").split("/")[0][:20] or "babe"
            line = apply_word_restriction(
                random.choice(_NUDGE_LINES).replace("{name}", name))[:_REPLY_MAX_CHARS]
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
        name = (resolve_fan_name(f) if f else "").split("/")[0][:20] or "babe"
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
    line = _pack_line("rung_escalate", cfg, fan_id) or "unlock this babe 😏"
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
        name = (resolve_fan_name(f) if f else "").split("/")[0][:20] or "babe"
        line = _pack_line("aftercare", cfg, fan_id, name=name) or "mmm come here 🥰"
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
                        override) -> tuple[str, str]:
    """Her sleep window. An operator override wins; otherwise it is DERIVED from
    her own outbound hour histogram — the UI does not ask a question the data
    already answers. Too little history ⇒ rhythm.DEFAULT_SLEEP, never "always
    awake" (a girl who never sleeps is a bot, and the absence of evidence has to
    fail safe)."""
    if isinstance(override, (list, tuple)) and len(override) == 2 and all(override):
        return (str(override[0]), str(override[1]))
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
    gap, and an already-waited-floored reply must log the full inbound→send latency."""
    prior = list(_recent_realized_raw(rst))
    prior.append({"d": round(max(0.0, float(realized_s)), 1),
                  "b": int(bubbles), "i": int(bool(informal))})
    await _save_rhythm(account_id, fan_id,
                       recent_turns_json=json.dumps(prior[-20:]))


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


async def _fan_ladder_state(account_id: str, fan_id: int, f: Fan | None,
                            ladder: LadderState | None) -> upsell.FanState:
    """The two facts the price actually depends on: the LARGEST single PPV he has
    ever paid (the history ceiling — never ask >1.5x it) and the rung he JUST bought
    (the escalation base). Read from PAID messages, never from lifetime_spend_cents:
    spend is a SUM, and a fan who tipped $5 forty times has never once paid $200."""
    async with get_session() as s:
        mx = (await s.execute(
            select(func.max(Message.price_cents)).where(
                Message.account_id == str(account_id),
                Message.fan_id == int(fan_id),
                Message.is_paid.is_(True))
        )).scalar_one_or_none()
    max_paid = int(mx or 0)
    last_paid: int | None = None
    # Only a HOT ladder escalates off the last rung — outside the hot window the
    # next ask is a cold open again, not last_paid * 1.5 forever.
    if ladder is not None and ladder.status == upsell.STATUS_HOT:
        async with get_session() as s:
            last_paid = (await s.execute(
                select(LadderQuote.price_cents)
                .where(LadderQuote.account_id == str(account_id),
                       LadderQuote.fan_id == int(fan_id),
                       LadderQuote.paid.is_(True))
                .order_by(LadderQuote.paid_at.desc(), LadderQuote.id.desc())
                .limit(1)
            )).scalars().first()
        last_paid = int(last_paid) if last_paid else None
    ever = max_paid > 0 or int(getattr(f, "lifetime_spend_cents", 0) or 0) > 0
    return upsell.FanState(fan_id=int(fan_id), max_single_paid_cents=max_paid,
                           last_paid_cents=last_paid, has_ever_paid=ever)


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
        "money he's already spent with you: " + " and ".join(parts)
        + ". He's a PROVEN spender — when it fits, reference what he bought/tipped "
        "like you remember it, stay warm and familiar, and never talk to him like "
        "he's never paid you."
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
    already paid). Fed to _quote_item so cheaper items are skipped and he climbs
    a tier — a $50 buyer gets the $60 video, never the $24 set again."""
    if not cfg.get("proven_spend_floor_enabled"):
        return 0
    mx = int(getattr(fstate, "max_single_paid_cents", 0) or 0)
    if mx <= 0:
        return 0
    return int(mx * float(cfg.get("proven_spend_floor_mult") or 1.0))


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
    band, _src = upsell.derive_band(human_asks_cents=human, account_median_cents=median)
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


async def _trigger_make_right_apology(account_id: str, fan_id: int) -> None:
    """Hand a hard-declining fan to make_right for the ONE de-escalation apology
    turn. The latest inbound message id keys the incident, so a webhook replay or
    the next sweep classifying the same message can't double-apologise.
    Best-effort: losing the apology must never lose the decline handling."""
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
                                      "message_id": int(mid) if mid else None}})
        ax.wake_supervisor()
    except Exception:
        log.warning("hard-decline make_right enqueue failed account=%s fan=%s",
                    account_id, fan_id, exc_info=True)


def _pack_line(slot: str, cfg: dict, fan_id: int, *, name: str = "babe",
               price_cents: int | None = None) -> str | None:
    """One line from the account's script pack (UI overrides > shipped defaults), in
    the account's language (cfg._account_lang; English per-slot fallback)."""
    overrides = cfg.get("script_pack_overrides")
    return script_packs.render(
        slot, rng=random.Random(f"pack:{slot}:{fan_id}:{datetime.utcnow():%Y%m%d%H%M}"),
        name=name, price_cents=price_cents,
        overrides=overrides if isinstance(overrides, dict) else None,
        lang=cfg.get("_account_lang", "en"))


# ── Prompt (forked from of_ai_chat._build_messages — adds the sell seam) ─────

def _build_messages(persona: str, f: Fan, c: _Cand, asked: set[str],
                    history_tail: int = _HISTORY_TAIL,
                    style_on: bool = False,
                    nonnative_on: bool = False,
                    sell_block: str = "",
                    content_ask: bool = False,
                    escalation: bool = False,
                    hot_thread: bool = False,
                    bot_accused: bool = False,
                    painful_on: bool = True,
                    lang: str = "en",
                    profile: "FanProfile | None" = None,
                    ask_every: int = 0,
                    buyer_facts: list[str] | None = None,
                    clock: str = "",
                    sticker_mode: str = "skip") -> tuple[list[dict], list[str]]:
    """Compose the (system, user) pair — of_ai_chat's girly info-gather prompt
    with one structural difference: `sell_block`. Empty (M2) → the no-offers
    line stays, byte-equal behavior. Non-empty (M3) → the catalog/offer rules
    replace it. The ask/breather dice, the facts block, and the style variants
    are copied verbatim so the voice can't drift from of_ai_chat's."""
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
        if _nonempty(val):
            facts.append(f"{label}: {str(val).strip()[:80]}")
    # gen_info's rich profile — the bio + bullet notes a human chatter reads before
    # replying. This is what makes a bubble land as "she remembers me", not generic.
    teases: list[str] = []
    if profile is not None:
        if _nonempty(profile.short_bio):
            facts.append(f"about him: {str(profile.short_bio).strip()[:400]}")
        if _nonempty(profile.bullet_points):
            bp = str(profile.bullet_points).strip().replace("\n", "; ")[:600]
            facts.append(f"notes on him: {bp}")
        teases = [str(t).strip()[:140]
                  for t in (profile.tease1, profile.tease2, profile.tease3) if _nonempty(t)]
    # Spend/tip history (computed async at the call site) — proven-spender context
    # shared by the chatter and the seller. Empty for a non-spender → prompt
    # stays byte-equal.
    if buyer_facts:
        facts.extend(buyer_facts)
    facts_block = ("\n".join(f"- {x}" for x in facts)
                   if facts else "- (nothing on file yet)")
    # A concrete nudge to WEAVE IN a specific detail — the difference between a bubble
    # that reads as a form letter and one that reads as her, mid-conversation. Only
    # added when there's something to reference (a bio/notes fact, a tease, or a
    # recent thing he said), so a profile-less fan's prompt is unchanged.
    personal_lines: list[str] = []
    if profile is not None and (_nonempty(profile.short_bio) or _nonempty(profile.bullet_points)):
        personal_lines.append(
            "Work in ONE specific, natural detail from what you know about him above "
            "(his job, a hobby, something going on in his life) — like you actually "
            "remember him. Don't recite a list; drop one nugget the way a girlfriend would.")
    if teases:
        personal_lines.append(
            "You may riff on one of these lines the team wrote for him — reword it in "
            "your own voice, don't paste it verbatim: " + " | ".join(teases))
    personal_block = ("\n" + "\n".join(personal_lines)) if personal_lines else ""

    history = c.messages[-history_tail:]
    convo = "\n".join(
        f"{'FAN' if d == 'in' else 'YOU'}: {b}" for d, b in history if b
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

    if bot_accused:
        # §6.4 first strike — he thinks she might be a bot. Defensiveness ("I'm a
        # real person, I promise!") is exactly what a bot does; the humane, effective
        # move is to brush it off and pivot to something HE said. Sell nothing.
        need_block = (
            "He thinks you might be a bot. DON'T get defensive and DON'T list "
            "evidence that you're real — that is exactly what a bot does. Brush it "
            "off in ONE short, breezy line, then bring up something HE told you "
            "earlier. Sell nothing this message and ask no get-to-know question."
        )
        presented = []
        ask = False
    elif content_ask and sell_block.strip():
        # He's asking for content and there's a live manifest: the gather goal
        # yields — this message is the pitch, not another interview question.
        need_block = (
            "HE IS ASKING FOR CONTENT RIGHT NOW — don't change the subject and "
            "don't ask a get-to-know question this message. Pick the piece from "
            "WHAT YOU CAN SELL that best fits the vibe, tease it from its "
            "description, give the terms, and end with the >>OFFER line."
        )
        presented = []
        ask = False
    elif escalation and sell_block.strip():
        # He's leaning in / getting physical with a live manifest: stop teasing and
        # convert. Softer than a content-ask (he didn't literally say "show me"), so
        # one flirty line THEN the offer — never a cold price-drop.
        need_block = (
            "HE'S CLEARLY INTO IT RIGHT NOW — leaning in, getting flirty/physical. "
            "This is the moment to SELL, not tease again. Don't ask a get-to-know "
            "question. Match his heat with one short line, then pick the piece from "
            "WHAT YOU CAN SELL that fits the vibe, tease it from its description, give "
            "the terms, and end with the >>OFFER line."
        )
        presented = []
        ask = False
    elif hot_thread:
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
            "THE THREAD IS HOT — he is in a sexual conversation with you RIGHT NOW.\n"
            "This is the moment that makes money, and there is exactly one way to throw "
            "it away: ask him a get-to-know question. Do NOT ask about his job, his day, "
            "his city, his hobbies, his age. Nothing kills a live scene faster.\n"
            "Run the scene:\n"
            "1. STAY IN IT. Present tense, first person, dirty, SHORT — the way you'd "
            "actually type mid-scene. Not a paragraph, not a compliment.\n"
            "2. MAKE HIM SAY WHAT HE WANTS. If he hasn't told you exactly what he'd do "
            "to you (or want done to him), ask — 'what would you do to me if you were "
            "here right now?', 'how would you have me?'. His answer is the whole game.\n"
            "3. MIRROR IT BACK, HOTTER. When he tells you, say it back in your own "
            "words, escalated, as if it's already happening."
        )
        if sell_block.strip():
            need_block += (
                "\n4. THEN SELL HIM WHAT HE JUST DESCRIBED. Pick the piece from WHAT YOU "
                "CAN SELL that matches HIS OWN WORDS, and write the caption as the NEXT "
                "LINE OF THIS SCENE — never as a product. 'here we go babe, legs spread "
                "apart, waiting for you' — not 'check out my new video'. End with the "
                ">>OFFER line. If he hasn't described anything yet, do step 2 first and "
                "sell on the next message."
            )
        presented = []
        ask = False
    elif not question_lines:
        need_block = "You know enough about him now — just chat and flirt naturally."
    elif ask:
        need_block = (
            "YOUR GOAL THIS MESSAGE: find out ONE of these about him. Pick whichever "
            "flows best from what he just said and weave it in naturally — any order "
            "is fine. If he dodged something earlier, DON'T re-ask it back-to-back; "
            "just move on to another one and you can poke it again later:\n" + question_lines
        )
    elif fan_just_asked:
        need_block = (
            "THIS MESSAGE: he just asked you something — answer it warmly and briefly "
            "in your own words, and DON'T fire a question back this time. (You'll ask "
            "next time.)"
        )
    else:
        need_block = random.choice(_BREATHER_VARIANTS)

    # Item 15 — sticky name: once we hold a DURABLE name for him (team-curated
    # custom_nickname or an extracted real_name), pin it so the model greets him by
    # it and NEVER re-interviews for his name — killing the "what's ur name" loop
    # even deep into a chat. resolve_fan_name already prefers these, so `name` here
    # is exactly the token to keep using (same split/clip as the facts line).
    have_durable_name = bool(name) and (_nonempty(getattr(f, "custom_nickname", None))
                                        or _nonempty(getattr(f, "real_name", None)))
    call_him = (
        f"\n\nHIS NAME: call him {name.split('/')[0][:40]} from here on — we already "
        "know it, so NEVER ask his name again; just use it naturally."
        if have_durable_name else "")

    name_dodged = (not have_durable_name and not _nonempty(f.real_name)
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
        style = ("a cat sticker says it all this time — if one of the CAT "
                 "STICKERS below fits his last message, reply with ONLY the "
                 "STICKER line, no text at all. Only write text if truly "
                 "none fits.")

    humanizer = f"\n\n{STYLE_HUMANIZER}" if style_on else ""
    nonnative = f"\n\n{NONNATIVE_REGISTER}" if nonnative_on else ""
    # Sticker protocol enters the prompt only on an allow/solo roll — a model
    # that can't see it can't over-use it (measured 48% attach when always on).
    _sticker_block = cat_stickers.prompt_block(sticker_mode)
    stickers = f"\n\n{_sticker_block}" if _sticker_block else ""

    # M3 seam: with no catalog in play the no-offers rule applies (chat-only,
    # current of_ai_chat behavior); with one, a short pointer goes in the intro
    # and the FULL sell block lands as its own section near the end of the
    # prompt (high salience — inlining 20 manifest lines mid-sentence buried it).
    has_sell = bool(sell_block.strip())
    offers_line = ("you DO have real content you can send or sell when the "
                   "moment is right — see WHAT YOU CAN SELL below."
                   if has_sell else "don't offer pics or videos yet.")
    sell_section = f"\n\n{sell_block.strip()}\n\n" if has_sell else "\n\n"

    # The prompt clock ("" when the account has no tz configured → byte-equal
    # prompt) — same block as of_ai_chat. "what time is it where you are?" is
    # the classic bot trap, and a model with no clock invents one.
    clock_block = (
        f"RIGHT NOW for you it is {clock}. If the time, your day, or what "
        "you're doing comes up, stay consistent with this clock — never claim "
        "a different time of day.\n\n" if clock else "")

    system = (
        f"{persona}\n"
        "You're texting a fan to get to know him while flirting. You are 100% human "
        "— never sound like a bot or an assistant. Use only what you've learned "
        "about him; don't share your own info unless he asks; "
        f"{offers_line} "
        "He may send several texts in a row — read them all, reply to "
        "the latest.\n\n"
        f"{PAINFUL_TEXTING + chr(10) + chr(10) if painful_on else ''}"
        f"{clock_block}"
        f"{need_block}{dodge_note}{call_him}\n\n"
        f"STYLE FOR THIS MESSAGE — {style}\n\n"
        "HOW YOU TEXT (a real 22yo girl, not an assistant):\n"
        "- Short and casual. lowercase, contractions, u/ur/ya. React to what he "
        "said in a few words first.\n"
        "- VARY it every time — don't open the same way twice, and don't reuse a "
        "phrase or an emoji you've already used in this chat.\n"
        "- At most ONE question, never one he already answered (if his answer was "
        "vague, ask a quick follow-up instead of re-asking). Don't narrate, no "
        "paragraphs.\n"
        "- If he gets explicit early: don't go along with it — playfully tease and "
        "slow it down, then steer back to getting to know him. Warm and flirty, "
        "never cold or preachy.\n"
        f"{_good_examples(f, asked, have_durable_name)}\n"
        f"{ONPLATFORM_GUARDRAIL}"
        f"{humanizer}{nonnative}{stickers}"
        f"{sell_section}"
        + ("Your reply is ONLY the message text — no JSON, quotes, or metadata. "
           "The ONE exception: the final >>OFFER line when you pitch a piece "
           "(it's stripped before sending — the fan never sees it)."
           if has_sell else
           "Your reply is ONLY the message text — no JSON, quotes, or metadata.")
        # Without this carve-out the contract line above suppresses the marker
        # entirely — verified live: 4/4 solo rolls produced no STICKER line
        # until the exception was stated here.
        + (" The final STICKER: <tag> line is ALSO allowed (stripped before "
           "sending — he only sees the gif)." if _sticker_block else "")
        # OUTPUT-LANGUAGE block at the very END (prefix-cache safe); "" for en. It also
        # pins the >>OFFER token so a Spanish reply never leaks a translated marker.
        + _language.output_language_directive(lang)
    )
    user = (
        f"What you know about him:\n{facts_block}{personal_block}\n\n"
        f"Recent conversation (oldest→newest):\n{convo}\n\n"
        "Reply to his last message now, in the STYLE FOR THIS MESSAGE above."
    )
    return ([{"role": "system", "content": system},
             {"role": "user", "content": user}], presented)


# ── Hot-lead TRINITY (S2→S1→S3) ──────────────────────────────────────────────
# The operator's main flow for a HOT lead (he's demanding content): escalate and
# ask for a TIP (S2); if he doesn't tip within a rung gap, ONE direct tip nudge
# (S1); if still no tip, hand off to the real OFFER engine — a locked PPV with
# preview + price (S3), NOT a free-text price beg. Gated on hotsell_trinity_enabled
# (default OFF). Per-fan stage lives in fans.custom_fields[_HOTSELL_KEY] (JSON,
# no migration), mirroring the _typo_fix throttle's storage.
_HOTSELL_KEY = "_hotsell"
_HOTSELL_GAP_S = upsell.RUNG_GAP_S  # min seconds in a stage before advancing (180)

# S2/S1 inject a tip-ask directive as the ai_chatter sell_block. S3 emits NO
# directive here — the caller lets the normal offer manifest ride so the model can
# write a real ">>OFFER <id>" (the engine then sends the locked PPV + preview).
_HOTSELL_S2 = (
    "HE ASKED TO SEE CONTENT and he's HOT. do NOT slow him down or say 'earn it'. "
    "MATCH his heat and escalate in ONE short filthy-teasing line, then tell him to "
    "send you a lil tip right here n you'll send him something — teasing, never "
    "needy, never the bare word 'tip'.")
_HOTSELL_S1 = (
    "he hasn't tipped yet but he's still into it. nudge him ONCE more, direct but "
    "playful: ONE short line making it easy — send a lil something right here n "
    "you'll spoil him. NEVER beg, NEVER scold, NEVER shame him for not paying, keep "
    "it hot.")


def _hotsell_advance(stage: str | None, entered_iso: str | None,
                     tipped_since_cents: int, now: datetime,
                     gap_s: int = _HOTSELL_GAP_S) -> tuple[str, str | None]:
    """Pure stage machine → (action, directive). Actions:
      'paid' → he tipped since entering the stage; exit (clear state).
      'S2'/'S1' → (re)emit that tip-ask rung's directive; caller persists the stage.
      'wait' → in a stage but the rung gap hasn't elapsed; hold, don't re-ask.
      'S3' → gap elapsed after S1 with no tip; hand to the offer engine (no directive).
    Pure + seedless so it unit-tests without a DB."""
    if tipped_since_cents > 0:
        return "paid", None
    if stage is None:
        return "S2", _HOTSELL_S2           # first touch: open with escalate+tip-ask
    try:
        entered = datetime.fromisoformat(entered_iso) if entered_iso else now
    except Exception:
        entered = now
    if (now - entered).total_seconds() < gap_s:
        return "wait", None                # too soon to escalate the ask
    if stage == "S2":
        return "S1", _HOTSELL_S1           # no tip after the gap → direct nudge
    return "S3", None                      # S1 (or later) exhausted → real PPV


async def _hotsell_load(account_id: str, fan_id: int) -> tuple[str | None, str | None]:
    """(stage, entered_iso) for this fan, or (None, None)."""
    async with get_session() as s:
        fan = (await s.execute(select(Fan).where(
            Fan.account_id == str(account_id), Fan.fan_id == int(fan_id)))).scalar_one_or_none()
        cf = json.loads(fan.custom_fields) if fan and fan.custom_fields else {}
    st = cf.get(_HOTSELL_KEY) or {}
    return st.get("stage"), st.get("at")


async def _hotsell_save(account_id: str, fan_id: int,
                        stage: str | None, at_iso: str | None) -> None:
    """Persist (or clear, when stage is None) the fan's hot-sell stage."""
    async with get_session() as s:
        fan = (await s.execute(select(Fan).where(
            Fan.account_id == str(account_id), Fan.fan_id == int(fan_id)))).scalar_one_or_none()
        if fan is None:
            return
        try:
            cf = json.loads(fan.custom_fields) if fan.custom_fields else {}
        except Exception:
            cf = {}
        if stage is None:
            cf.pop(_HOTSELL_KEY, None)
        else:
            cf[_HOTSELL_KEY] = {"stage": stage, "at": at_iso}
        fan.custom_fields = json.dumps(cf)
        await s.commit()


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
    resume_h = max(0, int(cfg.get("resume_after_manual_hours") or 0))
    max_replies = int(payload.get("max_replies") or cfg.get("max_fans_per_tick") or 8)

    model = await resolve_model(account_id, _PURPOSE, payload.get("model"))
    typing_wpm = await load_typing_wpm(account_id)
    typing_indicator = await load_typing_indicator(account_id)
    style_on = (await load_style_flags(account_id))[_PURPOSE]
    typo_on = (await load_typo_flags(account_id))[_PURPOSE]
    nonnative_on = (await load_nonnative_flags(account_id))[_PURPOSE]
    painful_on = await load_painful_texting_flag(account_id)  # brevity/emotion framing (default ON)
    stickers_on = await load_cat_stickers_flag(account_id)    # cat reaction gifs (default ON)
    sticker_skip_w, sticker_solo_w, sticker_gap_min = \
        await load_cat_sticker_tuning(account_id)             # per-account rate knobs
    account_lang = await _language.load_account_language(account_id)  # output language + guard gate
    max_bubbles = STYLE_MAX_BUBBLES if style_on else 2
    persona = await _load_persona(account_id)

    # Cadence controller (items 10/17/18/21) — OFF unless the account opts in.
    cadence_on = bool(cfg.get("cadence_enabled"))
    nudge_on = cadence_on and bool(cfg.get("nudge_enabled"))
    nudge_min = int(cfg.get("nudge_after_minutes") or 0)
    session_gap_min = int(cfg.get("session_gap_minutes") or 0) if cadence_on else 0

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

    cfg_row = await _load_cfg_row(account_id)
    tz_off = (rhythm.tz_offset_for(getattr(cfg_row, "timezone", None),
                                   getattr(cfg_row, "utc_offset", None))
              if rhythm_on else None)
    # Prompt clock: independent of the rhythm flag — the chat model must know
    # HER local time even when human-rhythm pacing is off. None ⇒ no clock line.
    clock_tz = rhythm.tz_offset_for(getattr(cfg_row, "timezone", None),
                                    getattr(cfg_row, "utc_offset", None))
    sleep_win = (await _sleep_window(account_id, tz_off, cfg.get("sleep_window"))
                 if rhythm_on else rhythm.DEFAULT_SLEEP)
    media_asks: dict[int, list[int]] = {}
    acct_median: int | None = None
    lib_bounds = (upsell.OF_PRICE_FLOOR_CENTS, 20_000)
    if pricing_on:
        media_asks, acct_median, lib_bounds = await _price_context(account_id, cfg_row)

    blacklist, skip_reasons = await _load_stop_lists(account_id)
    old_fan_ids: set[int] = ({fid for fid, r in skip_reasons.items()
                              if r == _OLD_FAN_SKIP} if engage_old else set())
    mid_funnel_fans = await _load_mid_funnel_fans(account_id)
    by_fan = await _gather(account_id, only_fan_ids or None,
                           session_gap_min=session_gap_min)

    client = await asyncio.to_thread(ax._make_client, account_id)

    # ── M3 offer layer: resolve unlocks FIRST (a fan doesn't have to speak to
    # buy), then load the catalog + the open-offer map the prompts read.
    cfg_offer_mode = str(cfg.get("offer_mode") or "ppv")
    intent_only = bool(cfg.get("intent_only"))
    pivot_on_escalation = bool(cfg.get("pivot_on_escalation"))
    esc_min_msgs = int(cfg.get("min_fan_msgs_before_escalation_pitch") or 0)
    # Inert without the gate — force_ask rides ON gate_ok, and with the gate off the
    # gate never runs, so there is nothing to ride. Guarded here (not just at the call
    # site) so a config with force_ask on and the gate off can't look armed.
    force_ask = bool(cfg.get("force_ask")) and gate_on
    # Max unpaid PPVs he may hold at once — configurable, but never below the default 2
    # (the whole point is that a second offer in a row is allowed).
    max_open_offers = max(_MAX_OPEN_OFFERS, int(cfg.get("max_open_offers") or 0))
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
    scripts, catalog_items = await _load_catalog(account_id)
    offer_stats = await _resolve_open_offers(account_id, client, cfg,
                                             dry_run=dry_run,
                                             only_fan_ids=only_fan_ids or None)
    open_by_fan = {int(o.fan_id): o for o in await _open_offers(account_id)}
    # Recent payers (tip/PPV unlock in the last RECENT_PAYER_HOURS) count as
    # intent in closer mode — the seller rides a just-paid hot moment even if the
    # latest line isn't an explicit buy-ask. autoreply skips this exact set, so a
    # hot lead is never demoted to never-sell keep-warm. Mirrors engaged_subset.
    recent_payers = await recent_payer_fans(account_id, list(by_fan.keys()))
    # Newest money-event time per fan — the post-purchase talk window (item 17).
    money_at = await _last_money_at(account_id, by_fan.keys()) if cadence_on else {}
    # Proven-spend cap floor per fan (item 21b) — {fid: highest cap his rolling-window
    # paid spend earns}, folded into the cadence gate. Only computed when cadence is on
    # AND at least one spend rule is configured, so an off flag / empty list costs nil.
    spend_caps = (await _spend_caps(account_id, by_fan.keys(),
                                    cfg.get("msg_limits_by_spend") or [], datetime.utcnow())
                  if cadence_on else {})
    # Ladder + rhythm state for this sweep — one query each, and NOT EVEN READ when
    # the lanes are off (an off flag must cost nothing, not just change nothing).
    ladders = await _load_ladders(account_id, by_fan.keys()) if gate_on else {}
    rstates = await _load_rhythm(account_id, by_fan.keys()) if rhythm_on else {}
    # What the HUMANS did in these threads: a chatter's unpaid PPV is a live ask, and a
    # PPV he unlocked is a purchase — neither has ever been visible to this engine.
    # Read whenever the gate or rhythm is on, because both are misled without it.
    human_money = (await _human_money_signals(account_id, by_fan.keys(), datetime.utcnow())
                   if (gate_on or rhythm_on) else {})
    # gen_info profiles (bio / bullet notes / teases) → the prompt, so the AI knows
    # his story, not just his tags. Always loaded (personalization is not gated).
    profiles = await _load_profiles(account_id, by_fan.keys())
    asks_by_fan, acct_hour_asks, acct_day_asks = (
        await _ask_counters(account_id, datetime.utcnow()) if gate_on else ({}, 0, 0))

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
    skipped_sla_fresh = 0   # backup mode: inbound younger than the SLA
    skipped_manual = 0      # a human chatted too recently (cautious resume)

    for fan_id, c in by_fan.items():
        forced = fan_id in force_ids
        if fan_id in blacklist:
            skipped_listed += 1
            continue
        reason = skip_reasons.get(fan_id)
        if (reason is not None and reason not in _GRADUATION_SKIPS and not forced
                and fan_id not in old_fan_ids):
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
                    rhythm_waiting += 1
                    continue
                # The pause ELAPSED but the resume job never reached the send path
                # (its lease was taken, a cooldown hit, a gate skipped him). Drop the
                # stale wake_at so he is a candidate again.
                #
                # `deferrals` is deliberately NOT cleared here: the one-hop cap is per
                # PENDING REPLY, not per lifetime. Keeping it means decide() hits the
                # cap on this tick and answers him INLINE — which is the whole point of
                # the cap (he is owed a reply and must get one). Clearing it here would
                # let him be deferred a second time for the same unanswered message.
                # It resets on the send, and below on a NEW inbound — a new message is
                # a new obligation, and rhythm must be free to pause on it, or the
                # feature would silently degrade to one-shot for that fan forever.
                fresh_inbound = (_rs.updated_at is not None and c.last_in_at is not None
                                 and c.last_in_at > _rs.updated_at)
                await _save_rhythm(account_id, fan_id, wake_at=None,
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
                if (int(f.lifetime_spend_cents or 0) == 0
                        and (f.source or "") == "creator_we_follow"):
                    skipped_spam += 1
                    continue
                if int(f.lifetime_spend_cents or 0) >= gate_cents:
                    skipped_whale += 1
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
    hard_stops = 0          # gate: chargeback/report/unsubscribe → ladder STOPPED
    soft_acks = 0           # gate: "i'm broke" → keep talking, stop selling 24h
    gate_blocked = 0        # gate: no price in front of this fan right now
    offers_parked = 0       # …and the reason was transient → PendingOffer row
    rungs_quoted = 0        # priced rungs that actually went out (ladder_quote rows)
    taps_expired = 0        # tap-outs that served their TTL and reopened (not a life sentence)
    rhythm_deferred = 0     # lease released + resume job enqueued (never slept)
    cover_lines_sent = 0    # "sorry babe was in the shower 🚿" before the reply
    stickers_sent = 0       # cat reaction gifs delivered (incl. sticker-only replies)
    price_errors = 0        # §4.1: priced attaches OF rejected → offer dropped, resent unpriced
    spend_regret_stops = 0  # §6.1: "im out of money" → 24h soft stop + COOLDOWN
    companion_routed = 0    # §6.3: "i just wanna talk" → seller OFF, conversation ON
    bot_accusations = 0     # §6.4: "are you a bot" → offer suppressed (2nd strike ⇒ COMPANION)
    spend_capped = 0        # §6.2: 7d paid-spend brake → COMPANION for the window
    ppv_drops = 0           # §3.6: inline setup→attach pacing holds
    paid_state_refreshed = 0  # PPVs the fan had unlocked that our is_paid still called unpaid
    errors = 0
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
        if cadence_on:
            cad_stop, _cad_tier, _cad_cap = _cadence_gate(
                c, pending=open_by_fan.get(fan_id),
                recent_payer=fan_id in recent_payers,
                money_at=money_at.get(fan_id),
                pic=fan_id in intent_fan_ids, now=now, cad=cfg,
                spend_cap=spend_caps.get(fan_id, 0))
            if cad_stop:
                skipped_cadence += 1
                continue
        if await ax.fan_on_cooldown(account_id, fan_id):
            skipped_cooldown += 1
            continue
        if not await ax.acquire_fan_lease(account_id, fan_id, _PURPOSE):
            skipped_locked += 1
            continue
        sent_ok = False
        try:
            f = fans.get(fan_id) or Fan(account_id=str(account_id), fan_id=fan_id)

            # Did he UNLOCK the ask that's sitting in front of him? `is_paid` is stamped
            # at ingest and never re-read, so a PPV he paid for reads as unpaid until the
            # ledger lands (up to 10h). One OF read settles it — and only when there IS a
            # live unpaid ask AND he has spoken since, so a quiet thread costs nothing.
            if (gate_on or rhythm_on) and human_money.get(fan_id, (None, None))[0]:
                _ask_at = human_money[fan_id][0]
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
            bot_accused_first = False
            # A HARD decline WINS over the poverty/companion brakes — always. Otherwise a
            # message that carries both a distress token and a chargeback ("im tapped out,
            # im disputing this charge and reporting you") hits detect_spend_regret first
            # and takes the 24h regret pause instead of the HARD consequence — the 72h
            # offers-pause + the make_right de-escalation apology. The hard signal must
            # keep its own, stronger handling even when softer tokens ride along.
            hard_decline = (gate_on and
                            upsell.classify_decline(c.last_body) == upsell.DECLINE_HARD)
            if hard_decline:
                await _handle_decline(account_id, fan_id, upsell.DECLINE_HARD, now)
                if not dry_run:
                    await _trigger_make_right_apology(account_id, fan_id)
                hard_stops += 1
                log.info("ai_chatter HARD decline account=%s fan=%s — 72h offers-pause "
                         "+ make_right apology (never a permanent bot stop)",
                         account_id, fan_id)
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
                name = (resolve_fan_name(f) or "").split("/")[0][:20] or "babe"
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
                name = (resolve_fan_name(f) or "").split("/")[0][:20] or "babe"
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
                    name = (resolve_fan_name(f) or "").split("/")[0][:20] or "babe"
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

            # §6.4 — bot accusation. Stateful: 1st strike suppresses the offer this
            # turn and brushes it off in one line; 2nd strike ends selling (COMPANION
            # for the session). Never skip_list, never a hard stop — 96% of accusers
            # keep talking and 23% go on to pay. Also honour an ACTIVE companion/
            # cooldown window from an earlier turn (seller OFF, conversation ON).
            if gate_on:
                _lad_now = ladders.get(fan_id)
                if _lad_now is not None:
                    if _lad_now.companion_until and _lad_now.companion_until > now:
                        seller_off = True     # §6.3/§6.1 window still live
                    if _lad_now.cooldown_until and _lad_now.cooldown_until > now:
                        seller_off = True     # §6.2 post-multibuy ease-off (talk only)
                if _detect_bot_accusation(c.last_body):
                    bot_accusations += 1
                    prev = int(_lad_now.bot_accused_count or 0) if _lad_now is not None else 0
                    new_count = prev + 1
                    if new_count >= 2:
                        await _save_ladder(account_id, fan_id,
                                           status=upsell.STATUS_COMPANION,
                                           bot_accused_count=new_count,
                                           companion_until=now + timedelta(hours=24))
                        seller_off = True
                        log.info("ai_chatter 2nd bot-accusation → COMPANION account=%s "
                                 "fan=%s", account_id, fan_id)
                    else:
                        await _save_ladder(account_id, fan_id, bot_accused_count=new_count)
                        seller_off = True         # suppress the offer this turn
                        bot_accused_first = True  # + brush it off, single bubble

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
                _h_ask, _h_paid = human_money.get(fan_id, (None, None))
                away = rhythm.decide_availability(rhythm.RhythmCtx(
                    account_id=str(account_id), fan_id=fan_id,
                    last_inbound_at=c.last_in_at, last_outbound_at=c.last_out_at,
                    # A live ladder suppresses break rolls: never strand a sell. A
                    # HUMAN's unpaid PPV is just as live a sell as one of ours — without
                    # this, rhythm scored a thread with a chatter's $45 ask sitting in it
                    # as a boring free-chat and took a coffee break mid-close.
                    # BOUNDED to _ASK_BREAKPROOF_WINDOW: glued for the 30 minutes that
                    # are the sale, then free to be a person again (with a cover line).
                    ladder_open=bool(lad0 is not None and lad0.status
                                     in (upsell.STATUS_OPEN, upsell.STATUS_HOT))
                    or _breakproof(_h_ask, rnow0),
                    last_paid_at=_newest(
                        lad0.last_paid_at if lad0 is not None else None, _h_paid),
                    his_last_latency_s=_his_last_latency_s(c),  # heat: his pace
                    fan_hot=_fan_hot(c),                        # heat: he's escalating
                    sleep_window=sleep_win, tz_offset_minutes=tz_off,
                    no_sleep=rhythm_no_sleep,
                    last_cover_at=(rst0.last_cover_at if rst0 is not None else None),
                    enabled=True,
                ), rnow0, random.Random(f"rhythm:{account_id}:{fan_id}:{rnow0.timestamp()}"))
                if away is not None and int(getattr(rst0, "deferrals", 0) or 0) < 1:
                    await _save_rhythm(account_id, fan_id, context=away.context,
                                       wake_at=away.wake_at, deferrals=1)
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
                                            _EXTRACT_HISTORY_TAIL, purpose=_PURPOSE)
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
            sell_block = ""
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
                    human_money.get(fan_id, (None, None))[0])
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
            open_count = await _open_offer_count(account_id, fan_id) if pending is not None else 0
            second_offer = pending is not None and open_count < max_open_offers
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
            elif pending is not None and open_count >= max_open_offers:
                # Max unpaid PPVs already on the table — stop pitching, just chat.
                sell_block = _pending_block(pending, await _get_item(int(pending.item_id)))
            elif catalog_items and await _offer_caps_ok(account_id, fan_id, caps_cfg):
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
                            human_money.get(fan_id, (None, None))[0]),
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
                if offerable:
                    if second_offer:
                        sell_block = _second_offer_block(
                            pending, await _get_item(int(pending.item_id)),
                            offerable, scripts, cfg_offer_mode, quotes or None)
                    else:
                        sell_block = _manifest_block(offerable, scripts, cfg_offer_mode,
                                                     quotes=quotes or None)

            # fan_lang resolved above (haggle detection) — drives the reply language
            # AND the bilingual buy-signal detectors below.
            content_ask = bool(offerable) and _language.is_content_ask(c.last_body, fan_lang)
            # Lean-in pivot: he's getting physical/horny (ESCALATION) with a live
            # manifest and HAS chatted a bit — ride it as an offer instead of teasing
            # again. An explicit content-ask already owns the pivot, so don't
            # double-count. Offer pacing caps still gate whether `offerable` is live.
            escalation = (bool(offerable) and pivot_on_escalation and not content_ask
                          and c.fan_msg_n >= esc_min_msgs
                          and _language.is_escalation(c.last_body, fan_lang))
            buyer_facts = await _buyer_facts(account_id, fan_id)
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
            msgs, presented = _build_messages(persona, f, c, asked, history_tail,
                                              sticker_mode=sticker_mode,
                                              style_on=style_on,
                                              nonnative_on=nonnative_on,
                                              sell_block=sell_block,
                                              content_ask=content_ask,
                                              escalation=escalation,
                                              hot_thread=hot_thread,
                                              bot_accused=bot_accused_first,
                                              painful_on=painful_on,
                                              lang=fan_lang,
                                              profile=profiles.get(fan_id),
                                              ask_every=(old_q_every
                                                         if fan_id in old_fan_ids
                                                         else 0),
                                              buyer_facts=buyer_facts,
                                              clock=_clock_line(clock_tz))
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
            if sticker_mode == "skip":
                sticker_tag = None
            offer_item = offerable.get(offer_id) if offer_id is not None else None
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
            _trigger = ("hot" if (force_ask and hot_thread) else
                        "ask" if (force_ask and content_ask
                                  and (gate_ok or ask_override)) else
                        "stale" if stale_ask else None)
            # Per-TICK account budget on forced asks. _offer_caps_ok does NOT pace a
            # fan's FIRST offer (its min-msgs branch is skipped when he has no prior
            # ContentOffer), so on the tick force_ask/floor is first enabled, EVERY
            # long-standing never-offered fan trips at once — ~1,283 fans on this roster.
            # That is a burst of priced sends in one minute, exactly the OF-session shape
            # the per-hour cap exists to prevent, and the per-run snapshot of that cap
            # can't see it. This bounds the blast: the floor drips instead of flooding.
            if (_trigger and (gate_ok or (_trigger == "ask" and ask_override))
                    and not seller_off
                    and offer_item is None and offerable
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
            raw, _leak = guard_offplatform(raw, random.Random(f"{fan_id}:{raw}"))
            if _leak:
                offer_item = None  # a guarded reply must not carry a paid attach
                log.info("ai_chatter off-platform leak guarded account=%s fan=%s reasons=%s",
                         account_id, fan_id, _leak)
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
            if (teaser is None and convo_teaser_cfg is not None and not dry_run
                    and not _leak and not seller_off):
                # Adaptive cadence: if nobody is selling him a PPV (none pending, none
                # going out this turn), the convo-teaser is his ONLY offer — fire it
                # SOONER (after ~10 of his msgs). If a PPV is already in play, keep the
                # configured spacing (20) so we don't pile a teaser on top of a live ask.
                _tcfg = convo_teaser_cfg
                if pending is None and offer_item is None:
                    _tcfg = {**convo_teaser_cfg,
                             "after": min(int(convo_teaser_cfg.get("after") or 20), 10)}
                _tstate = _tip_reward.teaser_state(f)
                _since = None
                if _tstate.get("at"):
                    try:
                        _since = datetime.fromisoformat(str(_tstate["at"]))
                    except Exception:
                        _since = None
                # Adaptive ladder climb/soften signal: did HER last teaser sell? Only
                # her own teaser unlock counts (Message.is_paid on that id) — an
                # ai_chatter catalog buy never moves this ladder. Queried only in
                # adaptive mode, only when the last teaser was priced.
                _t_last_price = int(_tstate.get("last_price") or 0)
                _t_last_free = bool(_tstate.get("last_free"))
                _t_sold = False
                if (_tcfg.get("adaptive") and _t_last_price > 0
                        and _tstate.get("last_msg")):
                    _t_sold = await _teaser_sold(account_id, fan_id,
                                                 int(_tstate["last_msg"]))
                try:
                    _msgs_since = await _fan_msgs_since(account_id, fan_id, _since)
                    teaser = await _tip_reward.pick_convo_teaser(
                        client, account_id, fan_id, tcfg=_tcfg,
                        msgs_since_last=_msgs_since, rung=int(_tstate.get("rung") or 0),
                        last_price_cents=_t_last_price, last_sold=_t_sold,
                        last_was_free=_t_last_free, now=now)
                except Exception:
                    log.debug("ai_chatter convo_teaser pick failed account=%s fan=%s",
                              account_id, fan_id, exc_info=True)
                if teaser is not None and teaser["price_cents"] > 0:
                    # A priced rung obeys the broke/declined brake. `ladders` is only
                    # loaded under the gate (§3152), so on a gate-off account fan_ladder
                    # is None here — read the pause AUTHORITATIVELY (one query, only for a
                    # paid rung) so a broke man is never sent a $10/$50 tease.
                    _lad = (fan_ladder if fan_ladder is not None
                            else (await _load_ladders(account_id, [fan_id])).get(fan_id))
                    _paused = (_lad is not None and _lad.offers_paused_until
                               and _lad.offers_paused_until > now)
                    if _paused or hot_teaser_paid_tick >= _MAX_FORCED_ASKS_PER_TICK:
                        teaser = None      # brake + the per-tick paid cap
            if not dry_run:
                await _bump_attempt(account_id, fan_id, now)
            parts = [_language.apply_word_restriction(p, fan_lang)[:_REPLY_MAX_CHARS]
                     for p in split_for_bubbles(raw, max_bubbles,
                                                rng=random.Random(f"split:{fan_id}:{raw}"))
                     if p.strip()][:max_bubbles]
            parts = [p for p in parts if not _looks_like_echo(p, c.last_body)]
            if style_on and parts:
                recent_out = [b for d, b in c.messages if d == "out"]
                parts = _dedupe_lead_reaction(parts, recent_out)
            # Don't ship his name as a standalone bubble — fold it into a neighbour.
            parts = _merge_lone_name_bubbles(
                parts, (resolve_fan_name(f) or "").split("/")[0] if f else "")
            # §6.4 first strike — brush it off in ONE line. A multi-bubble "no really
            # i'm real" reads as protesting-too-much; a single breezy line does not.
            if bot_accused_first and parts:
                parts = parts[:1]
            if not parts:
                # A sticker-only reply (empty text + a tag) is a legit pure
                # reaction — but never when an offer/teaser needs pitch text to
                # ride on, and never as the brush-off to a bot accusation.
                if (sticker_tag is None or offer_item is not None
                        or teaser is not None or bot_accused_first):
                    errors += 1
                    log.debug("ai_chatter dropped echo-only reply account=%s fan=%s",
                              account_id, fan_id)
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
            if catalog_items and offer_item is None:
                _phantom = teaser is None and pending is None

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
                parts = [apply_nonnative_style(p, protect=name_protect) for p in parts]
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
            # `parts` is empty only on a sticker-only reply — no text bubble to
            # time, so rhythm's decide() (which reads parts[0]) is skipped and
            # the sticker send below uses its own short hold.
            if rhythm_on and not rhythm_resume and parts:
                rnow = datetime.utcnow()
                d = rhythm.decide(rhythm.RhythmCtx(
                    account_id=str(account_id), fan_id=fan_id, text=parts[0],
                    typing_delay_s=typing_delay_seconds(parts[0], typing_wpm),
                    last_inbound_at=c.last_in_at, last_outbound_at=c.last_out_at,
                    # An open/hot ladder suppresses break rolls entirely: a ladder
                    # stranded mid-sell is the worst outcome in the system.
                    # …and so does a HUMAN's unpaid PPV, for _ASK_BREAKPROOF_WINDOW.
                    ladder_open=bool(fan_ladder is not None and fan_ladder.status
                                     in (upsell.STATUS_OPEN, upsell.STATUS_HOT))
                    or _breakproof(human_money.get(fan_id, (None, None))[0], rnow),
                    last_paid_at=_newest(
                        fan_ladder.last_paid_at if fan_ladder is not None else None,
                        human_money.get(fan_id, (None, None))[1]),
                    # HEAT (v3): his reply speed + whether he's escalating drive how fast
                    # she replies — a hot sext gets ~every-minute replies, a cold thread
                    # drifts. §3.4: the rolling last-20 realized latencies feed the fast-nudge.
                    his_last_latency_s=_his_last_latency_s(c),
                    fan_hot=_fan_hot(c),
                    recent_realized_s=_recent_realized_s(rst),
                    sleep_window=sleep_win, tz_offset_minutes=tz_off,
                    no_sleep=rhythm_no_sleep,
                    last_cover_at=(rst.last_cover_at if rst is not None else None),
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
                    await ax.enqueue_job(
                        account_id, _PURPOSE,
                        payload={"only_fan_ids": [int(fan_id)], "rhythm_resume": True,
                                 # The cover line is decided WITH the gap it explains;
                                 # rhythm_state has nowhere to put it, and re-deriving
                                 # it on the wake would be a second roll.
                                 "rhythm_cover": d.cover_line},
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
                # Bubble 0 carries the rhythm delay (which already INCLUDES its wpm
                # typing time); every later bubble keeps its own typing hold, exactly
                # as before. rhythm off ⇒ first_delay is None ⇒ nothing changes.
                delay_s = (first_delay if (idx == 0 and first_delay is not None)
                           else typing_delay_seconds(part, typing_wpm))
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
                    if float(drop) > float(delay_s):
                        run_inline_s += float(drop) - float(delay_s)   # charge the extra
                    delay_s = max(float(delay_s), float(drop))
                    ppv_drops += 1
                await hold_with_typing(account_id, fan_id, delay_s,
                                       typing_indicator=typing_indicator)
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
                    random.Random(f"gif:{account_id}:{fan_id}:{c.last_body}"))
                if gid is not None:
                    srng = random.Random(f"sdelay:{fan_id}:{gid}")
                    await hold_with_typing(account_id, fan_id,
                                           2.0 + 4.0 * srng.random(),
                                           typing_indicator=typing_indicator)
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
                        cat_stickers.mark_sent(account_id, fan_id)
                        sticker_sent = True
                        sent_ok = True
                        stickers_sent += 1
                        log.info("ai_chatter sticker sent account=%s fan=%s "
                                 "tag=%s gif=%s solo=%s", account_id, fan_id,
                                 sticker_tag, gid, not parts)
            if not parts and not sticker_sent:
                errors += 1     # sticker-only reply and the gif never landed
                continue
            await _mark_reply_sent(account_id, fan_id, now)
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
            await _maybe_refresh_profile(account_id, fan_id, c.fan_msg_n, now)
            # Item 22 — profile-less fan below gen_info's staleness gate: force one
            # regen so his notes get built now instead of never.
            await _maybe_bootstrap_profile(account_id, fan_id)
            sent += 1
            # Persist the offer the moment its message is confirmed on the wire.
            # A teaser is its own delivery (advance immediately); a paid offer
            # opens and waits on the unlock watcher. VaultSend rows land at
            # ATTACH time for media that actually went out (teaser/PPV) so the
            # unseen filter can never re-attach the same piece.
            if offer_item is not None and sent_ok:
                if escalation:
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
                        day = (rhythm.local_now(now, tz_off)).strftime("%Y-%m-%d")
                        prior = (int(fan_ladder.daily_ask_cents or 0)
                                 if (fan_ladder is not None
                                     and fan_ladder.daily_day == day) else 0)
                        await _save_ladder(
                            account_id, fan_id, status=upsell.STATUS_OPEN,
                            rung_index=rung_index + 1, last_ask_at=now,
                            session_idle_at=now, daily_day=day,
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
                    is_free=teaser["is_free"], set_rung=teaser.get("next_rung"))
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

    return {
        "mode": mode,
        "candidates": len(candidates),
        "replies_sent": sent,
        "offers_made": offers_made,
        "offers_made_on_escalation": offers_made_on_escalation,
        "offers_forced": offers_forced,
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
        "skipped_sla_fresh": skipped_sla_fresh,
        "skipped_manual": skipped_manual,
        "skipped_locked": skipped_locked,
        "skipped_cooldown": skipped_cooldown,
        "skipped_no_intent": skipped_no_intent,
        "skipped_cadence": skipped_cadence,
        "hard_stops": hard_stops,
        "soft_acks": soft_acks,
        "gate_blocked": gate_blocked,
        "offers_parked": offers_parked,
        "rungs_quoted": rungs_quoted,
        "taps_expired": taps_expired,
        "rhythm_deferred": rhythm_deferred,
        "rhythm_waiting": rhythm_waiting,
        "cover_lines_sent": cover_lines_sent,
        "stickers_sent": stickers_sent,
        "price_errors": price_errors,
        "spend_regret_stops": spend_regret_stops,
        "companion_routed": companion_routed,
        "bot_accusations": bot_accusations,
        "spend_capped": spend_capped,
        "ppv_drops": ppv_drops,
        "errors": errors,
        "cap_hit": cap_hit,
        "dry_run": dry_run,
        "model": model,
    }
