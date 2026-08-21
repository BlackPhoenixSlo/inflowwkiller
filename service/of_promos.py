"""service/of_promos.py — shared OnlyFans promo helpers.

Used by BOTH `promotions_api` (the Growth → Promotion surface) and
`automations/promo_reactivate` (the re-arm automation), so the OF contract lives
in exactly one place. Deliberately free of FastAPI imports so the automation
plugin scan stays light.

The contract below was captured LIVE (2026-07-22, Ava):
  • GET  /promotions  → {"hasMore": bool, "items": [ {...}, ... ]}   (NOT "list")
  • POST /promotions  → a LIST containing the created promo: [ {...} ]
  • create requires `discount` — an INTEGER PERCENT in [1, 100]; `price` is not
    a valid field.
  • an item carries: id, message, price, type, claimsCount, subscribeCounts,
    subscribeDays, createdAt, finishedAt, isFinished, canClaim.
    There is NO `discount` on read — the mirror row is the only record of it.
  • `isFinished` is NOT a reliable liveness flag: OF leaves it False on spent
    promos indefinitely (9 of Ava's 304, oldest 2025-12, all canClaim=False).
    A promo is on offer only when isFinished is false AND canClaim isn't false.
  • GET /promotions is PAGED (10 by default, limit honoured to 100+) and its
    `hasMore` is ALWAYS true — stop on a short page, never on hasMore.
  • deleting a promo does not remove it from /promotions; OF flips it to
    isFinished=true / canClaim=false and keeps it in history.
"""
from __future__ import annotations

import asyncio
import logging

import automation_executor as ax

log = logging.getLogger("of-relay.of_promos")


def of_field(item: dict, *names, default=None):
    """First present key among `names` (OF field spellings drift)."""
    for n in names:
        if n in item and item[n] is not None:
            return item[n]
    return default


def safe_int(v, default: int = 0) -> int:
    """Tolerant int coercion — OF drift ('30.0', dicts, None) must NEVER 500."""
    try:
        if isinstance(v, bool):
            return int(v)
        return int(float(v))
    except (TypeError, ValueError):
        return default


def coerce_of_id(v) -> int | None:
    """OF entry id → int, or None when it isn't numeric (drift-safe)."""
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def to_cents(v) -> int:
    try:
        return int(round(float(v) * 100))
    except (TypeError, ValueError):
        return 0


def normalise_of_promo(item: dict) -> dict:
    """One OF promo entry → our flat shape (coercion never raises).

    Liveness needs BOTH `isFinished` and `canClaim`. OF does not reliably flip
    isFinished on a spent promo: 9 of Ava's 304 promos carry isFinished=False
    with canClaim=False, the oldest from 2025-12 — dead for months. Reading
    isFinished alone showed them as live campaigns in the UI, and would have
    made promo_reactivate stand down forever ("a promo is already live") on an
    account that actually has no offer running.
    """
    finished = bool(of_field(item, "isFinished", default=False))
    can_claim = item.get("canClaim")
    # Absent canClaim → don't infer death from it; only an explicit False counts.
    is_live = (not finished) and (can_claim is not False)
    return {
        "of_promo_id": coerce_of_id(of_field(item, "id", "promoId")),
        "price_cents": to_cents(of_field(item, "price", default=0)),
        "subscribe_counts": safe_int(of_field(item, "subscribeCounts", "subscribe_counts", default=0)),
        "subscribe_days": safe_int(of_field(item, "subscribeDays", "subscribe_days", default=0)),
        "message": of_field(item, "message", "text", default=""),
        "promo_type": of_field(item, "type", default="all"),
        # performance = how many claimed it
        "subscriber_count": safe_int(of_field(item, "claimsCount", "countSubscribers",
                                              "subscribersCount", default=0)),
        "can_claim": can_claim,
        # "finished" here means "not on offer", which is what every caller
        # actually wants to know.
        "is_finished": not is_live,
        "status": "active" if is_live else "finished",
    }


def extract_created_promo(res) -> dict:
    """OF's create returns a LIST holding the created promo; some shapes wrap it
    as {"items":[...]} or return the bare object. Pull the created item out so
    the caller can capture of_promo_id — without it, delete/re-arm can't reach
    OF and a live promo is orphaned."""
    if isinstance(res, list) and res and isinstance(res[0], dict):
        return res[0]
    if isinstance(res, dict):
        items = res.get("items")
        if isinstance(items, list) and items and isinstance(items[0], dict):
            return items[0]
        return res
    return {}


def _rows_from_payload(raw, keys: tuple[str, ...]) -> list | None:
    """The item list out of an OF list response, or None if the shape is unusable
    (caller treats None as 'OF not reachable'). `keys` are the dict spellings to try
    in order — OF list bodies wrap the rows under different keys per endpoint
    (/promotions → "items", /trials → "list")."""
    if isinstance(raw, dict):
        for k in keys:
            v = raw.get(k)
            if v is not None:
                return v if isinstance(v, list) else None
        return None
    return raw if isinstance(raw, list) else None


_PAGE = 100
_MAX_PAGES = 12          # 1200 promos — a hard backstop, see below


async def paged_of_fetch(
    account_id: str,
    *,
    page_call,
    normalise,
    id_key: str,
    payload_keys: tuple[str, ...] = ("items", "list"),
    max_pages: int = _MAX_PAGES,
    need_ids: set[int] | None = None,
    label: str = "of",
) -> list[dict] | None:
    """Best-effort live OF fetch of a PAGED list endpoint. Returns the (possibly
    empty) normalised list on success, or None when the fetch failed (no session /
    OF error) — callers use None-vs-list to tell 'not reachable' apart from
    'reachable, zero rows'.

    OF list endpoints paginate and their `hasMore` flag is unreliable (it can stay
    true past the real end), so we stop on a short/empty page, with `max_pages` as a
    hard backstop. Dedupe is by `id_key`. `need_ids` is an early exit: once every id
    the caller cares about has been seen we stop paging.

      page_call(client, offset, limit) -> raw OF response
      normalise(dict)                  -> flat dict carrying its id under `id_key`
      payload_keys                     -> dict spellings the rows live under, in order
    """
    try:
        client = await asyncio.to_thread(ax._make_client, account_id)
    except Exception:
        log.info("%s OF client failed account=%s — mirror-only", label, account_id, exc_info=True)
        return None

    out: list[dict] = []
    seen: set[int] = set()
    outstanding = set(need_ids or ())
    for page in range(max_pages):
        try:
            raw = await asyncio.to_thread(page_call, client, page * _PAGE, _PAGE)
        except Exception:
            # A mid-page failure means we hold a PARTIAL history. Returning it
            # would look like "these rows no longer exist" to the caller, so
            # report unreachable instead and let it fail closed.
            log.info("%s OF fetch failed account=%s page=%s — mirror-only",
                     label, account_id, page, exc_info=True)
            return None
        rows = _rows_from_payload(raw, payload_keys)
        if rows is None:
            return None
        for x in rows:
            if not isinstance(x, dict):
                continue
            item = normalise(x)
            rid = item.get(id_key)
            if rid is not None:
                if rid in seen:
                    continue
                seen.add(rid)
                outstanding.discard(rid)
            out.append(item)
        if len(rows) < _PAGE:
            break                      # short page = end of history
        if need_ids is not None and not outstanding:
            break                      # found everything the caller asked about
    else:
        log.warning("%s paging hit the %s-page cap account=%s — history truncated",
                    label, max_pages, account_id)
    return out


async def fetch_of_promos(
    account_id: str,
    *,
    need_ids: set[int] | None = None,
    max_pages: int = _MAX_PAGES,
) -> list[dict] | None:
    """Best-effort live OF fetch of /promotions, PAGED. Thin wrapper over
    paged_of_fetch (the shared skeleton /trials uses too).

    /promotions' `hasMore` is useless — it stayed true past offset 180 on a
    230-promo history — so paging stops on a short page. `need_ids` is an early
    exit: promo_reactivate only needs to locate its own armed campaigns, so it
    costs one request instead of two dozen.
    """
    return await paged_of_fetch(
        account_id,
        page_call=lambda c, off, lim: c.promotions(offset=off, limit=lim),
        normalise=normalise_of_promo,
        id_key="of_promo_id",
        payload_keys=("items", "list"),
        max_pages=max_pages,
        need_ids=need_ids,
        label="promotions",
    )
