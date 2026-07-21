"""service/vault_ai_to_chatter.py — S8: approved vault-AI PPV drafts → sendable offers.

The vault-AI consumer (S6, `automations/vault_daily_reminder.apply_ppv_item`) exports
an APPROVED `kind="ppv"` review item into the PPV Library as a **disabled draft**:
one `ppv_library_config_json["ppvs"]` entry with a `vai-<review_item_id>` id, its
`suggested_script`/caption folded into `caption_texts` (the send copy), its `media_ids`
as the media set — and `enabled=False`, the library master left OFF (correction #2).

This module is the single **arm gate** that turns such a draft into an offer the AI
Chatter / AI Upseller (`automations/ppv_send.py`, `automations/upsell.py`) may pick and
send. The invariants it enforces, and never relaxes:

  * **master-gated** — the PPV-Library master (`ppv_library_config.enabled`) must be ON.
  * **disabled until an explicit arm** — the draft's own `enabled` must be True. Approval
    + export alone (`enabled=False`) is NOT arming: `approved ≠ applied ≠ armed`.
  * **never writes** — every function is a pure read over a parsed config dict. Nothing
    here mutates `ppv_library_config_json`, syncs a `ppv_send` rule, or sends. Arming is a
    separate, explicit operator action (the `ppv_library_config_api` PATCH that flips
    `enabled` and syncs the rule); this adapter only *reads* the result.

So: a draft that is merely approved is invisible here; only an approved-AND-armed draft
becomes a pickable offer, with its script as the copy and its `media_ids` as the media.
"""
from __future__ import annotations

from typing import Any

# Stable id prefix the exporter stamps on every draft it writes (S6
# `_ppv_draft_entry`: `f"vai-{review_item_id}"`). It is how a Library entry is
# recognised as vault-AI-sourced without a schema change.
VAI_PREFIX = "vai-"


def is_vault_ai_offer(ppv: Any) -> bool:
    """True iff this Library entry was produced by the vault-AI PPV export."""
    return isinstance(ppv, dict) and str(ppv.get("id") or "").startswith(VAI_PREFIX)


def is_master_on(cfg: Any) -> bool:
    """The PPV-Library master gate — the same `cfg.get("enabled")` `ppv_send.run`
    checks before any send. OFF ⇒ nothing in the library is sendable."""
    return isinstance(cfg, dict) and bool(cfg.get("enabled"))


def is_armed(cfg: Any, ppv: Any) -> bool:
    """A vault-AI draft is a SENDABLE offer only when BOTH gates are open: the
    library master (`cfg.enabled`) AND this entry (`ppv.enabled`). Export leaves the
    entry `enabled=False`, so an approved-but-un-armed draft returns False."""
    return is_master_on(cfg) and is_vault_ai_offer(ppv) and bool(ppv.get("enabled"))


def _media_ids(ppv: dict) -> list[int]:
    out: list[int] = []
    for m in (ppv.get("media_ids") or []):
        try:
            v = int(m)
        except (TypeError, ValueError):
            continue
        if v > 0 and v not in out:
            out.append(v)
    return out


def offer_copy(ppv: dict) -> str:
    """The send copy — the exported `suggested_script`/caption. The consumer folds it
    into `caption_texts` (the field `ppv_send._pick_caption` reads verbatim), so this
    surfaces the same text the wire would carry. '' when the draft has no copy yet (a
    draft the operator finishes before arming)."""
    if not isinstance(ppv, dict):
        return ""
    for t in (ppv.get("caption_texts") or []):
        if isinstance(t, str) and t.strip():
            return t.strip()
    return ""


def to_offer(ppv: dict) -> dict:
    """Normalise a Library entry into the chatter/upseller offer shape. `copy` = the
    send copy, `media_ids` = the media set, `price_cents` = the operator-authored base
    price (never re-derived here). Read-only; does not check the arm gate — callers use
    `armed_vault_ai_offers`/`pickable_offer` for that."""
    media = _media_ids(ppv)
    media_set = set(media)
    previews: list[int] = []
    for m in (ppv.get("preview_options") or []):
        try:
            v = int(m)
        except (TypeError, ValueError):
            continue
        if v in media_set and v not in previews:
            previews.append(v)
    return {
        "id": str(ppv.get("id") or ""),
        "source": "vault_ai",
        "name": str(ppv.get("name") or "")[:60],
        "media_ids": media,
        "preview_media_ids": previews,
        "copy": offer_copy(ppv),
        "price_cents": int(ppv.get("base_price_cents") or 0),
    }


def _ppvs(cfg: Any) -> list[dict]:
    if not isinstance(cfg, dict):
        return []
    ppvs = cfg.get("ppvs")
    return [p for p in ppvs if isinstance(p, dict)] if isinstance(ppvs, list) else []


def armed_vault_ai_offers(cfg: Any) -> list[dict]:
    """The pool the AI Chatter / Upseller may pick from: every vault-AI draft that is
    sendable RIGHT NOW — master ON, entry armed, and deliverable (has media). Master
    OFF or a disabled entry ⇒ that draft is absent. Never mutates `cfg`, never arms."""
    if not is_master_on(cfg):
        return []
    return [to_offer(p) for p in _ppvs(cfg)
            if is_vault_ai_offer(p) and bool(p.get("enabled")) and _media_ids(p)]


def pickable_offer(cfg: Any, ppv_id: Any) -> dict | None:
    """The single vault-AI offer with this id IFF it is armed+sendable, else None.
    The pick-an-offer gate `ppv_send` consults: a non-vault-AI id, or an un-armed /
    master-off / media-less draft, yields None (⇒ not a sendable offer)."""
    pid = str(ppv_id or "")
    if not pid.startswith(VAI_PREFIX):
        return None
    for off in armed_vault_ai_offers(cfg):
        if off["id"] == pid:
            return off
    return None
