"""
fan_state.py — `fans.custom_fields`, the shared per-fan state blob.

One JSON column holds every automation's per-fan scratch state, namespaced by a
leading underscore: `_hot_teaser`, `_typo_fix`, `_image_reply`, `_tip_request`,
`_bot_dare`. Five modules used to hand-roll the same read-modify-write against it
— an identical `_load_custom_fields` was copy-pasted verbatim into tip_reward and
tip_request, and ai_chatter grew a fresh pair per feature.

That duplication is not just noise: every writer has to read the WHOLE blob, edit
its own key and write the whole blob back, so a writer that gets the cycle subtly
wrong clobbers a sibling automation's state. Owning it here makes that cycle exist
once. (Same hazard already documented for `ai_fields_json` on the vault side.)

Why its OWN module and not `_common`: this is STORAGE, and it is the only thing
in here. `_common` is 2.4k lines spanning skip-lists, prompt blocks, style layers,
typo injection, nickname push and text normalisation, and it imports `llm_client`
plus four models to do them — so `tip_request` reaching for one JSON column had to
pull all of that in. Same reasoning that produced `jsonsafe` and `vision` one layer
down: a leaf should be reachable without importing a grab-bag to get at it.

Deps are deliberately the minimum that a DB accessor needs — the session factory
and the `Fan` model, nothing else. Keep it that way: prompts, policy and "when do
we stamp" belong to the automation that owns the feature.
"""
from __future__ import annotations

import json

from sqlalchemy import select

from db.engine import get_session
from db.models import Fan
from jsonsafe import load_dict


def fan_state(fan: Fan | None, key: str) -> dict:
    """One namespaced slot out of a fan's blob — `{}` when absent or unparseable.

    PURE, and takes the already-loaded row on purpose: callers in a sweep loop
    have the `Fan` in hand, so asking the DB again per fan is a round trip for a
    field that is already in memory.

    Type-guarded, not just `.get(key) or {}`: a slot that somehow holds a string
    or a list is corruption from an older writer, and every caller wants "treat it
    as unset" rather than an AttributeError three lines later."""
    value = _fan_state_all(fan).get(key)
    return value if isinstance(value, dict) else {}


def _fan_state_all(fan: Fan | None) -> dict:
    """The whole blob as a dict, `{}` on absent/NULL/corrupt JSON.

    Private on purpose: reading the raw blob is how a caller ends up hand-rolling
    the merge again. The three public entry points above/below cover every real
    use — read one slot, write one slot in your transaction, write one slot in its
    own.

    `load_dict` rather than a local try/except: "parse a JSON TEXT column, `{}` on
    anything else" is one rule with one home, and this module had quietly become
    its second implementation — with a narrower `except` than the canonical one,
    which is exactly the drift `jsonsafe` was written to end."""
    return load_dict(fan.custom_fields) if fan else {}


def put_fan_state(fan: Fan, key: str, value: dict | None) -> None:
    """Write (or, on `value=None`, clear) ONE slot on an ALREADY-LOADED row, inside
    the CALLER's session. Does not commit — the caller's transaction owns that.

    This exists so a writer that must be ATOMIC with other work can still reuse the
    read-modify-write. Several senders stamp a per-fan cooldown in the same
    transaction that inserts the `VaultSend` rows for what they just sent; splitting
    that into two transactions could leave a fan credited with a send that has no
    cooldown, so those sites cannot call `set_fan_state` below. They shouldn't have
    to hand-roll the blob merge either — that's the duplication this module exists
    to end."""
    cf = _fan_state_all(fan)
    if value is None:
        cf.pop(key, None)
    else:
        cf[key] = value
    fan.custom_fields = json.dumps(cf)


async def set_fan_state(account_id: str, fan_id: int, key: str,
                        value: dict | None) -> None:
    """`put_fan_state` in its own transaction, for a STANDALONE stamp.

    Re-reads the blob inside the session so a sibling automation's key, written
    since the caller loaded its own `Fan`, survives. Silently no-ops on a missing
    fan — a stamp is never worth raising into an automation's send path."""
    async with get_session() as s:
        fan = (await s.execute(select(Fan).where(
            Fan.account_id == str(account_id),
            Fan.fan_id == int(fan_id)))).scalar_one_or_none()
        if fan is None:
            return
        put_fan_state(fan, key, value)
        await s.commit()
