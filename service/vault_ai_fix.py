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


# ── The grading loop ────────────────────────────────────────────────
#
# The flags pass is a vision model guessing, and it has been wrong in a
# different direction after every prompt change: over-reporting exposure, then
# under-reporting it, then calling a fully-exposed still "not in frame". Each
# reversal was invisible to every automatic check and obvious to the operator
# in about four seconds of looking.
#
# So looking is part of the product, not part of the test harness. The operator
# is shown items pre-set to what the model believes and taps only what is
# wrong. Two things are then recorded, and BOTH matter:
#
#   * a correction   → written through `apply_fix`, locked, permanent. The data
#                      is right from then on no matter what the model does.
#   * a confirmation → the model's answer, stamped with the prompt version that
#                      produced it. This is the half that is easy to skip and
#                      the half that makes measurement possible: without it,
#                      "left alone" and "never looked at" are the same record,
#                      and an accuracy computed over them reads 100%.
#
# Accumulated, these turn model quality into a number the operator can see, per
# prompt version, on their own vault — instead of a claim made in a chat window.
GRADE_KEY = "_graded"


def grade_of(fields: dict[str, Any] | None) -> dict[str, Any]:
    """The operator's recorded verdict for this item, or {}."""
    if not isinstance(fields, dict):
        return {}
    g = fields.get(GRADE_KEY)
    return g if isinstance(g, dict) else {}


def _priority(fields: dict[str, Any], lanes: list[str], version: int) -> tuple:
    """Grading order. Never-graded first, then whatever costs most to get wrong.

    Two rounds of tuning were scored entirely on items that had already been
    graded, and a regression sat in the ungraded remainder through both without
    moving any total. Fresh items are where a real number comes from, so they
    lead — and within them, the ones that gate money or a mass send.
    """
    g = grade_of(fields)
    graded_this_version = int(g.get("v") or 0) >= version
    paid = any(ln in ("tease 10", "tease 50", "safe explicit") for ln in lanes)
    return (graded_this_version, not vault_ai_brief.contradictions(fields),
            not paid, not vault_ai_brief.any_bare(fields))


async def review_queue(account_id: str, *, limit: int = 60,
                       version: int = 0) -> dict[str, Any]:
    """Items for the operator to grade, most valuable first."""
    import vault_scripts

    async with get_session() as s:
        rows = (await s.execute(
            select(VaultItem.media_id, VaultItem.kind, VaultItem.ai_fields_json,
                   VaultItem.locked_fields_json)
            .where(VaultItem.account_id == account_id,
                   VaultItem.removed_at.is_(None),
                   VaultItem.kind.in_(("photo", "video"))))).all()

    plan = await vault_scripts.plan_ai_folders(account_id, keep=2)
    lanes: dict[int, list[str]] = {}
    for fold in plan["folders"]:
        for it in fold["items"]:
            lanes.setdefault(int(it["media_id"]), []).append(
                fold["name"].removeprefix("AI-"))

    out: list[dict[str, Any]] = []
    graded = 0
    for mid, kind, fj, lj in rows:
        fields = vault_ai_brief.load_fields(fj)
        if not vault_ai_brief.flags_known(fields):
            continue
        g = grade_of(fields)
        if int(g.get("v") or 0) >= version:
            graded += 1
        try:
            locked = set(json.loads(lj) if isinstance(lj, str) else (lj or []))
        except (TypeError, ValueError):
            locked = set()
        out.append({
            "media_id": int(mid), "kind": kind,
            "lanes": lanes.get(int(mid), []),
            "regions": {r: vault_ai_brief.vis(fields, r)
                        for r in vault_ai_brief.VIS_REGIONS},
            "over": {r: vault_ai_brief.garment_over(fields, r)
                     for r in vault_ai_brief.OVER_KEYS},
            "description": str(fields.get("description") or "")[:280],
            "clothing_state": _s(fields.get("clothing_state")),
            "explicitness": _s(fields.get("explicitness")),
            "codes": vault_ai_brief.contradictions(fields),
            "locked": sorted(locked & set(vault_ai_brief.VIS_REGIONS)),
            "graded": g,
            "_sort": _priority(fields, lanes.get(int(mid), []), version),
        })

    out.sort(key=lambda r: r["_sort"])
    for r in out:
        r.pop("_sort")
    return {"account_id": account_id, "prompt_version": version,
            "total": len(out), "graded": graded,
            "items": out[:max(1, int(limit))]}


async def record_grade(account_id: str, media_id: int, *,
                       corrections: dict[str, Any] | None = None,
                       version: int = 0, note: str = "") -> dict[str, Any]:
    """Record one operator verdict: corrections locked, the rest confirmed.

    Confirming is not a no-op. It says the operator looked at this answer, at
    this prompt version, and agreed — which is exactly the evidence
    `flags_accuracy` needs and exactly what an un-recorded "left it alone"
    cannot supply.
    """
    corrections = {k: v for k, v in (corrections or {}).items() if k in FIXABLE}
    applied: dict[str, Any] = {}
    if corrections:
        res = await apply_fix(account_id, media_id, corrections)
        if not res.get("ok"):
            return res
        applied = res["applied"]

    async with get_session() as s:
        item = await s.get(VaultItem, (account_id, media_id))
        if item is None:
            return {"ok": False, "error": "not_in_mirror", "media_id": media_id}
        fields = _fields(item)
        # The CONFIRMED answers are everything the operator did not change —
        # snapshotted, because the stored value will move on the next re-run
        # and then this record could no longer say what was agreed to.
        confirmed = {r: vault_ai_brief.vis(fields, r)
                     for r in vault_ai_brief.VIS_REGIONS
                     if r not in applied}
        fields[GRADE_KEY] = {"v": int(version), "confirmed": confirmed,
                             "corrected": sorted(applied), **({"note": note[:200]}
                                                              if note.strip() else {})}
        await s.execute(
            update(VaultItem)
            .where(VaultItem.account_id == account_id, VaultItem.media_id == media_id)
            .values(ai_fields_json=json.dumps(fields, ensure_ascii=False, default=str)))
        await s.commit()

    return {"ok": True, "media_id": media_id, "corrected": sorted(applied),
            "confirmed": sorted(confirmed)}


async def flags_accuracy(account_id: str, version: int = 0) -> dict[str, Any]:
    """How often the model agrees with the operator, per region.

    Counted ONLY over graded items, and only those graded at `version`: a score
    that mixes prompt versions measures nothing, and a score that treats
    ungraded items as correct is how a 100% gets reported on a vault nobody has
    checked. Corrections that are now locked are counted as the misses they
    were, from the grade record, not from the (since-repaired) stored value.
    """
    async with get_session() as s:
        rows = (await s.execute(
            select(VaultItem.media_id, VaultItem.ai_fields_json)
            .where(VaultItem.account_id == account_id,
                   VaultItem.removed_at.is_(None)))).all()

    per = {r: {"ok": 0, "n": 0} for r in vault_ai_brief.VIS_REGIONS}
    graded = stale = 0
    for _mid, fj in rows:
        fields = vault_ai_brief.load_fields(fj)
        g = grade_of(fields)
        if not g:
            continue
        if int(g.get("v") or 0) != int(version):
            stale += 1
            continue
        graded += 1
        corrected = set(g.get("corrected") or [])
        for region in vault_ai_brief.VIS_REGIONS:
            if region in corrected:
                per[region]["n"] += 1          # the model was wrong here
            elif region in (g.get("confirmed") or {}):
                per[region]["n"] += 1
                per[region]["ok"] += 1
    tot_ok = sum(v["ok"] for v in per.values())
    tot_n = sum(v["n"] for v in per.values())
    return {"account_id": account_id, "prompt_version": version,
            "graded_items": graded, "graded_other_versions": stale,
            "per_region": per,
            "accuracy": round(tot_ok / tot_n, 4) if tot_n else None,
            "answers": tot_n}
