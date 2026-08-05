"""Vault-AI ACCOUNT CONFIG contract — the ONE home for the default blob.

`account_ai_config.vault_ai_config_json` holds an operator's partial overrides;
the effective config is `DEFAULTS` deep-merged with that partial. Shape frozen in
plans/VAULT_AI_ACTIONS_CONTRACT.md §1. Absent/NULL → pure DEFAULTS, master OFF.
This blob is owned whole (no shallow-merge with the other `*_config_json`).

Why this module exists: the defaults used to live in `scripts_api` with a
hand-maintained partial COPY in `automations/describe_media`, so the API's GET
and the sweep's effective view were two blobs that merely happened to agree. Both
copies also hardcoded a stale value, which is how one provider rename became a
four-file edit. The API layer can't be the home (an automation importing a
FastAPI router module inverts the layering), so the contract lives here and both
sides import it.

MODEL NAMES HERE ARE `llm_client.MODELS` KEYS — our stable internal ids, never a
provider's wire name. `LLMModel.api_model` is the only place a wire name belongs;
a stored config that speaks wire names needs a translator to read it back, and
that translator rots the moment the provider renames anything.

Note `effective()` is PURE — it copies rather than mutating `stored`, so a caller
that persists the operator's partial (PATCH) and a caller that only renders the
merged view (GET) can share it without one corrupting the other.
"""
from __future__ import annotations

import copy
import json
from typing import Any

DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "suggest_only": True,
    # `escalation` and `escalate_below_confidence` used to sit here. Nothing read
    # either: `_describe_one` names its escalation model inline, and it escalates
    # on a refusal or a blank, never on a confidence score — that second feature
    # was designed and never built. A config key an operator can set and watch do
    # nothing is worse than no key at all, and both of these had defaults equal
    # to the hardcoded behaviour, so they read as working. If configurable
    # escalation is ever wanted, add it deliberately with a reader attached.
    "models": {
        "describe": "qwen3-vl-30b",
        "caption": "deepseek-v4-flash",
        "script": "deepseek-v4-flash",
    },
    "describe": {
        "cadence_hours": 6,
        "describe_all_cap_percent": 80,
        "max_items_per_run": 40,
        "images": True,
        "videos": True,
    },
    "pricing": {
        "enabled": True,
        "bands_by_tier": {
            "safe":       [300, 800],
            "suggestive": [500, 1500],
            "explicit":   [1000, 3000],
            "graphic":    [2000, 6000],
            "unknown":    [500, 1500],
        },
    },
    "tier_labels": {
        "safe": "Safe", "suggestive": "Suggestive", "explicit": "Explicit",
        "graphic": "Graphic", "unknown": "Unclassified",
    },
    "folders": {
        "mode": "internal",
        "taxonomy": [],
        "max_folders_per_item": 3,
    },
    "scoring": {"story": True, "tip": True},
    "daily_reminder": {
        "enabled": False,
        "auto_post": False,
        "auto_mass_message": False,
        "folder_name": "",
        "lines": [],
        "images_per_day": 2,
        "on_under_min_unseen": "use_fewer",
        "repeat_after_days": 30,
        "per_fan_cooldown_hours": 48,
        "daily_at": ["10:00"],
        "tz_offset_minutes": 0,
    },
    # ── PPV week/month arc (service/vault_ppv_week.py) ────────────────────────
    # Assembles a story-shaped WEEK (soft→payoff, copy references yesterday) from
    # already-derived vault content, repeated as exclusive waves across the month.
    # Every day hits the feed AND DMs (combine). Still suggest-only: the operator
    # generates a preview and confirms before anything is armed.
    "ppv_week": {
        "enabled": False,          # arc automation on (still nothing auto-sends)
        "weeks": 4,                # 1 = a single week, 4 = a month of waves
        "combine_feed_and_dm": True,   # each drop = paid post (feed) + mass PPV (DM)
        "in_voice_copy": True,     # captions written via PAINFUL_TEXTING, not template
    },
}


def clone_defaults() -> dict:
    """A fresh deep copy — callers merge into it, so never hand out the module
    dict itself."""
    return copy.deepcopy(DEFAULTS)


def deep_merge(base: dict, over: dict) -> dict:
    """Recursive dict merge: `over` beats `base` per key; nested dicts merge.

    Non-dict values (incl. lists) REPLACE, so a taxonomy edit / band change
    stores exactly what the operator typed — a per-index merge would trap old
    items in an updated list. Pure: `base` is never mutated.
    """
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def parse_stored(raw: Any) -> dict | None:
    """The stored partial as a dict, or None if the column holds no usable
    object (NULL/blank, unparseable JSON, or a non-object).

    None is distinguishable from `{}` on purpose: `{}` is a real "operator has
    overridden nothing", None is "this blob is broken" — the caller decides
    whether that deserves a log line. Both degrade to pure defaults.
    """
    if not raw:
        return None
    if isinstance(raw, dict):
        return raw
    try:
        val = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return val if isinstance(val, dict) else None


def effective(stored: Any) -> dict:
    """Defaults + operator overrides = the blob the UI and every consumer see.

    Takes a parsed dict OR the raw column, so a caller with nothing to say about
    a broken blob doesn't have to parse first. Anything unusable degrades to
    pure defaults (master OFF) rather than raising — a bad blob must never wedge
    a sweep.
    """
    return deep_merge(clone_defaults(), parse_stored(stored) or {})
