"""make_right — the Resolution Agent (the "make it right" safety net).

When the bot got the content WRONG — most importantly when a fan was **charged
twice for the same content** — this agent catches it FIRST and turns the mistake
into goodwill: a warm, human apology + a few pieces of FREE content the fan does
NOT already own (drawn from the tip-reward tip library), and — where money must be
returned — an operator-review flag (NEVER a silent auto-refund).

This is the BACKSTOP, not the fix. A separate prevention change
([plan/PREVENTION_FIX_NO_DUPLICATE_PPV.md]) stops the double-sale at the source;
this agent exists because prevention will never be perfect and a paying fan who
notices a double-charge before we do is a chargeback and a lost subscriber. It must
stay correct even after prevention lands (it will simply detect fewer incidents).

Design:
  • DETECTION is a small registry (`_DETECTORS` in make_right_detect.py — the
    pure-read scanners live there; this module owns REMEDIATION) — a new mistake
    class is one function + one row, not a rewrite. The headline `dup_charge`
    detector is LANE-AGNOSTIC and media-overlap-keyed — see _detect_dup_charges
    for the algorithm. Media-level overlap is the truth — two same-priced offers
    are NOT enough (guardrail: never apologise to a fan who wasn't actually
    double-charged).
  • REMEDIATION: one apology message + a small bundle of FREE, UNSEEN media from
    the tip_reward tip-library tiers (so the apology is never itself a repeat of
    the mistake), valued ≥ the amount he was wrongly charged (operator-tunable).
    Refunds are flagged for an operator, never moved automatically.
  • FRESHNESS: an incident is only worth an apology while it is still the thing in
    front of him — `max_msgs_since_charge` (default 3) skips any duplicate the thread
    has already moved past. Detection is unbounded (`lookback_days`) so a preview
    still shows the whole backlog; only the SEND is gated.
  • CAP: make-right fires UP TO TWICE per fan (a COUNT of status='resolved' rows),
    then STOPS — the 3rd detected incident lands 'operator_only' (no send). A fan
    who keeps hitting mistakes, or who could milk the free gifts, is a human
    problem, not another auto-gift.
  • IDEMPOTENCY: one resolution per `incident_key`, ever (`resolution_log`).
  • HOUSE DEFAULT (2026-08-05): ON for every model — `enabled` AND `auto_send`
    both default True, so an account that never opened the tab still catches its
    own double-charges. An operator can still untick either switch per account
    (a stored False wins over the default), and `dry_run` in the payload forces
    preview regardless. The two switches gate the dup-charge SCANNER only; the
    riskier `detect_undelivered` class stays opt-in, and a refund is still never
    moved automatically.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta
from random import Random

from sqlalchemy import func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

import automation_executor as ax
from attribution import write_outbound_attribution
from audiences import contact_guard_excludes
from automation_registry import register
import ownership
from db.engine import get_session
from db.models import (
    AccountAiConfig, Blacklist, Fan, Message, ResolutionLog, VaultSend,
)
from automations import (
    _vault_pick, content_resolver, tip_reward, tip_reward_config,
)
from automations.make_right_detect import _detect_all
from . import _voice
from ._common import load_voice_blocks as _load_voice_blocks
from ._common import (
    hold_with_typing, load_hard_skip_ids, load_operator_stop_ids,
    load_typing_indicator, load_typing_wpm, resolve_fan_name,
    should_skip_muted_creator, typing_delay_seconds,
)

log = logging.getLogger("of-relay.automation.make_right")

# Built-in defaults — DISABLED + preview-only so the agent is inert until a creator
# enables it AND opts into auto-send. Every numeric knob is operator-tunable.
_DEFAULTS: dict = {
    "enabled": True,           # master switch (detection still runs for preview)
    "auto_send": True,         # send apologies. OFF → preview-only even on a real run
    "lookback_days": 30,       # scan this far back for duplicate charges
    # FRESHNESS. An apology only lands while the mistake is still the thing in front
    # of him: open ONLY if at most this many messages have passed in the thread since
    # the duplicate charge (operator ruling 2026-08-05 — the first sweep apologised to
    # a fan 403 messages after the fact, which reads as a bot, not as care). The
    # delivery bubble of the purchase itself usually takes one slot. 0 = gate off.
    "max_msgs_since_charge": 3,
    "per_fan_cap": 2,          # "up to twice" then operator_only (anti-milking)
    # ⚠️ GIFT SIZE IS ONE KNOB — `gift_pieces_per_step`, below. `gift_value_match` +
    # `gift_piece_value_cents` solved a piece count from the over-charge and
    # `gift_min_count` + `gift_max_count` clamped it; v2 takes that count directly
    # and has read none of the four since. They outlived their code on the tab long
    # enough for both blake accounts to be set to min/max = 4 and gift 3, with no
    # screen saying which number lost. Deleted 2026-08-06 here, on the tab and in the
    # validator, which strips them from a stored config on its next save. If size
    # should key off the over-charge again it belongs INSIDE `gift_pieces_per_step`,
    # not in a second set of knobs racing it.
    "gift_tier": "",           # a specific tip_reward tier name, or "" → auto-pick by basis
    # 🚨 Make the apology gift be THE THING HE ASKED FOR. Without this the
    # remedy for "you sent me the wrong content" is a random unseen item from a
    # folder — which is the same mistake again, for free. When ON, the gift is
    # resolved from the thread's own promise via `content_resolver` (contract →
    # match → adversarial verify) and only falls back to the folder pull when
    # the resolver refuses. It fails OPEN because the gift is free and a generic
    # gift was never promised to be personal; a PRICED send must not.
    # Default ON since 2026-08-11 (operator: "make all this enabled by default,
    # all on"). It was OFF because this is a live automation on 17 accounts —
    # the reason it is safe to flip is that it changes WHICH free piece is
    # attached, never whether the apology is sent, and it fails open to the
    # folder pull. The cost is one resolver call per make-right; the win is that
    # a man apologised to for the wrong content stops being handed the wrong
    # content a second time.
    "gift_on_subject": True,
    "apology_caption": "",     # operator override for the apology text ('' → built-in)
    "flag_refund": True,       # raise an operator refund-review flag (money is NEVER auto-moved)
    "guard_hours": 12,         # cross-automation contact-guard window (OPEN only)
    # ── The exchange: apology → N free TURNS (each on his reply) → PPV → close ──
    #
    # ⚠️ ONE TURN BY DEFAULT (operator ruling 2026-08-06: "make right down to just
    # one text and another bubble with gift, not more"), superseding the 3 this
    # shipped with. `free_steps: 1` + `open_with_gift` makes `_build_steps` return
    # exactly ["apology_gift"], so `_open_exchange` closes the row `resolved` on the
    # spot: no silence check, no nudge, no PPV pivot, nothing waiting on a reply. An
    # apology that keeps talking is asking him to keep forgiving. The multi-turn
    # machinery is untouched — raising this knob gets the reply-driven exchange back.
    #
    # ⚠️ It counts TURNS, not images, and multiplies with `gift_pieces_per_step`:
    # prod ran 3 × 3, which is 9 free pieces over 3 messages. The old comment here
    # said "free pieces", and that wording is what the settings tab put on the label.
    "free_steps": 1,               # free TURNS before the PPV (1 = just the apology turn)
    "gift_pieces_per_step": 1,     # unseen pieces attached to ONE gift bubble per turn
    "open_with_gift": True,        # the opening turn is apology + free #1 (else apology only)
    "ppv_folder": "",              # tip-library folder for the PPV pivot ('' → no PPV, just close)
    "ppv_price_cents": 1500,       # the PPV ask ($15)
    "ppv_caption": "",             # PPV tease caption ('' → built-in)
    "nudge_hours": 24,             # silence at a step before ONE gentle nudge
    "close_hours": 48,             # silence before closing the exchange to an operator
    # Mistake class #2 — "paid but got nothing" (opt-in; fuzzier than a double-charge).
    "detect_undelivered": False,
    "undelivered_grace_hours": 2,  # a payment must be at least this old with no delivery
    # ── Hard-decline apology (payload-triggered by ai_chatter, NOT a detector) ──
    # Default ON, and INDEPENDENT of `enabled`/`auto_send` above: those two gate
    # the dup-charge SCANNER; this is the decline-policy consequence — the fan is
    # never ghosted over a classifier verdict, he gets a de-escalation apology.
    "on_hard_decline": True,
    # How many EXTRA free turns the objection-triggered apology gets after its
    # opening one. 0 = the single turn: one text bubble, one gift bubble, done.
    #
    # ⚠️ 2026-08-06 SUPERSEDES 2026-08-05. Yesterday's ruling was "i like this more
    # steps each time" and this was 2; today's is "make right down to just one text
    # and another bubble with gift, not more", and that reads on the whole module —
    # a fan who complained about content is in the same place as a fan who was
    # charged twice, and there is no reason the scanner lane says it once and this
    # lane says it three times. Raise it back to 2 to restore the longer recovery;
    # nothing else has to change.
    #
    #
    # More turns was never a drip: `_advance_phase` sends the next piece only after
    # HE writes back, so the exchange is paced by his own engagement and closes
    # itself to an operator if he goes quiet. A price is never appended in this lane
    # — `steps` here cannot contain `ppv`, whatever `ppv_folder` says.
    "apology_free_steps": 0,
}

# Warm, human apology openers (bubble 1), keyed to the MISTAKE so the words match
# what actually went wrong. Seeded per fan+incident via _step_rng, so a dry-run
# preview and the real send show the SAME words.
# One pool per MISTAKE — an apology aimed at the wrong grievance reads as not
# listening (08-04: arguing "that set really is different" turned a $8.28
# complaint into a fight). Never defend the content, never promise a refund,
# NEVER a price in a hard_decline exchange, and content_letdown may not admit
# a billing error — nothing was double-charged, he's just disappointed.
_APOLOGY_FRAMES = {
    "dup_charge": [
        "hey {name} 🙈 i think my messages got crossed and i sent you the same thing twice — that's totally on me, i'm so sorry",
        "omg {name} i just realised i doubled up on you by mistake 🥺 that was my fault, i feel awful",
    ],
    "paid_undelivered": ["hey {name} 🙈 i just saw you paid and i never sent you anything back — that's completely on me, i'm so sorry"],
    "hard_decline": ["{name} i'm sorry, i hear you 🥺 i got carried away with the paid stuff and that's on me. no more of that — i just like talking to you"],
    "content_dispute": ["oh no {name} 🙈 if you'd already seen those then i've charged you for nothing and that's completely on me, i'm sorry"],
    "content_letdown": ["aw {name} i'm sorry that one missed 🙈 that's on me for not reading you better — let me make it up to you"],
}
_DEFAULT_APOLOGY_KIND = "dup_charge"
# Mistake kinds whose apology wording is POLICY, not operator copy — the
# `apology_caption` override is ignored for these. Declared here, beside the
# frames, so each kind's whole contract (its words + who may replace them) is
# read in one place: adding a mistake class shouldn't mean hunting for a
# hardcoded name comparison further down the file.
#   • hard_decline — the override field was written for the dup-charge context
#     ("on me, so sorry babe 💕" + a gift). Letting it reach a fan the classifier
#     just read as chargeback/report language is exactly the wrong turn.
_FIXED_WORDING_KINDS = frozenset({"hard_decline"})
# Free-gift lead-ins (bubble 2 rides the apology), then the priced pivot and the
# quiet-nudge. Follow-up leads say {name}, never a literal "babe" — a pet name
# two bubbles after an apology that used his real name is the tell that nobody
# is really there.
_GIFT_LEADS = ["so here's a little something extra just for you, on me 💕"]
_FREE_LEADS = ["here's another one just because 🥰", "take this one as well {name} 💕"]
_PPV_TEASES = ["ok now that we're good 😌 i made you something special... unlock it? 😏"]
_NUDGE_LINES = ["still here whenever you're ready babe 💕"]

# ── The MALE pools ───────────────────────────────────────────────────────────
# Same keys, same counts. This engine ships DEFAULT OFF, but `ai_chatter` calls
# `_trigger_make_right_apology` on a HARD decline (a fan saying stop selling to
# me), so the day an operator ticks it on a male account these are what a fan
# reads at the worst possible moment.
#
# ⚠️ HE STILL APOLOGISES, AND THAT IS THE POINT. The register inverts everywhere
# else in the male lane; it must not here. A creator who cannot own a double
# charge is not a dom, he is a scammer, and "make it right" is the one surface
# where lowering yourself IS the correct move. What changes is the DELIVERY: he
# names the error, says it is his, says what happens next, and stops. Hers add
# "i feel awful" / "so not okay of me" — the fan is then expected to comfort her,
# which is the mechanic that cannot survive the swap.
#
# ⚠️ NO LINE HERE MAY PROMISE A REFUND. This module NEVER moves money — the
# header is explicit ("an operator-review flag, NEVER a silent auto-refund") and
# `flag_refund` only raises a flag for a human. A first draft of these lines said
# "the refund's going through now"; that is an automated false statement about a
# fan's money, and it survived a rules pass because nothing about the WORDING is
# wrong. Remediation here is free content plus a human looking at it. Hers never
# promise money either — note "let me fix it right now" is deliberately vague.
_APOLOGY_FRAMES_HIM = {
    "dup_charge": [
        "{name} you got charged twice for the same thing. thats my screwup, not yours. im fixing it",
        "{name} i sent you something you already bought. my mistake. im on it now",
    ],
    "paid_undelivered": ["{name} you paid and i never sent it. thats on me. fixing it now"],
    "hard_decline": ["understood {name}. i pushed too hard on the paid stuff. thats done. youre still welcome here"],
    "content_dispute": ["{name} you paid for something you already had. thats my error. it doesnt happen again"],
    "content_letdown": ["{name} that one missed. my call, not your fault. tell me what you actually want"],
}
# No {name} below (call sites still .replace it) — these ride right behind an
# apology bubble that already opened with his name, and a second address in two
# consecutive bubbles reads as a script. He never argues the value — a dom who
# tells a fan it is "worth the money" has already conceded the price.
_GIFT_LEADS_HIM = ["sending you something while i fix this. on me"]
_FREE_LEADS_HIM = ["heres another one. free", "that one too"]
_PPV_TEASES_HIM = ["were square. made something new since. your call 😏"]
_NUDGE_LINES_HIM = ["still here when you want to pick this back up"]

# Keyed by NAME, not by `id(pool)`. The first cut keyed this dict on the identity
# of the module-level lists, which meant any rebinding, copy or slice silently
# resolved to the FEMALE pool — the exact silent-wrong-gender failure this lane
# exists to prevent, with no error anywhere to notice it by. A name also fails
# loudly on a typo (KeyError) instead of quietly serving her words from him.
_POOLS: dict[str, tuple[list[str], list[str]]] = {
    "gift": (_GIFT_LEADS, _GIFT_LEADS_HIM),
    "free": (_FREE_LEADS, _FREE_LEADS_HIM),
    "ppv_tease": (_PPV_TEASES, _PPV_TEASES_HIM),
    "nudge": (_NUDGE_LINES, _NUDGE_LINES_HIM),
}


def _lane(name: str, voice: str) -> list[str]:
    """The pool called `name`, in this lane."""
    hers, his = _POOLS[name]
    return his if str(voice or "").strip().lower() == "him" else hers


def _apology_frames(voice: str) -> dict[str, list[str]]:
    return (_APOLOGY_FRAMES_HIM
            if str(voice or "").strip().lower() == "him" else _APOLOGY_FRAMES)


async def _load_config(account_id: str) -> dict:
    """account_ai_config.make_right_config_json shallow-merged over _DEFAULTS.
    Absent/NULL/parse-error → defaults (disabled + preview-only)."""
    async with get_session() as s:
        cfg = await s.get(AccountAiConfig, str(account_id))
    raw = getattr(cfg, "make_right_config_json", None) if cfg else None
    merged = dict(_DEFAULTS)
    if raw:
        try:
            stored = json.loads(raw) or {}
            merged.update({k: v for k, v in stored.items() if v is not None})
        except Exception:
            log.warning("bad make_right_config account=%s", account_id, exc_info=True)
    return merged


async def is_enabled(account_id: str) -> bool:
    """Cheap gate for a dispatcher: master switch only."""
    cfg = await _load_config(account_id)
    return bool(cfg.get("enabled"))


# ─────────────────────────────────────────────────────────────────────────────
# REMEDIATION
# ─────────────────────────────────────────────────────────────────────────────


def _build_steps(cfg: dict) -> list[str]:
    """The exchange as an ordered list of outbound TURNS. Default:
    apology(+free #1) → free → free → PPV. `open_with_gift` folds free #1 into the
    apology turn; `ppv_folder` empty (or price 0) drops the PPV pivot (just close)."""
    free_steps = max(0, int(cfg.get("free_steps") or 0))
    open_with_gift = bool(cfg.get("open_with_gift"))
    has_ppv = bool(str(cfg.get("ppv_folder") or "").strip()) and int(cfg.get("ppv_price_cents") or 0) > 0
    if open_with_gift and free_steps > 0:
        steps = ["apology_gift"]
        remaining = free_steps - 1
    else:
        steps = ["apology"]
        remaining = free_steps
    steps += ["free"] * remaining
    if has_ppv:
        steps.append("ppv")
    return steps


def _free_folders(cfg: dict, tip_cfg: dict, wrongful_cents: int) -> list[str]:
    """The tip-library folders the free pieces come from: `gift_tier` if pinned,
    else auto-pick the tier by the wrongful basis."""
    tier_name = str(cfg.get("gift_tier") or "").strip().lower()
    if tier_name:
        return tip_reward_config.tier_folders(tip_cfg, tier_name)
    tier = tip_reward._pick_tier(max(0, int(wrongful_cents or 0)), tip_cfg)
    return [f for f in (tier.get("folders") if tier else []) if str(f).strip()]


async def _exclude_media(account_id: str, fan_id: int) -> set[int]:
    """Media the fan already OWNS or has SEEN — the gift must avoid all of it, so
    the apology is never itself a repeat of the mistake. Union of tip_reward's
    seen-set (every VaultSend) with ownership.py's owned-set (purchased
    VaultSend + paid-message media + non-free delivered offers)."""
    seen = await ownership.seen_media(account_id, fan_id)
    owned = await ownership.owned_or_seen_media(account_id, fan_id)
    return seen | owned


async def _pull_unseen(client, account_id: str, fan_id: int, folders: list[str],
                       count: int) -> list[int]:
    """Up to `count` FRESH (unseen + unowned) media ids from `folders`. Empty when
    no folders / nothing fresh left / no client. The freshness gate re-reads
    VaultSend each call, so pieces already sent earlier in the exchange are excluded."""
    folders = [f for f in (folders or []) if str(f).strip()]
    if not folders or count <= 0 or client is None:
        return []
    by_name = await asyncio.to_thread(_vault_pick.resolve_folders, client)
    exclude = await _exclude_media(account_id, fan_id)
    return await asyncio.to_thread(_vault_pick.gather_unseen, client, folders,
                                   by_name, exclude, count)


async def _pull_gift(client, account_id: str, fan_id: int, folders: list[str],
                     count: int, cfg: dict) -> tuple[list[int], str]:
    """The apology gift, preferring THE THING HE ASKED FOR.

    Returns `(media_ids, source)` where source is "subject" | "folder" | "none",
    so the caller can put it in the run stats — whether the remedy was actually
    on-subject is the only interesting thing about it.

    ⚠️ Fails OPEN to the folder pull on any refusal. That is correct HERE and
    nowhere else: the gift is free and nobody promised it would be personal, so
    a generic piece is a smaller failure than an empty apology. A priced send
    faces the opposite trade and must refuse.
    """
    if cfg.get("gift_on_subject") and count > 0:
        try:
            res = await content_resolver.resolve(
                account_id, fan_id, count=count,
                seen=await _exclude_media(account_id, fan_id),
                # A free gift may draw from the described vault, not only the
                # curated shelves — but VERIFY still has to clear it, because
                # this fan is already unhappy about being sent the wrong thing.
                require_curated=False, verify=True)
            if res.ok:
                log.info("make_right gift ON-SUBJECT account=%s fan=%s %s",
                         account_id, fan_id, content_resolver.explain(res))
                return res.media_ids[:count], "subject"
            log.info("make_right gift subject-resolve refused account=%s fan=%s %s",
                     account_id, fan_id, content_resolver.explain(res))
        except Exception:  # noqa: BLE001
            log.warning("make_right gift subject-resolve failed account=%s fan=%s",
                        account_id, fan_id, exc_info=True)
    gift = await _pull_unseen(client, account_id, fan_id, folders, count)
    return gift, ("folder" if gift else "none")


def _fan_first_name(fan: Fan | None, voice: str = "her") -> str:
    """A real given name if we have one, else the LANE's address — never a raw
    location/tag. The fallback is not rare (of_display_name is empty for ~80% of
    fans), so on a male account this was "babe" on the majority of apologies."""
    raw = resolve_fan_name(fan) or ""
    name = raw.split("/")[0].strip() if raw else ""
    if name and name.isalpha() and 1 < len(name) <= 20:
        return name
    return _voice.blocks(voice).fan_address


def _step_rng(account_id: str, incident_key: str, step: int) -> Random:
    """THE seed for every apology/gift draw — preview/send parity rests on the
    open (step 0) and each follow-up step drawing from a byte-identical stream,
    so the frame a preview shows for an incident is the frame the real open
    sends. Single home; never inline the f-string."""
    return Random(f"make_right:{account_id}:{incident_key}:{step}")


def _apology_bubbles(fan: Fan | None, cfg: dict, rng: Random,
                     kind: str = _DEFAULT_APOLOGY_KIND,
                     voice: str = "her") -> list[str]:
    """The apology turn — one warm bubble, worded to match the MISTAKE `kind`. An
    operator `apology_caption` overrides it, except for the kinds whose wording is
    policy (_FIXED_WORDING_KINDS). `{name}` → the fan's given/pet name."""
    name = _fan_first_name(fan, voice)
    override = str(cfg.get("apology_caption") or "").strip()
    _frames = _apology_frames(voice)
    frames = _frames.get(kind) or _frames[_DEFAULT_APOLOGY_KIND]
    frame = (override if (override and kind not in _FIXED_WORDING_KINDS)
             else rng.choice(frames))
    return [frame.replace("{name}", name)]


async def _resolved_count(account_id: str, fan_id: int) -> int:
    """How many make-rights this fan has already received (the per-fan cap COUNT)."""
    async with get_session() as s:
        n = (await s.execute(
            select(func.count()).select_from(ResolutionLog).where(
                ResolutionLog.account_id == str(account_id),
                ResolutionLog.fan_id == int(fan_id),
                ResolutionLog.status == "resolved"))).scalar_one()
    return int(n or 0)


async def _incident_seen(account_id: str, incident_key: str) -> bool:
    """Idempotency — has this exact incident already been logged/handled?"""
    async with get_session() as s:
        row = (await s.execute(
            select(ResolutionLog.id).where(
                ResolutionLog.account_id == str(account_id),
                ResolutionLog.incident_key == incident_key))).first()
    return row is not None


async def _log_incident(account_id: str, fan_id: int, incident: dict, *,
                        status: str, remediation: dict, now: datetime) -> None:
    """Idempotent ledger write (UNIQUE on account_id+incident_key). Used to OPEN a
    resolution (status='in_progress') or record a terminal state (operator_only)."""
    async with get_session() as s:
        await s.execute(
            sqlite_insert(ResolutionLog)
            .values(account_id=str(account_id), fan_id=int(fan_id),
                    incident_key=incident["incident_key"], kind=incident["kind"],
                    detected_at=now, status=status,
                    remediation_json=json.dumps(remediation),
                    resolved_at=now if status == "resolved" else None)
            .on_conflict_do_nothing(index_elements=["account_id", "incident_key"])
        )


async def _update_resolution(account_id: str, incident_key: str, *, status: str,
                             remediation: dict, now: datetime) -> None:
    """Advance an existing in-progress resolution (UPDATE the row in place)."""
    async with get_session() as s:
        await s.execute(update(ResolutionLog).where(
            ResolutionLog.account_id == str(account_id),
            ResolutionLog.incident_key == incident_key).values(
            status=status, remediation_json=json.dumps(remediation),
            resolved_at=now if status == "resolved" else None))


async def _open_resolution_rows(account_id: str, only_fan_ids: set[int] | None = None) -> list:
    """The account's IN-PROGRESS resolutions (optionally scoped to specific fans)."""
    async with get_session() as s:
        q = select(ResolutionLog).where(
            ResolutionLog.account_id == str(account_id),
            ResolutionLog.status == "in_progress")
        if only_fan_ids:
            q = q.where(ResolutionLog.fan_id.in_([int(x) for x in only_fan_ids]))
        rows = (await s.execute(q)).scalars().all()
        s.expunge_all()
    return list(rows)


async def _fan_has_open_resolution(account_id: str, fan_id: int) -> bool:
    """One exchange per fan at a time — block opening a 2nd while one is live."""
    async with get_session() as s:
        r = (await s.execute(select(ResolutionLog.id).where(
            ResolutionLog.account_id == str(account_id),
            ResolutionLog.fan_id == int(fan_id),
            ResolutionLog.status == "in_progress"))).first()
    return r is not None


async def _fan_replied_since(account_id: str, fan_id: int, since_iso) -> bool:
    """True if the fan sent an inbound DM after `since_iso` — the step-advance gate."""
    since = _parse_dt(since_iso)
    async with get_session() as s:
        q = select(Message.message_id).where(
            Message.account_id == str(account_id), Message.fan_id == int(fan_id),
            Message.direction == "in")
        if since is not None:
            q = q.where(Message.created_at > since)
        r = (await s.execute(q.limit(1))).first()
    return r is not None


def _parse_rem(row) -> dict:
    try:
        return json.loads(row.remediation_json or "{}") or {}
    except Exception:
        return {}


def _parse_dt(iso):
    if not iso:
        return None
    try:
        return datetime.fromisoformat(str(iso))
    except Exception:
        return None


async def _get_fan(account_id: str, fan_id: int) -> Fan | None:
    async with get_session() as s:
        f = await s.get(Fan, (str(account_id), int(fan_id)))
        if f is not None:
            s.expunge(f)
    return f


async def _enqueue_silence_check(account_id: str, fan_id: int, cfg: dict,
                                 now: datetime) -> None:
    """Schedule a self-check after nudge_hours so a silent fan is nudged/closed even
    without a periodic sweep. Best-effort; mirrors ai_chatter's aftercare enqueue."""
    try:
        await ax.enqueue_job(
            account_id, "make_right", payload={"only_fan_ids": [int(fan_id)]},
            run_at=now + timedelta(hours=max(1, int(cfg.get("nudge_hours") or 24))))
    except Exception:
        log.debug("make_right silence-check enqueue failed account=%s fan=%s",
                  account_id, fan_id, exc_info=True)


async def _msgs_since_charge(account_id: str, fan_id: int,
                             charged_at: datetime | None) -> int | None:
    """How many messages the thread has moved on since the duplicate charge.

    This is the freshness measure: an apology for a charge 400 messages back reads
    as a machine noticing late, not as care. Counts BOTH directions (his replies and
    hers) because "the third message after the purchase" is a position in the thread,
    not a count of his typing — but excludes make_right's OWN sends so a re-open or a
    retry can't inflate its own staleness.

    Returns None when the charge time is unknown; the caller treats that as "cannot
    prove it's fresh" and skips, because the whole point is to not apologise blind.
    """
    if charged_at is None:
        return None
    async with get_session() as s:
        return int((await s.execute(
            select(func.count()).select_from(Message).where(
                Message.account_id == str(account_id),
                Message.fan_id == int(fan_id),
                Message.created_at > charged_at,
                func.coalesce(Message.automation_kind, "") != "make_right")
        )).scalar_one() or 0)


async def _build_exclusions(account_id: str, guard_hours: int, now: datetime,
                            fan_ids: set[int], *,
                            include_contact_guard: bool = True) -> set[int]:
    """The standard proactive-touch gauntlet (mirrors reengage_buyers): contact
    guard, hard skips, GLOBAL blacklist, paused, bots, muted creators. ADVANCE runs
    with `include_contact_guard=False` — we're mid-conversation, so a recent send to
    him must NOT block his own reply from being answered."""
    excl: set[int] = set()
    if include_contact_guard:
        excl |= await contact_guard_excludes(
            account_id, outbound_hours=guard_hours, inbound_hours=guard_hours)
    excl |= await load_hard_skip_ids(account_id)
    async with get_session() as s:
        excl |= set((await s.execute(select(Blacklist.fan_id))).scalars().all())
        excl |= {int(x) for x in (await s.execute(
            select(Fan.fan_id).where(
                Fan.account_id == str(account_id),
                Fan.automation_paused_until.is_not(None),
                Fan.automation_paused_until > now))).scalars().all()}
        if fan_ids:
            muted = (await s.execute(
                select(Fan).where(Fan.account_id == str(account_id),
                                  Fan.fan_id.in_([int(x) for x in fan_ids])))).scalars().all()
            excl |= {int(f.fan_id) for f in muted if should_skip_muted_creator(f)}
            excl |= {int(f.fan_id) for f in muted if bool(getattr(f, "is_bot", False))}
    return {int(x) for x in excl}


async def _send_bubbles(client, account_id: str, fan_id: int, bubbles: list[dict],
                        wpm, indicator, now: datetime) -> tuple[bool, int | None]:
    """Send an outbound TURN — a list of bubbles, each {text, media, price, previews}
    — typed one at a time. Writes attribution per bubble AND a VaultSend per media
    item (so a free piece never re-sends and a priced piece isn't re-offered).
    Returns (any_ok, last_media_message_id)."""
    any_ok = False
    last_media_msg: int | None = None
    for b in bubbles:
        text = b.get("text") or ""
        media = list(b.get("media") or [])
        price_cents = int(b.get("price_cents") or 0)
        previews = list(b.get("previews") or [])
        await hold_with_typing(account_id, fan_id,
                               typing_delay_seconds(text or "x", wpm),
                               typing_indicator=indicator)
        # OF's `price` is in DOLLARS (ppv_send.py:422); we keep cents everywhere else.
        kwargs: dict = {"media_files": media, "price": round(price_cents / 100, 2)}
        if previews:
            kwargs["previews"] = previews
        try:
            result = await asyncio.to_thread(
                lambda t=text, k=kwargs: client.send_message(fan_id, t, **k))
        except Exception:
            log.warning("make_right send failed account=%s fan=%s", account_id,
                        fan_id, exc_info=True)
            continue
        mid = result.get("id") if isinstance(result, dict) else None
        if not mid:
            continue
        any_ok = True
        await write_outbound_attribution(
            account_id=account_id, fan_id=int(fan_id), message_id=int(mid),
            sent_by_employee_id=None, automation_kind="make_right",
            body=str(result.get("text") or text), price_cents=price_cents,
            created_at=ax._parse_iso(result.get("createdAt")) or now, emit_live=True)
        if media:
            last_media_msg = int(mid)
            async with get_session() as s:
                for gm in media:
                    s.add(VaultSend(account_id=str(account_id), fan_id=int(fan_id),
                                    media_id=int(gm), message_id=int(mid),
                                    price_cents=price_cents, sent_at=now))
    return any_ok, last_media_msg


async def _do_step(client, account_id: str, fan_id: int, action: str, cfg: dict,
                   tip_cfg: dict, rng: Random, fan: Fan | None, wpm, indicator,
                   now: datetime, kind: str = _DEFAULT_APOLOGY_KIND) -> tuple[bool, list[int]]:
    """Send ONE step of the exchange. Returns (ok, media_ids_sent). Actions:
      apology / apology_gift → the apology (+ the first free piece)
      free                   → a warm line + `gift_pieces_per_step` fresh free pieces
      ppv                    → a tease + priced media from the PPV folder (the pivot)
      nudge                  → one gentle line (no media) when he's gone quiet
    A step with no fresh media still sends its line so the exchange never stalls; a
    missing PPV folder/price makes the ppv step a graceful no-op (the exchange closes)."""
    _v = (await _load_voice_blocks(account_id)).voice
    name = _fan_first_name(fan, _v)
    per_step = max(1, int(cfg.get("gift_pieces_per_step") or 1))

    if action in ("apology", "apology_gift"):
        bubbles = [{"text": b, "media": [], "price_cents": 0}
                   for b in _apology_bubbles(fan, cfg, rng, kind, _v)]
        media_sent: list[int] = []
        if action == "apology_gift":
            gift, _src = await _pull_gift(client, account_id, fan_id,
                                          _free_folders(cfg, tip_cfg, 0), per_step, cfg)
            if gift:
                bubbles.append({"text": rng.choice(_lane("gift", _v)).replace("{name}", name),
                                "media": gift, "price_cents": 0})
                media_sent = gift
        ok, _ = await _send_bubbles(client, account_id, fan_id, bubbles, wpm, indicator, now)
        return ok, media_sent

    if action == "free":
        gift, _src = await _pull_gift(client, account_id, fan_id,
                                      _free_folders(cfg, tip_cfg, 0), per_step, cfg)
        if not gift:
            # Nothing fresh left to give — no tip library, or he has seen it all.
            # Advance WITHOUT sending: every line in this pool promises a gift
            # ("and this one too, on me 😘"), and delivering that with nothing
            # attached is a second empty promise to a fan who is already unhappy.
            # Silence here just makes the exchange one turn shorter, which is the
            # honest outcome when there is nothing to hand him. (The apology turn
            # already works this way — it appends its gift bubble only `if gift`.)
            return True, []
        lead = rng.choice(_lane("free", _v)).replace("{name}", name)
        ok, _ = await _send_bubbles(client, account_id, fan_id,
                                    [{"text": lead, "media": gift, "price_cents": 0}],
                                    wpm, indicator, now)
        return ok, gift

    if action == "ppv":
        folder = str(cfg.get("ppv_folder") or "").strip()
        price_cents = int(cfg.get("ppv_price_cents") or 0)
        media = await _pull_unseen(client, account_id, fan_id, [folder], per_step) if folder else []
        if not media or price_cents <= 0:
            return True, []  # nothing to pitch → close gracefully
        tease = (str(cfg.get("ppv_caption") or "").strip()
                 or rng.choice(_lane("ppv_tease", _v))).replace("{name}", name)
        ok, _ = await _send_bubbles(
            client, account_id, fan_id,
            [{"text": tease, "media": media, "price_cents": price_cents, "previews": media[:1]}],
            wpm, indicator, now)
        if not ok:
            # The PPV is the OPTIONAL closing pivot — the make-right core (apology +
            # free) already landed. A failed pivot must NOT trap the fan in a retry
            # loop, so we still CLOSE the exchange (return ok) rather than re-sending
            # a failing PPV on every reply.
            log.warning("make_right ppv pivot failed — closing anyway account=%s fan=%s",
                        account_id, fan_id)
            return True, []
        return True, media

    if action == "nudge":
        line = rng.choice(_lane("nudge", _v)).replace("{name}", name)
        ok, _ = await _send_bubbles(client, account_id, fan_id,
                                    [{"text": line, "media": [], "price_cents": 0}],
                                    wpm, indicator, now)
        return ok, []

    return False, []


async def _open_exchange(client, account_id: str, fan_id: int, incident: dict,
                         steps: list[str], cfg: dict, tip_cfg: dict, fan,
                         wpm, indicator, now: datetime, *, rem_extra: dict) -> str:
    """Open ONE exchange: acquire the fan lease, send step 0, write the ledger
    row. Returns 'opened' | 'lease_busy' | 'send_failed'; exceptions propagate
    (the lease is still released) so each caller keeps its own error accounting.

    This is the ONE writer of the persisted `rem` contract that _advance_phase
    later parses back out of resolution_log.remediation_json (steps / step /
    last_step_at / nudged / kind / gift_history). The scanner lane and the
    hard-decline lane differ in gates and steps — never in how a first turn is
    sent and recorded. `incident` must carry kind / incident_key /
    wrongful_cents; `rem_extra` is the lane's own ledger fields
    (refund_flagged+message_ids+overlap_media, or trigger_message_id).

    The rng is seeded HERE via _step_rng(account_id, incident_key, 0) — the
    seeding contract's single home — so the frame a preview shows for this
    incident is the frame the real open sends. A one-turn `steps`
    closes resolved immediately; otherwise the row opens in_progress and the
    silence self-check is scheduled."""
    if not await ax.acquire_fan_lease(account_id, fan_id, "make_right"):
        return "lease_busy"
    try:
        rng = _step_rng(account_id, incident["incident_key"], 0)
        ok, media = await _do_step(client, account_id, fan_id, steps[0], cfg,
                                   tip_cfg, rng, fan, wpm, indicator, now,
                                   kind=incident["kind"])
        if not ok:
            return "send_failed"
        rem = {"steps": steps, "step": 1, "incident_key": incident["incident_key"],
               "kind": incident["kind"], "wrongful_cents": incident["wrongful_cents"],
               "gift_history": ([{"step": 0, "media": media}] if media else []),
               "last_step_at": now.isoformat(), "nudged": False, **rem_extra}
        done = rem["step"] >= len(steps)
        await _log_incident(account_id, fan_id, incident,
                            status="resolved" if done else "in_progress",
                            remediation=rem, now=now)
        if not done:
            await _enqueue_silence_check(account_id, fan_id, cfg, now)
        return "opened"
    finally:
        await ax.release_fan_lease(account_id, fan_id)


async def _advance_phase(account_id: str, cfg: dict, tip_cfg: dict, client, wpm,
                         indicator, now: datetime, only_fan_ids: set[int] | None,
                         stats: dict, *, apology_only: bool = False) -> None:
    """Continue every IN-PROGRESS exchange whose fan replied since the last step —
    or, if he's gone quiet, nudge once then close to an operator. Reply-driven; the
    webhook enqueues this fan-scoped right after his DM, the periodic sweep catches
    the rest."""
    rows = await _open_resolution_rows(account_id, only_fan_ids)
    if not rows:
        return
    nudge_h = int(cfg.get("nudge_hours") or 24)
    close_h = int(cfg.get("close_hours") or 48)
    fan_ids = {int(r.fan_id) for r in rows}
    # Hard exclusions ONLY — NOT the contact guard (we're mid-conversation with him).
    hard_excl = await _build_exclusions(account_id, 0, now, fan_ids,
                                        include_contact_guard=False)
    for r in rows:
        fid = int(r.fan_id)
        if fid in hard_excl:
            continue
        rem = _parse_rem(r)
        # `apology_only` = the dup-charge SCANNER is switched off, so only the
        # objection lane's own exchanges may continue. `trigger_message_id` is the
        # marker: `_run_payload_apology` is the only writer of it. Without this the
        # operator's scanner off-switch would silently resume scanner exchanges.
        if apology_only and rem.get("trigger_message_id") is None:
            continue
        steps = rem.get("steps") or _build_steps(cfg)
        step = int(rem.get("step") or 0)
        last_iso = rem.get("last_step_at")
        nudged = bool(rem.get("nudged"))
        if step >= len(steps):  # defensive — should already be resolved
            await _update_resolution(account_id, r.incident_key, status="resolved",
                                     remediation=rem, now=now)
            continue
        last_dt = _parse_dt(last_iso)
        silent = (now - last_dt) if last_dt else None
        replied = await _fan_replied_since(account_id, fid, last_iso)
        fan = await _get_fan(account_id, fid)
        rng = _step_rng(account_id, r.incident_key, step)

        if replied:
            if not await ax.acquire_fan_lease(account_id, fid, "make_right"):
                continue
            try:
                ok, media = await _do_step(client, account_id, fid, steps[step], cfg,
                                           tip_cfg, rng, fan, wpm, indicator, now,
                                           kind=rem.get("kind") or _DEFAULT_APOLOGY_KIND)
                if ok:
                    rem["step"] = step + 1
                    rem["last_step_at"] = now.isoformat()
                    rem["nudged"] = False
                    if media:
                        rem.setdefault("gift_history", []).append({"step": step, "media": media})
                    done = rem["step"] >= len(steps)
                    await _update_resolution(account_id, r.incident_key,
                                             status="resolved" if done else "in_progress",
                                             remediation=rem, now=now)
                    stats["advanced"] += 1
                    if done:
                        stats["resolved"] += 1
                        log.info("make_right exchange complete account=%s fan=%s key=%s",
                                 account_id, fid, r.incident_key)
                    else:
                        await _enqueue_silence_check(account_id, fid, cfg, now)
                else:
                    stats["errors"] += 1
            except Exception:
                stats["errors"] += 1
                log.warning("make_right advance failed account=%s fan=%s", account_id,
                            fid, exc_info=True)
            finally:
                await ax.release_fan_lease(account_id, fid)
            continue

        if silent is not None and silent > timedelta(hours=close_h):
            rem["reason"] = "went_silent"
            await _update_resolution(account_id, r.incident_key, status="operator_only",
                                     remediation=rem, now=now)
            stats["closed_silent"] += 1
        elif silent is not None and silent > timedelta(hours=nudge_h) and not nudged:
            if not await ax.acquire_fan_lease(account_id, fid, "make_right"):
                continue
            try:
                ok, _ = await _do_step(client, account_id, fid, "nudge", cfg, tip_cfg,
                                       rng, fan, wpm, indicator, now)
                if ok:
                    rem["nudged"] = True
                    await _update_resolution(account_id, r.incident_key,
                                             status="in_progress", remediation=rem, now=now)
                    stats["nudged"] += 1
                    await _enqueue_silence_check(account_id, fid, cfg, now)
            except Exception:
                stats["errors"] += 1
            finally:
                await ax.release_fan_lease(account_id, fid)


async def _run_payload_apology(account_id: str, cfg: dict, hd: dict, *,
                            dry_run: bool, now: datetime) -> dict:
    """The payload-triggered apology lane. Originally (07-23) just the decline
    consequence: a fan the classifier read as chargeback/report/unsubscribe gets ONE
    de-escalation apology turn (+ the usual free piece), never permanent silence.

    It now carries every `_objection` verdict, including `content_letdown`, which is
    NOT a hard decline at all — the payload key stays `hard_decline` because it is a
    stable wire name between the two modules, but the `reason` inside it is the
    truth. Whatever the sub-class, the shape here is identical: one turn, one
    apology, no price. Payload-triggered by ai_chatter's
    hard-decline handler — there is no detector; the classifier verdict IS the
    incident. One turn, closes immediately: no nudges, no PPV pivot (a priced
    tease at a man who just said "scam" would be the mistake, not the fix).

    The payload may name a `reason` (08-04) — the decline SUB-CLASS, so the
    apology answers the sentence he actually wrote. Today that is
    `content_dispute`; anything unrecognised degrades to the generic
    de-escalation rather than guessing, and the reason is what lands in the
    ledger's `kind` column for the operator reading it back.

    Runs INDEPENDENT of `enabled`/`auto_send` (those gate the dup-charge
    scanner); its own gate is `on_hard_decline` (default ON). Idempotent per
    triggering message, honours the per-fan resolved cap (anti-milking: "say
    'unsubscribe', farm a freebie" stops at the cap), and is blocked ONLY by an
    operator stop (hand-restrict / OF-restrict / muted / blacklist / bot) — the
    contact guard and the routine pause column must not eat the apology."""
    fan_id = int(hd["fan_id"])
    trigger_mid = hd.get("message_id")
    # Keyed by the TRIGGER, never by the sub-class: two reads of the same inbound
    # (a webhook replay, or a sweep re-classifying it after the regexes change)
    # must collide even if they disagree about which apology it deserves. A
    # `content_dispute:` prefix would let exactly that pair double-apologise.
    incident_key = (f"hard_decline:{int(trigger_mid)}" if trigger_mid
                    else "hard_decline:{}:{}".format(fan_id, now.strftime("%Y%m%d%H")))
    # An unknown reason must not silently reach _apology_bubbles' `.get(kind) or
    # dup_charge` fallback — a fan who disputed a charge would be told "you got
    # charged twice", which is a different (and false) admission.
    kind = str(hd.get("reason") or "").strip() or "hard_decline"
    if kind not in _APOLOGY_FRAMES or kind not in _APOLOGY_FRAMES_HIM:
        log.warning("make_right unknown hard-decline reason %r account=%s fan=%s "
                    "— using the generic de-escalation", kind, account_id, fan_id)
        kind = "hard_decline"
    base = {"status": "ok", "mode": "hard_decline", "fan_id": fan_id,
            "kind": kind, "incident_key": incident_key}
    if not cfg.get("on_hard_decline", True):
        return {**base, "action": "disabled"}
    if await _incident_seen(account_id, incident_key):
        return {**base, "action": "already_handled"}

    # Only what _log_incident / _open_exchange actually read (kind, incident_key,
    # wrongful_cents) — the trigger message rides in rem_extra into the ledger.
    incident = {"kind": kind, "fan_id": fan_id,
                "incident_key": incident_key, "wrongful_cents": 0}
    cap = max(1, int(cfg.get("per_fan_cap") or 2))
    if await _resolved_count(account_id, fan_id) >= cap:
        if not dry_run:
            await _log_incident(account_id, fan_id, incident, status="operator_only",
                                remediation={"reason": "per_fan_cap"}, now=now)
        return {**base, "action": "operator_only", "reason": "per_fan_cap"}

    fan = await _get_fan(account_id, fan_id)
    async with get_session() as s:
        blacklisted = (await s.execute(select(Blacklist.fan_id).where(
            Blacklist.fan_id == int(fan_id)))).first() is not None
    if (fan_id in await load_operator_stop_ids(account_id) or blacklisted
            or should_skip_muted_creator(fan) or bool(getattr(fan, "is_bot", False))):
        return {**base, "action": "excluded"}

    tip_cfg = await tip_reward_config._load_config(account_id)
    # The recovery runs longer than one turn (`apology_free_steps`), and every turn
    # past the first is REPLY-DRIVEN: `_advance_phase` only moves when he writes
    # back, nudges once at `nudge_hours`, and closes to an operator at
    # `close_hours`. So a fan who says nothing gets exactly the opening turn.
    #
    # ⚠️ A HARD DECLINE STAYS ONE TURN, and that is not a config oversight. Those
    # are the chargeback / report / unsubscribe words — the fan is not merely
    # disappointed, he is threatening the account. More messages to him is how a
    # complaint becomes a platform case; the de-escalation is to own it once and
    # stop talking. Every other verdict (`content_letdown`, `content_dispute`,
    # `paid_undelivered`) is an unhappy fan who is still engaged, and those are
    # exactly the ones a longer recovery saves.
    #
    # `ppv` is deliberately NOT reachable from here whatever `ppv_folder` holds: a
    # priced tease inside an apology is the mistake, not the fix.
    extra = (0 if kind == "hard_decline"
             else max(0, int(cfg.get("apology_free_steps") or 0)))
    steps = ["apology_gift" if cfg.get("open_with_gift") else "apology"]
    steps += ["free"] * extra
    if dry_run:
        rng = _step_rng(account_id, incident_key, 0)
        return {**base, "dry_run": True, "action": "would_open", "steps": steps,
                "apology": _apology_bubbles(
                    fan, cfg, rng, kind,
                    (await _load_voice_blocks(account_id)).voice)[0]}

    client = await asyncio.to_thread(ax._make_client, account_id)
    wpm = await load_typing_wpm(account_id)
    indicator = await load_typing_indicator(account_id)
    # _open_exchange propagates exceptions (lease still released) so each caller
    # keeps its own error accounting — a bare escape here would fail the job with
    # no resolution_log row, and the next sweep would re-apologize to the fan.
    try:
        outcome = await _open_exchange(client, account_id, fan_id, incident, steps,
                                       cfg, tip_cfg, fan, wpm, indicator, now,
                                       rem_extra={"trigger_message_id": trigger_mid})
    except Exception:
        log.warning("make_right hard-decline open failed account=%s fan=%s",
                    account_id, fan_id, exc_info=True)
        return {**base, "status": "error", "action": "send_failed"}
    if outcome == "opened":
        log.info("make_right apology opened account=%s fan=%s key=%s steps=%d",
                 account_id, fan_id, incident_key, len(steps))
        # `resolved` must mirror the ledger row. It was hardcoded True, which was
        # accurate while this lane was always a single turn and became a lie the
        # moment it could open a multi-turn exchange.
        return {**base, "action": "opened", "resolved": len(steps) <= 1}
    if outcome == "lease_busy":
        return {**base, "action": "lease_busy"}
    return {**base, "status": "error", "action": "send_failed"}


@register("make_right")
async def run(account_id: str, payload: dict, *, run_id: int) -> dict:
    cfg = await _load_config(account_id)
    payload = payload or {}
    dry_run = bool(payload.get("dry_run"))
    now = datetime.utcnow()

    # Payload-triggered hard-decline apology — its own lane, its own gates.
    if payload.get("hard_decline"):
        return await _run_payload_apology(account_id, cfg, payload["hard_decline"],
                                       dry_run=dry_run, now=now)

    # SENDING requires BOTH the master switch AND the auto-send opt-in. Detection
    # still runs so a preview always works — but a real run with either flag off is
    # PREVIEW-ONLY (nothing sends). This is the ships-OFF / dry-run-first guarantee.
    preview_only_reason = None
    if not dry_run:
        if not cfg.get("enabled"):
            preview_only_reason = "disabled"
        elif not cfg.get("auto_send"):
            preview_only_reason = "auto_send_off"
    send_mode = (not dry_run) and (preview_only_reason is None)

    only_raw = payload.get("only_fan_ids")
    only_fan_ids = {int(x) for x in only_raw} if only_raw else None

    tip_cfg = await tip_reward_config._load_config(account_id)
    # The objection lane opens MULTI-TURN exchanges (`apology_free_steps`) and is
    # independent of the scanner's two switches — so its exchanges must be able to
    # CONTINUE where the scanner is off, which is most accounts. Without this an
    # apology could open and never advance: the fan gets turn one and the row sits
    # in_progress until `close_hours` retires it to an operator, which reads to him
    # as being dropped mid-recovery.
    apology_advance = (not dry_run) and bool(cfg.get("on_hard_decline", True))
    client = (await asyncio.to_thread(ax._make_client, account_id)
              if (send_mode or dry_run or apology_advance) else None)
    wpm = await load_typing_wpm(account_id)
    indicator = await load_typing_indicator(account_id)

    stats = {"opened": 0, "advanced": 0, "resolved": 0, "nudged": 0,
             "closed_silent": 0, "operator_only": 0, "errors": 0,
             "skipped_excluded": 0, "already_handled": 0, "stale": 0}
    preview: list[dict] = []

    # ── ADVANCE phase (real runs only) — continue open exchanges on his replies ──
    if send_mode or apology_advance:
        await _advance_phase(account_id, cfg, tip_cfg, client, wpm, indicator, now,
                             only_fan_ids, stats,
                             apology_only=not send_mode)

    # ── OPEN phase — detect new double-charges and start an exchange ──
    incidents = await _detect_all(account_id, cfg, now, only_fan_ids)
    stats["candidates"] = len(incidents)

    if incidents:
        cap = max(1, int(cfg.get("per_fan_cap") or 2))
        # 0 is a valid "contact-guard off" — don't let `or 12` clobber it.
        gh = cfg.get("guard_hours", 12)
        guard_hours = int(gh) if gh is not None else 12
        # 0 is a valid "freshness gate off" — same trap, same shape.
        mm = cfg.get("max_msgs_since_charge", 3)
        max_msgs = int(mm) if mm is not None else 3
        fan_ids = {int(i["fan_id"]) for i in incidents}
        excl = await _build_exclusions(account_id, guard_hours, now, fan_ids)
        resolved_counts = {fid: await _resolved_count(account_id, fid) for fid in fan_ids}
        async with get_session() as s:
            fans = {int(f.fan_id): f for f in (await s.execute(select(Fan).where(
                Fan.account_id == str(account_id),
                Fan.fan_id.in_([int(x) for x in fan_ids])))).scalars().all()}
            s.expunge_all()
        opened_fids: set[int] = set()  # at most one new exchange per fan per run

        for inc in incidents:
            fid = int(inc["fan_id"])
            fan = fans.get(fid)
            name = resolve_fan_name(fan) or ""
            base = {"fan_id": fid, "name": name, "incident_key": inc["incident_key"],
                    "kind": inc["kind"], "wrongful_cents": inc["wrongful_cents"]}

            if await _incident_seen(account_id, inc["incident_key"]):
                stats["already_handled"] += 1
                preview.append({**base, "action": "already_handled"})
                continue
            # FRESHNESS — before the cap, so a stale incident reads as "stale" and
            # never burns a per-fan slot or an operator_only row it doesn't deserve.
            if max_msgs > 0:
                since = await _msgs_since_charge(account_id, fid, inc.get("last_charge_at"))
                if since is None or since > max_msgs:
                    stats["stale"] += 1
                    preview.append({**base, "action": "stale",
                                    "msgs_since_charge": since,
                                    "reason": ("charge time unknown" if since is None
                                               else f"{since} messages since the charge")})
                    continue
            if resolved_counts.get(fid, 0) >= cap:
                stats["operator_only"] += 1
                if send_mode:
                    await _log_incident(account_id, fid, inc, status="operator_only",
                                        remediation={"reason": "per_fan_cap",
                                                     "wrongful_cents": inc["wrongful_cents"],
                                                     "message_ids": inc["message_ids"]}, now=now)
                preview.append({**base, "action": "operator_only", "reason": "per_fan_cap"})
                continue
            if fid in excl:
                stats["skipped_excluded"] += 1
                preview.append({**base, "action": "excluded"})
                continue
            if fid in opened_fids or await _fan_has_open_resolution(account_id, fid):
                preview.append({**base, "action": "in_progress"})
                continue

            steps = _build_steps(cfg)
            refund_flag = bool(cfg.get("flag_refund")) and int(inc["wrongful_cents"] or 0) > 0

            if not send_mode:
                # Same rng seed as _open_exchange, so the preview shows the exact
                # frame a real open would send. Both draws live ONLY here — a real
                # open must not burn a vault lookup (gift_preview) it never uses.
                rng = _step_rng(account_id, inc["incident_key"], 0)
                apology = _apology_bubbles(
                    fan, cfg, rng, inc["kind"],
                    (await _load_voice_blocks(account_id)).voice)[0]
                gift_preview = await _pull_unseen(
                    client, account_id, fid,
                    _free_folders(cfg, tip_cfg, inc["wrongful_cents"]),
                    max(1, int(cfg.get("gift_pieces_per_step") or 1))) if client else []
                preview.append({**base, "apology": apology, "steps": steps,
                                "gift_media_ids": gift_preview, "refund_flagged": refund_flag,
                                "action": "would_open"})
                continue

            # OPEN for real: send step 0 (apology / apology+free #1), write the ledger.
            try:
                outcome = await _open_exchange(
                    client, account_id, fid, inc, steps, cfg, tip_cfg, fan,
                    wpm, indicator, now,
                    rem_extra={"refund_flagged": refund_flag,
                               "message_ids": inc["message_ids"],
                               "overlap_media": inc["overlap_media"]})
                if outcome == "opened":
                    opened_fids.add(fid)
                    stats["opened"] += 1
                    if len(steps) <= 1:
                        stats["resolved"] += 1
                    log.info("make_right opened account=%s fan=%s key=%s steps=%s refund=%s",
                             account_id, fid, inc["incident_key"], steps, refund_flag)
                elif outcome == "send_failed":
                    stats["errors"] += 1
            except Exception:
                stats["errors"] += 1
                log.warning("make_right open failed account=%s fan=%s key=%s",
                            account_id, fid, inc["incident_key"], exc_info=True)

    if not send_mode:
        # Surface active exchanges too, so the operator sees the whole picture.
        for r in await _open_resolution_rows(account_id, only_fan_ids):
            rem = _parse_rem(r)
            preview.append({"fan_id": int(r.fan_id), "incident_key": r.incident_key,
                            "kind": r.kind, "action": "in_progress",
                            "step": int(rem.get("step") or 0),
                            "steps": rem.get("steps") or []})
        # The apology lane ADVANCES on this path too (it is independent of the
        # scanner's switches), so its counters ride along — a run that really sent
        # a recovery turn must not report itself as a pure preview.
        return {"dry_run": True, "preview_only_reason": preview_only_reason,
                "advanced": stats["advanced"], "resolved": stats["resolved"],
                "nudged": stats["nudged"], "closed_silent": stats["closed_silent"],
                "candidates": stats["candidates"],
                "would_open": sum(1 for p in preview if p["action"] == "would_open"),
                "operator_only": sum(1 for p in preview if p["action"] == "operator_only"),
                "excluded": stats["skipped_excluded"],
                "already_handled": stats["already_handled"],
                "stale": stats["stale"],
                "in_progress": sum(1 for p in preview if p["action"] == "in_progress"),
                "preview": preview[:25]}
    return {"status": "ok", **stats}
