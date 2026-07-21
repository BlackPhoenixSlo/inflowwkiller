"""service/vault_ai_service.py — vault-AI producers (folder / tip / story).

Pure, side-effect-scoped module: reads only DB rows (through
`vault_ai_effective.effective_value` — never a raw column), writes only to
`vault_items` scores (respecting `locked_fields_json`) and to
`vault_ai_review_items` as pending proposals. ZERO OF calls, ZERO sends, ZERO
LLM calls — those layers happen elsewhere (describe upstream, consumers
downstream). Suggest-only per plans/VAULT_AI_ACTIONS_CONTRACT.md §2: every
ACTION is a review row, nothing hits OnlyFans until an operator approves and a
consumer applies it.

Public entry points:

    propose_folders(account_id, *, config=None)  → list[int] of new review-item ids
        Cluster described items by tag → emit `kind="folder"` review items
        (contract payload shape). Deduped against pending folder proposals
        already awaiting review for the same folder_key.

    score_tip_vault(account_id, *, config=None)  → int updated
        Compute + persist `vault_items.tip_vault_score` (0-100) for described
        items whose score isn't operator-locked.

    score_story(account_id, *, config=None)      → int updated
        Compute + persist `vault_items.story_score` (0-100) and
        `story_suitable` (bool) for described items whose scores aren't locked.

    run_all(account_id, *, config=None)          → dict counts
        Convenience runner for the three producers above.

All three take a pre-parsed `config` dict or load one from
`AccountAiConfig.vault_ai_config_json` if omitted.
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Iterable

from sqlalchemy import select, update

from db.engine import get_session
from db.models import AccountAiConfig, VaultAiReviewItem, VaultItem
from vault_ai_effective import effective_value, is_locked
import vault_ai_baseline as vab

log = logging.getLogger("of-relay.vault_ai_service")


# ── Tag vocabularies (kept small + literal — describe prompt writes lowercase
# words from a fixed vocabulary, so exact-match set membership is sufficient).

_SELLABLE_TAGS = {
    "nude", "naked", "topless", "lingerie", "ass", "booty", "boobs", "tits",
    "pussy", "toy", "dildo", "vibrator", "masturbation", "fingering", "sex",
    "blowjob", "cum", "tease", "shower", "bath", "feet", "anal", "squirt",
    "creampie", "riding",
}
_STORY_FRIENDLY_TAGS = {
    "face", "portrait", "selfie", "mirror", "closeup", "outfit", "bikini",
    "dress", "beach", "travel", "flowers", "gym", "pov", "tease",
}
_STORY_HOSTILE_TAGS = {
    "sex", "blowjob", "cum", "pussy", "penetration", "fingering",
    "masturbation", "anal", "dildo", "squirt", "creampie", "riding",
}

# Tier keys come from the describe prompt: sfw|suggestive|explicit|hardcore.
# `safe`/`graphic`/`unknown` are contract-side synonyms (VAULT_AI_ACTIONS §1),
# accepted so an operator config that uses either wording still resolves.
_TIER_TIP_BASE = {
    "sfw": 10, "safe": 10,
    "suggestive": 30,
    "explicit": 65,
    "hardcore": 90, "graphic": 90,
    "unknown": 25,
}
_TIER_STORY_BASE = {
    "sfw": 85, "safe": 85,
    "suggestive": 65,
    "explicit": 25,
    "hardcore": 5, "graphic": 5,
    "unknown": 50,
}

# Tags with almost no discriminative value → skipped as folder cluster keys
# (would produce a "Selfie" folder of half the vault).
_TAG_STOP_WORDS = {"solo", "closeup", "pov"}

# Folder proposer knobs (contract keeps operator-tunable ones in config —
# these are engine limits so a single sweep can't flood the review tab).
_DEFAULT_MIN_GROUP_SIZE = 3
_DEFAULT_MAX_FOLDERS_PER_RUN = 20
_STORY_SUITABLE_THRESHOLD = 55
_VIDEO_STORY_CAP = 40  # OF stories are short still-images-friendly; cap videos


# ── Config loading ────────────────────────────────────────────────────

async def _load_config(account_id: str, cfg: dict | None) -> dict:
    """Return the effective vault-AI config dict (dict-passthrough or DB load).

    None ⇒ fetch AccountAiConfig.vault_ai_config_json and parse. Missing/absent
    ⇒ empty dict (callers use `.get(..., default)` throughout so absence is safe;
    the master `enabled` gate is a UI/consumer concern, not a producer one —
    describers already refuse to run when it's off)."""
    if cfg is not None:
        return cfg
    async with get_session() as s:
        row = await s.get(AccountAiConfig, account_id)
    if row is None or not row.vault_ai_config_json:
        return {}
    try:
        parsed = json.loads(row.vault_ai_config_json)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


# ── Field readers (effective_value is the ONE read path) ──────────────

def _effective_tags(item: VaultItem, config: dict) -> list[str]:
    """Effective tag list for an item. `vault_items.tags` is a JSON string on
    disk, but an operator override or `ai_fields_json['tags']` returns a list
    directly — normalize both shapes to a lowercased, deduped, stripped list."""
    val: Any = effective_value(item, "tags", config, default=[])
    if isinstance(val, str):
        try:
            val = json.loads(val)
        except (TypeError, ValueError):
            return []
    if not isinstance(val, (list, tuple)):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for t in val:
        s = str(t).strip().lower()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def _effective_tier(item: VaultItem, config: dict) -> str:
    """Effective explicitness tier as a lowercase key (empty string when unset)."""
    val = effective_value(item, "explicitness_tier", config, default="")
    return str(val or "").strip().lower()


# ── Score computation ────────────────────────────────────────────────

def _clamp(n: int) -> int:
    return max(0, min(100, int(n)))


def compute_tip_score(item: VaultItem, config: dict | None = None) -> int:
    """Score how sellable a described item is for tip_reward (0-100).

    Tier gives the baseline (safe/sfw stays low, hardcore stays high). Sellable
    tags each add a small boost; `tip_vault_flag` (LLM's own sellable read)
    nudges up when True, down when False. Bounded 0..100."""
    cfg = config or {}
    tier = _effective_tier(item, cfg) or "unknown"
    score = _TIER_TIP_BASE.get(tier, _TIER_TIP_BASE["unknown"])
    tags = _effective_tags(item, cfg)
    hits = sum(1 for t in tags if t in _SELLABLE_TAGS)
    score += min(hits, 4) * 5  # cap the tag contribution so tier still dominates
    flag = effective_value(item, "tip_vault_flag", cfg)
    if flag is True:
        score += 10
    elif flag is False:
        score -= 25
    return _clamp(score)


def compute_story_score(item: VaultItem, config: dict | None = None) -> int:
    """Score how well-suited a described item is for a story post (0-100).

    Stories reach the whole (paying) fanbase and OF UI moderates them harder
    than DMs, so this rewards safer + face-forward framing and penalises
    hardcore acts. Videos are capped — OF stories are ≤15s and typically
    still-image driven, so a long video should not top a still."""
    cfg = config or {}
    tier = _effective_tier(item, cfg) or "unknown"
    score = _TIER_STORY_BASE.get(tier, _TIER_STORY_BASE["unknown"])
    tags = _effective_tags(item, cfg)
    friendly = sum(1 for t in tags if t in _STORY_FRIENDLY_TAGS)
    hostile = sum(1 for t in tags if t in _STORY_HOSTILE_TAGS)
    score += min(friendly, 3) * 8
    score -= min(hostile, 3) * 15
    if getattr(item, "kind", None) == "video":
        return _clamp(min(score, _VIDEO_STORY_CAP))
    return _clamp(score)


# ── Producer: tip_vault_score ────────────────────────────────────────

async def score_tip_vault(account_id: str, *, config: dict | None = None) -> int:
    """Compute + persist `vault_items.tip_vault_score` for every described item.

    Skips items where `tip_vault_score` is operator-locked. Returns the count
    of rows updated."""
    cfg = await _load_config(account_id, config)
    if not (cfg.get("scoring") or {}).get("tip", True):
        return 0
    updated = 0
    async with get_session() as s:
        items = (
            await s.execute(
                select(VaultItem).where(
                    VaultItem.account_id == account_id,
                    VaultItem.removed_at.is_(None),
                    VaultItem.describe_status == "described",
                )
            )
        ).scalars().all()
        for it in items:
            if is_locked(it, "tip_vault_score"):
                continue
            new = compute_tip_score(it, cfg)
            if it.tip_vault_score == new:
                continue
            await s.execute(
                update(VaultItem)
                .where(
                    VaultItem.account_id == account_id,
                    VaultItem.media_id == it.media_id,
                )
                .values(tip_vault_score=new)
            )
            updated += 1
        await s.commit()
    return updated


# ── Producer: story_score + story_suitable ───────────────────────────

async def score_story(account_id: str, *, config: dict | None = None) -> int:
    """Compute + persist `story_score` and `story_suitable` on described items.

    `story_suitable` is a bool cutoff at `_STORY_SUITABLE_THRESHOLD` — it's the
    field a consumer filters on ("this media is safe to post as a story"), while
    `story_score` retains the ordering. Both are locked-field-aware.
    Returns the count of rows touched."""
    cfg = await _load_config(account_id, config)
    if not (cfg.get("scoring") or {}).get("story", True):
        return 0
    updated = 0
    async with get_session() as s:
        items = (
            await s.execute(
                select(VaultItem).where(
                    VaultItem.account_id == account_id,
                    VaultItem.removed_at.is_(None),
                    VaultItem.describe_status == "described",
                )
            )
        ).scalars().all()
        for it in items:
            score_l = is_locked(it, "story_score")
            suit_l = is_locked(it, "story_suitable")
            if score_l and suit_l:
                continue
            new_score = compute_story_score(it, cfg)
            new_suit = new_score >= _STORY_SUITABLE_THRESHOLD
            vals: dict[str, Any] = {}
            if not score_l and it.story_score != new_score:
                vals["story_score"] = new_score
            if not suit_l and it.story_suitable != new_suit:
                vals["story_suitable"] = new_suit
            if not vals:
                continue
            await s.execute(
                update(VaultItem)
                .where(
                    VaultItem.account_id == account_id,
                    VaultItem.media_id == it.media_id,
                )
                .values(**vals)
            )
            updated += 1
        await s.commit()
    return updated


# ── Producer: folder proposals (feat 4) ──────────────────────────────

def _display_name(key: str, taxonomy_map: dict[str, str]) -> str:
    """Prefer an operator-authored label from the taxonomy; else Title Case."""
    if key in taxonomy_map:
        return taxonomy_map[key]
    return key.replace("_", " ").replace("-", " ").title()


def _dominant(seq: Iterable[str]) -> str:
    """Most-common value in `seq` (empty string if empty)."""
    counts: dict[str, int] = {}
    for v in seq:
        if not v:
            continue
        counts[v] = counts.get(v, 0) + 1
    if not counts:
        return ""
    return max(counts.items(), key=lambda kv: kv[1])[0]


async def propose_folders(
    account_id: str,
    *,
    config: dict | None = None,
    min_group_size: int = _DEFAULT_MIN_GROUP_SIZE,
    max_folders_per_run: int = _DEFAULT_MAX_FOLDERS_PER_RUN,
) -> list[int]:
    """Cluster described items by shared tag → enqueue folder review items.

    - Ignores tags in `_TAG_STOP_WORDS` (too broad to name a folder).
    - If `config.folders.taxonomy` is non-empty, only proposes folders whose
      key sits in that operator-authored list. The taxonomy entries can be
      plain strings ("beach") or objects (`{"key": "...", "name": "..."}`) so
      the operator controls display wording too.
    - Deduped against still-pending folder proposals on the same account (a
      re-run won't stack a second "Beach" card behind an unresolved one).
    - Emits contract payload shape:
        `{ folder_key, folder_name, media_ids, rationale }`
      + baseline_json with per-media AI-field hashes + a folder_snapshot.

    Returns the ids of any newly inserted review items (may be empty)."""
    cfg = await _load_config(account_id, config)
    folder_cfg = cfg.get("folders") or {}
    taxonomy_raw = folder_cfg.get("taxonomy") or []
    taxonomy_keys: set[str] = set()
    taxonomy_map: dict[str, str] = {}
    for entry in taxonomy_raw:
        if isinstance(entry, str):
            k = entry.strip().lower()
            if k:
                taxonomy_keys.add(k)
        elif isinstance(entry, dict):
            k = str(entry.get("key") or entry.get("name") or "").strip().lower()
            if k:
                taxonomy_keys.add(k)
                if entry.get("name"):
                    taxonomy_map[k] = str(entry["name"])

    new_ids: list[int] = []

    async with get_session() as s:
        pending = (
            await s.execute(
                select(VaultAiReviewItem).where(
                    VaultAiReviewItem.account_id == account_id,
                    VaultAiReviewItem.kind == "folder",
                    VaultAiReviewItem.status == "pending",
                )
            )
        ).scalars().all()
        existing_keys: set[str] = set()
        for r in pending:
            try:
                payload = json.loads(r.payload_json or "{}")
            except (TypeError, ValueError):
                continue
            k = str(payload.get("folder_key") or "").strip().lower()
            if k:
                existing_keys.add(k)

        items = (
            await s.execute(
                select(VaultItem).where(
                    VaultItem.account_id == account_id,
                    VaultItem.removed_at.is_(None),
                    VaultItem.describe_status == "described",
                )
            )
        ).scalars().all()

        # Bucket items by tag (each item lands in each of its own tags' buckets;
        # small vaults are the norm, so O(items × tags) is fine).
        buckets: dict[str, list[VaultItem]] = {}
        for it in items:
            for tag in _effective_tags(it, cfg):
                if tag in _TAG_STOP_WORDS:
                    continue
                if taxonomy_keys and tag not in taxonomy_keys:
                    continue
                buckets.setdefault(tag, []).append(it)

        # Largest clusters first — most-visible-folder-first ordering in the
        # review tab, and the per-run cap protects the biggest signals.
        ordered = sorted(buckets.items(), key=lambda kv: -len(kv[1]))
        for tag, group in ordered:
            if len(new_ids) >= max_folders_per_run:
                break
            if len(group) < min_group_size:
                continue
            if tag in existing_keys:
                continue

            media_ids = sorted({int(it.media_id) for it in group})
            tiers = [_effective_tier(it, cfg) for it in group]
            dom_tier = _dominant(tiers) or "unknown"
            co_tags = _dominant_cotags(group, tag, cfg)
            rationale_bits = [f"n={len(media_ids)}", f"tier={dom_tier}"]
            if co_tags:
                rationale_bits.append("co=" + ",".join(co_tags))
            rationale = f"clustered on tag={tag} · " + " · ".join(rationale_bits)

            display = _display_name(tag, taxonomy_map)
            payload = {
                "folder_key": tag,
                "folder_name": display,
                "media_ids": media_ids,
                "rationale": rationale,
            }
            # Snapshot the CURRENT world for exactly these keys via the shared
            # baseline view — so the stored baseline is byte-identical to what
            # vault_ai_api recomputes at approve time when nothing has changed
            # (values in the skeleton are ignored; only the keys/ids matter).
            baseline = await vab.current_baseline_view(s, account_id, {
                "config_version": None,
                "media_hashes": {str(it.media_id): None for it in group},
                "folder_snapshot": {display: None},
            })
            row = VaultAiReviewItem(
                account_id=account_id,
                kind="folder",
                status="pending",
                payload_json=json.dumps(payload, ensure_ascii=False),
                baseline_json=json.dumps(baseline, ensure_ascii=False),
            )
            s.add(row)
            await s.flush()
            new_ids.append(row.id)
            existing_keys.add(tag)
        await s.commit()

    return new_ids


def _dominant_cotags(
    group: list[VaultItem], anchor: str, config: dict, *, top_n: int = 3
) -> list[str]:
    """Top co-occurring tags in a cluster (excluding the anchor tag + stop
    words) — surfaced in the folder rationale so an operator sees WHY the
    cluster hung together, not just its tag name."""
    counts: dict[str, int] = {}
    for it in group:
        for t in _effective_tags(it, config):
            if t == anchor or t in _TAG_STOP_WORDS:
                continue
            counts[t] = counts.get(t, 0) + 1
    return [t for t, _ in sorted(counts.items(), key=lambda kv: -kv[1])[:top_n]]


# ── Convenience runner ───────────────────────────────────────────────

async def run_all(account_id: str, *, config: dict | None = None) -> dict[str, int]:
    """Run all three producers in order; return per-producer counts.

    Ordering matters only in that scoring depends on nothing folder-side; a
    caller may still run the individual functions if a later cadence needs a
    different mix (e.g. re-score more often than re-cluster)."""
    cfg = await _load_config(account_id, config)
    tip = await score_tip_vault(account_id, config=cfg)
    story = await score_story(account_id, config=cfg)
    folder_ids = await propose_folders(account_id, config=cfg)
    return {
        "tip_scored": tip,
        "story_scored": story,
        "folders_proposed": len(folder_ids),
    }
