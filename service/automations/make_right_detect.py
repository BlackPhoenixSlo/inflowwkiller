"""make_right's DETECTION registry — pure-read incident scanners.

Split from make_right.py (which owns remediation: the apology/gift exchange
engine and the two lanes). Detection is self-contained: its only inputs are the
make_right cfg, the DB, and ownership's paid-predicate; nothing here touches the
send/ledger machinery, and a new mistake class is one function + one `_DETECTORS`
row IN THIS FILE, not a rewrite.

Each detector: async (account_id, cfg, now, only_fan_ids) -> list[incident dict].
An incident dict: {kind, fan_id, incident_key, message_ids, item_ids,
                   overlap_media, wrongful_cents, evidence}.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

from sqlalchemy import func, select

import ownership
from db.engine import get_session
from db.models import (
    CatalogItem, ContentOffer, LadderQuote, MessageMedia, Transaction, VaultSend,
)

log = logging.getLogger("of-relay.automation.make_right")

# Money-truth charge kinds (mirror transactions.py / the attribution view). A tip
# only becomes a "duplicate" if it resolves to media that OVERLAPS another charge,
# so including tips here is safe (a bare thank-you tip resolves to no media).
_CHARGE_TX_KINDS = ("ppv_message", "ppv_post", "tip")
# A delivered offer counts as a paid charge only when the money was real — NEVER a
# free teaser (resolved_by='free'), or every reused-media item becomes a false
# "double sale". The one copy of the predicate lives with the ownership readers.
_PAID_RESOLVED_BY = ownership.OWNED_RESOLVED_BY


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

    # (fan_id, message_id) -> charge accumulator
    # {item_ids:set, amount:int|None, sources:set, at:datetime|None}
    charges: dict[tuple[int, int], dict] = {}

    def _add(fan_id, message_id, *, item_id=None, amount=None, at=None, source):
        if fan_id is None or message_id is None:
            return  # a charge with no message id can't be deduped/resolved by media
        key = (int(fan_id), int(message_id))
        c = charges.setdefault(key, {"item_ids": set(), "amount": None,
                                     "sources": set(), "at": None})
        if item_id is not None:
            c["item_ids"].add(int(item_id))
        if amount is not None and (c["amount"] is None or int(amount) > c["amount"]):
            c["amount"] = int(amount)
        # WHEN the money moved — the anchor the freshness gate measures from. Sources
        # disagree by seconds (a quote is sent before it is paid), so keep the LATEST.
        if at is not None and (c["at"] is None or at > c["at"]):
            c["at"] = at
        c["sources"].add(source)

    async with get_session() as s:
        # (a) transactions — the money-truth, present regardless of which automation
        # sent it (the lane-agnostic backstop for funnel/mass/teaser buys).
        q_tx = select(Transaction.fan_id, Transaction.message_id, Transaction.amount_cents,
                      Transaction.occurred_at).where(
            Transaction.account_id == str(account_id),
            Transaction.kind.in_(_CHARGE_TX_KINDS),
            Transaction.status.in_(("cleared", "pending")),
            Transaction.fan_id.is_not(None),
            Transaction.message_id.is_not(None),
            Transaction.occurred_at >= since)
        if _fan_in:
            q_tx = q_tx.where(Transaction.fan_id.in_(_fan_in))
        for fid, mid, amt, occ in (await s.execute(q_tx)).all():
            _add(fid, mid, amount=amt, at=occ, source="txn")

        # (b) delivered, NON-FREE content_offers — carry the item (→ media) and the
        # PPV message id. This is what caught the Daniel incident.
        q_off = select(ContentOffer.fan_id, ContentOffer.offer_message_id,
                       ContentOffer.item_id, ContentOffer.price_cents,
                       ContentOffer.resolved_at).where(
            ContentOffer.account_id == str(account_id),
            ContentOffer.status == "delivered",
            ContentOffer.resolved_by.in_(_PAID_RESOLVED_BY),
            ContentOffer.offer_message_id.is_not(None),
            ContentOffer.resolved_at >= since)
        if _fan_in:
            q_off = q_off.where(ContentOffer.fan_id.in_(_fan_in))
        for fid, msg, item, price, resolved in (await s.execute(q_off)).all():
            _add(fid, msg, item_id=item, amount=price, at=resolved, source="offer")

        # (c) paid ladder quotes — same PPV message id, carry the item.
        q_lq = select(LadderQuote.fan_id, LadderQuote.message_id,
                      LadderQuote.item_id, LadderQuote.price_cents,
                      LadderQuote.sent_at).where(
            LadderQuote.account_id == str(account_id),
            LadderQuote.paid.is_(True),
            LadderQuote.message_id.is_not(None),
            LadderQuote.sent_at >= since)
        if _fan_in:
            q_lq = q_lq.where(LadderQuote.fan_id.in_(_fan_in))
        for fid, msg, item, price, sent in (await s.execute(q_lq)).all():
            _add(fid, msg, item_id=item, amount=price, at=sent, source="quote")

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
            last_at: datetime | None = None
            for m in comp_msgs:
                item_ids |= charges[(fid, m)]["item_ids"]
                amt = charges[(fid, m)]["amount"]
                if amt:
                    amounts.append(int(amt))
                at = charges[(fid, m)]["at"]
                if at is not None and (last_at is None or at > last_at):
                    last_at = at
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
                # The freshness anchor: the LAST of the duplicate charges. An apology
                # is owed for the charge he just made, not the one he made in July.
                "last_charge_at": last_at,
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
            "last_charge_at": occ,
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
