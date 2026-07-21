"""service/vault_ai_fix.py — let the operator settle a describe/flags dispute.

Two vision passes look at the same picture. `describe` writes prose plus
`clothing_state`; the cheap `flags` pass writes, per region, whether she is
bare / covered / not in frame, and NAMES what is covering her. Where they
contradict each other, one of them is provably wrong — and which one differs
item by item. On AriaFree, all ten current disputes are `clothing_state:
fully_nude` on a still where the flags name a thong or a bodysuit, and the
flags are right; the reverse also happens.

Nothing here guesses. `list_disputes` renders BOTH readings and the two
resolutions that would settle it; `apply_fix` writes whichever the operator
picked. Automating the choice was the temptation and is exactly wrong — the
whole reason this file exists is that a confident wrong call about what is in a
picture is what sells a fan something that is not there.

DURABILITY. A fix is written three ways, because a correction that a re-run
silently reverts is worse than no correction — the operator would have no
reason to look again:

  * into `ai_fields_json`, so every existing reader (`vault_scripts` lanes,
    `vault_catalog_seed` pricing, the copy brief) sees it with no changes;
  * into `operator_overrides_json`, the documented override tier;
  * into `locked_fields_json`, which `_flags_one` / `_describe_one` check
    before writing, so a forced re-run refreshes everything EXCEPT this.

That is the same override+lock contract `vault_ai_effective` already defines
for `description` and `tags`; this only extends it to the exposure fields.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import select, update

import vault_ai_brief
from db.engine import get_session
from db.models import VaultItem

log = logging.getLogger("of-relay.vault_ai_fix")

# The exposure fields an operator may correct. Deliberately narrow: these are
# the ones the lanes and the price tiers read, and the ones the two passes can
# contradict each other about. Everything else stays with the existing editor.
FIXABLE: tuple[str, ...] = (
    "clothing_state", "explicitness",
    *vault_ai_brief.VIS_REGIONS,
    *vault_ai_brief.OVER_KEYS.values(),
    "underwear_visible",
)

# `clothing_state` values, low → high reveal. Used to PROPOSE a value when the
# operator says the flags were right; never applied without them choosing it.
_CLOTHING_FOR = {
    # (breasts bare?, vulva bare?) → the state that matches what is on show
    (False, False): "lingerie_on",
    (True, False): "pulled_down",     # topless, still covered below
    (False, True): "pulled_aside",    # covered above, exposed below
    (True, True): "partially_off",    # both out, something still on
}


def _s(v: Any) -> str:
    return str(v or "").strip().lower()


def _fields(item: VaultItem) -> dict[str, Any]:
    return vault_ai_brief.load_fields(item.ai_fields_json)


def _locked(item: VaultItem) -> set[str]:
    raw = item.locked_fields_json
    try:
        got = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return set()
    return set(got) if isinstance(got, (list, tuple, set)) else set()


def propose(fields: dict[str, Any], codes: list[str]) -> dict[str, dict[str, Any]]:
    """The two ways this dispute could be settled, as literal field writes.

    Returned as `{"flags": {...}, "describe": {...}}` — "flags" meaning "the
    flags pass saw it right, correct the describe fields to match", and
    "describe" the reverse. Either may come back empty when that side has
    nothing to change, which is itself informative: it means only one reading
    is actually available and the operator is confirming rather than choosing.
    """
    bare_b = vault_ai_brief.is_bare(fields, "breasts_vis")
    bare_v = vault_ai_brief.is_bare(fields, "vulva_vis")
    trust_flags: dict[str, Any] = {}
    trust_describe: dict[str, Any] = {}

    if "nude_but_clothed" in codes:
        # Flags right → she is not nude, so `fully_nude` is wrong.
        trust_flags["clothing_state"] = _CLOTHING_FOR[(bare_b, bare_v)]
        # Describe right → she IS nude, so there is no garment on her. Drop the
        # named garments and open whatever they were covering.
        for region, key in vault_ai_brief.OVER_KEYS.items():
            if vault_ai_brief.garment_over(fields, region):
                trust_describe[key] = None
                if vault_ai_brief.vis(fields, region) == "covered":
                    trust_describe[region] = "bare"
        trust_describe["underwear_visible"] = False

    if "lingerie_but_nothing_on" in codes:
        trust_flags["clothing_state"] = "fully_nude" if not (
            bare_b or bare_v) else _CLOTHING_FOR[(bare_b, bare_v)]
        trust_describe["underwear_visible"] = True

    if "dressed_but_bare" in codes or "sfw_but_bare" in codes:
        # Flags right → she is not dressed / not sfw.
        trust_flags["clothing_state"] = _CLOTHING_FOR[(bare_b, bare_v)]
        if "sfw_but_bare" in codes:
            trust_flags["explicitness"] = "suggestive"
        # Describe right → nothing is actually bare; it is covered.
        for region in vault_ai_brief.VIS_REGIONS:
            if vault_ai_brief.is_bare(fields, region):
                trust_describe[region] = "covered"

    if "penetration_offscreen" in codes:
        # Flags right → nothing genital is in shot, so the recorded
        # penetration cannot be visible in this frame.
        trust_flags["penetration"] = "none"
        trust_describe["vulva_vis"] = "bare"

    return {"flags": trust_flags, "describe": trust_describe}


async def list_disputes(account_id: str) -> dict[str, Any]:
    """Every item whose two passes contradict each other, with both readings.

    Read-only. Ordered oldest-first so the operator works a stable list rather
    than a set that reshuffles as they fix it.
    """
    async with get_session() as s:
        rows = (await s.execute(
            select(VaultItem.media_id, VaultItem.kind, VaultItem.ai_fields_json,
                   VaultItem.locked_fields_json, VaultItem.created_at)
            .where(VaultItem.account_id == account_id,
                   VaultItem.removed_at.is_(None),
                   VaultItem.kind.in_(("photo", "video")))
            .order_by(VaultItem.created_at.asc()))).all()

    out: list[dict[str, Any]] = []
    checked = 0
    for mid, kind, fj, lj, _ in rows:
        fields = vault_ai_brief.load_fields(fj)
        if not vault_ai_brief.flags_known(fields):
            continue
        checked += 1
        codes = vault_ai_brief.contradictions(fields)
        if not codes:
            continue
        try:
            locked = set(json.loads(lj) if isinstance(lj, str) else (lj or []))
        except (TypeError, ValueError):
            locked = set()
        out.append({
            "media_id": int(mid),
            "kind": kind,
            "codes": codes,
            "reasons": [vault_ai_brief.CONTRADICTIONS.get(c, c) for c in codes],
            "description": str(fields.get("description") or "")[:400],
            "clothing_state": _s(fields.get("clothing_state")),
            "explicitness": _s(fields.get("explicitness")),
            "underwear_visible": fields.get("underwear_visible"),
            "regions": {r: vault_ai_brief.vis(fields, r)
                        for r in vault_ai_brief.VIS_REGIONS},
            "over": {r: vault_ai_brief.garment_over(fields, r)
                     for r in vault_ai_brief.OVER_KEYS},
            "resolved": sorted(locked & set(FIXABLE)),
            "propose": propose(fields, codes),
        })
    return {"account_id": account_id, "checked": checked,
            "disputes": out, "count": len(out)}


async def apply_fix(account_id: str, media_id: int,
                    values: dict[str, Any]) -> dict[str, Any]:
    """Write one operator correction, locked against future re-runs.

    `values` is the literal field→value map to apply — normally one of the two
    `propose()` branches, optionally hand-edited. Unknown keys are dropped
    rather than rejected, so a client sending a whole item back cannot
    accidentally rewrite a field this endpoint was never meant to own.
    """
    edits = {k: v for k, v in (values or {}).items() if k in FIXABLE}
    if not edits:
        return {"ok": False, "error": "no_fixable_fields"}

    bad = [k for k, v in edits.items()
           if k in vault_ai_brief.VIS_REGIONS and v not in vault_ai_brief.VIS_STATES]
    if bad:
        return {"ok": False, "error": "bad_region_state", "fields": bad}

    async with get_session() as s:
        item = await s.get(VaultItem, (account_id, media_id))
        if item is None:
            return {"ok": False, "error": "not_in_mirror", "media_id": media_id}

        fields = _fields(item)
        overrides = vault_ai_brief.load_fields(item.operator_overrides_json)
        locked = _locked(item)

        for key, val in edits.items():
            if val is None:
                fields.pop(key, None)
            else:
                fields[key] = val
            overrides[key] = val
            locked.add(key)

        await s.execute(
            update(VaultItem)
            .where(VaultItem.account_id == account_id, VaultItem.media_id == media_id)
            .values(ai_fields_json=json.dumps(fields, ensure_ascii=False, default=str),
                    operator_overrides_json=json.dumps(overrides, ensure_ascii=False,
                                                       default=str),
                    locked_fields_json=json.dumps(sorted(locked))))
        await s.commit()

    log.info("vault_ai_fix account=%s media=%s fixed=%s",
             account_id, media_id, sorted(edits))
    return {"ok": True, "media_id": media_id, "applied": edits,
            "locked": sorted(locked),
            "still_disagrees": vault_ai_brief.contradictions(fields)}
