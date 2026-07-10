"""service/settings_transfer_api.py — full account-settings export/import.

  GET  /admin/settings-export?account_id=  → {document}
  POST /admin/settings-import              → {mode, applied, report, backup_document, backup_id}

Two modes, decided by `document.account_id == target`:
  restore — same account: everything applied verbatim (media included).
  clone   — different account: everything EXCEPT per-account references.
            Media refs (vault ids/folders) AND identity refs (fan ids, OF list
            ids, tagged creators) are stripped, and the account lands QUIET:
            every imported rule AND every config-blob enabled flag is off until
            the operator reviews the report and re-enables deliberately.

ppv_send rules are never imported — `ppv_library_config_json` is canonical and
the existing `_sync_rules` regenerates them (stamping the TARGET account_id).
Catalog items are never deleted (ContentOffer.item_id is ondelete=CASCADE).
Rules are never deleted (ScheduledJob.rule_id + executor dedupe need stable ids).

Before ANY mutation the target's current settings are exported and atomically
persisted to SETTINGS_BACKUP_DIR (newest 20 per account); a failed backup write
aborts the import untouched. The same preimage returns as `backup_document`.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import delete as sa_delete, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from auth import assert_account_owned
from automation_registry import load_automation_plugins, registered_kinds
from db.engine import get_session
from db.models import (
    Account,
    AccountAiConfig,
    AutomationRule,
    CatalogItem,
    CatalogScript,
    FunnelAccountMedia,
    MassMessageFunnel,
    Prompt,
    SavedReply,
)
from nudge_config_api import _validate_config as _validate_nudge_config
from ppv_library_config_api import _sync_rules, _validate as _validate_ppv_config

log = logging.getLogger("of-relay.settings_transfer_api")

router = APIRouter()

FORMAT = "chatterly-settings"
EXPORT_VERSION = 1
# Import support is decoupled from the current export version: a future v2
# server keeps importing v1 files through this set + per-version adapters.
SUPPORTED_IMPORT_VERSIONS = {1}

SECTIONS = (
    "account_ai_config",
    "automation_rules",
    "funnel_account_media",
    "catalog_scripts",
    "catalog_items",
    "saved_replies",
    "prompts",
)

_MAX_DOC_BYTES = 5 * 1024 * 1024
_MAX_SECTION_ROWS = 2000
_BACKUP_KEEP = 20

# The 17 data columns of account_ai_config (PK + updated_at excluded). The
# export test asserts this list against AccountAiConfig.__table__.columns so a
# future column can't silently fall out of the export.
CONFIG_SCALAR_COLS = ("persona", "welcome_rules", "utc_offset", "location",
                      "daily_cost_cap_cents", "model")
CONFIG_JSON_COLS = ("time_activities_json", "time_images_json", "welcome_pinned_json",
                    "model_by_purpose", "nudge_config_json", "webhook_config_json",
                    "autoreply_config_json", "style_config_json", "tip_reward_config_json",
                    "ai_chatter_config_json", "ppv_library_config_json")
CONFIG_COLS = CONFIG_SCALAR_COLS + CONFIG_JSON_COLS

# Config blobs whose `enabled` flag is consumed by senders/dispatchers OUTSIDE
# the rule executor (e.g. W7 webhook dispatch fires off the WS pump reading
# webhook_config_json directly) — on clone these are forced off too.
_ENABLED_FLAG_BLOBS = ("nudge_config_json", "webhook_config_json", "autoreply_config_json",
                       "tip_reward_config_json", "ai_chatter_config_json",
                       "ppv_library_config_json")

# ── Strip key sets (exact key-name match; booleans like with_image are NOT here).
STRIP_KEYS_MEDIA = frozenset({
    "media_ids", "media_files", "previews", "preview_options",
    "preview_media_files", "media_folder_id", "preview_media_folder_id",
    "preview_media_count", "folder_id", "media_id", "image",
})
# The mis-send class: fan ids, OF list ids, tagged creators. `user_lists` /
# `excluded_user_lists` mix PORTABLE builtin names ("fans") with per-account
# custom-list ids that are digit-STRINGS — those two get a value-level split
# (keep alpha names, drop digit entries); the rest are removed whole.
STRIP_KEYS_IDENTITY = frozenset({
    "list_ids", "exclude_list_ids", "excluded_user_lists", "user_lists",
    "included_users", "excluded_users", "force_ids", "only_fan_ids",
    "refill_ids", "nudge_fan_ids", "fan_ids", "usernames", "targets",
    "mass_run_id", "tagged_users_json",
})
_LIST_SPLIT_KEYS = frozenset({"user_lists", "excluded_user_lists"})

_DIGITS_RE = re.compile(r"^\d+$")


def _known_kinds() -> set[str]:
    # scrape_chats registers via automation_executor (not a plugin file), and
    # live data has a scrape_chats rule — it must not import as "unknown".
    load_automation_plugins()
    return set(registered_kinds()) | {"scrape_chats"}


def _loads(raw: Any, fallback: Any) -> Any:
    if raw in (None, ""):
        return None
    try:
        v = json.loads(raw)
    except Exception:
        return fallback
    return v


# ─────────────────────────────────────────────────────────────── export ──

async def _collect_export(s, account_id: str) -> dict:
    """Build the full v1 settings document for an account."""
    acc = await s.get(Account, account_id)
    cfg = await s.get(AccountAiConfig, account_id)
    cfg_out: dict[str, Any] = {}
    for col in CONFIG_SCALAR_COLS:
        cfg_out[col] = getattr(cfg, col) if cfg is not None else None
    for col in CONFIG_JSON_COLS:
        raw = getattr(cfg, col) if cfg is not None else None
        cfg_out[col] = _loads(raw, raw)  # unparseable → raw string, preserved

    rules = (await s.execute(
        select(AutomationRule).where(AutomationRule.account_id == account_id)
        .order_by(AutomationRule.id)
    )).scalars().all()
    rules_out = [{
        "name": r.name, "kind": r.kind,
        "trigger_json": _loads(r.trigger_json, r.trigger_json),
        "steps_json": _loads(r.steps_json, r.steps_json),
        "is_enabled": bool(r.is_enabled),
        "quiet_hours_json": _loads(r.quiet_hours_json, r.quiet_hours_json),
        "frequency_caps_json": _loads(r.frequency_caps_json, r.frequency_caps_json),
    } for r in rules]

    fam_rows = (await s.execute(
        select(FunnelAccountMedia, MassMessageFunnel.name)
        .join(MassMessageFunnel, MassMessageFunnel.id == FunnelAccountMedia.funnel_id)
        .where(FunnelAccountMedia.account_id == account_id)
        .order_by(FunnelAccountMedia.funnel_id)
    )).all()
    fam_out = [{
        "funnel_id": fam.funnel_id, "funnel_name": fname,
        "opening_media_ids": _loads(fam.opening_media_ids, []) or [],
        "steps_media_json": _loads(fam.steps_media_json, {}) or {},
    } for fam, fname in fam_rows]

    scripts = (await s.execute(
        select(CatalogScript).where(CatalogScript.account_id == account_id)
        .order_by(CatalogScript.id)
    )).scalars().all()
    scripts_out = [{"id": c.id, "name": c.name, "theme": c.theme, "status": c.status}
                   for c in scripts]

    items = (await s.execute(
        select(CatalogItem).where(CatalogItem.account_id == account_id)
        .order_by(CatalogItem.id)
    )).scalars().all()
    items_out = [{
        "id": i.id, "script_id": i.script_id, "position": i.position, "kind": i.kind,
        "label": i.label, "description_for_ai": i.description_for_ai,
        "media_ids": _loads(i.media_ids, []) or [],
        "preview_media_ids": _loads(i.preview_media_ids, []) or [],
        "duration_sec": i.duration_sec, "price_cents": i.price_cents,
        "tip_unlock_cents": i.tip_unlock_cents, "is_free_teaser": bool(i.is_free_teaser),
        "tags": _loads(i.tags, []) or [], "enabled": bool(i.enabled),
    } for i in items]

    replies = (await s.execute(
        select(SavedReply).where(SavedReply.account_id == account_id)
        .order_by(SavedReply.id)
    )).scalars().all()
    replies_out = [{
        "title": r.title, "text": r.text, "price_cents": r.price_cents,
        "locked_text": bool(r.locked_text),
        "media_json": _loads(r.media_json, []) or [],
        "previews_json": _loads(r.previews_json, []) or [],
        "tagged_users_json": _loads(r.tagged_users_json, []) or [],
        "gif_id": r.gif_id, "gif_url": r.gif_url,
        "script_id": r.script_id, "script_step": r.script_step,
    } for r in replies]

    prompts = (await s.execute(
        select(Prompt).where(Prompt.account_id == account_id).order_by(Prompt.id)
    )).scalars().all()
    prompts_out = [{
        "name": p.name, "purpose": p.purpose, "version": p.version, "body": p.body,
        "variables_json": _loads(p.variables_json, []) or [],
        "response_schema_json": _loads(p.response_schema_json, p.response_schema_json),
        "is_active": bool(p.is_active),
    } for p in prompts]

    # funnel_id → name for EVERY funnel referenced anywhere (funnel media rows +
    # rule steps), so a cross-deployment import can remap by unique name.
    funnel_ids: set[int] = {f["funnel_id"] for f in fam_out}
    for r in rules_out:
        steps = r["steps_json"] if isinstance(r["steps_json"], dict) else {}
        for fid in _steps_funnel_ids(steps):
            funnel_ids.add(fid)
    funnel_names: dict[str, str] = {str(f["funnel_id"]): f["funnel_name"] for f in fam_out}
    missing = [fid for fid in funnel_ids if str(fid) not in funnel_names]
    if missing:
        rows = (await s.execute(
            select(MassMessageFunnel.id, MassMessageFunnel.name)
            .where(MassMessageFunnel.id.in_(missing))
        )).all()
        for fid, fname in rows:
            funnel_names[str(fid)] = fname

    return {
        "format": FORMAT,
        "version": EXPORT_VERSION,
        "exported_at": datetime.utcnow().isoformat() + "Z",
        "account_id": account_id,
        "nickname": (acc.nickname if acc is not None else None),
        "funnel_names": funnel_names,
        "sections": {
            "account_ai_config": cfg_out,
            "automation_rules": rules_out,
            "funnel_account_media": fam_out,
            "catalog_scripts": scripts_out,
            "catalog_items": items_out,
            "saved_replies": replies_out,
            "prompts": prompts_out,
        },
    }


def _steps_funnel_ids(steps: dict) -> list[int]:
    """funnel_id refs a rule's steps_json can carry: top-level + messages[]."""
    out: list[int] = []
    for holder in [steps] + [m for m in (steps.get("messages") or []) if isinstance(m, dict)]:
        fid = holder.get("funnel_id")
        if isinstance(fid, int) or (isinstance(fid, str) and _DIGITS_RE.match(fid or "")):
            out.append(int(fid))
    return out


# ───────────────────────────────────────────────────────────── preflight ──

def _preflight(document: Any) -> dict:
    """Strict whole-document validation BEFORE any mutation. 422 on failure.

    An ABSENT section key means a malformed/truncated file and is rejected;
    an explicitly EMPTY list/dict means "empty" and applies as such.
    """
    if not isinstance(document, dict):
        raise HTTPException(422, {"error": "bad_document", "detail": "document must be an object"})
    if document.get("format") != FORMAT:
        raise HTTPException(422, {"error": "bad_format", "expected": FORMAT})
    ver = document.get("version")
    if ver not in SUPPORTED_IMPORT_VERSIONS:
        raise HTTPException(422, {"error": "unsupported_version",
                                  "supported": sorted(SUPPORTED_IMPORT_VERSIONS)})
    if not str(document.get("account_id") or "").strip():
        raise HTTPException(422, {"error": "missing_account_id"})
    try:
        if len(json.dumps(document)) > _MAX_DOC_BYTES:
            raise HTTPException(422, {"error": "document_too_large", "max_bytes": _MAX_DOC_BYTES})
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(422, {"error": "bad_document", "detail": "not JSON-serializable"})
    sections = document.get("sections")
    if not isinstance(sections, dict):
        raise HTTPException(422, {"error": "missing_section", "section": "sections"})
    for name in SECTIONS:
        if name not in sections:
            raise HTTPException(422, {"error": "missing_section", "section": name})
    cfg = sections["account_ai_config"]
    if not isinstance(cfg, dict):
        raise HTTPException(422, {"error": "bad_section", "section": "account_ai_config"})
    for name in SECTIONS[1:]:
        rows = sections[name]
        if not isinstance(rows, list):
            raise HTTPException(422, {"error": "bad_section", "section": name})
        if len(rows) > _MAX_SECTION_ROWS:
            raise HTTPException(422, {"error": "section_too_large", "section": name,
                                      "max_rows": _MAX_SECTION_ROWS})
        for row in rows:
            if not isinstance(row, dict):
                raise HTTPException(422, {"error": "bad_section_row", "section": name})
    for r in sections["automation_rules"]:
        if not str(r.get("kind") or "").strip():
            raise HTTPException(422, {"error": "bad_rule", "detail": "rule missing kind"})
    fn = document.get("funnel_names")
    if fn is not None and not isinstance(fn, dict):
        raise HTTPException(422, {"error": "bad_document", "detail": "funnel_names must be an object"})
    return document


# ──────────────────────────────────────────────────────────── sanitizing ──

class _Report:
    def __init__(self) -> None:
        self.rules: list[dict] = []
        self.rule_counts = {"imported": 0, "updated": 0, "disabled_missing": 0,
                            "skipped_ppv_send": 0, "unknown_disabled": 0}
        self.config_flags_forced_off: dict[str, Any] = {}
        self.created_rows: dict[str, list] = {"catalog_scripts": [], "catalog_items": [],
                                              "saved_replies": [], "prompts": []}
        self.surplus_rows: list[dict] = []
        self.deleted_rows: list[dict] = []
        self.media_stripped: list[str] = []
        self.identity_stripped: list[str] = []
        self.backstop_hits: list[str] = []
        self.warnings: list[str] = []
        self.sections_applied: list[str] = []
        # exact paths the typed pass processed — the backstop skips these so it
        # never re-wipes deliberately-kept portable entries (e.g. ["fans"]).
        self.visited: set[tuple] = set()

    def as_dict(self) -> dict:
        return {
            "sections_applied": self.sections_applied,
            "rules": {**self.rule_counts, "entries": self.rules},
            "config_flags_forced_off": self.config_flags_forced_off,
            "created_rows": self.created_rows,
            "surplus_rows": self.surplus_rows,
            "deleted_rows": self.deleted_rows,
            "media_stripped": self.media_stripped,
            "identity_stripped": self.identity_stripped,
            "backstop_hits": self.backstop_hits,
            "warnings": self.warnings,
        }


def _path_str(path: tuple) -> str:
    return ".".join(str(p) for p in path)


def _split_list_field(values: Any) -> tuple[list, list]:
    """user_lists/excluded_user_lists: keep portable alpha builtin names, drop
    JSON numbers AND all-digit strings (custom list ids never map across
    accounts — and they are digit-STRINGS in production data, never ints)."""
    kept: list = []
    dropped: list = []
    for v in (values if isinstance(values, list) else []):
        if isinstance(v, (int, float)) or (isinstance(v, str) and _DIGITS_RE.match(v)):
            dropped.append(v)
        else:
            kept.append(v)
    return kept, dropped


def _strip_ids_in(holder: dict, path: tuple, rep: _Report) -> tuple[bool, bool]:
    """Typed strip of media+identity keys at ONE dict level. Returns
    (had_media, had_identity) for what was actually removed here."""
    had_media = had_identity = False
    for key in list(holder.keys()):
        kpath = path + (key,)
        val = holder[key]
        if key in _LIST_SPLIT_KEYS:
            kept, dropped = _split_list_field(val)
            rep.visited.add(kpath)
            if dropped:
                holder[key] = kept
                rep.identity_stripped.append(_path_str(kpath))
                had_identity = True
        elif key in STRIP_KEYS_IDENTITY:
            rep.visited.add(kpath)
            non_empty = val not in (None, "", [], {})
            del holder[key]
            if non_empty:
                rep.identity_stripped.append(_path_str(kpath))
                had_identity = True
        elif key in STRIP_KEYS_MEDIA:
            rep.visited.add(kpath)
            non_empty = val not in (None, "", [], {}, 0)
            if isinstance(val, list):
                holder[key] = []
            else:
                del holder[key]
            if non_empty:
                rep.media_stripped.append(_path_str(kpath))
                had_media = True
    return had_media, had_identity


def _sanitize_rule_steps(kind: str, steps: dict, path: tuple, rep: _Report) -> tuple[bool, bool]:
    """Typed clone-strip for a rule's steps_json: media + identity keys at the
    documented sites — top level, messages[] (mass_premade/send_mass_message),
    posts[] (auto_posts), and slots day→slot dicts (mass_nudge/online_blast)."""
    had_media = had_identity = False

    def acc(res: tuple[bool, bool]) -> None:
        nonlocal had_media, had_identity
        had_media |= res[0]
        had_identity |= res[1]

    acc(_strip_ids_in(steps, path, rep))
    for lk in ("messages", "posts"):
        seq = steps.get(lk)
        if isinstance(seq, list):
            for i, entry in enumerate(seq):
                if isinstance(entry, dict):
                    acc(_strip_ids_in(entry, path + (lk, i), rep))
    slots = steps.get("slots")
    if isinstance(slots, dict):
        for day, day_v in slots.items():
            if not isinstance(day_v, dict):
                continue
            for slot, slot_v in day_v.items():
                if isinstance(slot_v, dict):
                    acc(_strip_ids_in(slot_v, path + ("slots", day, slot), rep))
    return had_media, had_identity


def _sanitize_config_clone(cfg: dict, rep: _Report) -> dict:
    """Clone-strip the account_ai_config section (media/identity refs + the
    "clone lands quiet" enabled-flag force-off). Mutates a deep copy."""
    cfg = json.loads(json.dumps(cfg))
    base = ("sections", "account_ai_config")

    # time_images_json: the whole map is {slot → vault media id}.
    ti_path = base + ("time_images_json",)
    rep.visited.add(ti_path)
    if cfg.get("time_images_json"):
        rep.media_stripped.append(_path_str(ti_path))
    cfg["time_images_json"] = {} if cfg.get("time_images_json") is not None else None

    # nudge: slots.*.*.image are vault ids; nudge_line_image/info_line_image are
    # BOOLEANS and stay. enabled forced off below.
    nudge = cfg.get("nudge_config_json")
    if isinstance(nudge, dict):
        slots = nudge.get("slots")
        if isinstance(slots, dict):
            for day, day_v in slots.items():
                if not isinstance(day_v, dict):
                    continue
                for slot, slot_v in day_v.items():
                    if isinstance(slot_v, dict) and "image" in slot_v:
                        kpath = base + ("nudge_config_json", "slots", day, slot, "image")
                        rep.visited.add(kpath)
                        if slot_v.get("image"):
                            rep.media_stripped.append(_path_str(kpath))
                        slot_v["image"] = []

    # tip_reward: tip_request.media_id is a vault id; tiers[].folders are NAMES
    # (portable — resolved per-account at send) and are kept, with a warning.
    tr = cfg.get("tip_reward_config_json")
    if isinstance(tr, dict):
        treq = tr.get("tip_request")
        if isinstance(treq, dict) and "media_id" in treq:
            kpath = base + ("tip_reward_config_json", "tip_request", "media_id")
            rep.visited.add(kpath)
            if treq.get("media_id"):
                rep.media_stripped.append(_path_str(kpath))
            treq["media_id"] = None
        if any(t.get("folders") for t in (tr.get("tiers") or []) if isinstance(t, dict)):
            rep.warnings.append("tip_reward tiers reference vault FOLDER NAMES — "
                                "verify those folders exist on this account")

    # ppv library: strip media leaves, keep captions/prices/cadence; every ppv
    # disabled for re-linking; last_posted_ppv_id is rotation state → cleared.
    ppv = cfg.get("ppv_library_config_json")
    if isinstance(ppv, dict):
        for i, p in enumerate(ppv.get("ppvs") or []):
            if not isinstance(p, dict):
                continue
            for mk in ("media_ids", "preview_options"):
                kpath = base + ("ppv_library_config_json", "ppvs", i, mk)
                rep.visited.add(kpath)
                if p.get(mk):
                    rep.media_stripped.append(_path_str(kpath))
                p[mk] = []
            p["enabled"] = False
        kpath = base + ("ppv_library_config_json", "last_posted_ppv_id")
        rep.visited.add(kpath)
        ppv["last_posted_ppv_id"] = None

    # Clone lands QUIET: config-blob enabled flags are consumed by senders /
    # dispatchers outside the rule executor (W7 reads webhook config off the WS
    # pump) — force every one off, preserving source values in the report.
    for col in _ENABLED_FLAG_BLOBS:
        blob = cfg.get(col)
        if isinstance(blob, dict) and "enabled" in blob:
            rep.config_flags_forced_off[col] = blob.get("enabled")
            blob["enabled"] = False
            rep.visited.add(base + (col, "enabled"))
    return cfg


def _backstop_scrub(node: Any, path: tuple, rep: _Report) -> None:
    """Defense-in-depth: recursive exact-key scrub over everything the typed
    pass did NOT visit. A hit here means the typed spec has a gap — the strip
    still happens (fail closed) and the path lands in backstop_hits for triage."""
    if isinstance(node, dict):
        for key in list(node.keys()):
            kpath = path + (key,)
            if (key in STRIP_KEYS_MEDIA or key in STRIP_KEYS_IDENTITY) and kpath not in rep.visited:
                val = node[key]
                if val not in (None, "", [], {}, 0):
                    rep.backstop_hits.append(_path_str(kpath))
                if isinstance(val, list):
                    node[key] = []
                else:
                    del node[key]
                continue
            _backstop_scrub(node.get(key), kpath, rep)
    elif isinstance(node, list):
        for i, item in enumerate(node):
            _backstop_scrub(item, path + (i,), rep)


# ─────────────────────────────────────────────────────────────── backup ──

def _backup_dir() -> Path:
    raw = os.environ.get("SETTINGS_BACKUP_DIR", "").strip()
    if raw:
        return Path(raw).resolve()
    return (Path(__file__).resolve().parent / "data" / "settings_backups").resolve()


def _write_backup(account_id: str, document: dict) -> str:
    """Atomically persist the preimage; prune to newest _BACKUP_KEEP. Returns the
    opaque backup id (basename). Any failure raises — the caller ABORTS the
    import before mutating anything."""
    adir = _backup_dir() / account_id
    adir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(adir, 0o700)
    except OSError:
        pass
    ts = datetime.utcnow().strftime("%Y%m%dT%H%M%S")
    name = f"{ts}-{uuid.uuid4().hex[:8]}.json"
    tmp = adir / (name + ".tmp")
    data = json.dumps(document, ensure_ascii=False, indent=2)
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, adir / name)
    try:  # fsync the dir so the rename survives a host crash
        dfd = os.open(adir, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass
    backups = sorted(p for p in adir.iterdir() if p.suffix == ".json")
    for old in backups[:-_BACKUP_KEEP]:
        try:
            old.unlink()
        except OSError:
            pass
    return name


# ─────────────────────────────────────────────────────────────── routes ──

@router.get("/admin/settings-export")
async def get_settings_export(account_id: str = Query(...)) -> dict[str, Any]:
    assert_account_owned(account_id)
    async with get_session() as s:
        document = await _collect_export(s, account_id)
    return {"document": document}


class _ImportBody(BaseModel):
    account_id: str
    document: dict


@router.post("/admin/settings-import")
async def post_settings_import(body: _ImportBody = Body(...)) -> dict[str, Any]:
    target = body.account_id
    # The TARGET is the only DB resource touched; the document's account_id is
    # provenance (it may not even exist on this deployment).
    assert_account_owned(target)
    document = _preflight(body.document)
    mode = "restore" if str(document["account_id"]) == str(target) else "clone"
    rep = _Report()

    async with get_session() as s:
        acc = await s.get(Account, target)
        if acc is None:
            raise HTTPException(404, {"error": "unknown_account", "account_id": target})

        # Backup FIRST (abort-before-mutation on any failure), and prove the
        # preimage is itself re-importable through the same preflight.
        backup_document = await _collect_export(s, target)
        try:
            _preflight(backup_document)
        except HTTPException as exc:
            raise HTTPException(500, {"error": "backup_not_reimportable", "detail": exc.detail})
        try:
            backup_id = await asyncio.to_thread(_write_backup, target, backup_document)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(500, {"error": "backup_write_failed", "detail": str(exc)})

        sections = json.loads(json.dumps(document["sections"]))  # deep copy
        funnel_names: dict[str, str] = {str(k): str(v) for k, v in
                                        (document.get("funnel_names") or {}).items()}

        # ── sanitize (clone) ──
        cfg_in: dict = sections["account_ai_config"]
        if mode == "clone":
            cfg_in = _sanitize_config_clone(cfg_in, rep)
            sections["account_ai_config"] = cfg_in

        rules_in: list[dict] = []
        for idx, r in enumerate(sections["automation_rules"]):
            kind = str(r.get("kind") or "")
            if kind == "ppv_send":
                # Derived — regenerated from ppv_library_config_json by _sync_rules.
                rep.rule_counts["skipped_ppv_send"] += 1
                continue
            entry = {
                "source_ordinal": idx, "kind": kind, "name": str(r.get("name") or kind),
                "match_method": None, "target_rule_id": None,
                "source_enabled": bool(r.get("is_enabled")), "final_enabled": None,
                "remediation": "safe",
            }
            steps = r.get("steps_json")
            steps = steps if isinstance(steps, dict) else (_loads(steps, None) or {})
            spath = ("sections", "automation_rules", idx, "steps_json")
            if mode == "clone":
                had_media, had_identity = _sanitize_rule_steps(kind, steps, spath, rep)
                if had_identity:
                    entry["remediation"] = "repick_audience"
                elif had_media:
                    entry["remediation"] = "relink_media"
            if kind not in _known_kinds():
                entry["remediation"] = "review"
                rep.rule_counts["unknown_disabled"] += 1
                enabled = False
            else:
                enabled = bool(r.get("is_enabled"))
            if mode == "clone":
                enabled = False  # clone lands quiet — re-enable deliberately
            entry["final_enabled"] = enabled
            rules_in.append({"rule": r, "steps": steps, "enabled": enabled,
                             "entry": entry, "spath": spath})

        if mode == "clone":
            # Backstop over EVERYTHING the typed pass didn't visit (fail closed;
            # a hit = typed-spec gap, reported loudly, import still commits).
            # Rule steps are scrubbed at their ORIGINAL document paths so the
            # visited-path exemption lines up exactly with the typed pass.
            _backstop_scrub({"account_ai_config": cfg_in}, ("sections",), rep)
            for ri in rules_in:
                _backstop_scrub(ri["steps"], ri["spath"], rep)
            for sec in ("catalog_items", "saved_replies"):
                for i, row in enumerate(sections[sec]):
                    if sec == "catalog_items":
                        for mk in ("media_ids", "preview_media_ids"):
                            kpath = ("sections", sec, i, mk)
                            rep.visited.add(kpath)
                            if row.get(mk):
                                rep.media_stripped.append(_path_str(kpath))
                            row[mk] = []
                    else:
                        for mk in ("media_json", "previews_json"):
                            kpath = ("sections", sec, i, mk)
                            rep.visited.add(kpath)
                            if row.get(mk):
                                rep.media_stripped.append(_path_str(kpath))
                            row[mk] = []
                        kpath = ("sections", sec, i, "tagged_users_json")
                        rep.visited.add(kpath)
                        if row.get("tagged_users_json"):
                            rep.identity_stripped.append(_path_str(kpath))
                        row["tagged_users_json"] = []
            if rep.media_stripped and any(sections["catalog_items"]):
                rep.warnings.append("catalog item media cleared — re-link media from this "
                                    "account's vault before enabling ai_chatter selling")

        # ── validators: CLONE blobs go through the same validators UI saves
        # use (media-stripped configs must satisfy the same invariants — the
        # normalized form is stored). RESTORE writes blobs VERBATIM so the
        # roundtrip stays byte-identical (incl. rotation state like
        # last_posted_ppv_id); the validated COPY only feeds _sync_rules.
        ppv_cfg_raw = cfg_in.get("ppv_library_config_json")
        ppv_for_sync: dict | None = None
        if isinstance(ppv_cfg_raw, dict):
            try:
                ppv_for_sync = _validate_ppv_config(ppv_cfg_raw)
            except HTTPException as exc:
                if mode == "clone":
                    raise
                rep.warnings.append(
                    f"ppv config failed today's validator on restore ({exc.detail}) — "
                    "stored verbatim; ppv_send rules left untouched")
            if mode == "clone" and ppv_for_sync is not None:
                cfg_in["ppv_library_config_json"] = ppv_for_sync
        nudge_raw = cfg_in.get("nudge_config_json")
        if mode == "clone" and isinstance(nudge_raw, dict):
            cfg_in["nudge_config_json"] = _validate_nudge_config(nudge_raw)

        # ── (b) upsert all 17 account_ai_config columns in one statement ──
        now = datetime.utcnow()
        values: dict[str, Any] = {"account_id": target, "updated_at": now}
        for col in CONFIG_SCALAR_COLS:
            values[col] = cfg_in.get(col)
        if values.get("utc_offset") is None:
            values["utc_offset"] = 0
        if values.get("daily_cost_cap_cents") is None:
            values["daily_cost_cap_cents"] = 100
        for col in CONFIG_JSON_COLS:
            v = cfg_in.get(col)
            values[col] = json.dumps(v) if isinstance(v, (dict, list)) else v
        await s.execute(
            sqlite_insert(AccountAiConfig).values(**values).on_conflict_do_update(
                index_elements=["account_id"],
                set_={k: v for k, v in values.items() if k != "account_id"})
        )
        rep.sections_applied.append("account_ai_config")

        # ── (c) reconcile rules in place — never delete ──
        existing_rules = (await s.execute(
            select(AutomationRule).where(
                AutomationRule.account_id == target,
                AutomationRule.kind != "ppv_send",  # _sync_rules' domain
            ).order_by(AutomationRule.id)
        )).scalars().all()
        by_kind: dict[str, list[AutomationRule]] = {}
        for er in existing_rules:
            by_kind.setdefault(er.kind, []).append(er)
        matched_ids: set[int] = set()

        def _dumps_maybe(v: Any) -> str | None:
            if v is None:
                return None
            return json.dumps(v) if isinstance(v, (dict, list)) else str(v)

        for kind in sorted({ri["entry"]["kind"] for ri in rules_in}):
            imps = [ri for ri in rules_in if ri["entry"]["kind"] == kind]
            pool = [er for er in by_kind.get(kind, [])]
            # name match first, then positional, then insert
            for ri in imps:
                hit = next((er for er in pool if er.name == ri["entry"]["name"]), None)
                if hit is not None:
                    ri["match"] = hit
                    ri["entry"]["match_method"] = ("kind" if len(imps) == 1 and
                                                   len(by_kind.get(kind, [])) == 1 else "kind_name")
                    pool.remove(hit)
            rest = [ri for ri in imps if "match" not in ri]
            for ri, er in zip(rest, pool):
                ri["match"] = er
                ri["entry"]["match_method"] = "positional"
            # leftover existing rules are disabled after the apply loop

        for ri in rules_in:
            r, entry = ri["rule"], ri["entry"]
            trigger = _dumps_maybe(r.get("trigger_json")) or "{}"
            steps = json.dumps(ri["steps"])
            quiet = _dumps_maybe(r.get("quiet_hours_json"))
            caps = _dumps_maybe(r.get("frequency_caps_json"))
            er = ri.get("match")
            if er is not None:
                er.name = entry["name"]
                er.trigger_json = trigger
                er.steps_json = steps
                er.is_enabled = ri["enabled"]
                er.quiet_hours_json = quiet
                er.frequency_caps_json = caps
                matched_ids.add(er.id)
                entry["target_rule_id"] = er.id
                rep.rule_counts["updated"] += 1
            else:
                nr = AutomationRule(account_id=target, name=entry["name"], kind=entry["kind"],
                                    trigger_json=trigger, steps_json=steps,
                                    is_enabled=ri["enabled"], quiet_hours_json=quiet,
                                    frequency_caps_json=caps)
                s.add(nr)
                await s.flush()
                matched_ids.add(nr.id)
                entry["target_rule_id"] = nr.id
                entry["match_method"] = "inserted"
                rep.rule_counts["imported"] += 1
            rep.rules.append(entry)

        for er in existing_rules:
            if er.id not in matched_ids and er.is_enabled:
                er.is_enabled = False
                rep.rule_counts["disabled_missing"] += 1
                rep.rules.append({"source_ordinal": None, "kind": er.kind, "name": er.name,
                                  "match_method": "disabled_missing", "target_rule_id": er.id,
                                  "source_enabled": True, "final_enabled": False,
                                  "remediation": "review"})
        rep.sections_applied.append("automation_rules")

        # ── (d) regenerate ppv_send rules from the canonical config ──
        if isinstance(ppv_cfg_raw, dict) and ppv_for_sync is None:
            pass  # restore of an unvalidatable legacy blob: leave ppv rules as-is
        else:
            await _sync_rules(s, target,
                              ppv_for_sync if ppv_for_sync is not None
                              else {"enabled": False, "ppvs": []})

        # ── (e) catalog: scripts upsert by name; items id-preserving upserts.
        # NEVER delete items — ContentOffer.item_id is ondelete=CASCADE.
        existing_scripts = (await s.execute(
            select(CatalogScript).where(CatalogScript.account_id == target)
        )).scalars().all()
        script_by_name = {cs.name: cs for cs in existing_scripts}
        script_map: dict[int, int] = {}
        for row in sections["catalog_scripts"]:
            name = str(row.get("name") or "").strip()
            if not name:
                continue
            cs = script_by_name.get(name)
            if cs is None:
                cs = CatalogScript(account_id=target, name=name,
                                   theme=row.get("theme"),
                                   status=str(row.get("status") or "draft"))
                s.add(cs)
                await s.flush()
                script_by_name[name] = cs
                rep.created_rows["catalog_scripts"].append(cs.id)
            else:
                cs.theme = row.get("theme")
                cs.status = str(row.get("status") or cs.status)
            if row.get("id") is not None:
                script_map[int(row["id"])] = cs.id
        matched_script_ids = {cs.id for cs in script_by_name.values()}
        for cs in existing_scripts:
            if cs.name not in {str(r.get("name") or "").strip() for r in sections["catalog_scripts"]}:
                rep.surplus_rows.append({"entity": "catalog_scripts", "target_id": cs.id,
                                         "identifying_fields": {"name": cs.name},
                                         "reason": "unmatched_existing"})

        existing_items = (await s.execute(
            select(CatalogItem).where(CatalogItem.account_id == target)
            .order_by(CatalogItem.id)
        )).scalars().all()
        items_pool: dict[int | None, list[CatalogItem]] = {}
        for it in existing_items:
            items_pool.setdefault(it.script_id, []).append(it)
        used_item_ids: set[int] = set()
        for row in sections["catalog_items"]:
            old_sid = row.get("script_id")
            new_sid = script_map.get(int(old_sid)) if old_sid is not None else None
            if old_sid is not None and new_sid is None:
                rep.warnings.append(f"catalog item '{row.get('label')}' references a script "
                                    "not present in the document — imported as a single")
            pool = [it for it in items_pool.get(new_sid, []) if it.id not in used_item_ids]
            label = row.get("label")
            same_label = [it for it in pool if it.label == label]
            target_item = same_label[0] if len(same_label) == 1 else (pool[0] if pool else None)
            fields = dict(
                script_id=new_sid, position=row.get("position"),
                kind=str(row.get("kind") or "video"), label=label,
                description_for_ai=row.get("description_for_ai"),
                media_ids=json.dumps(row.get("media_ids") or []),
                preview_media_ids=json.dumps(row.get("preview_media_ids") or []),
                duration_sec=row.get("duration_sec"),
                price_cents=int(row.get("price_cents") or 0),
                tip_unlock_cents=int(row.get("tip_unlock_cents") or 0),
                is_free_teaser=bool(row.get("is_free_teaser")),
                tags=json.dumps(row.get("tags") or []),
                enabled=bool(row.get("enabled", True)),
            )
            if target_item is not None:
                for k, v in fields.items():
                    setattr(target_item, k, v)
                used_item_ids.add(target_item.id)
            else:
                ni = CatalogItem(account_id=target, **fields)
                s.add(ni)
                await s.flush()
                used_item_ids.add(ni.id)
                rep.created_rows["catalog_items"].append(ni.id)
        for it in existing_items:
            if it.id not in used_item_ids:
                rep.surplus_rows.append({"entity": "catalog_items", "target_id": it.id,
                                         "identifying_fields": {"label": it.label,
                                                                "script_id": it.script_id},
                                         "reason": "unmatched_existing"})
        rep.sections_applied += ["catalog_scripts", "catalog_items"]

        # ── (f) saved replies: script_id passes through VERBATIM (free-text
        # template-group name, NOT a catalog FK). Title → positional → insert.
        existing_replies = (await s.execute(
            select(SavedReply).where(SavedReply.account_id == target)
            .order_by(SavedReply.id)
        )).scalars().all()
        used_reply_ids: set[int] = set()
        for row in sections["saved_replies"]:
            title = row.get("title")
            # title match, positional among same-title/untitled rows (id order):
            # avail is id-ordered with used rows excluded, so repeated titles /
            # untitled rows pair up positionally — restore stays duplication-free.
            avail = [sr for sr in existing_replies if sr.id not in used_reply_ids]
            same_title = [sr for sr in avail if sr.title == title]
            tr = same_title[0] if same_title else None
            fields = dict(
                title=title, text=str(row.get("text") or ""),
                price_cents=int(row.get("price_cents") or 0),
                locked_text=bool(row.get("locked_text")),
                media_json=json.dumps(row.get("media_json") or []),
                previews_json=json.dumps(row.get("previews_json") or []),
                tagged_users_json=json.dumps(row.get("tagged_users_json") or []),
                gif_id=row.get("gif_id"), gif_url=row.get("gif_url"),
                script_id=row.get("script_id"), script_step=row.get("script_step"),
                updated_at=now,
            )
            if tr is not None:
                for k, v in fields.items():
                    setattr(tr, k, v)
                used_reply_ids.add(tr.id)
            else:
                nr = SavedReply(account_id=target, **fields)
                s.add(nr)
                await s.flush()
                used_reply_ids.add(nr.id)
                rep.created_rows["saved_replies"].append(nr.id)
        for sr in existing_replies:
            if sr.id not in used_reply_ids:
                rep.surplus_rows.append({"entity": "saved_replies", "target_id": sr.id,
                                         "identifying_fields": {"title": sr.title},
                                         "reason": "unmatched_existing"})
        rep.sections_applied.append("saved_replies")

        # ── (j) prompts: account-scoped, upsert by (name, version) ──
        existing_prompts = (await s.execute(
            select(Prompt).where(Prompt.account_id == target)
        )).scalars().all()
        p_by_key = {(p.name, p.version): p for p in existing_prompts}
        seen_pkeys: set[tuple] = set()
        for row in sections["prompts"]:
            key = (str(row.get("name") or ""), int(row.get("version") or 1))
            seen_pkeys.add(key)
            pr = p_by_key.get(key)
            fields = dict(
                purpose=str(row.get("purpose") or ""), body=str(row.get("body") or ""),
                variables_json=json.dumps(row.get("variables_json") or []),
                response_schema_json=(json.dumps(row["response_schema_json"])
                                      if isinstance(row.get("response_schema_json"), (dict, list))
                                      else row.get("response_schema_json")),
                is_active=bool(row.get("is_active", True)),
                created_by_employee_id=None,  # employee ids don't map across deployments
            )
            if pr is not None:
                for k, v in fields.items():
                    setattr(pr, k, v)
            else:
                np = Prompt(account_id=target, name=key[0], version=key[1], **fields)
                s.add(np)
                await s.flush()
                rep.created_rows["prompts"].append(np.id)
        for p in existing_prompts:
            if (p.name, p.version) not in seen_pkeys:
                rep.surplus_rows.append({"entity": "prompts", "target_id": p.id,
                                         "identifying_fields": {"name": p.name,
                                                                "version": p.version},
                                         "reason": "unmatched_existing"})
        rep.sections_applied.append("prompts")

        # ── (g) funnel media: RESTORE ONLY — full-surface replacement ──
        if mode == "restore":
            local_funnels = {f.name: f.id for f in (await s.execute(
                select(MassMessageFunnel))).scalars().all()}
            existing_fam = (await s.execute(
                select(FunnelAccountMedia).where(FunnelAccountMedia.account_id == target)
            )).scalars().all()
            fam_by_fid = {f.funnel_id: f for f in existing_fam}
            kept_fids: set[int] = set()
            for row in sections["funnel_account_media"]:
                fname = str(row.get("funnel_name") or funnel_names.get(str(row.get("funnel_id")), ""))
                fid = local_funnels.get(fname)
                if fid is None:
                    rep.warnings.append(f"funnel media for '{fname or row.get('funnel_id')}' "
                                        "skipped — no funnel with that name here")
                    continue
                kept_fids.add(fid)
                fam = fam_by_fid.get(fid)
                if fam is None:
                    s.add(FunnelAccountMedia(
                        funnel_id=fid, account_id=target,
                        opening_media_ids=json.dumps(row.get("opening_media_ids") or []),
                        steps_media_json=json.dumps(row.get("steps_media_json") or {})))
                else:
                    fam.opening_media_ids = json.dumps(row.get("opening_media_ids") or [])
                    fam.steps_media_json = json.dumps(row.get("steps_media_json") or {})
                    fam.updated_at = now
            for fam in existing_fam:
                if fam.funnel_id not in kept_fids:
                    rep.deleted_rows.append({"entity": "funnel_account_media",
                                             "funnel_id": fam.funnel_id})
            if any(f.funnel_id not in kept_fids for f in existing_fam):
                conds = [FunnelAccountMedia.account_id == target]
                if kept_fids:
                    conds.append(FunnelAccountMedia.funnel_id.notin_(kept_fids))
                await s.execute(sa_delete(FunnelAccountMedia).where(*conds))
            rep.sections_applied.append("funnel_account_media")
        else:
            rep.warnings.append("funnel media not imported on clone — vault ids are "
                                "per-account; bind this account's media in the funnel editor")

        # ── (i) funnel-reference remap in reconciled rule steps ──
        local_by_name = {f.name: f.id for f in (await s.execute(
            select(MassMessageFunnel))).scalars().all()}
        for ri in rules_in:
            er_id = ri["entry"]["target_rule_id"]
            steps = ri["steps"]
            changed = False
            for holder in [steps] + [m for m in (steps.get("messages") or [])
                                     if isinstance(m, dict)]:
                fid = holder.get("funnel_id")
                if fid is None:
                    continue
                fname = funnel_names.get(str(fid))
                local = local_by_name.get(fname) if fname else None
                if local is not None:
                    if local != fid:
                        holder["funnel_id"] = local
                        changed = True
                else:
                    row = (await s.execute(select(MassMessageFunnel.id).where(
                        MassMessageFunnel.id == (int(fid) if str(fid).isdigit() else -1)
                    ))).first()
                    if row is None:
                        rep.warnings.append(
                            f"rule '{ri['entry']['name']}' references funnel_id {fid} "
                            "which doesn't exist here — rule disabled")
                        ri["entry"]["final_enabled"] = False
                        rule_obj = await s.get(AutomationRule, er_id)
                        if rule_obj is not None:
                            rule_obj.is_enabled = False
            if changed and er_id is not None:
                rule_obj = await s.get(AutomationRule, er_id)
                if rule_obj is not None:
                    rule_obj.steps_json = json.dumps(steps)

        # unknown top-level sections → ignored + reported
        for extra in set(document["sections"]) - set(SECTIONS):
            rep.warnings.append(f"unknown section '{extra}' ignored")

    log.info("settings_import account=%s mode=%s rules=%s backstop_hits=%d",
             target, mode, rep.rule_counts, len(rep.backstop_hits))
    return {"mode": mode, "applied": True, "report": rep.as_dict(),
            "backup_document": backup_document, "backup_id": backup_id}
