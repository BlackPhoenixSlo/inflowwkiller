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
that way: `pack_sender` re-exporting this would make the pair a cycle. The single
caller (`welcome_chatter_for_info`) imports this module directly.
"""
from __future__ import annotations

import json
import logging
from dataclasses import replace

from db.engine import get_session
from db.models import CatalogItem

from . import upsell
from .pack_audit import audit_ask
from .pack_claim import Claim, ask_clause
from .pack_sender import (
    PACK_LANGUAGES, REFUSE_AUDIT, REFUSE_LANGUAGE, REFUSE_NO_SHELF,
    REFUSE_TOO_THIN, Delivery, PackPlan, _ask_claim, _available, _bought_media,
    _refusal, _singleton_item,
)

log = logging.getLogger("of-relay.automation.pack_farewell")

FAREWELL_CATEGORY = "gather_close"

# Below 2 pictures a fixed price buys "a lot of money for few items" — the
# 2026-07-31 shape. The claim states the exact count either way; this floor is
# about the value shape, not honesty. A configured count below it is clamped UP
# (and the config validator holds the knob to ≥2), so the only TOO_THIN refusal
# left is a genuinely thin POOL.
FAREWELL_MIN_ITEMS = 2


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
