"""service/vault_catalog_copy.py — write NEW catalog lines from what is in the
vault.

`vault_catalog_seed.DEFAULT_RUNGS` offers 14 captions, and they are the same 14
for every account: proven, free, and a hard ceiling. Nothing in the product
could ever produce a 15th idea, or a line that sounds like THIS creator rather
than the shipped default.

This writes lines from the vault's own vision facts. It groups the media no
catalog row is selling yet, hands each group's facts to the copy model, and
returns caption rows in the same shape as `vault_catalog_seed.suggest_texts` —
so they land in the same pick-list, get filled by the same "Fill content", and
are saved by the same button. Nothing here writes to the catalog.

Three things keep a generated line honest:

  1. **It is grounded in `vault_ai_brief`, not in the label.** `SELLING_BRIEF`
     is handed the facts a vision model actually recorded and forbids promising
     anything absent from them — no penetration unless PENETRATION is set, no
     "watch me finish" on build-up. A vaguer true line outsells a specific false
     one, because the false one is a refund.
  2. **A line is only offered for media that EXISTS and is unsold.** Groups are
     built from the unbound pool, so every generated caption is fillable by
     construction — the opposite failure from the shipped defaults, where
     "Collab scene" describes content this creator has never filmed.
  3. **Price comes from the content, not the model.** The LLM never picks a
     number; `vault_catalog_seed.price_floor` does, off the same operator floors
     (toys/acts $100, explicit video $50, nude image $25).

Read-only. Costs one cheap LLM call per group, through `llm_client` so it is
capped and audited like every other production call.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

import llm_client
import vault_ai_brief
import vault_catalog_seed
from llm_client import LLMCapExceeded, LLMError

log = logging.getLogger("of-relay.vault_catalog_copy")

# House default. Text-only, so the vision model is not involved.
DEFAULT_MODEL = "deepseek-v4-flash"

# One call per group; a vault with 40 outfits would otherwise be a bill. The
# groups are ranked by size first, so the cap keeps the BIGGEST sellable
# clusters rather than an arbitrary slice.
MAX_GROUPS = 8

# A group smaller than this cannot carry its own catalog row.
MIN_GROUP = 2

_SCHEMA = ('{"label": "<2-4 words, operator-facing, e.g. \'Black lace fingering\'>", '
           '"caption": "<ONE line she would DM him, first person, no quotes>"}')


def _group_key(row: dict[str, Any]) -> tuple[str, str, bool] | None:
    """(outfit, tier, is_payoff) — the axes that decide what a line may claim.

    Outfit is the colour+material family (`vault_catalog_seed._garment_tokens`),
    which is what makes a set read as one shoot; tier and payoff decide what the
    copy is allowed to promise at all, so mixing them inside one caption would
    force the line to lie about half its own media.
    """
    colours, toks = vault_catalog_seed._garment_tokens(row)
    sell = row["sell"]
    if not colours:
        # No colour named. Lace alone is too weak to anchor a claim on, but it
        # still separates "some lace thing" from genuinely unlabelled media.
        return ("lace", sell["tier"], bool(sell["stands_alone"])) if "lace" in toks else None

    # PRIMARY colour only — material and secondary colours are deliberately
    # dropped. Keying on colour+material split pink into "pink" and "pink lace",
    # and the graded vault ended up with three near-identical $25 rows ("Pink lace
    # tease", "Pink tease buildup", "Pink tease build-up") competing for one
    # pool: the first two took every pink still and the third was left with
    # nothing to fill. One outfit should produce one line.
    return (sorted(colours)[0], sell["tier"], bool(sell["stands_alone"]))


def group_unsold(rows: list[dict[str, Any]], bound: set[int]
                 ) -> list[dict[str, Any]]:
    """Sellable clusters of media no catalog row owns, biggest first."""
    buckets: dict[tuple, list[dict[str, Any]]] = {}
    for r in rows:
        if r["media_id"] in bound:
            continue
        key = _group_key(r)
        if key is None:
            continue
        buckets.setdefault(key, []).append(r)

    out = []
    for (outfit, tier, payoff), items in buckets.items():
        if len(items) < MIN_GROUP:
            continue
        items.sort(key=vault_catalog_seed._tameness, reverse=True)
        out.append({
            "outfit": outfit, "tier": tier, "payoff": payoff, "items": items,
            "kind": ("image_set" if sum(1 for i in items if i["kind"] == "photo")
                     >= len(items) / 2 else "video"),
        })
    out.sort(key=lambda g: -len(g["items"]))
    return out


def group_brief(group: dict[str, Any]) -> str:
    """The facts block for one group.

    Built from its STRONGEST item (the one the caption is really selling) plus a
    line describing the rest, so the model writes about a real piece instead of
    an average of several.
    """
    lead = group["items"][0]
    facts = vault_ai_brief.item_facts(
        lead["fields"], description=lead["text"],
        duration_seconds=lead["duration"], kind=lead["kind"])
    photos = sum(1 for i in group["items"] if i["kind"] == "photo")
    videos = len(group["items"]) - photos
    return (
        f"{facts}\n\n"
        f"THIS IS A SET: {photos} photo(s) and {videos} video(s) from the same "
        f"{group['outfit']} shoot, sold as ONE bundle. The facts above are its "
        f"strongest piece; the rest are the same outfit and no more explicit.\n\n"
        f"{vault_ai_brief.SELLING_BRIEF}\n\n"
        f"Return STRICT JSON only, no markdown:\n{_SCHEMA}\n"
        "The caption is what she DMs him — one line, her voice, lowercase is "
        "fine, at most one emoji. Do not name a price. Do not mention how many "
        "photos or videos there are."
    )


def _parse(text: str) -> dict[str, Any]:
    m = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not m:
        return {}
    try:
        out = json.loads(m.group(0))
    except (TypeError, ValueError):
        return {}
    return out if isinstance(out, dict) else {}


async def generate_lines(account_id: str, *, limit: int = MAX_GROUPS,
                         model: str = DEFAULT_MODEL) -> dict[str, Any]:
    """Caption rows written from the vault's unsold media. Read-only.

    Returns the same row shape as `vault_catalog_seed.suggest_texts` so the
    pick-list, the filler and Save all work on it unchanged. `media_ids` rides
    along purely so the UI can show what a line was written FROM — the row is
    still added empty and filled by "Fill content", which re-derives the match
    from the text like any other row.
    """
    rows = await vault_catalog_seed._load_rows(account_id)
    v2 = [r for r in rows if vault_catalog_seed._is_v2(r["fields"])]
    if rows and not v2:
        return {"account_id": account_id, "proposals": [], "blocked": (
            "this vault has no V2 describe fields yet — there are no facts to "
            "write a line from. Run Describe (V2) first."), "summary": {}}

    bound = await vault_catalog_seed._bound_media(account_id)
    groups = group_unsold(rows, bound)[:max(1, int(limit))]
    if not groups:
        return {"account_id": account_id, "proposals": [], "blocked": (
            "every described item is already sold by a catalog row — nothing "
            "unsold left to write a line about."), "summary": {}}

    proposals: list[dict[str, Any]] = []
    errors: list[str] = []
    spent = 0
    for g in groups:
        try:
            res = await llm_client.chat(
                model=model,
                messages=[{"role": "user", "content": group_brief(g)}],
                purpose="catalog_copy", account_id=account_id, temperature=0.8)
        except LLMCapExceeded as e:
            errors.append(f"daily LLM cap reached after {len(proposals)} line(s)")
            log.warning("catalog copy capped account=%s: %s", account_id, e)
            break
        except LLMError as e:                       # noqa: BLE001
            errors.append(str(e)[:120])
            continue
        spent += int(res.cost_cents or 0)
        data = _parse(res.content)
        caption = str(data.get("caption") or "").strip().strip('"')
        label = str(data.get("label") or "").strip().strip('"')
        if not caption:
            errors.append("model returned no caption")
            continue
        items = g["items"]
        proposals.append({
            "label": label or f"{g['outfit']} set".strip().title(),
            "description_for_ai": caption,
            "kind": g["kind"],
            "price_cents": max(vault_catalog_seed.price_floor(items),
                               2500 if g["kind"] == "image_set" else 5000),
            "floor_cents": vault_catalog_seed.price_floor(items),
            "fillable": len(items),
            "needs": MIN_GROUP,
            "media_ids": [i["media_id"] for i in items],
            "source": (f"{g['outfit']} · {g['tier']}"
                       + (" · payoff" if g["payoff"] else " · build-up")),
        })

    # The pick-list keys a checkbox on the label, so two groups that happen to
    # get the same name from the model would share one tick. Disambiguate with
    # the group they came from rather than a bare counter.
    seen: dict[str, int] = {}
    for p in proposals:
        base = p["label"]
        seen[base] = seen.get(base, 0) + 1
        if seen[base] > 1:
            p["label"] = f"{base} ({p['source'].split(' · ')[0]})"

    proposals.sort(key=lambda p: p["price_cents"])
    return {
        "account_id": account_id,
        "proposals": proposals,
        "blocked": "",
        "shoot": [],
        "summary": {
            "groups": len(groups),
            "generated": len(proposals),
            "errors": len(errors),
            "cost_millicents": spent,
            "unbound_media": len(rows) - len(bound),
        },
        "errors": errors,
    }
