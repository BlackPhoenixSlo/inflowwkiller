"""service/automations/pack_farewell.py — welcome_chatter_for_info's parting PPV.

When the gather finishes with a fan (profile done, or the runaway cutoff), the
LAST message he gets is a few pictures, priced — instead of the silence that
used to end the lane. The media comes from ONE operator-picked vault folder and
the price is a fixed knob, not the ladder: this fires exactly once per fan, at a
moment he never asked for anything, so a negotiated quote has nothing to
negotiate against. The attribution bucket answers "does the farewell sell" on
its own row.

## Why it is its own module

Split out of `pack_sender` on 2026-08-16, when the describe-derived caption
phrase pushed that file past 1,000 lines. This is the cut that costs a reader
the least: the farewell is a whole PRODUCT — its own category, its own catalog
row, its own price rule, its own trigger — and it shares only the spine every
lane shares (`_available`, `_bought_media`, `_ask_claim`, the audit, the wire).

⚠️ The dependency runs ONE WAY, `pack_farewell` → `pack_sender`, and it must stay
that way: `pack_sender` re-exporting this would make the pair a cycle. Its callers
(`welcome_chatter_for_info`'s graduation and `ai_chatter`'s payer-floor rescue)
import this module directly — both of THEM import each other, so the send has to
live down here where neither owns it.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import replace
from datetime import datetime, timedelta

from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from db.engine import get_session
from db.models import CatalogItem, Fan

from . import _vault_pick, content_resolver, upsell
from .pack_audit import audit_ask
from .pack_claim import Claim, ask_clause, voice_line_ok
from .pack_sender import (
    PACK_LANGUAGES, REFUSE_AUDIT, REFUSE_LANGUAGE, REFUSE_NO_SHELF,
    REFUSE_TOO_THIN, Delivery, PackPlan, _ask_claim, _available, _bought_media,
    _refusal, _singleton_item, deliver,
)

log = logging.getLogger("of-relay.automation.pack_farewell")

FAREWELL_CATEGORY = "gather_close"

# Below 2 pictures a fixed price buys "a lot of money for few items" — the
# 2026-07-31 shape. The claim states the exact count either way; this floor is
# about the value shape, not honesty. A configured count below it is clamped UP
# (and the config validator holds the knob to ≥2), so the only TOO_THIN refusal
# left is a genuinely thin POOL.
FAREWELL_MIN_ITEMS = 2

# How long a fan waits between gather-close attempts. ONE clock covers both
# callers — the graduation is simply the first attempt, and every later one is
# `ai_chatter`'s rescue. Operator's number (2026-08-24).
GATHER_CLOSE_RETRY_DAYS = 3

# Digit- and currency-free on purpose: `compose_caption` REJECTS a voice line
# carrying either (the claim clause above it is the contract), so a number here
# would silently drop the whole line.
FAREWELL_LINE = "made u a lil something 😘 open it when ur alone"


async def ensure_farewell_item(account_id: str) -> CatalogItem:
    """ONE reusable `CatalogItem` for gather-close sends, per account.

    Same reason as `ensure_ask_item`: `ContentOffer.item_id` is a non-nullable
    FK, and one row per account keeps "did the farewell sell" answerable.
    `enabled=False` so it never enters the ordinary manifest.
    """
    async with get_session() as s:
        row = await _singleton_item(s, account_id, "rung:gather-close")
        if row is None:
            row = CatalogItem(
                account_id=str(account_id), script_id=None, kind="image_set",
                label="gather · parting set", enabled=False,
                # COUNT-FREE, like every stored description in this module.
                description_for_ai="a small set of her photos, sent as a "
                                   "parting gift offer when the getting-to-know"
                                   "-you chat wraps up",
                price_cents=1000,
                tags=json.dumps(["rung:gather-close"]))
            s.add(row)
            await s.flush()
        return row


async def plan_farewell_delivery(account_id: str, fan_id: int,
                                 media_pool: list[int], *,
                                 price_cents: int = 1000, count: int = 3,
                                 cfg: dict | None = None
                                 ) -> tuple[Delivery | None, dict]:
    """Decide a gather-close send from an operator-picked folder's media.

    `(delivery, refusal)` — exactly one is truthy, like `plan_pack_delivery`.

    PHOTOS ONLY (the feature is "a few pictures"), solo-only, un-bought, tier
    ranked — `_available`, the same house rules as every other source. The price
    is the knob, clamped to OF's wire range; the claim comes from `_ask_claim`,
    which for a subject-free lane means the count AND what she is doing in it,
    and `audit_ask` holds the send to that count.

    ⚠️ Deliberately NOT behind `pack_send_enabled` (`_guard`): that switch arms
    the ASK lane. This lane's own switch is the folder — an operator who picked
    one turned it on, and clearing it turns it off. The language guard still
    applies: the clause is English (see PACK_LANGUAGES).
    """
    cfg = cfg or {}
    empty = PackPlan(str(account_id), int(fan_id), FAREWELL_CATEGORY, "", None,
                     [], [], 0, Claim("", 0))
    # `_account_lang` is the key `ai_chatter._load_config` actually stamps on the
    # blob ("language" is read for parity with `_guard`, which predates it).
    lang = str(cfg.get("_account_lang") or cfg.get("language")
               or "en").strip().lower()
    if lang not in PACK_LANGUAGES:
        return None, _refusal(replace(empty, refusal=REFUSE_LANGUAGE, detail=lang))
    if not media_pool:
        return None, _refusal(replace(empty, refusal=REFUSE_NO_SHELF))

    bought = await _bought_media(account_id, fan_id)
    avail = await _available(account_id, fan_id,
                             [m for m in media_pool if m not in bought],
                             company=False, media_kind="photo")
    picked = avail[:max(FAREWELL_MIN_ITEMS, int(count or 0) or 3)]
    if len(picked) < FAREWELL_MIN_ITEMS:
        return None, _refusal(replace(empty, refusal=REFUSE_TOO_THIN,
                                      detail=f"{len(picked)} un-bought photos"))

    item = await ensure_farewell_item(account_id)
    px = max(upsell.OF_PRICE_FLOOR_CENTS,
             min(int(price_cents or 0) or 1000, upsell.OF_PRICE_MAX_CENTS))
    claim = await _ask_claim(account_id, fan_id, picked, subject=None,
                             substitute=False, clause=ask_clause)
    bad = await audit_ask(account_id, picked, claim, False)
    if bad:
        log.warning("farewell audit REFUSED account=%s fan=%s: %s",
                    account_id, fan_id, "; ".join(bad))
        return None, _refusal(replace(empty, item_id=item.id, price_cents=px,
                                      claim=claim, refusal=REFUSE_AUDIT,
                                      detail="; ".join(bad)))
    plan = PackPlan(str(account_id), int(fan_id), FAREWELL_CATEGORY, "",
                    item.id, picked, [], px, claim)
    return Delivery(
        plan,
        lambda: audit_ask(account_id, picked, claim, False),
    ), {}


def gather_close_on(cfg: dict | None) -> bool:
    """Is this account's gather-close configured at all? The FOLDER is the whole
    switch — an operator who picked one turned it on, clearing it turns it off.

    Public so a caller can ask BEFORE paying for a fan lease. `send_gather_close`
    asks again as its own first line: this is a pre-filter, never the authority,
    and both read the one key so they cannot drift.
    """
    return bool(str((cfg or {}).get("gather_close_folder") or "").strip())


def due_for_gather_close(f: Fan | None, now: datetime) -> bool:
    """Has this fan's retry clock run out? Never attempted → yes.

    Reads `fans.gather_close_at` off a row the caller already loaded, so the
    common answer (no, he had one recently) costs nothing. `ai_chatter` asks this
    for every fan its payer floor turns away, on every tick.
    """
    at = getattr(f, "gather_close_at", None)
    return at is None or at <= now - timedelta(days=GATHER_CLOSE_RETRY_DAYS)


async def _stamp_attempt(account_id: str, fan_id: int, f: Fan | None,
                         now: datetime) -> None:
    """Start the retry clock. UPSERT, not UPDATE: a candidate sourced from
    `messages` may have no `Fan` row yet (the graduation builds a transient one),
    and a bare UPDATE would match 0 rows and leave the clock unarmed — which is
    the storm this column exists to stop. Mirrored onto the in-memory row so the
    caller's own sweep sees it without a re-read."""
    async with get_session() as s:
        await s.execute(
            sqlite_insert(Fan)
            .values(account_id=str(account_id), fan_id=int(fan_id),
                    gather_close_at=now, updated_at=now)
            .on_conflict_do_update(
                index_elements=["account_id", "fan_id"],
                set_={"gather_close_at": now, "updated_at": now},
            )
        )
    if f is not None:
        f.gather_close_at = now


async def send_gather_close(client, cfg: dict, account_id: str, fan_id: int,
                            f: Fan | None, *, answer_to: str = "",
                            dry_run: bool = False) -> bool:
    """The gather-close PPV: a few pictures from the operator-picked folder,
    priced, sent to a fan this lane is otherwise done talking to.

    ONE message, not two. The caption is the audited claim clause — the count
    plus what she is doing in the media, off the vault's own describe pass — and
    under it her line to him. `answer_to` is his last message: pass it and that
    line ANSWERS him (`content_resolver.answer_line`, which is handed `f` so it
    can answer him as HIM), leave it empty and it is the parting line.
    The graduation is the second case; ai_chatter's rescue is the first.

    True only when the priced send went out (or dry-ran) — every other outcome
    (no folder configured, clock not up, folder missing, pool too thin, audit
    refusal, wire error, unexpected exception) is logged and False. NEVER RAISES,
    so no caller's graduation or sweep can hinge on a send.

    The folder knob doubles as the switch: empty → the feature is off and this
    returns immediately, before the clock is even read.

    🚨 THE CLOCK IS STAMPED BEFORE THE ATTEMPT, not after a successful one. The
    expensive half (a vault read, `_available`'s sweep, a claim LLM call) runs
    before anything can be known about the outcome, so a refusal that stamped
    nothing would replay in full on the next tick, and the next — every ~30s for
    as long as the fan stayed unanswered. Stamping first also means a crash
    mid-send cannot re-arm that loop.
    """
    if not gather_close_on(cfg):
        return False
    folder = str(cfg["gather_close_folder"]).strip()
    now = datetime.utcnow()
    if not due_for_gather_close(f, now):
        return False
    await _stamp_attempt(account_id, fan_id, f, now)
    try:
        pool = await asyncio.to_thread(_vault_pick.folder_media_pool, client, folder)
        if pool is None:
            log.info("gather-close folder not found account=%s name=%r",
                     account_id, folder)
            return False
        d, refused = await plan_farewell_delivery(
            account_id, fan_id, pool,
            price_cents=int(cfg.get("gather_close_price_cents") or 1000),
            count=int(cfg.get("gather_close_count") or 3),
            cfg=cfg)
        if d is None:
            log.info("gather-close refused account=%s fan=%s: %s %s",
                     account_id, fan_id, refused.get("reason"),
                     refused.get("detail") or "")
            return False
        # Written AFTER the plan, so a refusal never pays for a line nobody
        # reads — and after the stamp, so it can never become a per-tick call.
        # `voice_line_ok` is the guarantee the prompt's own rules are only a
        # request: a line carrying a digit or a media word would state a second
        # claim under the audited one, and `compose_caption` would drop it
        # SILENTLY, leaving him a bare clause and no sentence at all.
        line = await content_resolver.answer_line(
            account_id, fan_id, answer_to, f)
        res = await deliver(client, d,
                            voice_line=line if voice_line_ok(line) else FAREWELL_LINE,
                            dry_run=dry_run)
        return str(res.get("status")) in ("ok", "dry_run")
    except Exception:
        log.warning("gather-close failed account=%s fan=%s",
                    account_id, fan_id, exc_info=True)
        return False
