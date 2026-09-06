"""customs_api.py — the owed-customs queue. THE operator surface for this feature.

  GET  /admin/customs                  → every fan currently owed a custom
  POST /admin/customs/clear            → mark one delivered

WHY IT IS CROSS-ACCOUNT BY DEFAULT
----------------------------------
The queue exists so that nobody has to remember to go and look. A per-account
page is a surface you only visit if you already suspect something is on it.
Omitting `account_id` returns every owed fan across every account the principal
can see, oldest debt first, so one glance covers the whole roster.

WHAT "OWED" MEANS
-----------------
`fans.customs_owed_at IS NOT NULL` — see `automations/_customs`. One column, one
writer (`customs_watch`), and this endpoint plus the operator's button.

⚠️ IT USED TO BE A " Custom" SUFFIX ON THE FAN'S ONLYFANS NICKNAME, on the theory
that the operator would see it in the OF inbox without having to come here. The
theory was fine; the storage was not. `custom_nickname` is rewritten from
structured facts by the chat engines on every tick, so the marker was erased about
a minute after it was written and this queue was permanently, silently empty. The
page is now the only surface, which is why the untracked-accounts warning below
renders even when there is nothing owed.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, NamedTuple

from fastapi import APIRouter, Body, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, select

import llm_client
from auth import assert_account_owned, clamp_account_filter
from automations import _customs
from db.engine import get_session
from db.models import Account, AutomationRule, Fan, Message, Transaction

# How far back the REVIEW list reaches. A backlog to clear once, not a
# permanent second queue.
_REVIEW_DAYS = 60

# How much thread the RECORDING BRIEF reads. Bigger than the Read pane's
# defaults on purpose: the pane is anchored on a tip and is for a human who can
# scroll, while the brief is anchored on the OLDEST outstanding order and has to
# CONTAIN the message that placed it — a man describes what he wants ("naked gym
# routine, health food") and tips afterwards, sometimes days and a hundred
# messages before the tip that completes the order.
#
# SYMMETRIC, 100 EACH SIDE, and the reasoning below is why that is enough rather
# than why one side should be bigger. These are COUNTS, not days — see
# `customs_context` on why a time window is the wrong shape here.
#
# `before` is the side that has to reach: the anchor is the OLDEST outstanding
# tip and the request is made BEFORE the money arrives ("naked gym routine,
# health food" … then he tips), so the ask sits in the run-up, behind however
# much small talk happened in between. `after` only has to cover the tips that
# followed and any clarifying back-and-forth. So `before` is the side that must
# never be the SMALL one — and at 100/100 it is not.
#
# ⚠️ THEY WERE THE WRONG WAY ROUND IN THE FIRST CUT AND A TEST CAUGHT IT: with
# before=60/after=140 a thread carrying 80 messages between the ask and the tip
# returned 61 rows and did NOT contain the ask — the exact failure this was
# written to fix, reproduced by the fix itself. The correction was to stop
# starving `before`, not to starve `after` in return.
#
# Operator spec 2026-09-05: *"scrape those 100 messages over the tips before and
# after and generate CONCRETE SUMMARY OF WHAT WE NEED TO DO"*. 100 each side —
# the operator named the number and named it symmetrically, so a tuned asymmetry
# here would be this file inventing a spec it was handed.
#
# The long-run-up case is not solved by widening this window at all: the window
# is topped up with the order's own lines from the WHOLE thread (see
# `customs_order`), so an ask placed weeks before the tip reaches the model
# whatever `before` is set to. That is why this stayed a round, symmetric number.
_BRIEF_BEFORE = 100
_BRIEF_AFTER = 100

# How many of HIS lines reach the model. The window above decides what is
# fetched; this decides what survives into the prompt, and the two have to agree
# about which END matters — see the truncation note in `customs_brief`.
#
# ⚠️ IT MUST COVER THE WHOLE 100+100 WINDOW PLUS THE ORDER SPAN, AND AT 400 IT
# DOES — which is the point: on any ordinary thread the truncation below never
# fires at all. That is deliberate, because when it DID fire it was wrong. At 60
# it kept 40 lines from July and 20 from the end and dropped the 3 Sep message
# that WAS the order, and the live model wrote a work order off a July line.
#
# So this is a backstop for a pathological thread, not a budget: it is sized
# ABOVE the window rather than tuned against it, and the both-ends split in
# `customs_brief` is what makes the rare firing survivable rather than what
# makes this number safe.
_BRIEF_MAX_LINES = 400

log = logging.getLogger("of-relay.customs_api")

router = APIRouter()


# "What is an ORDER" (a burst of stacked tips, floor tested on the TOTAL) is
# `_customs.orders_per_fan` — the same burst definition customs_watch marks
# off. The FLOOR can still differ: this queue judges at the global MIN_CENTS
# while the watch honors the rule's per-account `min_cents`, so on an account
# with an override the review list may show bursts the watch deliberately
# does not book (marked: False rows — a human decides).


def _order_tips_q(*conditions):
    """The canonical SELECT for "tips that may be ORDERS", plus `conditions`.

    THE COLUMN TUPLE IS A CONTRACT, not a convenience: `_customs.orders_per_fan`
    and `bursts_per_fan` both destructure exactly
    `(account_id, fan_id, amount_cents, occurred_at, message_id)` in that order,
    and both require `occurred_at DESC` because the burst walk assumes rows
    arrive newest-first. Three call sites here were spelling that out by hand,
    which is three chances to reorder a column or forget the sort.

    It also carries the two filters that must never be omitted — DM tips only,
    and SETTLED money only. The settled filter is exactly what `customs_api` was
    missing while `customs_watch` had it, which showed refunded work as owed;
    baking it into the shared selector is what stops a fourth query
    reintroducing that bug."""
    return (select(Transaction.account_id, Transaction.fan_id,
                   Transaction.amount_cents, Transaction.occurred_at,
                   Transaction.message_id)
            .where(Transaction.kind.in_(_customs.ORDER_KINDS),
                   Transaction.status.in_(_customs.SETTLED_STATUSES),
                   *conditions)
            .order_by(Transaction.occurred_at.desc()))


def _chat_href(account_id: str, fan_id: int, msg_id) -> str:
    """The thread, anchored on the tip when we have its message id — landing on
    the message that has to be READ to decide, not merely on the fan."""
    base = f"/chat/{account_id}/{int(fan_id)}"
    return f"{base}?msg={int(msg_id)}" if msg_id else base


@router.get("/admin/customs")
async def list_customs(account_id: str | None = Query(None)) -> dict[str, Any]:
    """Every fan owed a custom. `account_id` optional — omit for the whole roster."""
    allowed = clamp_account_filter(account_id)

    async with get_session() as s:
        # `customs_owed_at IS NOT NULL` is the whole test now. It used to be a
        # string suffix on `custom_nickname`, fetched for every fan on the roster
        # and filtered in Python because a substring match would have caught
        # "Johny/Colombia/31/Customer Service". A column indexes, cannot be
        # ambiguous, and cannot be rewritten by the nickname pipeline.
        q = select(Fan.account_id, Fan.fan_id, Fan.customs_owed_at,
                   Fan.of_display_name, Fan.of_username,
                   Fan.lifetime_spend_cents).where(
            Fan.customs_owed_at.is_not(None))
        if allowed is not None:
            q = q.where(Fan.account_id.in_(allowed))
        owed = (await s.execute(q)).all()

        if not owed:
            # STILL report untracked accounts. An empty queue plus a silent
            # config gap looks exactly like a healthy empty queue, and that is
            # the state this whole feature exists to make impossible.
            return {"customs": [], "count": 0,
                    "untracked": await _untracked(allowed)}

        # The orders behind the debt, shown so the operator can see WHAT was paid
        # without leaving the page — a $100 and a $200 order are different pieces
        # of work, and two $100s are two voice notes.
        keys = {(r[0], int(r[1])) for r in owed}
        # How far back to scan. The OLDEST debt on the board, minus a burst
        # window so the tip that opened it cannot fall outside its own scan —
        # `customs_owed_at` is stamped with the tip's time, not the sweep's.
        # Floored at `_REVIEW_DAYS` so one ancient un-cleared row cannot drag the
        # query back across all history. A debt older than the floor still shows;
        # only the amount reconstruction is bounded, and it already falls back to
        # `customs_owed_at` below when the scan comes up empty.
        oldest = min((r[2] for r in owed if r[2] is not None), default=None)
        floor_at = datetime.utcnow() - timedelta(days=_REVIEW_DAYS)
        since = max(oldest - timedelta(minutes=_customs.BURST_MINUTES),
                    floor_at) if oldest is not None else floor_at
        # NO floor in SQL — see `_customs.orders_per_fan`: a sub-floor tip
        # stacked onto a qualifying one is part of the same order, so the floor
        # is a question about the burst TOTAL and is asked in Python.
        #
        # BOUNDED ON BOTH AXES. This used to pull every tip ever recorded on
        # every account holding an owed fan, survivable only because
        # `orders_per_fan` collapsed it to one tuple per fan. Counting ALL of a
        # fan's orders makes an unbounded scan both slower and wrong — a whale's
        # entire history would render as outstanding work.
        tips = (await s.execute(_order_tips_q(
            Transaction.account_id.in_({k[0] for k in keys}),
            Transaction.fan_id.in_({k[1] for k in keys}),
            Transaction.occurred_at >= since,
        ))).all()
        # EVERY unsettled order per fan, not just the newest — see
        # `_customs.bursts_per_fan`. A man who tips $100 on Monday and $100 on
        # Tuesday is owed two voice notes, and the second had no surface at all:
        # `_unmarked` below subtracts fans who are already owed, so it could not
        # appear there either.
        all_bursts = {k: v for k, v in _customs.bursts_per_fan(tips).items()
                      if k in keys}

        names = {a: (n or a) for a, n in (await s.execute(
            select(Account.id, Account.nickname)
            .where(Account.id.in_({k[0] for k in keys}))
        )).all()}

    out = []
    for acct, fid, owed_at, disp, uname, spend in owed:
        bursts = all_bursts.get((acct, int(fid)), [])
        # TOTAL across every outstanding order, and the ANCHOR stays the newest
        # one — the operator opens the thread at the most recent thing he said,
        # which is where the work order usually is. `order_count` is what makes
        # the total legible: "$200" alone reads as one big custom, "$200 · 2
        # tips" says two voice notes are owed.
        cents = sum(b[0] for b in bursts) if bursts else None
        at, msg_id = (bursts[0][1], bursts[0][2]) if bursts else (None, None)
        out.append({
            "account_id": acct,
            "account_name": names.get(acct, acct),
            "fan_id": int(fid),
            "marked": True,
            "display_name": disp or uname or f"fan #{fid}",
            # THE HANDLE, shipped separately from the display name and never
            # folded into it. The operator's job after reading this row is to
            # find the man in OnlyFans, and OF search takes the @username — a
            # display name ("Dennis 🔥") finds nothing. The row rendered only
            # `display_name`, which falls back to the username, so the two were
            # indistinguishable on screen and un-copyable apart: operator ruling
            # 2026-09-05, *"you need to copy and paste the exact username"*.
            "of_username": uname or None,
            "tip_cents": cents,
            "order_count": len(bursts),
            # Every order behind the total, newest first, so the pane can offer
            # one "Read" per order rather than only per fan.
            "orders": [{"cents": int(c), "at": a.isoformat() if a else None,
                        "msg_id": int(m) if m else None}
                       for c, a, m in bursts],
            # `customs_owed_at` is the ledger and therefore the authority on WHEN
            # he paid. The transaction scan is a nicety that tells the operator
            # how much; when it comes up empty (a tip older than its own lookback,
            # a fan marked by hand) the row still has to age correctly, so fall
            # back to the column rather than emitting null and sorting to the top.
            "tipped_at": (at or owed_at).isoformat() if (at or owed_at) else None,
            "lifetime_spend_cents": int(spend or 0),
            "chat_href": _chat_href(acct, fid, msg_id),
        })
    out.extend(await _unmarked(allowed, {(r["account_id"], r["fan_id"]) for r in out}))
    # Oldest debt first: the man who has been waiting longest is the one at risk.
    out.sort(key=lambda r: (r["tipped_at"] or "9999"))
    return {"customs": out, "count": len(out),
            "untracked": await _untracked(allowed)}


async def _unmarked(allowed: list[str] | None,
                    already: set[tuple]) -> list[dict[str, Any]]:
    """Qualifying tips carrying NO marker — the ones nothing was watching for.

    Operator ruling 2026-08-04: *"show them in the customs field and we will
    manually take a look and delete if needed"*. Before this they were a COUNT in
    a banner, which tells you a problem exists without letting you resolve it;
    every one had to be hunted down in OnlyFans by hand. As rows they get the
    same Sent button, and clearing one stamps `customs_cleared_at` so it settles
    and stops reappearing.

    These are a REVIEW list, not a debt list — a $100 DM tip is an order on the
    male accounts and was generosity five times on the female ones last month,
    and nothing here can tell which. `marked: False` says so on the row, and the
    settled ones are excluded so the list drains as it is worked.

    Bounded to `_REVIEW_DAYS`: this is a backlog to clear once, not a permanent
    second queue."""
    since = datetime.utcnow() - timedelta(days=_REVIEW_DAYS)
    async with get_session() as s:
        # NO floor — `_customs.orders_per_fan` tests the burst TOTAL.
        q = _order_tips_q(Transaction.occurred_at >= since,
                          Transaction.fan_id.is_not(None))
        if allowed is not None:
            q = q.where(Transaction.account_id.in_(allowed))
        tips = (await s.execute(q)).all()
        if not tips:
            return []

        keys = {(a, int(f)) for a, f, _, _, _ in tips} - already
        if not keys:
            return []
        fans = {(f.account_id, int(f.fan_id)): f for f in (await s.execute(
            select(Fan).where(Fan.account_id.in_({k[0] for k in keys})))
        ).scalars().all()}
        names = {a: (n or a) for a, n in (await s.execute(
            select(Account.id, Account.nickname)
            .where(Account.id.in_({k[0] for k in keys}))
        )).all()}

    out = []
    # EVERY order, not just the newest — the same correction the owed list above
    # already took. `orders_per_fan` returns one burst per fan, so a man who
    # tipped $100 on Monday and $100 on Tuesday appeared here as ONE $100
    # question, and the older one had no other surface at all: this list is the
    # last one, and the owed list excludes it by construction. Both halves of
    # the queue now count orders the same way (`_customs._walk_bursts`).
    for key, bursts in _customs.bursts_per_fan(tips).items():
        if key not in keys:
            continue
        acct, fid = key
        fan = fans.get(key)
        for cents, at, msg_id in bursts:
            # Already settled — delivered, or an operator cleared it. Not a
            # question. Per ORDER, so clearing an old one leaves a newer one
            # standing rather than settling the whole fan.
            if fan is not None and fan.customs_cleared_at and at <= fan.customs_cleared_at:
                continue
            out.append({
                "account_id": acct,
                "account_name": names.get(acct, acct),
                "fan_id": int(fid),
                "marked": False,
                "display_name": ((fan.of_display_name or fan.of_username) if fan else None)
                                or f"fan #{fid}",
                # Same contract as the owed rows above — a review row is handed off
                # by exactly the same copy-paste, so it cannot be the one shape
                # missing the handle.
                "of_username": (fan.of_username if fan else None) or None,
                "tip_cents": int(cents or 0),
                "tipped_at": at.isoformat() if at else None,
                "lifetime_spend_cents": int((fan.lifetime_spend_cents if fan else 0) or 0),
                # Anchor the chat on the tip itself — see the page for why the id
                # matters more than the fan link alone.
                "chat_href": _chat_href(acct, fid, msg_id),
            })
    return out


async def _untracked(allowed: list[str] | None) -> list[dict[str, Any]]:
    """Accounts taking qualifying tips with NO enabled `customs_watch` rule.

    THE ONE HOLE IN A PER-ACCOUNT SWITCH is that an eligible account never gets
    the rule and nobody notices — which is silently the same outcome as having no
    feature at all, because the money still arrives and nothing records it. So
    the queue reports it: a tip big enough to be an order, on an account that is
    not watching for them, is a configuration question shown next to the work it
    would otherwise have created.

    Deliberately NOT auto-enabling. What a $100 DM tip MEANS differs by account —
    it is an order on the male accounts and was 5x generosity on the female ones
    last month — so switching the watcher on for anyone who takes one would mark
    those fans owed and stop the bot selling to them."""
    since = datetime.utcnow() - timedelta(days=14)
    async with get_session() as s:
        q = (select(Transaction.account_id, func.count(),
                    func.max(Transaction.amount_cents))
             .where(Transaction.kind.in_(_customs.ORDER_KINDS),
                    Transaction.amount_cents >= _customs.MIN_CENTS,
                    # Money that actually arrived. A charged-back tip is not
                    # evidence that an account is taking custom orders, and
                    # warning about it sends the operator to enable a watcher
                    # for revenue that left again.
                    Transaction.status.in_(_customs.SETTLED_STATUSES),
                    Transaction.occurred_at >= since)
             .group_by(Transaction.account_id))
        if allowed is not None:
            q = q.where(Transaction.account_id.in_(allowed))
        tipped = (await s.execute(q)).all()
        if not tipped:
            return []

        watching = {a for (a,) in (await s.execute(
            select(AutomationRule.account_id)
            .where(AutomationRule.kind == "customs_watch",
                   AutomationRule.is_enabled.is_(True),
                   AutomationRule.account_id.in_({t[0] for t in tipped}))
        )).all()}

        names = {a: (n or a) for a, n in (await s.execute(
            select(Account.id, Account.nickname)
            .where(Account.id.in_({t[0] for t in tipped}))
        )).all()}

    return [{"account_id": a, "account_name": names.get(a, a),
             "qualifying_tips": int(n), "biggest_cents": int(mx or 0)}
            for a, n, mx in tipped if a not in watching]


@router.get("/admin/customs/context")
async def customs_context(
    account_id: str = Query(...),
    fan_id: int = Query(...),
    at: str | None = Query(None, description="ISO anchor; default = his newest qualifying tip"),
    before: int = Query(18, ge=1, le=250),
    after: int = Query(6, ge=0, le=250),
) -> dict[str, Any]:
    """The conversation AROUND the tip — what was actually ordered.

    This is the answer to "how do we know what to do with this row". A tip on its
    own says a man paid; the twenty messages around it say what he paid FOR. From
    a real thread (Amia / 142006425): "I can start doing it babe immediately
    after you tip for it" / "Make sure you talk dirty to daddy Dennis while
    you're riding me" / $200 + $100 / "Wanna see me in a sexy lingerie" — a
    complete work order, reconstructed from messages we already store.

    COUNT-BASED, not a time window. A time window is empty on a slow thread and
    unbounded on a fast one; N-before/M-after always returns something to read.

    It also makes the chat deep-link's page bound stop mattering for the common
    case: the context arrives WITH the row, so nobody has to reach a 700-hour-old
    message in the OnlyFans-backed thread pane to make the call."""
    assert_account_owned(account_id)

    async with get_session() as s:
        anchor_at = None
        if at:
            try:
                parsed = datetime.fromisoformat(at.replace("Z", "+00:00"))
            except ValueError:
                raise HTTPException(422, "bad `at` timestamp")
            # CONVERT to UTC before dropping the offset. `.replace(tzinfo=None)`
            # alone DISCARDS it, so "03:11:48+02:00" anchored at 03:11 instead of
            # 01:11 and read two hours of the wrong conversation. The column is
            # tz-naive UTC; anything arriving with an offset has to be moved, not
            # stripped. Naive input is already UTC by that same convention.
            anchor_at = (parsed.astimezone(timezone.utc).replace(tzinfo=None)
                         if parsed.tzinfo is not None else parsed)
        if anchor_at is None:
            # His newest tip, floor-free. The floor is a question about an
            # ORDER TOTAL now (see _customs.orders_per_fan), so a $60+$60 order has a row
            # in the queue but no single tip that clears it — asking for one here
            # would return no anchor for a fan the queue is actively showing.
            # Picking the anchor and judging significance are different jobs.
            anchor_at = (await s.execute(
                select(func.max(Transaction.occurred_at))
                .where(Transaction.account_id == str(account_id),
                       Transaction.fan_id == int(fan_id),
                       # Anchoring on a charged-back tip points the operator at
                       # a conversation about money that was taken back.
                       Transaction.status.in_(_customs.SETTLED_STATUSES),
                       Transaction.kind.in_(_customs.ORDER_KINDS))
            )).scalar_one_or_none()
        if anchor_at is None:
            return {"anchor_at": None, "messages": []}

        cols = (Message.message_id, Message.direction, Message.body,
                Message.created_at, Message.is_tip, Message.price_cents)
        older = (await s.execute(
            select(*cols).where(Message.account_id == str(account_id),
                                Message.fan_id == int(fan_id),
                                Message.is_unsent.is_(False),
                                Message.created_at <= anchor_at)
            .order_by(Message.created_at.desc()).limit(before)
        )).all()
        newer = (await s.execute(
            select(*cols).where(Message.account_id == str(account_id),
                                Message.fan_id == int(fan_id),
                                Message.is_unsent.is_(False),
                                Message.created_at > anchor_at)
            .order_by(Message.created_at.asc()).limit(after)
        )).all()

    # Reuse the canonical stripper rather than adding a FOURTH copy — there are
    # already three in the tree (automation_executor, event_transcoder,
    # transaction_ingest). Imported locally, like the OF client below, so this
    # module stays importable without dragging the executor in at load time.
    from automation_executor import _strip_html

    def _row(r) -> dict[str, Any]:
        mid, direction, body, created, is_tip, price = r
        return {
            "message_id": int(mid),
            "from_fan": direction == "in",
            # Bodies are stored as OF's HTML. The page renders text, so strip
            # here rather than shipping markup to a React tree.
            "text": _strip_html(body or ""),
            "at": created.isoformat() if created else None,
            "is_tip": bool(is_tip),
            "price_cents": int(price or 0),
        }

    return {"anchor_at": anchor_at.isoformat(),
            "messages": [_row(r) for r in reversed(older)] + [_row(r) for r in newer]}


class _Msg(NamedTuple):
    """ONE stripped thread row, as the order scan reads it.

    A bare 5-tuple with the same fields was indexed positionally in six places
    (`m[4]` for is_tip, `m[2]` for the text, `m[1]` for the direction), which is
    unreadable at the call site and silently wrong if the SELECT's column order
    ever moves. The names cost one class and make both impossible."""
    message_id: int
    direction: str          # "in" = the fan, "out" = us
    text: str               # already `_strip_html`-ed and stripped
    at: datetime | None
    is_tip: bool


# ── THE ORDER LINES ──────────────────────────────────────────────────────────
#
# THE ANCHOR WORDS open an order. Deliberately the narrowest possible set — a
# word that only appears when somebody is talking about commissioned work. This
# is what decides WHERE the order starts, so a loose word here (`video`, say,
# which appears in every PPV caption we have ever sent) drags the span back
# across unrelated history.
_ORDER_ANCHOR_WORDS = ("custom", "voice note", "voicenote", "commission")

# THE SPEC WORDS keep a line when the span is too long to print whole. Wider
# than the anchors, because by this point the span is already bounded and the
# job is recall: these are the words a spec is written in — the length, the
# wardrobe, the thing to say.
_ORDER_SPEC_WORDS = (
    "custom", "video", "vid", "clip", "record", "film", "commission",
    "minute", "minutes", "min", "mins", "long", "length",
    "wear", "wearing", "hat", "shirt", "shirtless", "naked", "outfit",
    "say", "name", "talk", "content", "details", "describe", "described",
)

# A GAP THIS LONG ENDS AN ORDER. One live fan has a COMPLETED 2024 custom and a
# live 2026 one in the same thread; without this the span opens in November
# 2024 and the operator reads two years of two different jobs. 45 days is longer
# than the quiet stretches inside one live order (one ran 6 days, another had a
# 24-day silence mid-layaway) and far shorter than the 16 months between his
# two orders.
_ORDER_GAP_DAYS = 45

# Above this many lines the span is FILTERED to spec-bearing lines instead of
# printed whole. Tuned against both live rows: a fast order (6 days) prints
# whole at 32 lines, and a layaway (60 days of daily chat) is 238 lines whole
# and 33 filtered. Both land readable.
_ORDER_MAX_FULL = 40

# How far back to look at all, and the row cap. Not a window around the tip —
# the whole relationship. See the docstring: 58 days separated one live order
# from the tip that surfaced it.
_ORDER_SEARCH_DAYS = 400
_ORDER_SEARCH_MAX = 4000


def _norm(s: str) -> str:
    """Whitespace/quote-insensitive key for matching a quote to its message."""
    return " ".join(s.replace("’", "'").replace("‘", "'").replace("“", '"')
                    .replace("”", '"').lower().split())


def _whole_messages(quotes: list[str], fan_msgs: list[dict]) -> list[str]:
    """Swap each model quote for the FULL stored message it came from.

    The operator's rule is "whole message if it's that important", and a model
    told never to shorten still does. So the prompt is not the enforcement;
    this is. A quote that is not a substring of anything he sent is dropped —
    a line shown as "his words" must be his."""
    out: list[str] = []
    for q in quotes:
        nq = _norm(q).strip('."\' …')
        if not nq:
            continue
        hit = next((m["text"] for m in fan_msgs if nq in _norm(m["text"])), None)
        if hit and hit.strip() not in out:
            out.append(hit.strip())
    return out


def _is_blast(text: str, blast: dict[str, int]) -> bool:
    """Did we send this exact text to more than one fan?

    OUR MARKETING IS THE LOUDEST THING IN THE THREAD and it is full of the words
    an order is written in — "unlock it and you get everything, every set, every
    clip", sent to the whole roster. On the live rows this was most of what a
    keyword scan returned, which is precisely the "not clear enough" the operator
    named. A line that went to N fans is not this fan's order, whatever it says.

    Compared on a prefix because the same blast arrives with and without its
    `<br/>` variants, which strip to texts differing only in whitespace."""
    return blast.get(text[:120], 0) > 1


@router.get("/admin/customs/order")
async def customs_order(
    account_id: str = Query(...),
    fan_id: int = Query(...),
) -> dict[str, Any]:
    """WHAT HE ORDERED, in his words, found across the WHOLE thread.

    This serves the operator's 2026-09-05 ruling — *"copy and paste the exact
    username, which account, and what the customer said"*. `customs_context`
    cannot serve it, and why not is the entire reason this endpoint exists.

    ⚠️ THE REQUEST IS NOT NEAR THE TIP, AND ON A LAYAWAY CUSTOM IT IS NOWHERE
    NEAR IT. Verified against two live rows, not argued from first principles:

      fan A — order placed in July. The tip that put him on the queue landed
      in September, and he had been paying it off in $100 instalments the whole
      time ("I'm now 400/700 of what I owe you for that custom"). That is 58
      days and several hundred messages apart. A 160-message `before` window
      reads none of it — what it actually returns is small talk.

      fan B — the ask is SPLIT across two days (the scenario and wardrobe one
      day, "five minute video" the next), and then RENEGOTIATED BY US to ten
      minutes. Keeping only his longest single message loses half the order;
      keeping only HIS messages loses the spec that was actually agreed.

    So: scan the whole thread, keep the lines that are ABOUT an order, return
    them oldest-first with dates. BOTH DIRECTIONS, and that is deliberate — the
    spec gets settled in the back-and-forth ("ten minutes, no cuts, that covers
    the hat, the toy, and every filthy word"), so a fan-only view of that
    conversation is not the order.

    NO MODEL, on purpose. The operator films against this text, and a paraphrase
    that quietly rewrites "ten minutes, no cuts" costs a take. Matching is a
    literal substring test he can predict, and every line is returned verbatim.
    """
    assert_account_owned(account_id)
    import re

    from automation_executor import _strip_html

    since = datetime.utcnow() - timedelta(days=_ORDER_SEARCH_DAYS)
    async with get_session() as s:
        rows = (await s.execute(
            select(Message.message_id, Message.direction, Message.body,
                   Message.created_at, Message.is_tip)
            .where(Message.account_id == str(account_id),
                   Message.fan_id == int(fan_id),
                   Message.is_unsent.is_(False),
                   Message.created_at >= since)
            # Newest-first under the cap so it sheds ANCIENT history rather than
            # the live negotiation; re-sorted oldest-first immediately below.
            .order_by(Message.created_at.desc())
            .limit(_ORDER_SEARCH_MAX)
        )).all()
        rows = list(reversed(rows))

        # Every outbound body on this account, counted by how many DIFFERENT
        # fans received it. One grouped query, not one per line — see `_is_blast`.
        #
        # ⚠️ SUMMED ONTO THE NORMALISED KEY, NEVER ASSIGNED. `GROUP BY
        # Message.body` groups on the RAW body, so one blast stored with and
        # without its `<br/>`/`<p>` wrapper comes back as SEVERAL rows that strip
        # to the same text. A dict comprehension collapses them by OVERWRITING —
        # last row wins — so three variants each sent to one fan scored 1 apiece
        # and `_is_blast`'s `> 1` let our own PPV copy through as the fan's own
        # words. The prefix key unified them; the counting undid it.
        #
        # Summing distinct-fan counts across variants slightly over-counts a fan
        # who received two of them. That is the SAFE direction here: this filter
        # only ever REMOVES lines from the order span, so over-counting drops one
        # more line of our marketing, while under-counting puts marketing in the
        # work order the operator films from.
        blast: dict[str, int] = {}
        for b, n in (await s.execute(
                select(Message.body, func.count(func.distinct(Message.fan_id)))
                .where(Message.account_id == str(account_id),
                       Message.direction == "out",
                       Message.is_unsent.is_(False))
                .group_by(Message.body)
        )).all():
            key = _strip_html(b or "").strip()[:120]
            blast[key] = blast.get(key, 0) + int(n)

    # Strip once; every pass below reads the same cleaned text.
    msgs = [_Msg(int(mid), direction, _strip_html(body or "").strip(),
                 created, bool(is_tip))
            for mid, direction, body, created, is_tip in rows]

    def _own_words(text: str, direction: str) -> bool:
        """A line that is this fan's conversation, not our mass marketing."""
        return not (direction == "out" and _is_blast(text, blast))

    # ── 1. WHERE DOES THE CURRENT ORDER OPEN?
    # The earliest anchor line reachable from the newest one without crossing a
    # `_ORDER_GAP_DAYS` silence. Walking back from the NEWEST is what keeps a
    # finished older order out of a live one's span.
    # INBOUND ONLY. Our per-fan AI nudges ("<name>, still thinking about that
    # custom idea you pitched") are unique per recipient, so the blast filter
    # cannot catch them, and one of them opened a live span on a sentence the
    # fan never said. Only the fan can open an order.
    anchors = [i for i, m in enumerate(msgs)
               if not m.is_tip and m.direction == "in" and m.text
               and any(w in m.text.lower() for w in _ORDER_ANCHOR_WORDS)]
    if not anchors:
        return {"lines": [], "count": 0, "opened_at": None,
                "truncated": False, "reason": "no order talk found in this thread"}
    start = anchors[-1]
    for j in range(len(anchors) - 1, 0, -1):
        prev_at, cur_at = msgs[anchors[j - 1]].at, msgs[anchors[j]].at
        if prev_at and cur_at and (cur_at - prev_at).days > _ORDER_GAP_DAYS:
            break
        start = anchors[j - 1]

    # ── 2. THE SPAN, in order. Tips stay in: they are how the operator sees a
    # LAYAWAY — a live fan paid $100 at a time against a $700 custom, and a span
    # showing only words reads as though the job is fully paid.
    span = [m for m in msgs[start:]
            if m.is_tip or (len(m.text) >= 12 and _own_words(m.text, m.direction))]

    # ── 3. Print it whole when it is short enough to read; otherwise keep the
    # spec-bearing lines. WORD-BOUNDARY matching here, unlike the anchor scan:
    # the spec words are short ("min", "say", "hat") and a substring test makes
    # "min" match "reminded" and "say" match "says". The anchors are long enough
    # not to need it.
    truncated = False
    if len(span) > _ORDER_MAX_FULL:
        truncated = True
        pat = re.compile(r"\b(" + "|".join(_ORDER_SPEC_WORDS) + r")\b")
        span = [m for m in span if m.is_tip or pat.search(m.text.lower())]

    return {
        "opened_at": msgs[start].at.isoformat() if msgs[start].at else None,
        # true = the span was too long to print whole and only spec-bearing
        # lines survived, so the operator knows there is thread he is not seeing.
        "truncated": truncated,
        "count": len(span),
        "lines": [{"message_id": m.message_id, "from_fan": m.direction == "in",
                   "is_tip": m.is_tip, "text": m.text,
                   "at": m.at.isoformat() if m.at else None}
                  for m in span],
    }


class _BriefBody(BaseModel):
    account_id: str
    fan_id: int
    at: str | None = None


@router.post("/admin/customs/brief")
async def customs_brief(body: _BriefBody = Body(...)) -> dict[str, Any]:
    """A WORK ORDER for one owed custom: what to record, in the fan's terms.

    The Read pane already shows the thread; this reads it FOR the operator and
    says what the voice note has to contain. It exists because the answer is
    usually spread across several messages ("25 back 25 front", a name to say, a
    scenario) and reconstructing it means re-reading the thread every time
    somebody picks the row up.

    ⚠️ THE MODEL NEVER STATES AN AMOUNT, AND THAT IS THE WHOLE SAFETY ARGUMENT.
    `transactions` knows exactly what was paid; a model asked to attribute
    dollars to deliverables will invent a split, the operator will film to it,
    and the recording is burned before anyone notices. So the money is passed IN
    as ground truth, the schema has no field for it, and the prompt forbids
    restating it. Deliverables come from the model; figures come from the DB.

    ON CLICK ONLY, never on queue load — a per-row call for rows nobody opens is
    money spent on nothing. Cheap model (`deepseek-v4-flash`), and every call is
    capped and audited by `llm_client.chat` like any other.

    Best-effort by construction: a missing key, a cap hit or a bad JSON body
    returns `ok: false` with a reason rather than raising, because the raw thread
    is rendered underneath and the operator is never blocked on this."""
    assert_account_owned(body.account_id)

    # Ground truth for the money, computed the same way the queue row is, so the
    # brief and the row can never disagree about what he paid.
    async with get_session() as s:
        tips = (await s.execute(_order_tips_q(
            Transaction.account_id == str(body.account_id),
            Transaction.fan_id == int(body.fan_id),
            Transaction.occurred_at >= datetime.utcnow()
            - timedelta(days=_REVIEW_DAYS),
        ))).all()
    bursts = _customs.bursts_per_fan(tips).get(
        (str(body.account_id), int(body.fan_id)), [])
    paid_cents = sum(b[0] for b in bursts)

    # THE WINDOW HAS TO REACH THE MESSAGE THAT PLACED THE ORDER, and that is
    # older than the tip that paid for it — a man says "naked gym routine, health
    # food" and tips afterwards. Anchoring on his NEWEST tip and reading back a
    # fixed 50 was the bug the operator hit: on a chatty thread, or with a second
    # order days later, the request falls outside the window and the brief
    # confidently summarises the wrong conversation.
    #
    # So the span is anchored on the OLDEST outstanding order and runs to now.
    # `customs_context` is count-based (see its docstring: a time window is empty
    # on a slow thread and unbounded on a fast one), so this asks for everything
    # after that anchor via `after`, and keeps a healthy `before` for the
    # run-up in which the ask is usually made.
    anchor_at = body.at or (bursts[-1][1].isoformat() if bursts else None)
    ctx = await customs_context(account_id=body.account_id, fan_id=body.fan_id,
                                at=anchor_at, before=_BRIEF_BEFORE,
                                after=_BRIEF_AFTER)
    window = ctx.get("messages") or []
    # The order's own lines from the WHOLE thread. On a layaway the ask sits
    # weeks behind the tip, outside any window; these carry it in.
    span = (await customs_order(account_id=body.account_id,
                                fan_id=body.fan_id)).get("lines") or []
    by_id = {int(m["message_id"]): m for m in span}
    for m in window:
        by_id.setdefault(int(m["message_id"]), m)
    msgs = sorted(by_id.values(), key=lambda m: m.get("at") or "")
    if not msgs:
        return {"ok": False, "reason": "no stored messages around this tip"}

    # BOTH DIRECTIONS, labelled. The creator routinely SETS the spec ("ten
    # minutes, no cuts, that covers the hat, the toy") after the fan proposes
    # something else; a fan-only transcript hands the model the wrong length.
    def _line(m) -> str:
        day = (m.get("at") or "")[:10]
        if m.get("is_tip"):
            return f"TIP {day}: {m.get('text') or ''}".rstrip()
        who = "HIM" if m.get("from_fan") else "US"
        return f"{who} {day}: {(m.get('text') or '').strip()}"
    lines = [_line(m) for m in msgs
             if m.get("is_tip") or (m.get("text") or "").strip()]
    if not any(m.get("from_fan") and not m.get("is_tip") for m in msgs):
        return {"ok": False, "reason": "he never said what he wanted in this window"}

    # Keep BOTH ends when it does not fit: the opening carries the ask, the
    # closing carries the last correction ("actually make it longer").
    if len(lines) > _BRIEF_MAX_LINES:
        head = _BRIEF_MAX_LINES * 2 // 3
        lines = lines[:head] + ["…"] + lines[-(_BRIEF_MAX_LINES - head):]
    transcript = "\n".join(lines)
    system = (
        "You read one OnlyFans fan's chat with the creator and write the WORK "
        "ORDER for a custom he paid for. The creator's team records it from "
        "your note alone, so be concrete. Lines are labelled HIM (the fan), US "
        "(the creator) and TIP. Reply with JSON only: "
        '{"summary": str, "his_words": [str], '
        '"call_him": str|null, "found_request": bool}. '
        "summary: 2-3 plain sentences saying exactly what to make — what it is "
        "(video or voice note), how long, what to wear or use, what to say and "
        "how, what to call him. Imperative, like a note to the person holding "
        "the camera. A US line counts as spec ONLY when it states what THIS "
        "custom will be — its length, what it covers, what is worn or used — "
        "and when US sets that after HIM proposed something else, the US line "
        "is the agreed spec: use it and say so. Everything else US writes — "
        "sexting, flirting, selling, promises — is NOT the order; never turn it "
        "into an instruction. Only include wardrobe, props or actions that were "
        "stated for this custom; do not carry them over from other chat. "
        "If HIM asked for more than one custom over time, describe only the "
        "one the most recent tips paid for. Everything in the summary must be "
        "traceable to a HIM line in his_words or to a US line that states this "
        "custom's contents; if you cannot point to one, leave it out. "
        "Write it out in full — never abbreviate ('wear the hat, shirt off, toy "
        "out', not 'hat/shirt/toy'). "
        "his_words: the 1-3 HIM messages that ARE the request, copied COMPLETE "
        "and word for word — the whole message every time, never shortened, "
        "never trailing off with '...'. "
        "call_him: the name he wants used, or null. If he asked to be called "
        "something different later in the chat, the later name wins. "
        "found_request: true ONLY if these messages contain him asking for "
        "something specific. If he never says what he wants here — the ask was "
        "earlier than this excerpt, or he only discussed price — set it false, "
        "leave todo empty and say in summary that the ask is not in this "
        "excerpt. A confident guess gets recorded and the take is wasted. "
        "NEVER state, guess, split or total any dollar amount — the system "
        "already knows what he paid and shows it. Do not invent anything he "
        "did not ask for."
    )
    try:
        res = await llm_client.chat(
            model="deepseek-v4-flash",
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": transcript}],
            purpose="customs_brief",
            account_id=str(body.account_id),
            fan_id=int(body.fan_id),
            response_format={"type": "json_object"},
            temperature=0.2,
        )
        # `parsed` is llm_client's own json.loads of the content when
        # response_format=json_object; fall back for a model that ignored it.
        brief = res.parsed if isinstance(res.parsed, dict) else json.loads(res.content)
    except Exception as exc:
        log.warning("customs brief failed account=%s fan=%s: %s",
                    body.account_id, body.fan_id, exc)
        return {"ok": False, "reason": str(exc)}

    return {
        "ok": True,
        # From the DB, never from the model.
        "paid_cents": paid_cents,
        "order_count": len(bursts),
        # A model that cannot see the request must not be rendered as a work
        # order — see the prompt. Default TRUE so a model that omits the field
        # degrades to the old behaviour rather than blanking every brief.
        "found_request": bool(brief.get("found_request", True)),
        "summary": str(brief.get("summary") or "").strip(),
        # WHOLE MESSAGES, enforced here rather than trusted to the prompt: the
        # live model still returned the back half of a message that begins
        # "How would I not and…" as if it were whole. Each quote is swapped
        # for the full stored message that contains it; one that matches
        # nothing he sent is dropped rather than shown as his.
        "his_words": _whole_messages(
            [str(x).strip() for x in (brief.get("his_words") or []) if str(x).strip()],
            [m for m in msgs if m.get("from_fan") and not m.get("is_tip")])[:3],
        "call_him": (str(brief["call_him"]).strip()
                     if brief.get("call_him") else None),
    }


class _ClearBody(BaseModel):
    account_id: str
    fan_id: int


@router.post("/admin/customs/clear")
async def clear_custom(body: _ClearBody = Body(...)) -> dict[str, Any]:
    """Mark one custom delivered — blanks `customs_owed_at` and stamps settled.

    Nothing is pushed to OnlyFans. This used to also strip a " Custom" suffix from
    the fan's OF display name and PUT the shortened name back; both halves are
    gone with the suffix, and with them the failure mode where the push failed,
    was reported as `pushed_to_of: false`, and was never retried by anything."""
    assert_account_owned(body.account_id)

    async with get_session() as s:
        fan = await s.get(Fan, {"account_id": str(body.account_id),
                                "fan_id": int(body.fan_id)})
        if fan is None:
            raise HTTPException(404, "fan not found")
        # `clear` returns False when nothing was owed — either two operators
        # clicked the same row, or this is a REVIEW row (a qualifying tip nothing
        # was watching for). Either way it must SETTLE: without the stamp a review
        # row reappears on every load and the backlog can never be worked down.
        already = not _customs.clear(fan)
        if already:
            fan.customs_cleared_at = datetime.utcnow()
        await s.commit()

    log.info("customs CLEARED account=%s fan=%s already_clear=%s",
             body.account_id, body.fan_id, already)
    return {"ok": True, "already_clear": already}
