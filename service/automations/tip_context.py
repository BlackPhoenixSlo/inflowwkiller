"""service/automations/tip_context.py — match reward media to what the fan wants.

The seller's LLM makes free-text promises ("the whole lingerie set"); tip_reward's
delivery is a static folder pull. This module closes that gap for the reward
bundle: read the recent thread, match it against the local vault-AI descriptions
(VaultItem.search_text from the describe pass), and return unseen PHOTO ids that
fit what the fan asked for. Pure selection — the caller (tip_reward.run) decides
where the picks slot in its bundle.

Failure contract: [] on ANY failure (no candidates, no messages, LLM error/cap) —
a reward must never be blocked by the matcher. Hermetic in tests: no VaultItem
rows ⇒ no candidates ⇒ no LLM call.
"""
from __future__ import annotations

import logging

import llm_client                       # module import so tests can patch .chat
from automations._common import resolve_model
from db.engine import get_session
from db.models import Message, VaultItem
from sqlalchemy import select

log = logging.getLogger("of-relay.automation.tip_context")

# Guardrails: cap the candidate list the LLM sees (prompt size) and the
# per-description excerpt length.
_CANDIDATES_MAX = 150
_DESC_LEN = 160


async def _recent_thread_lines(account_id: str, fan_id: int, n_msgs: int) -> list[str]:
    """The last `n_msgs` non-unsent messages as 'FAN:/HER:' lines, oldest first.
    PPV/tip markers ride along so the matcher can see what was promised vs paid."""
    async with get_session() as s:
        rows = (await s.execute(
            select(Message.direction, Message.body, Message.price_cents, Message.is_tip)
            .where(Message.account_id == str(account_id),
                   Message.fan_id == int(fan_id),
                   Message.is_unsent.is_(False))
            .order_by(Message.created_at.desc())
            .limit(max(1, int(n_msgs)))
        )).all()
    lines: list[str] = []
    for direction, body, price_cents, is_tip in reversed(rows):
        who = "FAN" if direction == "in" else "HER"
        tag = ""
        if is_tip:
            tag = " [tip]"
        elif int(price_cents or 0) > 0:
            tag = f" [PPV ${int(price_cents or 0) / 100:.0f}]"
        body = " ".join(str(body or "").split())[:300]
        if body or tag:
            lines.append(f"{who}{tag}: {body}")
    return lines


async def _candidates(account_id: str, seen: set[int]) -> list[tuple[int, str]]:
    """(media_id, one-line description) for unseen PHOTOS this account's vault-AI
    has described — search_text first (desc+tags+terms, built by the describe
    pass), then the legacy description columns. Recent uploads first, capped."""
    async with get_session() as s:
        rows = (await s.execute(
            select(VaultItem.media_id, VaultItem.search_text, VaultItem.description,
                   VaultItem.video_description)
            .where(VaultItem.account_id == str(account_id),
                   VaultItem.kind == "photo")
            .order_by(VaultItem.created_at.desc())
            .limit(_CANDIDATES_MAX * 3)
        )).all()
    out: list[tuple[int, str]] = []
    for mid, search_text, desc, vdesc in rows:
        if int(mid) in seen:
            continue
        text = " ".join(str(search_text or desc or vdesc or "").split())
        if not text:
            continue
        out.append((int(mid), text[:_DESC_LEN]))
        if len(out) >= _CANDIDATES_MAX:
            break
    return out


async def pick_context_media(account_id: str, fan_id: int, *, seen: set[int],
                             limit: int, n_msgs: int) -> list[int]:
    """Up to `limit` unseen photo ids matching what the fan wants, judged by the
    account's LLM from the last `n_msgs` thread messages + vault descriptions.
    [] when there is nothing to match or on any failure — never raises."""
    if limit <= 0:
        return []
    try:
        candidates = await _candidates(account_id, seen)
        if not candidates:
            return []
        lines = await _recent_thread_lines(account_id, fan_id, n_msgs)
        if not lines:
            return []
        catalog = "\n".join(f"{mid}: {text}" for mid, text in candidates)
        system = (
            "You match OnlyFans vault photos to what a fan is asking for. "
            "Read the chat, then pick AT MOST "
            f"{int(limit)} photo ids from the catalog whose descriptions match "
            "what the fan asked to see or was promised (outfit, body part, "
            "scenario). Only pick clear matches — an empty list is the right "
            "answer when nothing fits. Reply as JSON: {\"media_ids\": [..]}."
        )
        user = "CHAT (oldest first):\n" + "\n".join(lines) + \
               "\n\nPHOTO CATALOG (id: description):\n" + catalog
        model = await resolve_model(account_id, "tip_reward")
        res = await llm_client.chat(
            model=model,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}],
            purpose="tip_reward_context_pick",
            account_id=str(account_id), fan_id=int(fan_id),
            response_format={"type": "json_object"}, temperature=0.2,
        )
        parsed = res.parsed if isinstance(res.parsed, dict) else {}
        raw_ids = parsed.get("media_ids") or []
        valid = {mid for mid, _ in candidates}
        picked: list[int] = []
        for x in raw_ids:
            try:
                mid = int(x)
            except (TypeError, ValueError):
                continue
            if mid in valid and mid not in picked:
                picked.append(mid)
            if len(picked) >= limit:
                break
        return picked
    except Exception:
        log.warning("context_pick failed account=%s fan=%s — folder fallback",
                    account_id, fan_id, exc_info=True)
        return []
