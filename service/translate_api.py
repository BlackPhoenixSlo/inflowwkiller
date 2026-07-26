"""
translate_api.py — POST /admin/translate: batch-translate chat texts to English
for the inbox's 🌐 readability toggle.

The browser can't call Google's endpoint itself (no CORS), so the relay proxies
it. We use the free web endpoint (translate.googleapis.com, client=gtx) rather
than the paid Cloud Translation API — no key to provision, and if Google ever
rate-limits it the UI just silently shows originals. Source language is
auto-detected and echoed back so the UI can tag bubbles with "(es)" etc.;
messages detected as the target language render unchanged client-side.

Request  : {"texts": ["hola", ...], "target": "en"}
Response : {"results": [{"text": "hello", "lang": "es"} | null, ...]}  (same order)

THREE TIERS, checked in order — process memory, the `translation_cache` table,
then Google. The DB tier is what makes the toggle feel instant on anything but a
brand-new thread: before it existed the only caches were a per-tab Map and this
module's dict, so every page reload and every relay deploy re-bought the same
translations (~1.5s per 40 uncached bubbles, measured from the VPS). Chat text
repeats hard — 193k rendered texts over 30 days of prod collapse to 87k distinct
— so a durable, TEXT-keyed store is worth far more than its ~10MB ceiling.

Everything past tier 1 is BATCHED: one SELECT for all misses, one INSERT for all
fresh translations. Per-text round-trips would trade a network stall for a DB one.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import TypedDict

import httpx
from fastapi import APIRouter, Body
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from db.engine import get_session
from db.models import TranslationCache

log = logging.getLogger("uvicorn.error")

router = APIRouter()


class Translation(TypedDict):
    """One answer, in the ONE shape all three tiers speak.

    Every tier used to hand around a bare `dict` with these two keys — gtx's
    parser, the memory cache, the table reader and the HTTP response — so a typo
    in any of them would have surfaced as a silently missing translation rather
    than as an error. Naming the shape once costs four lines and makes every
    signature below say what it means."""
    text: str            # the target-language text
    lang: str            # ISO 639-1 source language as DETECTED ("es", …) or "und"

GOOGLE_URL = "https://translate.googleapis.com/translate_a/single"
MAX_TEXTS = 100          # per request; the UI chunks well below this
MAX_CHARS = 3000         # per text; chat messages are far shorter
_CONCURRENCY = 8
_CACHE_CAP = 8000

# Row ceiling for the durable tier. ~200 bytes/row (a 59-char mean body plus its
# translation) puts 50k rows near 10MB — deliberately modest next to a chatterly.db
# that has been corrupted before, and still roomy: only the accounts a chatter
# actually reads with 🌐 on ever land here, not all 87k distinct texts.
_DB_CAP = 50_000
_DB_PRUNE_TO = 40_000
# The cap check is a COUNT — cheap, but not free per request. Amortize it over
# this many newly-cached rows; overshooting the cap by <1% between sweeps costs
# nothing, and the steady state (every text already cached) never counts at all.
_PRUNE_EVERY = 500
_rows_since_prune = 0

# text_hash → Translation. Successes only — a transient network failure must not
# pin "untranslatable" until the next relay restart. Tier 1: a read-through mirror
# of the table, so a hot thread doesn't touch SQLite either.
#
# It is a CACHE, not an accumulator: it evicts (see _mem_put), so no request may
# read its own results back out of it. Each request keeps its own `resolved` map.
_cache: dict[str, Translation] = {}

# Serializes the cap sweep. Two concurrent batches both seeing count > cap would
# otherwise each delete a slice, over-pruning; the lock makes the second one
# re-count and no-op.
_prune_lock = asyncio.Lock()


def _cache_key(text: str, target: str) -> str:
    return hashlib.md5(f"{target}\x00{text}".encode("utf-8", "replace")).hexdigest()


def _mem_put(key: str, value: Translation) -> None:
    """Write through to tier 1, shedding the oldest quarter at the cap."""
    if len(_cache) >= _CACHE_CAP:
        for k in list(_cache)[: _CACHE_CAP // 4]:
            _cache.pop(k, None)
    _cache[key] = value


def _parse_gtx(data) -> Translation | None:
    """The gtx payload is a bare JSON array: data[0] is the segment list
    ([translated, original, ...] per sentence), data[2] the detected source
    language. Anything malformed → None (caller shows the original)."""
    try:
        segments = data[0] or []
        translated = "".join(seg[0] for seg in segments if seg and seg[0])
        lang = (data[2] or "").lower().split("-")[0]
        if not translated:
            return None
        return Translation(text=translated, lang=lang or "und")
    except (IndexError, TypeError):
        return None


async def _fetch_one(
    client: httpx.AsyncClient, sem: asyncio.Semaphore, text: str, target: str,
) -> Translation | None:
    """One uncached gtx call. Knows nothing about caching — the endpoint owns
    every tier, so there is exactly one place a hit/miss decision is made."""
    async with sem:
        try:
            r = await client.get(GOOGLE_URL, params={
                "client": "gtx", "sl": "auto", "tl": target, "dt": "t",
                "ie": "UTF-8", "oe": "UTF-8", "q": text,
            })
            r.raise_for_status()
            return _parse_gtx(r.json())
        except (httpx.HTTPError, ValueError):
            return None


# ── durable tier ────────────────────────────────────────────────────────
#
# Both halves swallow their own exceptions: a translation is a nice-to-have
# readability aid, and a locked/corrupt DB must degrade it to "slower", never to
# a 500 that blanks the thread.

async def _db_get(hashes: list[str]) -> dict[str, Translation]:
    """text_hash → Translation for whichever of `hashes` are stored."""
    if not hashes:
        return {}
    try:
        async with get_session() as s:
            rows = (await s.execute(
                select(TranslationCache.text_hash,
                       TranslationCache.translated,
                       TranslationCache.src_lang)
                .where(TranslationCache.text_hash.in_(hashes))
            )).all()
        return {r.text_hash: Translation(text=r.translated, lang=r.src_lang)
                for r in rows}
    except Exception:  # noqa: BLE001 — see module note: degrade, never fail
        log.warning("translate: durable cache read failed", exc_info=True)
        return {}


async def _db_put(target: str, fresh: dict[str, tuple[str, Translation]]) -> None:
    """Persist `text_hash → (source_text, Translation)` in one statement.

    ON CONFLICT DO NOTHING rather than an upsert: two chatters opening the same
    thread at once race here, and their answers are identical, so the loser has
    nothing to correct. Keeping the first write also keeps `created_at` honest
    for the age-ordered prune."""
    if not fresh:
        return
    try:
        async with get_session() as s:
            await s.execute(
                sqlite_insert(TranslationCache).values([
                    {"text_hash": h, "target": target, "src_lang": v["lang"] or "und",
                     "source_text": src, "translated": v["text"]}
                    for h, (src, v) in fresh.items()
                ]).on_conflict_do_nothing(index_elements=["text_hash"]),
            )
            await s.commit()
    except Exception:  # noqa: BLE001
        log.warning("translate: durable cache write failed", exc_info=True)
        return

    global _rows_since_prune
    _rows_since_prune += len(fresh)
    if _rows_since_prune >= _PRUNE_EVERY:
        _rows_since_prune = 0
        await _prune()


async def _prune() -> None:
    """Hold the table at _DB_CAP rows, dropping the oldest down to _DB_PRUNE_TO.

    Age is the only sane eviction key here: bumping a `last_used_at` would turn
    every cache HIT — the fast path this whole module exists to protect — back
    into a write."""
    async with _prune_lock:
        try:
            async with get_session() as s:
                total = (await s.execute(
                    select(func.count()).select_from(TranslationCache)
                )).scalar_one()
                if total <= _DB_CAP:
                    return
                doomed = (await s.execute(
                    select(TranslationCache.text_hash)
                    .order_by(TranslationCache.created_at.asc())
                    .limit(total - _DB_PRUNE_TO)
                )).scalars().all()
                await s.execute(
                    delete(TranslationCache)
                    .where(TranslationCache.text_hash.in_(doomed)))
                await s.commit()
            log.info("translate: pruned %d cached translations", len(doomed))
        except Exception:  # noqa: BLE001
            log.warning("translate: cache prune failed", exc_info=True)


class TranslateBody(BaseModel):
    texts: list[str] = Field(..., max_length=MAX_TEXTS)
    target: str = "en"


@router.post("/admin/translate")
async def translate_batch(body: TranslateBody = Body(...)):
    target = (body.target or "en").lower().split("-")[0]
    texts = [(t or "").strip()[:MAX_CHARS] for t in body.texts]
    keys = [_cache_key(t, target) if t else "" for t in texts]

    # `resolved` is this REQUEST's answers, and the only thing the response is
    # built from. Deliberately not the module cache: that one evicts, so reading
    # our own results back out of it would make correctness depend on an unwritten
    # "a batch is smaller than the eviction slice" invariant.
    resolved: dict[str, Translation] = {}
    # Misses, deduped — the UI sends whatever is on screen, and "hey" three times
    # is still one translation.
    need: dict[str, str] = {}

    # Tier 1 — process memory.
    for text, key in zip(texts, keys):
        if not text or key in resolved or key in need:
            continue
        hit = _cache.get(key)
        if hit is not None:
            resolved[key] = hit
        else:
            need[key] = text

    # Tier 2 — the durable table, one SELECT for every miss.
    if need:
        for key, value in (await _db_get(list(need))).items():
            _mem_put(key, value)
            resolved[key] = value
            need.pop(key, None)

    # Tier 3 — Google, for whatever nobody has ever translated.
    if need:
        pending = list(need.items())
        sem = asyncio.Semaphore(_CONCURRENCY)
        async with httpx.AsyncClient(timeout=8.0) as client:
            fetched = await asyncio.gather(
                *(_fetch_one(client, sem, text, target) for _, text in pending),
            )
        fresh: dict[str, tuple[str, Translation]] = {}
        for (key, text), value in zip(pending, fetched):
            if value is None:
                continue          # transient: leave uncached so the next try retries
            _mem_put(key, value)
            resolved[key] = value
            fresh[key] = (text, value)
        await _db_put(target, fresh)

    # A key still missing is a genuine failure → null → the UI shows the original.
    results = [resolved.get(key) if text else None for text, key in zip(texts, keys)]
    failed = sum(1 for t, r in zip(texts, results) if t and r is None)
    if failed:
        log.warning("translate: %d/%d texts failed (google gtx)", failed, len(texts))
    return {"results": results}
