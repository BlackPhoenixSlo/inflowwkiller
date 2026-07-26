"""One OF media dict → one `VaultItem` row. The mapping, stated once.

This is the only place that knows how OF's media shape lands in our mirror, and
it exists as its own module because TWO callers need it and they sit on opposite
sides of an import edge:

    vault_ai_api.collect      pages the whole vault and upserts every row
    vault_stills.serve        re-reads ONE media by id after a signature expires

`vault_stills` used to reach back into `vault_ai_api` through a lazy in-function
import to get at this — the same cycle-dodge that was just removed from
`vault_dupes`. A leaf module that imports its own parent at call time is a cycle
with the error deferred to runtime, not a design. Owning the mapping here makes
`vault_stills` an actual leaf and lets both callers import it at module scope.

The pairing of `row_values()` with `MIRROR_REFRESH_FIELDS` is the point: what a
re-read refreshes has to be a SUBSET of what a first sight writes, and keeping
the producer and the refresh list side by side is what stops them drifting.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any


def parse_iso(v: Any) -> datetime | None:
    if not v or not isinstance(v, str):
        return None
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:  # noqa: BLE001
        return None


def thumb_of(files: dict | None) -> str | None:
    """The url for the UI tile. Ordered widest-availability first — OF omits
    variants per media, so the fallback chain is load-bearing, not defensive."""
    if not isinstance(files, dict):
        return None
    for k in ("thumb", "squarePreview", "preview", "full"):
        f = files.get(k)
        if isinstance(f, dict) and f.get("url"):
            return f["url"]
    return None


def row_values(account_id: str, m: dict, run_id: int) -> dict[str, Any] | None:
    """Mirror-field values for one OF media dict. None if it has no id."""
    mid = m.get("id")
    if mid is None:
        return None
    files = m.get("files") if isinstance(m.get("files"), dict) else {}
    kind = m.get("type") or "photo"
    dur = m.get("duration")
    now = datetime.utcnow()
    # OF custom-folder membership: listStates[].hasMedia is the per-item "is this
    # media in that list" flag (Posts.hasMedia aligns with top-level hasPosts).
    of_folders = [
        ls.get("id") for ls in (m.get("listStates") or [])
        if isinstance(ls, dict) and ls.get("type") == "custom" and ls.get("hasMedia") and ls.get("id")
    ]
    return {
        "account_id": account_id,
        "media_id": int(mid),
        "kind": str(kind),
        "duration_seconds": int(dur) if isinstance(dur, (int, float)) and dur else None,
        "width": m.get("width") if isinstance(m.get("width"), int) else None,
        "height": m.get("height") if isinstance(m.get("height"), int) else None,
        "thumb_url": thumb_of(files),
        "full_url": (files.get("full") or {}).get("url") if isinstance(files.get("full"), dict) else None,
        "raw_json": json.dumps(m, ensure_ascii=False, default=str),
        "created_at": parse_iso(m.get("createdAt")) or now,
        "updated_at_of": m.get("updatedAt") if isinstance(m.get("updatedAt"), str) else None,
        "last_seen_run_id": run_id,
        "updated_at": now,
        "of_folder_ids": json.dumps(of_folders),
        # OF just handed us this media, so it is NOT removed — and saying so here
        # is what makes the soft-delete recoverable. `removed_at` is otherwise
        # one-way: nothing in the codebase ever cleared it, so a single bad stamp
        # hid the media from the grid, the picker, describe, the PPV lineup and
        # the catalog seed forever, recoverable only by hand-written SQL. An
        # operator's deliberate hide is NOT undone by this: that stamp is only
        # written after `hide_vault_media` succeeded on OF, and OF's bundle has no
        # unhide call, so a hidden media can never be paged back to us.
        "removed_at": None,
        # base searchable blob — describe enriches this later; on conflict we
        # DON'T overwrite it (preserve describe output).
        "search_text": str(kind).lower(),
        "tags": "[]",
    }


# What "we have seen this media again" refreshes on the mirror row. ONE list, used
# by the collect sweep's on-conflict set AND by `vault_stills._refresh_mirror`
# after a by-id re-sign — because `raw_json` and the columns DERIVED from it
# (thumb_url, of_folder_ids, …) have to move together, and two hand-rolled update
# sets are how they drift apart.
#
# Every AI / operator / describe field is deliberately absent: `description`,
# `tags`, `ai_fields_json`, `review_state`, `search_text`. Seeing a media again
# says nothing about what we concluded about it, and a re-sign that quietly reset
# a graded item would be indistinguishable from never having graded it.
MIRROR_REFRESH_FIELDS = (
    "kind", "duration_seconds", "width", "height", "thumb_url", "full_url",
    "raw_json", "updated_at_of", "updated_at", "of_folder_ids",
    # Seeing the media again is precisely the evidence that it is not gone, so
    # the same list that refreshes the urls also LIFTS the soft-delete. Without
    # it `removed_at` had no writer of None anywhere in the repo: a render-path
    # 404 (an expired signature, a session pointed at the wrong OF user) buried
    # the media permanently, and a full re-collect could not bring it back.
    "removed_at",
)
