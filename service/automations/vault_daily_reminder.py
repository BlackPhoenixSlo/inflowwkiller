"""service/automations/vault_daily_reminder.py — Vault-AI ACTION consumers (S6).

This is the *consuming* half of the suggest-only vault-AI loop. The producers
(`vault_ai_service`) and the review API (`vault_ai_api`) turn describe output into
`vault_ai_review_items` and let an operator approve them — but approval mutates
NOTHING (contract §2 correction #2: `approved ≠ applied ≠ armed`). The consumers
here are what finally act, and ONLY on an operator-approved item that is still
fresh:

  • Daily reminder (feat 10) — resolve `daily_reminder.folder_name` → an INTERNAL
    vault folder, pick `images_per_day` UNSEEN media (not in `vault_daily_usage`
    within the rotation window, honouring `on_under_min_unseen`) + one random line
    → emit a `kind="reminder"` review card. ON APPROVE the reminder is sent via the
    EXISTING mass-broadcast path (`send_mass_message.run`), which already applies
    the MASSdmEXCLUDE opt-out list + the per-fan contact guard; the reminder's own
    `per_fan_cooldown_hours` rides in as `exclude_replied_hours` (the fatigue guard,
    #11). `vault_daily_usage` is recorded ONLY after the send SUCCEEDS
    (select-then-record — mirrors `tip_reward.pick_hot_teaser` / `record_hot_teaser`),
    so a rejected/failed/stale proposal never burns a media out of the pool.

  • PPV solo assemble/export (feat 7/9) — an APPROVED `kind="ppv"` draft is exported
    to the PPV Library as a **DISABLED draft** (`enabled=false`, `feed_enabled=false`,
    the library master left OFF). We write the `ppv_library_config_json` column
    DIRECTLY and never sync a `ppv_send` rule, so nothing can send. Arming is a
    SEPARATE, explicit operator action in the PPV Library UI (its PUT is what syncs +
    enables the rule). `approved ≠ armed`.

  • Folder application (feat 4) — an APPROVED `kind="folder"` item materialises an
    INTERNAL `vault_folders` + `vault_folder_items` membership (never an OF write).
    That internal folder is the hand-off surface the tip/story paths consume by name
    (tip_reward resolves folders by name; auto_stories draws a media pool) — the S6
    "tip/story consumer" is this apply step reading effective fields through
    `effective_value` and refusing a stale item, NOT a new send path.

Discipline every consumer here shares (the generic contract from the S6 brief):
  1. act on `status="approved"` rows ONLY — a `pending` item never sends;
  2. read AI-populated fields via `vault_ai_effective.effective_value` (never a raw
     column);
  3. BEFORE applying, RE-CHECK staleness via `vault_ai_baseline.current_baseline_view`
     — a stale item must NOT send/apply (the world moved after approval). This mirrors
     the checker in `vault_ai_api` exactly (same `current_baseline_view`, same
     order-insensitive normalise) so a fresh item there is a fresh item here.

Two cadence automations self-register (`@register`) like every other P4 automation:
  • `vault_daily_reminder` — produce today's reminder card + apply approved reminders.
  • `vault_ai_consume`     — apply approved ppv + folder items.
Each uses its OWN `AsyncSession`(s) and `of_client` only through the mass-broadcast
path; the reminder sender is injectable so a test can drive the record-after-success
rotation without touching OnlyFans.
"""
from __future__ import annotations

import json
import logging
import random
from datetime import date, datetime, timedelta
from typing import Any, Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from automation_registry import register
from db.engine import get_session
from db.models import (
    AccountAiConfig,
    VaultAiReviewItem,
    VaultDailyUsage,
    VaultFolder,
    VaultFolderItem,
    VaultItem,
)
from vault_ai_effective import effective_value
import vault_ai_baseline as vab

log = logging.getLogger("of-relay.automation.vault_daily_reminder")

# Contract §1 daily_reminder defaults (used when a key is absent).
_DEF_IMAGES_PER_DAY = 2
_DEF_ON_UNDER_MIN = "use_fewer"          # "stop" | "use_fewer" | "repeat_after_window"
_DEF_REPEAT_AFTER_DAYS = 30
_DEF_PER_FAN_COOLDOWN_H = 48

_PPV_PRICE_FLOOR_CENTS = 300             # mirror ppv_send floor so an armed draft is sane
_PPV_PRICE_CEIL_CENTS = 20000

# Statuses that BLOCK producing a fresh reminder card (one awaits action already).
_OPEN_STATUSES = ("pending", "approved")


# ── Config loading (mirrors vault_ai_service._load_config) ────────────

async def _load_config(account_id: str, cfg: dict | None) -> dict:
    """Effective vault-AI config dict. None ⇒ load + parse
    `AccountAiConfig.vault_ai_config_json`; missing/blank ⇒ {} (callers use
    `.get(..., default)` so absence is safe)."""
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


# ── Staleness re-check (mirrors vault_ai_api exactly) ─────────────────

def _normalize_for_compare(value: Any) -> Any:
    """Order-insensitive normalise so stored vs recomputed compare as sets/maps —
    byte-identical to `vault_ai_api._normalize_for_compare` so a fresh item at
    approve time is a fresh item at apply time."""
    if isinstance(value, dict):
        return {str(k): _normalize_for_compare(value[k]) for k in sorted(value, key=str)}
    if isinstance(value, (list, tuple, set)):
        return sorted(
            (_normalize_for_compare(v) for v in value),
            key=lambda v: json.dumps(v, sort_keys=True, default=str),
        )
    return value


def _load_json(raw: str | None, default: Any) -> Any:
    if raw is None:
        return default
    if isinstance(raw, (dict, list)):
        return raw
    try:
        val = json.loads(raw)
    except (TypeError, ValueError):
        return default
    return val if val is not None else default


async def _is_stale(session, account_id: str, item: VaultAiReviewItem) -> bool:
    """True if the world moved since the proposal was built for any key the
    producer captured in `baseline_json`. A NULL/empty baseline means the producer
    opted out of staleness detection — always fresh (matches the checker)."""
    stored = _load_json(item.baseline_json, None)
    if not isinstance(stored, dict) or not stored:
        return False
    current = await vab.current_baseline_view(session, account_id, stored)
    trimmed = {k: stored[k] for k in current.keys() if k in stored}
    return _normalize_for_compare(trimmed) != _normalize_for_compare(current)


# ── Daily reminder: SELECTION (feat 10) ───────────────────────────────

async def _internal_folder_media(
    session, account_id: str, folder_name: str,
) -> list[int]:
    """Ordered media ids of the INTERNAL vault folder named `folder_name`
    (contract §1: `daily_reminder.folder_name` resolves to an internal folder by
    NAME). Empty list when no such live folder / no members."""
    folder = (
        await session.execute(
            select(VaultFolder).where(
                VaultFolder.account_id == account_id,
                VaultFolder.name == folder_name,
                VaultFolder.deleted_at.is_(None),
            )
        )
    ).scalars().first()
    if folder is None:
        return []
    rows = (
        await session.execute(
            select(VaultFolderItem.media_id)
            .where(
                VaultFolderItem.account_id == account_id,
                VaultFolderItem.folder_id == folder.id,
            )
            .order_by(VaultFolderItem.added_at.asc(), VaultFolderItem.media_id.asc())
        )
    ).all()
    return [int(r[0]) for r in rows]


async def _used_media(
    session, account_id: str, *, window_days: int | None, today: date,
) -> set[int]:
    """Media ids already used in `vault_daily_usage`. `window_days=None` ⇒
    all-time (never repeat); an int ⇒ only rows within the rotation window (so a
    media becomes eligible again after the window — the `repeat_after_window`
    policy)."""
    stmt = select(VaultDailyUsage.media_id).where(
        VaultDailyUsage.account_id == account_id
    )
    if window_days is not None:
        stmt = stmt.where(VaultDailyUsage.sent_on >= (today - timedelta(days=window_days)))
    rows = (await session.execute(stmt)).all()
    return {int(r[0]) for r in rows}


async def pick_daily_reminder(
    account_id: str,
    cfg: dict,
    *,
    today: date | None = None,
    rand: float | None = None,
) -> dict | None:
    """Pure SELECTION for the daily reminder — resolve folder → unseen media → one
    line. Returns `{folder_name, media_ids, line, images_per_day}` or None (folder
    empty / nothing unseen / `on_under_min_unseen == "stop"` with too few unseen).
    Sends NOTHING and writes NOTHING — mirrors `pick_hot_teaser`; the caller emits a
    review card and only a later approve+consume sends."""
    today = today or datetime.utcnow().date()
    dr = cfg.get("daily_reminder") or {}
    folder_name = str(dr.get("folder_name") or "").strip()
    if not folder_name:
        return None
    lines = [str(x).strip() for x in (dr.get("lines") or []) if str(x).strip()]
    if not lines:
        return None
    try:
        images_per_day = max(1, int(dr.get("images_per_day") or _DEF_IMAGES_PER_DAY))
    except (TypeError, ValueError):
        images_per_day = _DEF_IMAGES_PER_DAY
    policy = str(dr.get("on_under_min_unseen") or _DEF_ON_UNDER_MIN).strip().lower()
    try:
        repeat_after = max(1, int(dr.get("repeat_after_days") or _DEF_REPEAT_AFTER_DAYS))
    except (TypeError, ValueError):
        repeat_after = _DEF_REPEAT_AFTER_DAYS

    # "repeat_after_window" windows the unseen filter (a media re-enters the pool
    # after `repeat_after_days`); "stop"/"use_fewer" never repeat (all-time).
    window = repeat_after if policy == "repeat_after_window" else None

    async with get_session() as s:
        pool = await _internal_folder_media(s, account_id, folder_name)
        used = await _used_media(s, account_id, window_days=window, today=today)

    unseen = [mid for mid in pool if mid not in used]
    if not unseen:
        return None
    if len(unseen) < images_per_day and policy == "stop":
        return None  # not enough fresh media and the operator chose to hold

    picked = unseen[:images_per_day]
    r = random.random() if rand is None else float(rand)
    line = lines[int(r * len(lines)) % len(lines)]
    return {
        "folder_name": folder_name,
        "media_ids": picked,
        "line": line,
        "images_per_day": images_per_day,
    }


async def produce_reminder_card(
    account_id: str, cfg: dict, *, today: date | None = None,
    rand: float | None = None,
) -> int | None:
    """Emit a `kind="reminder"` review card for today (or None). Idempotent: skips
    when a reminder card is already open (pending/approved) so a re-run never
    stacks a second card behind an unresolved one. Snapshots the CURRENT world via
    the shared baseline view so approve-time / apply-time staleness checks are
    byte-identical when nothing changed."""
    pick = await pick_daily_reminder(account_id, cfg, today=today, rand=rand)
    if pick is None:
        return None
    async with get_session() as s:
        open_row = (
            await s.execute(
                select(VaultAiReviewItem.id).where(
                    VaultAiReviewItem.account_id == account_id,
                    VaultAiReviewItem.kind == "reminder",
                    VaultAiReviewItem.status.in_(_OPEN_STATUSES),
                )
            )
        ).first()
        if open_row is not None:
            return None

        payload = {
            "folder_name": pick["folder_name"],
            "media_ids": pick["media_ids"],
            "line": pick["line"],
            "audience": "mass_minus_excludes",
            "images_per_day": pick["images_per_day"],
        }
        baseline = await vab.current_baseline_view(s, account_id, {
            "config_version": None,
            "media_hashes": {str(mid): None for mid in pick["media_ids"]},
            "folder_snapshot": {pick["folder_name"]: None},
        })
        row = VaultAiReviewItem(
            account_id=account_id,
            kind="reminder",
            status="pending",
            payload_json=json.dumps(payload, ensure_ascii=False),
            baseline_json=json.dumps(baseline, ensure_ascii=False),
        )
        s.add(row)
        await s.flush()
        new_id = int(row.id)
        await s.commit()
    return new_id


# ── Daily reminder: CONSUME (send + record-after-success) ─────────────

Sender = Callable[..., Awaitable[dict]]


async def _default_sender(account_id: str, payload: dict, *, run_id: int) -> dict:
    """The real mass-broadcast path. Imported lazily so this module stays cheap to
    import (and test) without pulling the whole send stack + OF client seam."""
    from automations import send_mass_message
    return await send_mass_message.run(account_id, payload, run_id=run_id)


def _send_succeeded(result: Any) -> bool:
    """A mass-broadcast result is a success unless it explicitly reports skipped/
    error (send_mass_message returns `{"status": "skipped"|"error", ...}` on a
    no-op / failure, else a stats dict with no failure status)."""
    if not isinstance(result, dict):
        return False
    return str(result.get("status") or "").lower() not in {"skipped", "error"}


async def consume_reminders(
    account_id: str,
    cfg: dict,
    *,
    sender: Sender | None = None,
    run_id: int = 0,
    today: date | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Apply every APPROVED reminder card: re-check staleness, send via the
    mass-broadcast path, then record `vault_daily_usage` + flip `applied` ONLY on a
    successful send (select-then-record). A stale item is refused (left approved,
    never sent). Returns counts + the ids acted on."""
    sender = sender or _default_sender
    now = now or datetime.utcnow()
    today = today or now.date()
    dr = cfg.get("daily_reminder") or {}
    try:
        cooldown_h = max(0, int(dr.get("per_fan_cooldown_hours") or _DEF_PER_FAN_COOLDOWN_H))
    except (TypeError, ValueError):
        cooldown_h = _DEF_PER_FAN_COOLDOWN_H

    sent: list[int] = []
    stale: list[int] = []
    skipped: list[int] = []

    async with get_session() as s:
        rows = list((await s.execute(
            select(VaultAiReviewItem).where(
                VaultAiReviewItem.account_id == account_id,
                VaultAiReviewItem.kind == "reminder",
                VaultAiReviewItem.status == "approved",
            ).order_by(VaultAiReviewItem.id.asc())
        )).scalars().all())

    for row in rows:
        # Re-check staleness against a fresh session read of the current world.
        async with get_session() as s:
            if await _is_stale(s, account_id, row):
                stale.append(int(row.id))
                continue

        payload = _load_json(row.payload_json, {}) or {}
        media_ids = [int(m) for m in (payload.get("media_ids") or [])]
        line = str(payload.get("line") or "").strip()
        if not media_ids or not line:
            skipped.append(int(row.id))
            continue

        # The EXISTING mass-broadcast path: broadcast to all fans, minus the
        # MASSdmEXCLUDE opt-out list (send_mass_message applies it for an unpriced
        # send) minus the per-fan contact guard. The reminder's own fatigue
        # cooldown rides in as exclude_replied_hours (#11 — reuse teaser cooldown
        # semantics over the mass path).
        send_payload = {
            "text": line,
            "media_files": media_ids,
            "price": 0,
            "user_lists": ["fans"],
            "automation_kind": "vault_daily_reminder",
            "exclude_replied_hours": cooldown_h,
        }
        try:
            result = await sender(account_id, send_payload, run_id=run_id)
        except Exception:  # noqa: BLE001 — a failed send must NOT burn the pool
            log.warning("vault_daily_reminder: send failed item=%s account=%s",
                        row.id, account_id, exc_info=True)
            skipped.append(int(row.id))
            continue

        if not _send_succeeded(result):
            log.info("vault_daily_reminder: send no-op item=%s result=%s",
                     row.id, result)
            skipped.append(int(row.id))
            continue

        # SUCCESS → NOW record usage (one row/media/day, idempotent) + flip applied,
        # in one transaction. Never before the send confirms (record-after-success).
        async with get_session() as s:
            for mid in media_ids:
                await s.execute(
                    sqlite_insert(VaultDailyUsage)
                    .values(account_id=str(account_id), media_id=int(mid),
                            sent_on=today, created_at=now)
                    .on_conflict_do_nothing(
                        index_elements=["account_id", "media_id", "sent_on"])
                )
            item = await s.get(VaultAiReviewItem, int(row.id))
            if item is not None:
                item.status = "applied"
                item.resolved_at = now
            await s.commit()
        sent.append(int(row.id))

    return {"sent": sent, "stale": stale, "skipped": skipped}


# ── PPV export: disabled draft into the PPV Library (feat 7/9) ─────────

def _clamp_price(cents: Any) -> int:
    try:
        v = int(cents or 0)
    except (TypeError, ValueError):
        v = 0
    return max(_PPV_PRICE_FLOOR_CENTS, min(v, _PPV_PRICE_CEIL_CENTS))


def _ppv_draft_entry(item_id: int, payload: dict) -> dict:
    """Build a DISABLED PPV Library entry from an approved ppv review payload.
    `enabled=false` (no `ppv_send` rule ever arms it) + `feed_enabled=false`
    (not feed-postable). A stable id keyed on the review-item id makes a re-apply
    idempotent (replace, never duplicate)."""
    media_ids: list[int] = []
    for m in (payload.get("media_ids") or []):
        try:
            v = int(m)
        except (TypeError, ValueError):
            continue
        if v > 0 and v not in media_ids:
            media_ids.append(v)
    media_set = set(media_ids)
    previews = []
    for m in (payload.get("preview_media_ids") or []):
        try:
            v = int(m)
        except (TypeError, ValueError):
            continue
        if v in media_set and v not in previews:
            previews.append(v)
    # Copy: the DM caption is the send text; fall back to the script if the copy
    # call only produced one. Empty is allowed — it's a draft the operator finishes.
    caption = str(payload.get("caption") or "").strip()
    script = str(payload.get("script") or "").strip()
    texts = [t for t in (caption or script,) if t]
    return {
        "id": f"vai-{int(item_id)}",
        "name": str(payload.get("name") or "AI PPV")[:60],
        "media_ids": media_ids,
        "caption_pool_key": "",
        "caption_texts": texts,
        "feed_captions": [],
        "feed_caption_pool_key": "",
        "feed_enabled": False,       # not feed-postable (correction #2)
        "also_post_to_feed": False,
        "base_price_cents": _clamp_price(payload.get("price_cents")),
        "preview_options": previews,
        "sends_per_week": 1,
        "resend_monthly": False,
        "exclude_buyers": True,
        "enabled": False,            # DISABLED draft — gates the ppv_send rule OFF
    }


async def apply_ppv_item(
    session, account_id: str, item: VaultAiReviewItem, *, now: datetime,
) -> str:
    """Export ONE approved ppv draft to the PPV Library as a disabled draft, by
    writing `ppv_library_config_json` DIRECTLY (no rule sync ⇒ nothing arms). The
    library master `enabled` flag is left untouched (OFF stays OFF). Returns
    'applied' | 'stale'."""
    if await _is_stale(session, account_id, item):
        return "stale"
    payload = _load_json(item.payload_json, {}) or {}

    row = await session.get(AccountAiConfig, account_id)
    if row is None:
        row = AccountAiConfig(account_id=account_id)
        session.add(row)
    lib = _load_json(row.ppv_library_config_json, {}) or {}
    if not isinstance(lib, dict):
        lib = {}
    # NEVER flip the master on — a draft must not arm. Preserve whatever it is.
    lib.setdefault("enabled", False)
    ppvs = lib.get("ppvs")
    if not isinstance(ppvs, list):
        ppvs = []
    entry = _ppv_draft_entry(int(item.id), payload)
    ppvs = [p for p in ppvs if not (isinstance(p, dict) and p.get("id") == entry["id"])]
    ppvs.append(entry)
    lib["ppvs"] = ppvs
    row.ppv_library_config_json = json.dumps(lib, ensure_ascii=False)

    item.status = "applied"
    item.resolved_at = now
    return "applied"


# ── Folder application: internal membership (feat 4 + tip/story surface) ──

async def apply_folder_item(
    session, account_id: str, item: VaultAiReviewItem, *, now: datetime,
) -> str:
    """Materialise ONE approved folder proposal as an INTERNAL vault folder +
    memberships (never an OF write). This internal folder is what the tip/story
    paths consume by name downstream. Reads effective explicitness through
    `effective_value` (the one read path) for the applied-note. Returns
    'applied' | 'stale'."""
    if await _is_stale(session, account_id, item):
        return "stale"
    payload = _load_json(item.payload_json, {}) or {}
    folder_name = str(payload.get("folder_name") or "").strip()
    media_ids: list[int] = []
    for m in (payload.get("media_ids") or []):
        try:
            v = int(m)
        except (TypeError, ValueError):
            continue
        if v > 0 and v not in media_ids:
            media_ids.append(v)
    if not folder_name or not media_ids:
        return "stale"

    folder = (
        await session.execute(
            select(VaultFolder).where(
                VaultFolder.account_id == account_id,
                VaultFolder.name == folder_name,
                VaultFolder.deleted_at.is_(None),
            )
        )
    ).scalars().first()
    if folder is None:
        folder = VaultFolder(
            account_id=account_id, name=folder_name, created_by="vault_ai",
        )
        session.add(folder)
        await session.flush()

    # effective_value read path — surface the dominant tier for the applied note
    # (and to keep the tip/story consumer honest about reading through the resolver).
    tiers: list[str] = []
    for mid in media_ids:
        vi = await session.get(VaultItem, (account_id, mid))
        if vi is None:
            continue
        await session.execute(
            sqlite_insert(VaultFolderItem)
            .values(account_id=str(account_id), folder_id=int(folder.id),
                    media_id=int(mid), added_at=now)
            .on_conflict_do_nothing(
                index_elements=["account_id", "folder_id", "media_id"])
        )
        t = str(effective_value(vi, "explicitness_tier", default="") or "").strip().lower()
        if t:
            tiers.append(t)

    log.info("vault_daily_reminder: applied folder=%r account=%s media=%d tiers=%s",
             folder_name, account_id, len(media_ids), sorted(set(tiers)))
    item.status = "applied"
    item.resolved_at = now
    return "applied"


async def consume_actions(
    account_id: str, cfg: dict, *, kinds: tuple[str, ...] = ("ppv", "folder"),
    now: datetime | None = None,
) -> dict[str, Any]:
    """Apply every APPROVED item of the given `kinds` (ppv/folder). Each apply
    re-checks staleness itself and refuses a stale item. Returns per-kind
    applied/stale id lists."""
    now = now or datetime.utcnow()
    out: dict[str, Any] = {"applied": [], "stale": []}
    async with get_session() as s:
        rows = list((await s.execute(
            select(VaultAiReviewItem).where(
                VaultAiReviewItem.account_id == account_id,
                VaultAiReviewItem.kind.in_(kinds),
                VaultAiReviewItem.status == "approved",
            ).order_by(VaultAiReviewItem.id.asc())
        )).scalars().all())
        for row in rows:
            if row.kind == "ppv":
                verdict = await apply_ppv_item(s, account_id, row, now=now)
            elif row.kind == "folder":
                verdict = await apply_folder_item(s, account_id, row, now=now)
            else:
                continue
            out["applied" if verdict == "applied" else "stale"].append(int(row.id))
        await s.commit()
    return out


# ── Self-registering cadence automations ──────────────────────────────

@register("vault_daily_reminder")
async def run(account_id: str, payload: dict, *, run_id: int) -> dict:
    """Cadence: apply approved reminders (send + record-after-success), then
    produce today's reminder card if due and none is open. Master-gated + the
    daily_reminder sub-toggle — nothing produces or sends until both are on."""
    cfg = await _load_config(account_id, None)
    if not cfg.get("enabled"):
        return {"status": "skipped", "reason": "master_off"}
    if not (cfg.get("daily_reminder") or {}).get("enabled"):
        return {"status": "skipped", "reason": "daily_reminder_off"}
    consumed = await consume_reminders(account_id, cfg, run_id=run_id)
    produced = await produce_reminder_card(account_id, cfg)
    return {"consumed": consumed, "produced_card_id": produced}


@register("vault_ai_consume")
async def run_consume(account_id: str, payload: dict, *, run_id: int) -> dict:
    """Cadence: apply approved ppv + folder items (disabled-draft export /
    internal-folder membership). Master-gated. Sends nothing — no fan lease, no
    spend lock (a PPV draft is disabled; a folder is internal)."""
    cfg = await _load_config(account_id, None)
    if not cfg.get("enabled"):
        return {"status": "skipped", "reason": "master_off"}
    return await consume_actions(account_id, cfg)
