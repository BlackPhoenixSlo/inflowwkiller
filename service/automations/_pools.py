"""service/automations/_pools.py — text/media randomisers shared by auto_posts
and mass_premade.

An item (one post / one broadcast) can carry POOLS instead of fixed values, and
the pick happens at SEND time (so each drip/resend fires a fresh random choice):

  • texts: [str, …]        — one is chosen at random per fire (falls back to the
                             single `text` field when `texts` is absent).
  • media_files: [id, …]   — a pre-picked image POOL (e.g. 20 vault ids).
  • media_folder_id: int   — pull the pool from a vault folder (non-DRM photos).
  • media_count: int = 1   — how many images to attach per fire (sampled from the
                             combined pool; clamped to the pool size).

These are NOT registered as an automation — just helpers (no @register).
"""
from __future__ import annotations

import logging
import random

log = logging.getLogger("of-relay.automation.pools")


def int_list(raw: object) -> list[int]:
    """Coerce a list to ints, dropping non-numeric entries."""
    out: list[int] = []
    if isinstance(raw, list):
        for v in raw:
            try:
                out.append(int(v))
            except (TypeError, ValueError):
                continue
    return out


def pick_text(item: dict) -> str:
    """One text for this fire: a random entry from the `texts` pool when present
    (non-empty trimmed entries only), else the single `text` field."""
    texts = item.get("texts")
    if isinstance(texts, list):
        opts = [t.strip() for t in texts if isinstance(t, str) and t.strip()]
        if opts:
            return random.choice(opts)
    return (item.get("text") or "").strip()


def has_media_source(item: dict) -> bool:
    """True if the item could yield media — used for the pre-resolve empty check
    (before we hit OF to expand a folder)."""
    return bool(int_list(item.get("media_files")) or item.get("media_folder_id") is not None)


def _folder_photo_ids(client, folder_id: int) -> list[int]:
    """Non-DRM photo ids in one vault folder (mirrors auto_stories._usable_photos,
    but returns ids since posts/mass attach by vault id)."""
    page = client.vault_media(type="photo", list_id=int(folder_id),
                              limit=48, field="recent", sort="desc")
    items = page.get("list") or page.get("items") or []
    out: list[int] = []
    for it in items:
        full = ((it.get("files") or {}).get("full") or {})
        if full.get("url") and not full.get("drm"):
            try:
                out.append(int(it.get("id")))
            except (TypeError, ValueError):
                continue
    return out


def pick_media(item: dict, client=None) -> list[int]:
    """Sample `media_count` (default 1) ids from the item's pool for ONE fire.
    Pool = explicit `media_files` ∪ vault-folder photos (when `media_folder_id`
    is set AND a `client` is supplied). Sampling is without replacement and
    clamped to the pool size."""
    pool = int_list(item.get("media_files"))
    folder_id = item.get("media_folder_id")
    if folder_id is not None and client is not None:
        try:
            for i in _folder_photo_ids(client, int(folder_id)):
                if i not in pool:
                    pool.append(i)
        except Exception:
            log.warning("pick_media: folder pull failed folder=%r", folder_id, exc_info=True)
    if not pool:
        return []
    try:
        count = int(item.get("media_count") or 1)
    except (TypeError, ValueError):
        count = 1
    count = max(1, min(count, len(pool)))
    return random.sample(pool, count)
