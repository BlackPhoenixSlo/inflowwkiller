"""service/of_mirror.py — the shared lifecycle for an "OF-mirrored" resource.

A mirrored resource is one OnlyFans owns (trial links, promo campaigns) that we
keep a light local row for, so the operator's name survives and the table renders
without a live round-trip. Every such resource does the same three things:

  READ    merge the mirror rows with a best-effort live OF fetch  → merge_mirrors
  CREATE  call OF, then insert the mirror row                      (per-resource)
  DELETE  delete on OF (best-effort), then drop the mirror row     → delete_mirrored

The paged FETCH half already lives in of_promos.paged_of_fetch. These are the
other two halves, which promotions_api and trial_links_api had as near-identical
copies. CREATE stays per-resource: its OF call signature and the field mapping
into the mirror row are genuinely different each time, and wrapping that would
buy indirection rather than clarity.

Deliberately free of resource knowledge — the caller supplies its model, its OF
call, and its row projections.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Iterable

from fastapi import HTTPException

import automation_executor as ax
from auth import assert_account_owned
from db.engine import get_session

log = logging.getLogger("of-relay.of_mirror")


def merge_mirrors(
    mirrors: Iterable[Any],
    live: list[dict] | None,
    *,
    of_id_attr: str,
    live_id_key: str,
    from_mirror: Callable[[Any, dict | None], dict],
    from_live: Callable[[dict], dict],
) -> list[dict]:
    """Mirror rows ∪ live OF rows, mirrors first.

    Each mirror row is rendered by `from_mirror(row, live_or_None)` — it carries the
    operator's name and any local-only state, enriched with whatever OF says about it
    right now. Any live OF row with NO mirror (created outside fastt) is then appended
    read-only via `from_live(item)`.

    `live` is `paged_of_fetch`'s None-vs-list contract: None means OF was unreachable,
    so every mirror simply renders with `live=None` (the caller decides what an unknown
    live state looks like — never invent one here). The caller reports reachability to
    its UI as `of_available: live is not None`.
    """
    by_id = {int(x[live_id_key]): x for x in (live or [])
             if x.get(live_id_key) is not None}
    out: list[dict] = []
    seen: set[int] = set()
    for m in mirrors:
        of_id = getattr(m, of_id_attr, None)
        hit = by_id.get(int(of_id)) if of_id is not None else None
        if hit is not None:
            seen.add(int(of_id))
        out.append(from_mirror(m, hit))
    for item in (live or []):
        oid = item.get(live_id_key)
        if oid is None or int(oid) in seen:
            continue
        out.append(from_live(item))
    return out


async def delete_mirrored(
    model,
    mirror_id: int,
    *,
    of_id_attr: str,
    of_delete: Callable[[Any, int], Any],
    not_found: str,
    label: str,
) -> dict[str, Any]:
    """Delete a mirrored resource: OF first (best-effort), then the mirror row.

    The three-phase session dance is the point of this helper, and it is NOT
    incidental:

      1. open a session, load the row, own-check it, capture what we need, CLOSE.
      2. call OF with NO session held — it's a network round-trip that can hang,
         and SQLite has a single writer.
      3. re-open, re-load (it may have been deleted meanwhile), drop it.

    A failed OF delete is swallowed on purpose — the link may already be gone, or
    the session may be dead — but the mirror row still goes, so the operator is
    never stuck looking at a phantom row they can't remove. `of_deleted` reports
    which happened.
    """
    async with get_session() as s:
        row = await s.get(model, mirror_id)
        if row is None:
            raise HTTPException(404, not_found)
        assert_account_owned(row.account_id)
        account_id = row.account_id
        of_id = getattr(row, of_id_attr, None)

    of_deleted = False
    if of_id is not None:
        try:
            client = await asyncio.to_thread(ax._make_client, account_id)
            await asyncio.to_thread(of_delete, client, int(of_id))
            of_deleted = True
        except Exception:
            log.warning("%s OF delete failed account=%s of_id=%s",
                        label, account_id, of_id, exc_info=True)

    async with get_session() as s:
        row = await s.get(model, mirror_id)
        if row is not None:
            await s.delete(row)
    log.info("%s_deleted id=%s of_deleted=%s", label, mirror_id, of_deleted)
    return {"deleted": mirror_id, "of_deleted": of_deleted}
