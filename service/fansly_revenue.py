"""service/fansly_revenue.py — the Fansly revenue lane.

WHY THIS EXISTS, AND WHY IT IS NOT IN transaction_ingest.py
-----------------------------------------------------------
`transaction_ingest` is built around OF's `/api2/v2/payouts/transactions`: a
paginated, creator-side money ledger with `net`/`fee`/`vatAmount`, a
`loading -> cleared` status flip and an English `description` its `KIND_CATALOG`
prefix-matches. Fansly has NONE of that. Every creator-side sales endpoint 404s
(`/account/media/purchases`, `/purchases`, `/statements`, `/account/sales`, …),
so there is no ledger to walk, no fee split to read and no description to parse.

Fansly exposes exactly ONE creator-visible money signal: a purchase row in
`GET /notifications`. Confirmed live 2026-09-02 on a real $3 sale:

    {"id": "951442430324908036",          # stable -> our idempotency key
     "type": 2007,                        # media purchase
     "correlationId": "951440044332167168",     # the accountMedia grant bought
     "correlationGroupId": "951404711209099264", # the BUYER
     "createdAt": 1788335912,             # epoch SECONDS
     "metadata": "{\\"accountMediaPrice\\":3000}"}  # 1/1000-dollar units

Bolting that into `transaction_ingest` would mean threading a second, contrary
shape through `_parse_row`, `_classify_kind`, `_window_for`, the page loop and
the fingerprint/promote writer — a Fansly `if` in each. That is the spaghetti
this module exists to avoid: the platforms share the DESTINATION (`transactions`)
and nothing about the SOURCE, so they share the table and the model, not the
walk. Both of `transaction_ingest`'s entry points (`run_one_tick`, the 5-min
supervisor; `run_fast_tick`, the 30s freshness poll) dispatch here at their
first line on `fansly_backend.is_fansly`, and the OF path below is untouched.

WHERE THE PLATFORM KNOWLEDGE LIVES
----------------------------------
Not here. `FanslyShimClient.purchases()` owns which notification codes mean a
payment, where in `metadata` the price hides and that Fansly counts in
1/1000ths of a dollar; it hands back rows that already say `fan_id`,
`price_usd` and `created_at`. This module only decides what is BANKABLE and
writes it. That split is not tidiness: the shim also renders the same sale's
figure into the notification text the money rail reads, and when the two
disagreed about which metadata key held the price, one surface showed "$3.00"
while the ledger banked nothing.

WHAT IT DELIBERATELY DOES NOT CLAIM
-----------------------------------
* **Gross only.** Fansly never tells the creator its cut, so `net_cents`,
  `fee_cents` and `vat_cents` stay NULL rather than being imputed. Stats already
  count NULL nets as missing (`net_missing_count`) instead of treating gross as
  net — see /admin/stats/revenue's `include_net` note.
* **`status="cleared"`.** There is no pending/cleared lifecycle to observe. The
  sale happened; nothing will later flip it. (`_TRACKED_STATUSES` counts both,
  so this only affects which bucket the dashboard's pending column shows.)
* **Only purchases.** Tips would be `7001`, but no live tip has ever been
  captured on this account, so no tip row is written — inventing a `kind` from
  an unverified code is how you get revenue that never happened. When a real tip
  is captured, add the code to the shim's `_PURCHASE_NOTIF_TYPES` (or a tip set)
  and it flows through unchanged. `45012` (stream ticket) is in the same
  position: the shim LABELS it a purchase for the bell, but it has never been
  seen live, so it banks nothing and is logged when encountered rather than
  guessed at.
* **Refunds are invisible AND uncorrectable.** Fansly exposes no refund or
  chargeback signal; a reversed sale presumably just vanishes from the feed.
  Because writes are idempotent-forever (see below), a banked row STAYS banked,
  so a refund would leave totals permanently overstated by that amount. There
  is no fix available at this API surface — this is a documented limit of the
  lane, not an oversight.
* **Currency is assumed USD.** The notification carries no currency field, so
  there is nothing to read and inventing a lookup would be a guess. The OF lane
  defaults the same way and `stats.revenue` sums `amount_cents` without
  grouping by currency. The real exposure is that `accountMediaPrice` is
  presumably denominated in the CREATOR's payout currency: for a non-US
  creator this would label e.g. EUR as USD. Recorded here so the assumption is
  falsifiable rather than invisible.

WINDOW / DURABILITY — read this before trusting the totals.
`/notifications` is ONE account-wide page with no date cursor, so this reads
what is CURRENTLY on the feed; an old sale that has scrolled off is not
recoverable from any endpoint Fansly gives a creator. Writes are therefore
idempotent-forever rather than windowed: the partial unique index
`uq_tx_provider_id (account_id, provider_transaction_id)` makes re-seeing the
same notification a no-op, so a row observed ONCE while it was on the feed stays
banked permanently. That is the same "catch it while it is visible" contract the
`is_paid` ratchet already relies on (db.models.is_paid_ratchet), and the reason
both must keep running on a cadence rather than on demand.
"""
from __future__ import annotations

import asyncio
import functools as _functools
import json
import logging
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, NamedTuple

from sqlalchemy import select

import fansly_backend
from db.engine import get_session
from db.models import Transaction, TransactionScanHistory

log = logging.getLogger("of-relay.fansly_revenue")


@_functools.lru_cache(maxsize=1)
def _log_unbanked_codes(codes: frozenset) -> None:
    """Announce, ONCE per process, the purchase codes we knowingly do not bank.

    `FanslyShimClient.unbanked_purchase_codes()` is a constant derived from the
    shim's own tables (45012, stream tickets — labelled a purchase for the bell
    but never captured live, so no money is written for it). Money we
    deliberately do not count should be visible rather than silent, but it is
    the same set every tick: logging it per tick would be noise, and per
    process is enough to answer "why is the dashboard missing that sale".
    """
    if codes:
        log.info("fansly_revenue_unbanked_purchase_codes codes=%s", sorted(codes))

# Every proven Fansly sale is a PPV message unlock. The vocabulary is the
# catalog's own (transaction_ingest.KIND_CATALOG) so stats' `startswith("ppv")`
# bucketing — and therefore the dashboard's "Message earnings" card — works
# with no changes. WHICH notification codes count as a sale is the shim's
# knowledge, not ours: `client.purchases()` has already applied it.
_KIND = "ppv_message"


def _cents(price_usd: Any) -> int | None:
    """Dollars -> whole cents, half-up, or None if this is not a bankable
    amount.

    None (not 0) on junk or zero, so the caller SKIPS the row: a $0.00
    transaction in the ledger is indistinguishable from a real free unlock.

    `Decimal` rather than float arithmetic. `round(x * 100)` would both lose
    exactness on large values and round half-to-EVEN, which is inconsistent on
    the half-cent boundary ($0.015 -> 2c but $0.025 -> 2c). Money rounds up.
    """
    try:
        d = Decimal(str(price_usd))
    except (InvalidOperation, TypeError, ValueError):
        return None
    # NaN/Infinity survive the Decimal constructor but raise on comparison and
    # on quantize, so reject them explicitly rather than letting a junk price
    # take down the tick.
    if not d.is_finite() or d <= 0:
        return None
    try:
        cents = int((d * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except InvalidOperation:
        # An absurd magnitude (>= ~1e26) overflows the default 28-digit Decimal
        # context on quantize. Caught HERE because `_cents` runs outside the
        # tick's feed-failure try/except: an uncaught raise would take the
        # whole page down over one corrupt row, while every other unusable
        # value in this module merely drops its own row.
        return None
    return cents or None


def _occurred_at(raw: Any) -> datetime | None:
    """Fansly stamps epoch SECONDS -> naive UTC, or None if unreadable.

    Naive to match every other writer of this column (a tz-aware value would
    compare unequal against the stats date filters). None rather than "now":
    a fabricated timestamp files a real sale under the wrong day forever, and
    this module's whole contract is that a row which cannot supply a field is
    dropped rather than defaulted.
    """
    try:
        return datetime.fromtimestamp(int(raw), tz=timezone.utc).replace(tzinfo=None)
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _fan_id(raw: Any) -> int | None:
    """The buyer's accountId as the BigInteger the column wants.

    Fansly ids are 64-bit snowflakes delivered as STRINGS. Python ints are
    arbitrary-precision so this is lossless — only JS truncates them (see
    app/lib/fanId.ts).
    """
    try:
        v = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


class TickResult(NamedTuple):
    """What one Fansly tick actually knows.

    NOT OF's tick dict. That shape carries `pages` and `duration_ms`, and this
    lane can honestly supply neither — the feed has no pagination, and the
    caller is what holds the clock. Returning it anyway meant three `pages: 1`
    / `duration_ms: 0` literals that one seam silently overwrote and the other
    passed through as a lie. The seams translate; see `run_one_tick`.
    """
    rows_inserted: int
    error: str | None = None


class Sale(NamedTuple):
    """One bankable sale, in the ledger's own terms."""
    provider_id: str
    fan_id: int
    amount_cents: int
    occurred_at: datetime
    description: str
    # The source row, verbatim. Money rows must stay auditable back to the
    # evidence they were written from — this is the only surviving copy, since
    # the notification itself scrolls off the feed permanently.
    raw_json: str


def parse_purchase(row: dict) -> Sale | None:
    """A `FanslyShimClient.purchases()` row -> a `Sale`, or None to skip it.

    None means "not a bankable sale": no stable id to dedup on, no buyer to
    attribute to, no usable amount, or no readable timestamp. Never a guess —
    every field is read from the row, and a row that cannot supply one is
    dropped, because the alternative is revenue no fan ever paid.
    """
    provider_id = str(row.get("id") or "").strip()
    fan_id = _fan_id(row.get("fan_id"))
    amount_cents = _cents(row.get("price_usd"))
    occurred_at = _occurred_at(row.get("created_at"))
    if not provider_id or fan_id is None or amount_cents is None or occurred_at is None:
        return None
    return Sale(
        provider_id=provider_id,
        fan_id=fan_id,
        amount_cents=amount_cents,
        occurred_at=occurred_at,
        # The grant bought goes in `description`, NOT `message_id`: that column
        # joins onto OUR messages table, and a Fansly accountMedia id there
        # would make the PPV-history join silently match the wrong row.
        description=f"Fansly media purchase (grant {row.get('grant_id') or '?'})",
        raw_json=json.dumps(row, default=str)[:32 * 1024],
    )


def _bump_lifetime_fn():
    """`transaction_ingest._bump_lifetime`, resolved at call time.

    Imported lazily because `transaction_ingest` imports THIS module at the
    top level (that is where the platform seam lives), so a module-level
    import here would be circular. Resolving it per call — rather than
    copying the twenty lines that create the Fan stub and add to the rollup —
    is what keeps the two lanes' lifetime-spend behaviour identical by
    construction.
    """
    from transaction_ingest import _bump_lifetime
    return _bump_lifetime



async def _mark_scanned(account_id: str, *, inserted: int) -> None:
    """Keep this account's `TransactionScanHistory` row honest.

    The OF lane's state machine (backfill -> refresh, watermarks) is
    meaningless here: there is no dated window to walk and no history to page
    back through, because the feed only ever shows the present. But the row is
    SHARED bookkeeping — `_decide_mode` reads `fully_backfilled` and the admin
    dashboard renders `current_status` — so an unstamped Fansly account would
    sit at "backfilling" forever.

    Clearing `consecutive_failures`/`last_error` here is the SUCCESS half of
    the same contract the OF lane's `_mark_success` implements, and it is
    correct precisely because the failure half now exists too: a feed read
    that fails RAISES, so the supervisor calls `_record_failure` and the
    pause backoff works. An earlier revision also force-cleared
    `paused_until` on every tick, which quietly disabled that backoff
    permanently — a one-off repair of pre-existing stale failures was written
    as forever behaviour. It is not restored here: `_is_paused` is checked
    before the tick runs, so this line could never have un-paused an account
    anyway; a genuinely paused account is cleared by its backoff expiring,
    exactly as on OF.
    """
    async with get_session() as s:
        row = await s.get(TransactionScanHistory, account_id)
        if row is None:
            row = TransactionScanHistory(account_id=account_id)
            s.add(row)
        row.last_scan_at = datetime.utcnow()
        row.fully_backfilled = True
        row.current_status = "ok"
        row.consecutive_failures = 0
        row.last_error = None
        row.rows_inserted_total = (row.rows_inserted_total or 0) + inserted
        row.updated_at = datetime.utcnow()


async def run_one_tick(account_id: str) -> TickResult:
    """Bank every purchase currently visible on this account's feed.

    Idempotent by construction: each row is looked up by its notification id in
    `provider_transaction_id` before insert, so the 5-minute cadence re-sees the
    same sale for as long as it stays on the feed and banks it exactly once.
    """
    inserted = 0
    try:
        # Also threaded: on a cold cache this reads the session file and can
        # issue an ensure_device_id() HTTP call.
        client = await asyncio.to_thread(fansly_backend.get_client, account_id)
    except FileNotFoundError:
        # Same signal the OF lane reports when a session was never captured.
        return TickResult(rows_inserted=0, error="no_session")
    _log_unbanked_codes(frozenset(client.unbanked_purchase_codes()))

    try:
        # to_thread, like every other client call in this lane's OF sibling
        # (transaction_ingest wraps `client.transactions` the same way). The
        # shim is synchronous `requests`, and this runs while HOLDING the
        # per-account tick lock — calling it inline stalls the whole relay
        # event loop for the length of a Fansly HTTP round-trip.
        rows = await asyncio.to_thread(client.purchases)
    except Exception:
        # Raise, do NOT swallow. Recording the failure is what drives
        # consecutive_failures and the pause backoff, and a lane that fails
        # silently is the expensive outcome here: sales that scroll off the
        # single-page feed during a blind window are unrecoverable.
        #
        # The RAISE is the whole contract — recording is the caller's job, and
        # BOTH callers now do it (the 30s fast-poll loop used to only
        # `log.debug`, which was safe while the OF fast tick swallowed its own
        # fetch errors but silently dropped ours 120x an hour). Deliberately
        # not recorded here as well: the supervisor's own `except` also
        # records, so self-reporting would double-count every failure and trip
        # the 3-strike pause after 2.
        log.warning("fansly_revenue_feed_failed account=%s", account_id, exc_info=True)
        raise

    sales = [x for x in (parse_purchase(r) for r in rows) if x is not None]
    if not sales:
        await _mark_scanned(account_id, inserted=0)
        return TickResult(rows_inserted=0)

    async with get_session() as s:
        # One query for the whole page: the feed is small and re-read often, so
        # the common case is "every row already banked" and this settles it
        # without a per-row round trip.
        known = set((await s.execute(
            select(Transaction.provider_transaction_id).where(
                Transaction.account_id == account_id,
                Transaction.provider_transaction_id.in_(
                    [x.provider_id for x in sales]),
            )
        )).scalars().all())

        for sale in sales:
            if sale.provider_id in known:
                continue
            # Absorb what THIS loop banks, not just what the DB already had.
            # A notification id repeated within one page would otherwise miss
            # `known` twice, and the unique index would reject the second
            # insert at commit — inside the session's __aexit__, which rolls
            # the WHOLE session back. That loses every other sale on the page,
            # skips _mark_scanned, and burns a failure strike for a data
            # quirk. The SELECT above settles the cross-tick case; this line
            # settles the intra-page one.
            known.add(sale.provider_id)
            s.add(Transaction(
                account_id=account_id,
                fan_id=sale.fan_id,
                kind=_KIND,
                message_id=None,
                amount_cents=sale.amount_cents,
                currency="USD",
                occurred_at=sale.occurred_at,
                raw_json=sale.raw_json,
                provider_transaction_id=sale.provider_id,
                # No pending->cleared lifecycle exists on Fansly; see module
                # docstring. Gross only, so net/fee/vat stay NULL.
                status="cleared",
                source="ledger",
                description=sale.description,
            ))
            # The ledger row is only HALF the write the OF lane performs. Its
            # other half is this rollup, and skipping it is not a dashboard
            # lag: `fans.lifetime_spend_cents` is what the automations read to
            # make live money decisions 24/7 — autoreply's whale gate,
            # _customs' proven-spend floor, gen_info's tiers, nudge_online's
            # spend bands. Without it every Fansly buyer reads $0 FOREVER (no
            # other writer bumps these rows), so the AI keeps hard-selling a
            # fan who has already paid. `_bump_lifetime` also creates the Fan
            # stub, which matters more here than on OF: a PPV-only buyer never
            # triggers the inbound-DM path that would otherwise create it.
            # Same session as the insert, so the row and the rollup commit
            # together or not at all.
            await _bump_lifetime_fn()(s, account_id, sale.fan_id, sale.amount_cents)
            inserted += 1
            log.info("fansly_revenue_banked account=%s fan=%s cents=%d notif=%s",
                     account_id, sale.fan_id, sale.amount_cents, sale.provider_id)

    await _mark_scanned(account_id, inserted=inserted)
    return TickResult(rows_inserted=inserted)
