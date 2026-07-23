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
"""
from __future__ import annotations

import asyncio
import hashlib
import logging

import httpx
from fastapi import APIRouter, Body
from pydantic import BaseModel, Field

log = logging.getLogger("uvicorn.error")

router = APIRouter()

GOOGLE_URL = "https://translate.googleapis.com/translate_a/single"
MAX_TEXTS = 100          # per request; the UI chunks well below this
MAX_CHARS = 3000         # per text; chat messages are far shorter
_CONCURRENCY = 8
_CACHE_CAP = 8000

# md5(target + text) → {"text","lang"}. Successes only — a transient network
# failure must not pin "untranslatable" until the next relay restart.
_cache: dict[str, dict] = {}


def _cache_key(text: str, target: str) -> str:
    return hashlib.md5(f"{target}\x00{text}".encode("utf-8", "replace")).hexdigest()


def _parse_gtx(data) -> dict | None:
    """The gtx payload is a bare JSON array: data[0] is the segment list
    ([translated, original, ...] per sentence), data[2] the detected source
    language. Anything malformed → None (caller shows the original)."""
    try:
        segments = data[0] or []
        translated = "".join(seg[0] for seg in segments if seg and seg[0])
        lang = (data[2] or "").lower().split("-")[0]
        if not translated:
            return None
        return {"text": translated, "lang": lang or "und"}
    except (IndexError, TypeError):
        return None


async def _translate_one(
    client: httpx.AsyncClient, sem: asyncio.Semaphore, text: str, target: str,
) -> dict | None:
    text = (text or "").strip()[:MAX_CHARS]
    if not text:
        return None
    key = _cache_key(text, target)
    hit = _cache.get(key)
    if hit is not None:
        return hit
    async with sem:
        try:
            r = await client.get(GOOGLE_URL, params={
                "client": "gtx", "sl": "auto", "tl": target, "dt": "t",
                "ie": "UTF-8", "oe": "UTF-8", "q": text,
            })
            r.raise_for_status()
            out = _parse_gtx(r.json())
        except (httpx.HTTPError, ValueError):
            return None
    if out is not None:
        if len(_cache) >= _CACHE_CAP:
            for k in list(_cache)[: _CACHE_CAP // 4]:
                _cache.pop(k, None)
        _cache[key] = out
    return out


class TranslateBody(BaseModel):
    texts: list[str] = Field(..., max_length=MAX_TEXTS)
    target: str = "en"


@router.post("/admin/translate")
async def translate_batch(body: TranslateBody = Body(...)):
    target = (body.target or "en").lower().split("-")[0]
    sem = asyncio.Semaphore(_CONCURRENCY)
    async with httpx.AsyncClient(timeout=8.0) as client:
        results = await asyncio.gather(
            *(_translate_one(client, sem, t, target) for t in body.texts),
        )
    failed = sum(1 for t, r in zip(body.texts, results) if t.strip() and r is None)
    if failed:
        log.warning("translate: %d/%d texts failed (google gtx)", failed, len(body.texts))
    return {"results": list(results)}
