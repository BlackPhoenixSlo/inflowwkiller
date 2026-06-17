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
  • "backup"  (default) — the bot only steps in when an inbound fan message has
    sat unanswered for ≥ sla_minutes (chatters are slow). The W7 webhook wake
    enqueues a fan-scoped job delayed by the SLA; the periodic rule is the
    fallback sweep. At fire time the gate re-checks — if a human answered
    meanwhile, the fan is simply no longer "fan spoke last".
  • "always" — reply when eligible, like of_ai_chat today.

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
import json
import logging
import random
import re
from datetime import datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

import automation_executor as ax  # _make_client / lease / cooldown seams
import llm_client                  # call .chat at runtime so tests can patch it
from attribution import write_outbound_attribution
from automation_registry import register
from db.engine import get_session
from db.models import (
    AccountAiConfig, Blacklist, CatalogItem, CatalogProgress, CatalogScript,
    ContentOffer, Fan, Message, SkipList, Transaction, VaultSend,
)
from llm_client import LLMCapExceeded
from ._common import (
    CONTENT_ASK_RE, NONNATIVE_OUTPUTS, NONNATIVE_REGISTER, ONPLATFORM_GUARDRAIL,
    STYLE_3LINE, STYLE_BRIEF, STYLE_HUMANIZER, STYLE_MAX_BUBBLES,
    apply_nonnative_style, apply_word_restriction, coerce_ids, guard_offplatform,
    hold_with_typing, humanize_typos, load_nonnative_flags, load_style_flags,
    load_typing_indicator, load_typing_wpm, load_typo_flags,
    quarantine_if_undeliverable, resolve_fan_name, resolve_model,
    should_skip_muted_creator, skip_unreachable_fan, typing_delay_seconds,
)
# Deliberate sibling reuse — keeps the texting voice byte-compatible with
# of_ai_chat instead of forking 500 lines of tuned style machinery.
from .of_ai_chat import (
    _BREATHER_VARIANTS, _EXTRACT_HISTORY_TAIL, _HISTORY_TAIL, _MSG_CLIP,
    _NOID_PAUSE, _REPLY_MAX_CHARS, _REPLY_TEMPERATURE, _STYLE_VARIANTS,
    _bump_attempt, _dedupe_lead_reaction, _extract_and_fill, _load_mid_funnel_fans,
    _load_persona, _looks_like_echo, _mark_question_asked, _mark_reply_sent,
    _maybe_push_nickname, _maybe_refresh_profile, _nonempty, _pause_fan,
    _primary_ask_target, _questions_still_needed, _recent_ask_pattern,
    _strip_html, split_for_bubbles,
)

log = logging.getLogger("of-relay.automation.ai_chatter")

_PURPOSE = "ai_chatter"          # model_by_purpose key + automation_kind tag
_REPLY_COOLDOWN_S = 10           # live chat — same short rest as of_ai_chat

# of_ai_chat's graduation skip reasons — they mean "left the gather loop", NOT
# "never message". ai_chatter exists precisely for these fans, so it ignores
# them while respecting every other skip (unreachable, old_fan_pre_ai, manual).
_GRADUATION_SKIPS = frozenset({"spent", "too_long", "info"})

# Built-in defaults — any key the account config omits. DISABLED until a creator
# enables it. The offer_* knobs are read by the M3 offer engine.
_DEFAULTS: dict = {
    "enabled": False,
    "mode": "backup",                    # "backup" | "always"
    "intent_only": False,                # closer mode: only engage a fan whose
                                         # latest message shows buying intent
                                         # (_CONTENT_ASK_RE) or who has an open
                                         # offer. Pure chit-chat is left to the
                                         # team / Auto Convo. Zero LLM cost for
                                         # the fans it skips.
    "sla_minutes": 10,                   # backup: how slow is "slow"
    "max_lifetime_spend_cents": 100_000, # the whale gate ($1000)
    "offer_mode": "both",                # M3: "tip" | "ppv" | "both"
    "max_offers_per_fan_per_day": 2,     # M3
    "min_fan_msgs_between_offers": 4,    # M3
    "max_fans_per_tick": 8,
    "resume_after_manual_hours": 6,      # cautious resume after a human chatted
    "stall_ttl_hours": 48,               # M3: open offer → expired
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


# ── Candidate gathering (own pass — needs timing + human-send metadata) ──────

class _Cand:
    __slots__ = ("fan_id", "fan_msg_n", "last_dir", "last_body", "messages",
                 "last_in_at", "last_human_out_at")

    def __init__(self, fan_id: int):
        self.fan_id = fan_id
        self.fan_msg_n = 0
        self.last_dir = ""
        self.last_body = ""
        self.messages: list[tuple[str, str]] = []  # (direction, body) oldest→newest
        self.last_in_at: datetime | None = None
        self.last_human_out_at: datetime | None = None


async def _gather(account_id: str,
                  fan_ids: set[int] | None = None) -> dict[int, _Cand]:
    """One pass over the account's messages → per-fan history PLUS the two
    timestamps the gates need: when the fan last spoke (SLA age) and when a
    HUMAN last sent (manual outbound = automation_kind IS NULL and not part of
    a mass run — automations always tag automation_kind).

    When `fan_ids` is given (W7 fan-scoped dispatch), the scan is restricted to
    those fans IN SQL so reacting to one inbound DM never reads the whole
    account's message history. None/empty → the full-account sweep."""
    out: dict[int, _Cand] = {}
    where = [Message.account_id == str(account_id), Message.is_unsent.is_(False)]
    if fan_ids:
        where.append(Message.fan_id.in_(fan_ids))
    async with get_session() as s:
        rows = (await s.execute(
            select(Message.fan_id, Message.direction, Message.body,
                   Message.created_at, Message.automation_kind, Message.mass_run_id)
            .where(*where)
            .order_by(Message.fan_id, Message.created_at, Message.message_id)
        )).all()
    for fan_id, direction, body, created_at, automation_kind, mass_run_id in rows:
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
        elif automation_kind is None and mass_run_id is None:
            c.last_human_out_at = created_at
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


def _item_media(item: CatalogItem) -> list[int]:
    try:
        return [int(m) for m in json.loads(item.media_ids or "[]")]
    except Exception:
        return []


def _item_previews(item: CatalogItem) -> list[int]:
    try:
        ids = [int(m) for m in json.loads(item.preview_media_ids or "[]")]
    except Exception:
        return []
    media = set(_item_media(item))
    return [m for m in ids if m in media]


def _effective_mode(item: CatalogItem, cfg_mode: str) -> str | None:
    """Intersect the account's offer_mode with the item's terms. None = not
    sellable (a free teaser is deliverable regardless — see is_free_teaser)."""
    tip_ok = int(item.tip_unlock_cents or 0) > 0 and cfg_mode in ("tip", "both")
    ppv_ok = int(item.price_cents or 0) > 0 and cfg_mode in ("ppv", "both")
    if tip_ok and ppv_ok:
        return "both"
    if tip_ok:
        return "tip"
    if ppv_ok:
        return "ppv"
    return None


def _terms_str(item: CatalogItem, mode: str | None) -> str:
    if item.is_free_teaser:
        return "FREE — just send it when the moment fits"
    tip = f"tip ${int(item.tip_unlock_cents or 0) // 100}"
    ppv = f"${int(item.price_cents or 0) // 100} to unlock"
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
    scripts' openers drop out of the manifest."""
    seen = await _seen_media(account_id, fan_id)
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
        if any(m in seen for m in _item_media(it)):
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
                    scripts: dict[int, CatalogScript], cfg_mode: str) -> str:
    lines = []
    for iid, it in sorted(offerable.items()):
        mode = _effective_mode(it, cfg_mode)
        dur = ""
        if it.duration_sec:
            dur = f" {int(it.duration_sec) // 60}:{int(it.duration_sec) % 60:02d}"
        sc = scripts.get(int(it.script_id)) if it.script_id is not None else None
        theme = f" (part of your '{sc.name}' set: {(sc.theme or '')[:120]})" if sc else ""
        lines.append(f"- [id {iid}] {it.kind}{dur} — {it.label or 'untitled'}: "
                     f"{(it.description_for_ai or '').strip()} — {_terms_str(it, mode)}{theme}")
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


def _parse_offer_marker(raw: str) -> tuple[str, int | None]:
    """Extract the FIRST well-formed >>OFFER id, then strip EVERY marker-ish
    line (malformed ones too — a fan must never see the protocol)."""
    m = _OFFER_MARKER_RE.search(raw or "")
    offer_id = int(m.group(1)) if m else None
    clean = _OFFER_LINE_RE.sub("", raw or "").strip()
    return clean, offer_id


async def _record_offer(account_id: str, fan_id: int, item: CatalogItem,
                        mode: str, offer_message_id: int | None,
                        *, status: str = "open", resolved_by: str | None = None,
                        delivery_message_id: int | None = None) -> None:
    now = datetime.utcnow()
    async with get_session() as s:
        s.add(ContentOffer(
            account_id=str(account_id), fan_id=int(fan_id), item_id=int(item.id),
            script_id=int(item.script_id) if item.script_id is not None else None,
            mode=mode, price_cents=int(item.price_cents or 0),
            tip_unlock_cents=int(item.tip_unlock_cents or 0),
            offer_message_id=int(offer_message_id) if offer_message_id else None,
            status=status, resolved_by=resolved_by,
            delivery_message_id=int(delivery_message_id) if delivery_message_id else None,
            offered_at=now, resolved_at=now if status == "delivered" else None,
            updated_at=now))


async def _record_vault_sends(account_id: str, fan_id: int, media: list[int],
                              message_id: int | None, price_cents: int) -> None:
    now = datetime.utcnow()
    async with get_session() as s:
        for mid in media:
            s.add(VaultSend(account_id=str(account_id), fan_id=int(fan_id),
                            media_id=int(mid),
                            message_id=int(message_id) if message_id else None,
                            price_cents=int(price_cents), sent_at=now))


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
    caption = apply_word_restriction(random.choice(_UNLOCK_REACTIONS))
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
            await _record_vault_sends(account_id, int(offer.fan_id), media,
                                      int(msg_id), 0)
    return int(msg_id) if msg_id else None


async def _resolve_open_offers(account_id: str, client, cfg: dict,
                               *, dry_run: bool,
                               only_fan_ids: set[int] | None = None) -> dict:
    """The unlock watcher — runs every tick BEFORE candidate filtering (a fan
    doesn't have to speak to unlock). Three signals, in cost order: tips since
    offered_at (transactions table, real-time via the tip hook), the ledger
    convergence flipping messages.is_paid (≤10 min), and a targeted OF re-read
    for a recently-active fan (the bought-then-replied fast path). Also expires
    offers past the stall TTL."""
    stats = {"unlocked_tip": 0, "unlocked_ppv": 0, "offers_expired": 0,
             "deliveries_failed": 0, "would_unlock": 0}
    ttl_h = int(cfg.get("stall_ttl_hours") or 0)
    now = datetime.utcnow()
    for offer in await _open_offers(account_id):
        fan_id = int(offer.fan_id)
        if only_fan_ids and fan_id not in only_fan_ids:
            continue
        if ttl_h and offer.offered_at and offer.offered_at < now - timedelta(hours=ttl_h):
            if not dry_run:
                await _resolve_offer(int(offer.id), status="expired", resolved_by=None)
            stats["offers_expired"] += 1
            continue

        paid_by: str | None = None
        if offer.mode in ("ppv", "both") and offer.offer_message_id:
            if await _message_is_paid(account_id, int(offer.offer_message_id)):
                paid_by = "ppv_ledger"
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
            await _advance_progress(account_id, fan_id, item)
            stats["unlocked_tip" if paid_by == "tip" else "unlocked_ppv"] += 1
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


# ── Prompt (forked from of_ai_chat._build_messages — adds the sell seam) ─────

def _build_messages(persona: str, f: Fan, c: _Cand, asked: set[str],
                    history_tail: int = _HISTORY_TAIL,
                    style_on: bool = False,
                    nonnative_on: bool = False,
                    sell_block: str = "",
                    content_ask: bool = False) -> tuple[list[dict], list[str]]:
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
    facts_block = ("\n".join(f"- {x}" for x in facts)
                   if facts else "- (nothing on file yet)")

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

    if content_ask and sell_block.strip():
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

    name_dodged = (not _nonempty(f.real_name) and "name:2" in asked)
    dodge_note = (
        "\n\nIMPORTANT: you asked his name twice and he dodged — do NOT ask his name "
        "again. Pick a playful nickname for him and just use it from now on."
        if name_dodged else "")

    style_extra = ((STYLE_3LINE,) * 2 + (STYLE_BRIEF,) * 2) if style_on else ()
    style = random.choice(_STYLE_VARIANTS + style_extra)

    humanizer = f"\n\n{STYLE_HUMANIZER}" if style_on else ""
    nonnative = f"\n\n{NONNATIVE_REGISTER}" if nonnative_on else ""

    # M3 seam: with no catalog in play the no-offers rule applies (chat-only,
    # current of_ai_chat behavior); with one, a short pointer goes in the intro
    # and the FULL sell block lands as its own section near the end of the
    # prompt (high salience — inlining 20 manifest lines mid-sentence buried it).
    has_sell = bool(sell_block.strip())
    offers_line = ("you DO have real content you can send or sell when the "
                   "moment is right — see WHAT YOU CAN SELL below."
                   if has_sell else "don't offer pics or videos yet.")
    sell_section = f"\n\n{sell_block.strip()}\n\n" if has_sell else "\n\n"

    system = (
        f"{persona}\n"
        "You're texting a fan to get to know him while flirting. You are 100% human "
        "— never sound like a bot or an assistant. Use only what you've learned "
        "about him; don't share your own info unless he asks; "
        f"{offers_line} "
        "He may send several texts in a row — read them all, reply to "
        "the latest.\n\n"
        f"{need_block}{dodge_note}\n\n"
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
        "- GOOD: \"haha thanks 😏 what should i call u?\" / \"aww how old are ya\" / "
        "\"a chef? bet u cook fire, fave dish?\"\n\n"
        f"{ONPLATFORM_GUARDRAIL}"
        f"{humanizer}{nonnative}"
        f"{sell_section}"
        + ("Your reply is ONLY the message text — no JSON, quotes, or metadata. "
           "The ONE exception: the final >>OFFER line when you pitch a piece "
           "(it's stripped before sending — the fan never sees it)."
           if has_sell else
           "Your reply is ONLY the message text — no JSON, quotes, or metadata.")
    )
    user = (
        f"What you know about him:\n{facts_block}\n\n"
        f"Recent conversation (oldest→newest):\n{convo}\n\n"
        "Reply to his last message now, in the STYLE FOR THIS MESSAGE above."
    )
    return ([{"role": "system", "content": system},
             {"role": "user", "content": user}], presented)


# ── The automation ───────────────────────────────────────────────────────────

@register("ai_chatter")
async def run(account_id: str, payload: dict, *, run_id: int) -> dict:
    payload = payload or {}
    dry_run = bool(payload.get("dry_run"))
    force_ids = coerce_ids(payload.get("force_ids"))
    only_fan_ids = coerce_ids(payload.get("only_fan_ids"))
    history_tail = int(payload.get("history_tail") or _HISTORY_TAIL)

    cfg = await _load_config(account_id)
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
    max_bubbles = STYLE_MAX_BUBBLES if style_on else 2
    persona = await _load_persona(account_id)

    blacklist, skip_reasons = await _load_stop_lists(account_id)
    mid_funnel_fans = await _load_mid_funnel_fans(account_id)
    by_fan = await _gather(account_id, only_fan_ids or None)

    client = await asyncio.to_thread(ax._make_client, account_id)

    # ── M3 offer layer: resolve unlocks FIRST (a fan doesn't have to speak to
    # buy), then load the catalog + the open-offer map the prompts read.
    cfg_offer_mode = str(cfg.get("offer_mode") or "both")
    intent_only = bool(cfg.get("intent_only"))
    scripts, catalog_items = await _load_catalog(account_id)
    offer_stats = await _resolve_open_offers(account_id, client, cfg,
                                             dry_run=dry_run,
                                             only_fan_ids=only_fan_ids or None)
    open_by_fan = {int(o.fan_id): o for o in await _open_offers(account_id)}

    async with get_session() as s:
        fan_rows = (await s.execute(
            select(Fan).where(Fan.account_id == str(account_id))
        )).scalars().all()
    fans: dict[int, Fan] = {int(f.fan_id): f for f in fan_rows}

    now = datetime.utcnow()
    candidates: list[_Cand] = []
    skipped_listed = 0      # blacklist / non-graduation skip_list / paused
    skipped_not_turn = 0    # we (or nobody) spoke last
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
        if reason is not None and reason not in _GRADUATION_SKIPS and not forced:
            skipped_listed += 1
            continue
        if fan_id in mid_funnel_fans and not forced:
            skipped_listed += 1
            continue
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
            # (chatters are slow). A fresh message stays human turf for now.
            if mode == "backup" and sla_s:
                if c.last_in_at is None or c.last_in_at > now - timedelta(seconds=sla_s):
                    skipped_sla_fresh += 1
                    continue
        candidates.append(c)

    # Longest-waiting fan first — backup mode is an SLA queue, not a popularity
    # contest (of_ai_chat sorts by volume; here fairness wins).
    candidates.sort(key=lambda x: (x.last_in_at or now, x.fan_id))

    sent = 0
    offers_made = 0
    teasers_sent = 0
    would_offer = 0          # dry-run: offers that would have been recorded
    unbacked_stripped = 0    # price-talk bubbles dropped (no offer behind them)
    skipped_locked = 0
    skipped_cooldown = 0
    skipped_no_intent = 0   # intent_only: fan is just chatting, no buying signal
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
        # is left to the team / Auto Convo.
        if intent_only and open_by_fan.get(fan_id) is None \
                and not _CONTENT_ASK_RE.search(c.last_body or ""):
            skipped_no_intent += 1
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
            pending = open_by_fan.get(fan_id)
            if pending is not None:
                sell_block = _pending_block(pending, await _get_item(int(pending.item_id)))
            elif catalog_items and await _offer_caps_ok(account_id, fan_id, cfg):
                offerable = await _offerable_for_fan(account_id, fan_id,
                                                     cfg_offer_mode, scripts,
                                                     catalog_items)
                if offerable:
                    sell_block = _manifest_block(offerable, scripts, cfg_offer_mode)

            content_ask = bool(offerable) and bool(
                _CONTENT_ASK_RE.search(c.last_body or ""))
            msgs, presented = _build_messages(persona, f, c, asked, history_tail,
                                              style_on=style_on,
                                              nonnative_on=nonnative_on,
                                              sell_block=sell_block,
                                              content_ask=content_ask)
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
            offer_item = offerable.get(offer_id) if offer_id is not None else None
            if offer_id is not None and offer_item is None:
                log.info("ai_chatter offer marker rejected account=%s fan=%s id=%s",
                         account_id, fan_id, offer_id)
            offer_mode_eff = (_effective_mode(offer_item, cfg_offer_mode)
                              if offer_item is not None else None)
            raw, _leak = guard_offplatform(raw, random.Random(f"{fan_id}:{raw}"))
            if _leak:
                offer_item = None  # a guarded reply must not carry a paid attach
                log.info("ai_chatter off-platform leak guarded account=%s fan=%s reasons=%s",
                         account_id, fan_id, _leak)
            if not dry_run:
                await _bump_attempt(account_id, fan_id, now)
            parts = [apply_word_restriction(p)[:_REPLY_MAX_CHARS]
                     for p in split_for_bubbles(raw, max_bubbles,
                                                rng=random.Random(f"split:{fan_id}:{raw}"))
                     if p.strip()][:max_bubbles]
            parts = [p for p in parts if not _looks_like_echo(p, c.last_body)]
            if style_on and parts:
                recent_out = [b for d, b in c.messages if d == "out"]
                parts = _dedupe_lead_reaction(parts, recent_out)
            if not parts:
                errors += 1
                log.debug("ai_chatter dropped echo-only reply account=%s fan=%s",
                          account_id, fan_id)
                continue
            # Anti-hallucination floor: price talk with NO validated offer
            # behind it never reaches a fan on a selling account. Strip those
            # bubbles; if nothing survives, skip the reply entirely (silence
            # beats a promise we can't deliver).
            if catalog_items and offer_item is None:
                priced = [p for p in parts if _unbacked_talk(p)]
                if priced:
                    unbacked_stripped += 1
                    log.warning("ai_chatter unbacked price/specifics talk stripped "
                                "account=%s fan=%s bubbles=%r",
                                account_id, fan_id, priced)
                    parts = [p for p in parts if not _unbacked_talk(p)]
                    if not parts:
                        continue
            # Deterministic terms floor: a tip-unlock offer with no $ amount in
            # the pitch leaves the fan with no way to know the terms (a PPV's
            # locked box shows its price; a tip ask doesn't). Append the ask.
            if (offer_item is not None and not offer_item.is_free_teaser
                    and offer_mode_eff == "tip"
                    and not re.search(r"\$\s*\d", " ".join(parts))):
                parts = parts[:max_bubbles - 1] if len(parts) >= max_bubbles else parts
                parts.append(apply_word_restriction(
                    f"tip ${int(offer_item.tip_unlock_cents or 0) // 100} and its yours 😏"))
            name_protect = [n for n in (f.real_name, f.generated_nickname,
                                        f.of_display_name) if n]
            if nonnative_on:
                parts = [apply_nonnative_style(p, protect=name_protect) for p in parts]
            if typo_on:
                protect = name_protect + (list(NONNATIVE_OUTPUTS) if nonnative_on else [])
                parts = humanize_typos(parts, random.Random(f"{fan_id}:{raw}"),
                                       protect=protect, max_bubbles=max_bubbles)[:max_bubbles]

            if dry_run:
                sent += 1
                if offer_item is not None:
                    would_offer += 1
                continue

            offer_msg_id: int | None = None
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
                        kwargs = {"price": int(offer_item.price_cents or 0) // 100,
                                  "locked_text": False, "media_files": media}
                        previews = _item_previews(offer_item)
                        if previews:
                            kwargs["previews"] = previews
                await hold_with_typing(account_id, fan_id,
                                       typing_delay_seconds(part, typing_wpm),
                                       typing_indicator=typing_indicator)
                try:
                    result = await asyncio.to_thread(
                        lambda p=part, kw=kwargs: client.send_message(fan_id, p, **kw))
                except Exception as e:
                    errors += 1
                    await skip_unreachable_fan(account_id, fan_id, e, log=log)
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
                        automation_kind=_PURPOSE,  # ai_chatter
                        body=str(result.get("text") or part),
                        price_cents=ax._to_cents(kwargs.get("price", 0)),
                        created_at=ax._parse_iso(result.get("createdAt")) or datetime.utcnow(),
                        emit_live=True,
                    )
                    sent_ok = True
                    if offer_item is not None and idx == len(parts) - 1:
                        offer_msg_id = int(msg_id)  # the unlock watcher's anchor
                elif idx == 0:
                    first_no_id = True
                    break
            if send_failed:
                continue
            if first_no_id and not sent_ok:
                errors += 1
                reason = await quarantine_if_undeliverable(client, account_id, fan_id)
                if reason is None:
                    await _pause_fan(account_id, fan_id, now + _NOID_PAUSE)
                    log.warning("ai_chatter send returned no id account=%s fan=%s — paused %s",
                                account_id, fan_id, _NOID_PAUSE)
                continue
            await _mark_reply_sent(account_id, fan_id, now)
            target = _primary_ask_target(presented)
            if target:
                await _mark_question_asked(account_id, fan_id, target, asked)
            await _maybe_refresh_profile(account_id, fan_id, c.fan_msg_n, now)
            sent += 1
            # Persist the offer the moment its message is confirmed on the wire.
            # A teaser is its own delivery (advance immediately); a paid offer
            # opens and waits on the unlock watcher. VaultSend rows land at
            # ATTACH time for media that actually went out (teaser/PPV) so the
            # unseen filter can never re-attach the same piece.
            if offer_item is not None and sent_ok:
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
                        await _record_vault_sends(account_id, fan_id,
                                                  _item_media(offer_item),
                                                  offer_msg_id,
                                                  int(offer_item.price_cents or 0))
                    await _record_offer(account_id, fan_id, offer_item,
                                        offer_mode_eff or "tip", offer_msg_id)
                    offers_made += 1
                log.info("ai_chatter offer account=%s fan=%s item=%s mode=%s msg=%s",
                         account_id, fan_id, offer_item.id,
                         "free" if offer_item.is_free_teaser else offer_mode_eff,
                         offer_msg_id)
            try:
                await asyncio.to_thread(client.mark_chat_read, fan_id)
            except Exception:
                log.warning("ai_chatter mark_chat_read failed account=%s fan=%s",
                            account_id, fan_id, exc_info=True)
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
        "teasers_sent": teasers_sent,
        "would_offer": would_offer,
        "unbacked_stripped": unbacked_stripped,
        **offer_stats,
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
        "errors": errors,
        "cap_hit": cap_hit,
        "dry_run": dry_run,
        "model": model,
    }
