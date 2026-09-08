"""Put a finished vault import INTO the local mirror.

WHY THIS EXISTS. The dashboard grid serves from the local mirror the moment the
mirror holds any live row for the account (`VaultManagePanel`'s `useLocal`), and
nothing in the import path — `vault_upload`, the batch route, the CLI — ever
wrote one. So a file that genuinely reached her OnlyFans vault appeared in no
vault view at all until somebody happened to run a full re-collect. The
operator's reading of that is "the upload failed", and the next move is to
upload it again, onto OnlyFans' byte dedupe, which returns the same id and
changes nothing.

WHY IT IS ITS OWN MODULE. This is a USE CASE — a session, a COUNT, an OF client
and a loop — and it lived in `vault_mirror`, which is a leaf whose whole purpose
is to be importable from anywhere. That module's docstring makes the argument
itself: it owns the OF-media → mirror-row MAPPING so `vault_stills` can be an
actual leaf and import it at module scope instead of lazily reaching back into
`vault_ai_api`. Giving it `db.engine`, `db.models`, a session and an OF call put
the cycle it was built to remove back one layer up.

It is not in `vault_upload` either: that module deliberately keeps the run out of
the database (its state is a JSON file readable by a sweep that runs before
anything else boots), and handing it `db.engine` would trade one leaf for
another. `vault_upload` owns the RUN; this owns what the run's result means to
the mirror; `vault_mirror` owns the row shape. Three modules, three sentences.
"""
from __future__ import annotations

import logging
from typing import Any, Iterable

from sqlalchemy import func, select

import vault_mirror
import vault_stills
from db.engine import get_session
from db.models import VaultItem

log = logging.getLogger("of-relay.vault_ingest")


async def _has_live_mirror(account_id: str) -> bool:
    """Is the GRID reading the mirror for this account?

    `removed_at IS NULL` is not a detail — it is the same question the UI asks.
    `vault_ai_api.cache_summary` counts only live rows, and that count becomes
    `cachedCount > 0`, which is what chooses the mirror over live OnlyFans. A
    COUNT over every row answers a different question and fails in exactly the
    direction this check exists to prevent: a mirror can be entirely BURIED in
    ordinary operation, because `vault_stills` stamps `removed_at` on any OF
    "not found" and the mundane causes of that (an expired signature, a session
    pointed at the wrong OF user) are not rare. The grid falls back to live OF
    and looks perfectly healthy — and seeding three imported ids into that
    buried mirror would flip the grid onto a three-tile view of a 2,500-item
    vault.
    """
    async with get_session() as s:
        return bool(await s.scalar(
            select(func.count()).select_from(VaultItem)
            .where(VaultItem.account_id == account_id,
                   VaultItem.removed_at.is_(None))
        ))


async def ingest_ids(account_id: str, media_ids: Iterable[Any], client: Any) -> int:
    """Mirror the media a batch just uploaded. Returns how many landed.

    One OF call per id — `vault_stills.resolve_fresh`, the same by-id read the
    render path takes when a signature expires — which is affordable precisely
    because an import is tens of items, not the whole vault. Sharing that
    function rather than re-rolling the call is what keeps ONE place knowing
    what a 404 from OnlyFans means: `resolve_fresh` distinguishes a media OF has
    DELETED (permanent) from a timeout or a 500 (transient — retryable). A bare
    `except Exception` around `vault_media_by_id` collapses those two, and the
    negative cache that keeps a dead tile from costing two OF calls per render is
    built out of the difference.

    What this caller does NOT share is the soft-delete that normally rides along
    with a 404 — see the `mark_gone=False` at the call site. It is the one
    caller asking about a media it uploaded itself moments ago, so a 404 is a
    blip, not a deletion, and the write would let an import bury media the
    operator can currently see. Failures here are per-id and non-fatal in the
    strict sense: nothing in this loop writes anything but the upsert below.

    `resolve_fresh` refreshes a row that already exists; it cannot create one,
    which is exactly what an import needs. Hence the upsert below — it is the
    insert half, and it goes through `vault_mirror` so a first-sighted media
    gets the same row a collect sweep would have written for it.

    A failure is never the run's failure: the import has already succeeded on
    OnlyFans, and a mirror row we could not write is recoverable by a re-collect.
    Never let this be the thing that fails a run.

    THE REAL BOUND, because "an import is tens of items" is not quite the whole
    story. `vault_upload._apply_caps` counts only `pending` items, and a Google
    Drive memo hit is already `done` before caps run — so a re-run of a Drive
    folder that is entirely duplicates yields up to `max_files * 2` landed ids
    (`resolve_budget`), 80 at the default cap, every one of them a serial OF read
    here. Worse, each is REDUNDANT: `_memo_lookup` fetched that exact media dict
    minutes earlier to confirm the id still resolves and is not hidden.

    Reusing those dicts was considered and rejected. The only path from
    `_memo_lookup` to here runs through `run.json` — the run record `vault_upload`
    deliberately keeps small enough to be an atomically-written file that a
    pre-boot sweep can read — and putting a full OF media payload per item into
    it trades a bounded re-read for an unbounded state file. The cost is one
    extra OF read per already-imported file on a duplicate re-run, which is the
    cheapest run there is; the cost of the alternative is paid by every run.
    """
    # Only into a mirror the grid is actually reading. An account that has never
    # been collected is already fine without us: the grid reads OF directly, and
    # the batch route drops the server-side cache the moment the run ends.
    if not await _has_live_mirror(account_id):
        log.info("mirror ingest: account=%s has no LIVE mirror row (never "
                 "collected, or every row soft-deleted) — the grid is reading OF "
                 "directly and already shows the import; skipping", account_id)
        return 0

    rows: list[dict[str, Any]] = []
    for mid in dict.fromkeys(int(m) for m in media_ids if m is not None):
        try:
            # `mark_gone=False`: an id we just uploaded is not evidence of a
            # deletion. `resolve_fresh`'s default stamps `removed_at` on any
            # 404, which is right for the render path and wrong here — a Drive
            # memo hit re-imports an id that ALREADY has a mirror row, so one
            # blip from OF (or a session pointed at the wrong OF user) buried a
            # tile the operator could still see a second ago, and `bytes_for`'s
            # negative cache then answered `gone` on every render until the next
            # full re-collect. An import must not be able to remove media from
            # the vault view.
            fresh = await vault_stills.resolve_fresh(account_id, mid, client,
                                                     mark_gone=False)
        except Exception as e:  # noqa: BLE001 — one bad id is not the batch
            log.warning("mirror ingest: OF read raised for media=%s: %s", mid, e)
            continue
        if fresh.media is None:
            # "gone" is OF telling us the id we just uploaded does not exist,
            # which is worth a line of its own; "fetch_failed" is transient and
            # the next collect picks it up.
            log.warning("mirror ingest: no payload for media=%s (%s)",
                        mid, fresh.reason)
            continue
        vals = vault_mirror.row_values(account_id, fresh.media)
        if vals is not None:
            rows.append(vals)

    if not rows:
        return 0
    # ONE session and ONE commit for the UPSERTS, the way the collect sweep
    # commits a page, instead of a connection and a transaction per media for
    # work that is a single logical write. Precisely that and no more: the loop
    # above still commits per id inside `resolve_fresh`'s `_refresh_mirror`, so
    # an id that already had a mirror row is written twice and a process that
    # dies mid-loop still leaves those refreshes half applied. That is
    # tolerable — a refresh only re-signs urls a re-collect would rewrite
    # anyway, and the rows it touches are already visible in the grid. What
    # this batching actually buys is that the INSERTS — the first-sighted
    # media, the ones an import exists to make visible — land all or none.
    try:
        async with get_session() as s:
            for vals in rows:
                await s.execute(vault_mirror.upsert_stmt(vals))
            await s.commit()
    except Exception as e:  # noqa: BLE001 — the media IS in her vault either way
        log.warning("mirror ingest: write failed for account=%s (%d rows): %s",
                    account_id, len(rows), e)
        return 0
    log.info("mirror ingest: account=%s landed=%d", account_id, len(rows))
    return len(rows)
