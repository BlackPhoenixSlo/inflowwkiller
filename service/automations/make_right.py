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
  • DETECTION is a small registry (`_DETECTORS`) — a new mistake class is one
    function + one row, not a rewrite. The headline detector `dup_charge` is
    LANE-AGNOSTIC: it keys on the OF *message id* of each paid charge (from
    `transactions`, a `delivered` non-free `content_offers`, or a paid
    `ladder_quote`), resolves each charge's media (item media / vault_sends /
    message_media), and flags any two DISTINCT charges whose media OVERLAP.
    Media-level overlap is the truth — two same-priced offers are NOT enough
    (guardrail: never apologise to a fan who wasn't actually double-charged).
  • REMEDIATION: one apology message + a small bundle of FREE, UNSEEN media from
    the tip_reward tip-library tiers (so the apology is never itself a repeat of
    the mistake), valued ≥ the amount he was wrongly charged (operator-tunable).
    Refunds are flagged for an operator, never moved automatically.
  • CAP: make-right fires UP TO TWICE per fan (a COUNT of status='resolved' rows),
    then STOPS — the 3rd detected incident lands 'operator_only' (no send). A fan
    who keeps hitting mistakes, or who could milk the free gifts, is a human
    problem, not another auto-gift.
  • IDEMPOTENCY: one resolution per `incident_key`, ever (`resolution_log`).
  • Ships DEFAULT OFF + PREVIEW-ONLY: detection always runs (so an operator can
    eyeball the dry-run), but NOTHING sends until both `enabled` AND `auto_send`
    are ticked on. `dry_run` in the payload forces preview regardless.
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
from db.engine import get_session
from db.models import (
    AccountAiConfig, Blacklist, CatalogItem, ContentOffer, Fan, LadderQuote,
    Message, MessageMedia, ResolutionLog, Transaction, VaultSend,
)
from automations import tip_reward
from ._common import (
    hold_with_typing, load_hard_skip_ids, load_typing_indicator, load_typing_wpm,
    resolve_fan_name, should_skip_muted_creator, typing_delay_seconds,
)

log = logging.getLogger("of-relay.automation.make_right")

# Money-truth charge kinds (mirror transactions.py / the attribution view). A tip
# only becomes a "duplicate" if it resolves to media that OVERLAPS another charge,
# so including tips here is safe (a bare thank-you tip resolves to no media).
_CHARGE_TX_KINDS = ("ppv_message", "ppv_post", "tip")
# A delivered offer counts as a paid charge only when the money was real — NEVER a
# free teaser (resolved_by='free'), or every reused-media item becomes a false
# "double sale". Mirrors the prevention fix's non-free predicate.
_PAID_RESOLVED_BY = ("tip", "ppv_ledger", "ppv_txn", "ppv_fastpath", "manual")

# Built-in defaults — DISABLED + preview-only so the agent is inert until a creator
# enables it AND opts into auto-send. Every numeric knob is operator-tunable.
_DEFAULTS: dict = {
    "enabled": False,          # master switch (detection still runs for preview)
    "auto_send": False,        # opt-in: send apologies. OFF → preview-only even on a real run
    "lookback_days": 30,       # scan this far back for duplicate charges
    "per_fan_cap": 2,          # "up to twice" then operator_only (anti-milking)
    # Gift sizing — value it at ≥ the wrongful charge (default: match it). Pieces
    # come from the tip_reward tip-library tiers (the same giftable pool).
    "gift_value_match": True,
    "gift_piece_value_cents": 600,   # nominal $ per free piece for the match math
    "gift_min_count": 2,
    "gift_max_count": 4,
    "gift_tier": "",           # a specific tip_reward tier name, or "" → auto-pick by basis
    "apology_caption": "",     # operator override for the apology text ('' → built-in)
    "flag_refund": True,       # raise an operator refund-review flag (money is NEVER auto-moved)
    "guard_hours": 12,         # cross-automation contact-guard window (OPEN only)
    # ── Multi-turn exchange (v2): apology → N free (each on his reply) → PPV → close ──
    "free_steps": 3,               # free pieces given across his replies before the PPV
    "gift_pieces_per_step": 1,     # unseen pieces per free step
    "open_with_gift": True,        # the opening turn is apology + free #1 (else apology only)
    "ppv_folder": "",              # tip-library folder for the PPV pivot ('' → no PPV, just close)
    "ppv_price_cents": 1500,       # the PPV ask ($15)
    "ppv_caption": "",             # PPV tease caption ('' → built-in)
    "nudge_hours": 24,             # silence at a step before ONE gentle nudge
    "close_hours": 48,             # silence before closing the exchange to an operator
    # Mistake class #2 — "paid but got nothing" (opt-in; fuzzier than a double-charge).
    "detect_undelivered": False,
    "undelivered_grace_hours": 2,  # a payment must be at least this old with no delivery
}

# Warm, human apology openers (bubble 1), keyed to the MISTAKE so the words match
# what actually went wrong. Seeded per fan+incident so a dry-run preview and the real
# send show the SAME words.
_APOLOGY_FRAMES = {
    "dup_charge": [
        "hey {name} 🙈 i think my messages got crossed and i sent you the same thing twice — that's totally on me, i'm so sorry",
        "omg {name} i just realised i doubled up on you by mistake 🥺 that was my fault, i feel awful",
        "{name} i owe you an apology — looks like i sent you something you'd already gotten. my bad completely 💕",
    ],
    "paid_undelivered": [
        "hey {name} 🙈 i just saw you paid and i never sent you anything back — that's completely on me, i'm so sorry",
        "omg {name} you supported me and got NOTHING in return?? 🥺 that's my fault, let me fix it right now",
        "{name} i owe you an apology — you paid and i left you hanging with nothing. so not okay of me 💕",
    ],
}
_DEFAULT_APOLOGY_KIND = "dup_charge"
# Bubble 2 lead-in for the FIRST free gift (rides the apology).
_GIFT_LEADS = [
    "so here's a little something extra just for you, on me 💕",
    "let me make it up to you — these are free, just because 😘",
    "consider these a thank-you for your patience 🥰",
]
# Lead-in for the follow-up free pieces (sent as he keeps replying).
_FREE_LEADS = [
    "here's another one just because 🥰",
    "and this one too, on me 😘",
    "take this one as well babe 💕",
    "one more little treat for you 🙈",
]
# The soft pivot back to selling — a priced tease after we've made things right.
_PPV_TEASES = [
    "ok now that we're good 😌 i made you something special... unlock it? 😏",
    "since you've been so sweet about it — here's a lil something HOT, just for you 🔥",
    "feeling generous now 😈 want to see what i've really got? 💋",
]
# One gentle nudge if he goes quiet mid-exchange.
_NUDGE_LINES = [
    "still here whenever you're ready babe 💕",
    "no rush — just wanted you to know i'm thinking of you 🥰",
    "hope you're doing ok 🙈 message me back when you can",
]


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
# DETECTION registry — a new mistake class is one function + one row here.
# Each detector: async (account_id, cfg, now) -> list[incident dict].
# An incident dict: {kind, fan_id, incident_key, message_ids, item_ids,
#                    overlap_media, wrongful_cents, evidence}.
# ─────────────────────────────────────────────────────────────────────────────


def _item_media(media_ids_json: str | None) -> set[int]:
    """Decode a catalog_items.media_ids JSON array → set of OF media ids."""
    try:
        return {int(m) for m in json.loads(media_ids_json or "[]")}
    except Exception:
        return set()


async def _catalog_media_map(account_id: str, item_ids: set[int]) -> dict[int, set[int]]:
    """{catalog_item_id -> set(media_id)} for the given items (media_ids JSON)."""
    if not item_ids:
        return {}
    async with get_session() as s:
        rows = (await s.execute(
            select(CatalogItem.id, CatalogItem.media_ids).where(
                CatalogItem.account_id == str(account_id),
                CatalogItem.id.in_([int(i) for i in item_ids])))).all()
    return {int(i): _item_media(m) for i, m in rows}


async def _detect_dup_charges(account_id: str, cfg: dict, now: datetime,
                              only_fan_ids: set[int] | None = None) -> list[dict]:
    """Headline detector: charged twice for the same content, LANE-AGNOSTIC.

    A "charge" is keyed on the OF message id it rode on. We gather paid charges
    from three sources (they reference the SAME message id for one purchase, so a
    message-id dedup collapses them), resolve each charge's media, and flag any two
    DISTINCT charges of the same fan whose media OVERLAP.

    `only_fan_ids` scopes the scan to specific fans (a targeted/preview run, and the
    jaka<->Ava live-test guard) — mirrors _resolve_open_offers(only_fan_ids=...).
    """
    since = now - timedelta(days=max(1, int(cfg.get("lookback_days") or 30)))
    _fan_in = [int(x) for x in only_fan_ids] if only_fan_ids else None

    # (fan_id, message_id) -> charge accumulator {item_ids:set, amount:int|None, sources:set}
    charges: dict[tuple[int, int], dict] = {}

    def _add(fan_id, message_id, *, item_id=None, amount=None, source):
        if fan_id is None or message_id is None:
            return  # a charge with no message id can't be deduped/resolved by media
        key = (int(fan_id), int(message_id))
        c = charges.setdefault(key, {"item_ids": set(), "amount": None, "sources": set()})
        if item_id is not None:
            c["item_ids"].add(int(item_id))
        if amount is not None and (c["amount"] is None or int(amount) > c["amount"]):
            c["amount"] = int(amount)
        c["sources"].add(source)

    async with get_session() as s:
        # (a) transactions — the money-truth, present regardless of which automation
        # sent it (the lane-agnostic backstop for funnel/mass/teaser buys).
        q_tx = select(Transaction.fan_id, Transaction.message_id, Transaction.amount_cents).where(
            Transaction.account_id == str(account_id),
            Transaction.kind.in_(_CHARGE_TX_KINDS),
            Transaction.status.in_(("cleared", "pending")),
            Transaction.fan_id.is_not(None),
            Transaction.message_id.is_not(None),
            Transaction.occurred_at >= since)
        if _fan_in:
            q_tx = q_tx.where(Transaction.fan_id.in_(_fan_in))
        for fid, mid, amt in (await s.execute(q_tx)).all():
            _add(fid, mid, amount=amt, source="txn")

        # (b) delivered, NON-FREE content_offers — carry the item (→ media) and the
        # PPV message id. This is what caught the Daniel incident.
        q_off = select(ContentOffer.fan_id, ContentOffer.offer_message_id,
                       ContentOffer.item_id, ContentOffer.price_cents).where(
            ContentOffer.account_id == str(account_id),
            ContentOffer.status == "delivered",
            ContentOffer.resolved_by.in_(_PAID_RESOLVED_BY),
            ContentOffer.offer_message_id.is_not(None),
            ContentOffer.resolved_at >= since)
        if _fan_in:
            q_off = q_off.where(ContentOffer.fan_id.in_(_fan_in))
        for fid, msg, item, price in (await s.execute(q_off)).all():
            _add(fid, msg, item_id=item, amount=price, source="offer")

        # (c) paid ladder quotes — same PPV message id, carry the item.
        q_lq = select(LadderQuote.fan_id, LadderQuote.message_id,
                      LadderQuote.item_id, LadderQuote.price_cents).where(
            LadderQuote.account_id == str(account_id),
            LadderQuote.paid.is_(True),
            LadderQuote.message_id.is_not(None),
            LadderQuote.sent_at >= since)
        if _fan_in:
            q_lq = q_lq.where(LadderQuote.fan_id.in_(_fan_in))
        for fid, msg, item, price in (await s.execute(q_lq)).all():
            _add(fid, msg, item_id=item, amount=price, source="quote")

    if not charges:
        return []

    # Which fans have ≥2 distinct charge messages — the only ones that CAN dup.
    by_fan: dict[int, list[int]] = {}
    for (fid, mid) in charges:
        by_fan.setdefault(fid, []).append(mid)
    suspects = {fid: msgs for fid, msgs in by_fan.items() if len(msgs) >= 2}
    if not suspects:
        return []

    suspect_msgs = {mid for fid, msgs in suspects.items() for mid in msgs}

    # Resolve media per message. Prefer item media (Path C/D); fall back to the
    # message's own vault_sends / message_media rows (Path B/A) for lanes that
    # wrote no catalog item.
    all_item_ids: set[int] = set()
    for c in charges.values():
        all_item_ids |= c["item_ids"]
    cat_media = await _catalog_media_map(account_id, all_item_ids)

    vs_media: dict[int, set[int]] = {}
    mm_media: dict[int, set[int]] = {}
    async with get_session() as s:
        for msg, mid in (await s.execute(
                select(VaultSend.message_id, VaultSend.media_id).where(
                    VaultSend.account_id == str(account_id),
                    VaultSend.message_id.in_(list(suspect_msgs))))).all():
            if msg is not None:
                vs_media.setdefault(int(msg), set()).add(int(mid))
        for msg, mid in (await s.execute(
                select(MessageMedia.message_id, MessageMedia.media_id).where(
                    MessageMedia.account_id == str(account_id),
                    MessageMedia.message_id.in_(list(suspect_msgs))))).all():
            if msg is not None:
                mm_media.setdefault(int(msg), set()).add(int(mid))

    def _media_for(fid: int, mid: int) -> set[int]:
        c = charges[(fid, mid)]
        out: set[int] = set()
        for it in c["item_ids"]:
            out |= cat_media.get(int(it), set())
        out |= vs_media.get(int(mid), set())
        out |= mm_media.get(int(mid), set())
        return out

    incidents: list[dict] = []
    for fid, msgs in suspects.items():
        msgs = sorted(set(msgs))
        media = {mid: _media_for(fid, mid) for mid in msgs}
        # Union-find: connect two charge messages that share any media.
        parent = {m: m for m in msgs}

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for i in range(len(msgs)):
            for j in range(i + 1, len(msgs)):
                a, b = msgs[i], msgs[j]
                if media[a] and media[b] and (media[a] & media[b]):
                    parent[find(a)] = find(b)

        comps: dict[int, list[int]] = {}
        for m in msgs:
            comps.setdefault(find(m), []).append(m)

        for comp_msgs in comps.values():
            if len(comp_msgs) < 2:
                continue  # not a duplicate — a single charge
            comp_msgs = sorted(comp_msgs)
            overlap: set[int] = set()
            for i in range(len(comp_msgs)):
                for j in range(i + 1, len(comp_msgs)):
                    overlap |= (media[comp_msgs[i]] & media[comp_msgs[j]])
            item_ids: set[int] = set()
            amounts: list[int] = []
            for m in comp_msgs:
                item_ids |= charges[(fid, m)]["item_ids"]
                amt = charges[(fid, m)]["amount"]
                if amt:
                    amounts.append(int(amt))
            # He should have paid ONCE — the wrongful amount is everything past the
            # single largest legitimate charge.
            wrongful = sum(sorted(amounts, reverse=True)[1:]) if len(amounts) >= 2 else \
                (amounts[0] if amounts else 0)
            key_item = str(min(item_ids)) if item_ids else ("m" + str(min(overlap)))
            incident_key = "dupsale:{}:{}".format(
                key_item, "-".join(str(m) for m in comp_msgs))
            incidents.append({
                "kind": "dup_charge",
                "fan_id": int(fid),
                "incident_key": incident_key,
                "message_ids": comp_msgs,
                "item_ids": sorted(item_ids),
                "overlap_media": sorted(overlap),
                "wrongful_cents": int(wrongful),
                "evidence": {"charge_count": len(comp_msgs),
                             "amounts_cents": amounts},
            })
    return incidents


# Charges that should have delivered CONTENT (or, for a sub, access the fan expects
# more of). A cleared one with nothing sent after is "paid but got nothing".
_UNDELIVERED_TX_KINDS = ("ppv_message", "ppv_post", "tip", "subscribe", "subscription")


async def _detect_paid_undelivered(account_id: str, cfg: dict, now: datetime,
                                   only_fan_ids: set[int] | None = None) -> list[dict]:
    """Mistake class #2: a fan PAID but got NOTHING. A cleared/pending charge with NO
    content delivered to him AFTER it (no VaultSend, no delivered offer) once a grace
    window has passed — "i paid $6 and never got anything more." Chargeback bait.

    Off by default (`detect_undelivered`) — it's fuzzier than a double-charge; an
    operator opts in. Scoped by `only_fan_ids` like the other detectors.
    """
    lookback_days = max(1, int(cfg.get("lookback_days") or 30))
    grace_h = max(0, int(cfg.get("undelivered_grace_hours") or 2))
    since = now - timedelta(days=lookback_days)
    grace_cutoff = now - timedelta(hours=grace_h)   # paid at least grace_h ago
    _fan_in = [int(x) for x in only_fan_ids] if only_fan_ids else None

    async with get_session() as s:
        q = select(Transaction.id, Transaction.fan_id, Transaction.message_id,
                   Transaction.amount_cents, Transaction.occurred_at, Transaction.kind).where(
            Transaction.account_id == str(account_id),
            Transaction.kind.in_(_UNDELIVERED_TX_KINDS),
            Transaction.status.in_(("cleared", "pending")),
            Transaction.fan_id.is_not(None),
            Transaction.occurred_at >= since,
            Transaction.occurred_at <= grace_cutoff)
        if _fan_in:
            q = q.where(Transaction.fan_id.in_(_fan_in))
        txns = (await s.execute(q)).all()
        if not txns:
            return []
        fans = list({int(t.fan_id) for t in txns})
        # Latest delivery per fan = max(any VaultSend, any DELIVERED offer).
        vs = (await s.execute(select(VaultSend.fan_id, func.max(VaultSend.sent_at)).where(
            VaultSend.account_id == str(account_id),
            VaultSend.fan_id.in_(fans)).group_by(VaultSend.fan_id))).all()
        co = (await s.execute(select(ContentOffer.fan_id, func.max(ContentOffer.resolved_at)).where(
            ContentOffer.account_id == str(account_id),
            ContentOffer.fan_id.in_(fans),
            ContentOffer.status == "delivered").group_by(ContentOffer.fan_id))).all()

    last_deliv: dict[int, datetime] = {}
    for fid, t in list(vs) + list(co):
        if t is not None:
            prev = last_deliv.get(int(fid))
            last_deliv[int(fid)] = t if (prev is None or t > prev) else prev

    incidents: list[dict] = []
    for t in txns:
        fid = int(t.fan_id)
        occ = t.occurred_at
        ld = last_deliv.get(fid)
        if ld is not None and occ is not None and ld > occ:
            continue  # he got SOMETHING after paying → delivered, not an incident
        key_msg = t.message_id if t.message_id is not None else ("tx" + str(t.id))
        incidents.append({
            "kind": "paid_undelivered",
            "fan_id": fid,
            "incident_key": f"undelivered:{key_msg}",
            "message_ids": [int(t.message_id)] if t.message_id else [],
            "item_ids": [], "overlap_media": [],
            "wrongful_cents": int(t.amount_cents or 0),
            "evidence": {"amount_cents": int(t.amount_cents or 0), "tx_kind": t.kind,
                         "occurred_at": occ.isoformat() if occ else None},
        })
    return incidents


# The registry: (name, detector_fn, gate_config_key). A gate of None = always on.
# Add a mistake class = add one row. dup_charge always runs; the fuzzier classes
# ship OFF and an operator opts in per-account.
_DETECTORS = [
    ("dup_charge", _detect_dup_charges, None),
    ("paid_undelivered", _detect_paid_undelivered, "detect_undelivered"),
    # ("wrong_item", _detect_wrong_item, "detect_wrong_item"),          # slot ready
    # ("price_mismatch", _detect_price_mismatch, "detect_price_mismatch"),
]


async def _detect_all(account_id: str, cfg: dict, now: datetime,
                      only_fan_ids: set[int] | None = None) -> list[dict]:
    out: list[dict] = []
    for name, fn, gate in _DETECTORS:
        if gate and not cfg.get(gate):
            continue
        try:
            out.extend(await fn(account_id, cfg, now, only_fan_ids))
        except Exception:
            log.warning("make_right detector %s errored account=%s", name,
                        account_id, exc_info=True)
    # Most-hurt first (biggest wrongful charge), stable by key.
    out.sort(key=lambda inc: (-int(inc.get("wrongful_cents") or 0), inc["incident_key"]))
    return out


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
        return tip_reward._tier_folders(tip_cfg, tier_name)
    tier = tip_reward._pick_tier(max(0, int(wrongful_cents or 0)), tip_cfg)
    return [f for f in (tier.get("folders") if tier else []) if str(f).strip()]


async def _exclude_media(account_id: str, fan_id: int) -> set[int]:
    """Media the fan already OWNS or has SEEN — the gift must avoid all of it, so
    the apology is never itself a repeat of the mistake. Union of tip_reward's
    seen-set (every VaultSend) with ai_chatter's owned-set (purchased VaultSend +
    paid-message media). Imported lazily to avoid any import-order coupling."""
    from automations.ai_chatter import _owned_or_seen_media  # local: decouple
    seen = await tip_reward._seen_media(account_id, fan_id)
    owned = await _owned_or_seen_media(account_id, fan_id)
    return seen | owned


async def _pull_unseen(client, account_id: str, fan_id: int, folders: list[str],
                       count: int) -> list[int]:
    """Up to `count` FRESH (unseen + unowned) media ids from `folders`. Empty when
    no folders / nothing fresh left / no client. The freshness gate re-reads
    VaultSend each call, so pieces already sent earlier in the exchange are excluded."""
    folders = [f for f in (folders or []) if str(f).strip()]
    if not folders or count <= 0 or client is None:
        return []
    by_name = await asyncio.to_thread(tip_reward._resolve_folders, client, folders)
    exclude = await _exclude_media(account_id, fan_id)
    return await asyncio.to_thread(tip_reward._gather_unseen, client, folders,
                                   by_name, exclude, count)


def _fan_first_name(fan: Fan | None) -> str:
    """A real given name if we have one, else a pet name — never a raw location/tag."""
    raw = resolve_fan_name(fan) or ""
    name = raw.split("/")[0].strip() if raw else ""
    return name if (name and name.isalpha() and 1 < len(name) <= 20) else "babe"


def _apology_bubbles(fan: Fan | None, cfg: dict, rng: Random,
                     kind: str = _DEFAULT_APOLOGY_KIND) -> list[str]:
    """The apology turn — one warm bubble, worded to match the MISTAKE `kind`. An
    operator `apology_caption` overrides it. `{name}` → the fan's given/pet name."""
    name = _fan_first_name(fan)
    override = str(cfg.get("apology_caption") or "").strip()
    frames = _APOLOGY_FRAMES.get(kind) or _APOLOGY_FRAMES[_DEFAULT_APOLOGY_KIND]
    frame = override if override else rng.choice(frames)
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
    name = _fan_first_name(fan)
    per_step = max(1, int(cfg.get("gift_pieces_per_step") or 1))

    if action in ("apology", "apology_gift"):
        bubbles = [{"text": b, "media": [], "price_cents": 0}
                   for b in _apology_bubbles(fan, cfg, rng, kind)]
        media_sent: list[int] = []
        if action == "apology_gift":
            gift = await _pull_unseen(client, account_id, fan_id,
                                      _free_folders(cfg, tip_cfg, 0), per_step)
            if gift:
                bubbles.append({"text": rng.choice(_GIFT_LEADS).replace("{name}", name),
                                "media": gift, "price_cents": 0})
                media_sent = gift
        ok, _ = await _send_bubbles(client, account_id, fan_id, bubbles, wpm, indicator, now)
        return ok, media_sent

    if action == "free":
        gift = await _pull_unseen(client, account_id, fan_id,
                                  _free_folders(cfg, tip_cfg, 0), per_step)
        lead = rng.choice(_FREE_LEADS).replace("{name}", name)
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
                 or rng.choice(_PPV_TEASES)).replace("{name}", name)
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
        line = rng.choice(_NUDGE_LINES).replace("{name}", name)
        ok, _ = await _send_bubbles(client, account_id, fan_id,
                                    [{"text": line, "media": [], "price_cents": 0}],
                                    wpm, indicator, now)
        return ok, []

    return False, []


async def _advance_phase(account_id: str, cfg: dict, tip_cfg: dict, client, wpm,
                         indicator, now: datetime, only_fan_ids: set[int] | None,
                         stats: dict) -> None:
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
        rng = Random(f"make_right:{account_id}:{r.incident_key}:{step}")

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


@register("make_right")
async def run(account_id: str, payload: dict, *, run_id: int) -> dict:
    cfg = await _load_config(account_id)
    payload = payload or {}
    dry_run = bool(payload.get("dry_run"))
    now = datetime.utcnow()

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

    tip_cfg = await tip_reward._load_config(account_id)
    client = await asyncio.to_thread(ax._make_client, account_id) if (send_mode or dry_run) else None
    wpm = await load_typing_wpm(account_id)
    indicator = await load_typing_indicator(account_id)

    stats = {"opened": 0, "advanced": 0, "resolved": 0, "nudged": 0,
             "closed_silent": 0, "operator_only": 0, "errors": 0,
             "skipped_excluded": 0, "already_handled": 0}
    preview: list[dict] = []

    # ── ADVANCE phase (real runs only) — continue open exchanges on his replies ──
    if send_mode:
        await _advance_phase(account_id, cfg, tip_cfg, client, wpm, indicator, now,
                             only_fan_ids, stats)

    # ── OPEN phase — detect new double-charges and start an exchange ──
    incidents = await _detect_all(account_id, cfg, now, only_fan_ids)
    stats["candidates"] = len(incidents)

    if incidents:
        cap = max(1, int(cfg.get("per_fan_cap") or 2))
        # 0 is a valid "contact-guard off" — don't let `or 12` clobber it.
        gh = cfg.get("guard_hours", 12)
        guard_hours = int(gh) if gh is not None else 12
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
            rng = Random(f"make_right:{account_id}:{inc['incident_key']}:0")
            apology = _apology_bubbles(fan, cfg, rng, inc["kind"])[0]
            refund_flag = bool(cfg.get("flag_refund")) and int(inc["wrongful_cents"] or 0) > 0
            gift_preview = await _pull_unseen(
                client, account_id, fid, _free_folders(cfg, tip_cfg, inc["wrongful_cents"]),
                max(1, int(cfg.get("gift_pieces_per_step") or 1))) if client else []

            if not send_mode:
                preview.append({**base, "apology": apology, "steps": steps,
                                "gift_media_ids": gift_preview, "refund_flagged": refund_flag,
                                "action": "would_open"})
                continue

            # OPEN for real: send step 0 (apology / apology+free #1), write in_progress.
            if not await ax.acquire_fan_lease(account_id, fid, "make_right"):
                continue
            try:
                rem = {"steps": steps, "step": 1, "incident_key": inc["incident_key"],
                       "kind": inc["kind"], "wrongful_cents": inc["wrongful_cents"],
                       "refund_flagged": refund_flag, "message_ids": inc["message_ids"],
                       "overlap_media": inc["overlap_media"], "gift_history": [],
                       "last_step_at": now.isoformat(), "nudged": False}
                ok, media = await _do_step(client, account_id, fid, steps[0], cfg,
                                           tip_cfg, rng, fan, wpm, indicator, now,
                                           kind=inc["kind"])
                if ok:
                    opened_fids.add(fid)
                    if media:
                        rem["gift_history"].append({"step": 0, "media": media})
                    done = rem["step"] >= len(steps)
                    await _log_incident(account_id, fid, inc,
                                        status="resolved" if done else "in_progress",
                                        remediation=rem, now=now)
                    stats["opened"] += 1
                    if done:
                        stats["resolved"] += 1
                    else:
                        await _enqueue_silence_check(account_id, fid, cfg, now)
                    log.info("make_right opened account=%s fan=%s key=%s steps=%s refund=%s",
                             account_id, fid, inc["incident_key"], steps, refund_flag)
                else:
                    stats["errors"] += 1
            except Exception:
                stats["errors"] += 1
                log.warning("make_right open failed account=%s fan=%s key=%s",
                            account_id, fid, inc["incident_key"], exc_info=True)
            finally:
                await ax.release_fan_lease(account_id, fid)

    if not send_mode:
        # Surface active exchanges too, so the operator sees the whole picture.
        for r in await _open_resolution_rows(account_id, only_fan_ids):
            rem = _parse_rem(r)
            preview.append({"fan_id": int(r.fan_id), "incident_key": r.incident_key,
                            "kind": r.kind, "action": "in_progress",
                            "step": int(rem.get("step") or 0),
                            "steps": rem.get("steps") or []})
        return {"dry_run": True, "preview_only_reason": preview_only_reason,
                "candidates": stats["candidates"],
                "would_open": sum(1 for p in preview if p["action"] == "would_open"),
                "operator_only": sum(1 for p in preview if p["action"] == "operator_only"),
                "excluded": stats["skipped_excluded"],
                "already_handled": stats["already_handled"],
                "in_progress": sum(1 for p in preview if p["action"] == "in_progress"),
                "preview": preview[:25]}
    return {"status": "ok", **stats}
