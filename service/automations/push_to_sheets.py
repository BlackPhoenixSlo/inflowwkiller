"""service/automations/push_to_sheets.py — Automation A04: push_to_sheets.

Spec: library/one_section_of_automations/04_push_to_sheets.md.

TERMINAL / EXTERNAL node. Read-only on OUR DB, write-only to ONE Google Sheet —
there is NO OnlyFans traffic here at all. Per tick (`run(account_id, payload,
*, run_id)`):

  1. Read `fan_profiles` (the export's primary row set), `fans` (display name +
     last-touch), and `transactions` (live lifetime spend) for the account — each
     load on its OWN AsyncSession (the SQLAlchemy parallel-session footgun: never
     share one across branches). NO writes to any of these tables.
  2. Build a header row + one row per profiled fan.
  3. Clear the target tab's `A:Z`, then write the rows from `A1` via the Google
     Sheets v4 API. The blocking Google client runs inside `asyncio.to_thread` so
     it never stalls the event loop.
  4. Return a stats dict → the executor writes the `automation_runs` row
     (kind/started/finished/status/stats_json/error_text). A clean run records
     "ok"; an auth/transport failure raises and the executor records "error" —
     either way a run row lands.

CREDENTIAL ISOLATION (spec "Isolate the Google creds (env, not hardcoded)"):
    Nothing secret lives in this file. The OAuth client + cached token are read
    at runtime from files (gitignored) whose paths are env-overridable, or — for
    headless/containerised deploys — straight from an env var holding the token
    JSON. Resolution order for every knob is payload → env (process, then
    service/.env) → built-in default:

      GOOGLE_SHEETS_SPREADSHEET_ID   target spreadsheet id
      GOOGLE_SHEETS_TAB              target tab/sheet name
      GOOGLE_SHEETS_CREDENTIALS_FILE OAuth client secrets ("Desktop" type)
      GOOGLE_SHEETS_TOKEN_FILE       cached token (refresh + access)
      GOOGLE_SHEETS_TOKEN_JSON       token JSON inline (wins over the file; for
                                     servers that mount a secret, not a file)

HEADLESS-SAFE AUTH: the automation NEVER opens a browser. It loads the cached
token and, if expired, refreshes it with the stored refresh-token. If there is no
usable token it raises `PushToSheetsAuthError` (recorded as the run's error_text)
rather than blocking on `run_local_server`. The one-time interactive mint is a
human step — run `python -m automations.push_to_sheets --bootstrap` locally to
create token.json, then deploy that file (or its JSON via the env var).

Payload knobs (all optional): `spreadsheet_id`, `sheet_tab`, `limit` (export
ceiling), `create_tab` (auto-add the tab if absent — default True).

Scheduling: self-registers via `@register("push_to_sheets")` on import. Insert an
`automation_rules` row with `kind="push_to_sheets"` + `trigger_json=
{"every_seconds": N}` to run it periodically. NO edit to automation_executor.py.
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values, load_dotenv
from sqlalchemy import func, select

from automation_registry import register
from db.engine import get_session
from db.models import Fan, FanProfile, Transaction
from ._common import build_facts_note

log = logging.getLogger("of-relay.automation.push_to_sheets")

# Mirror llm_client / db.engine: backfill from service/.env without clobbering a
# value the host already exported (override=False).
_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(_ENV_FILE, override=False)

# Repo root holds the gitignored credentials.json / token.json by default
# (service/automations/ → service/ → <repo>).
_REPO_ROOT = Path(__file__).resolve().parents[2]

# Read/write a single spreadsheet. Narrowest scope that lets us clear + update.
_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# The live export target (the GOOGLESHEETS the operator shared). Overridable per
# run via payload / env so this default is convenience, not a hardcoded secret.
_DEFAULT_SPREADSHEET_ID = "1Al-dprIj-G_bC0UgAURnkXL2YnHyA5m0uz4AD1jsGhY"
_DEFAULT_SHEET_TAB = "Main"   # the shared tab the team reads; override per-account via env

_DEFAULT_LIMIT = 5000  # profiled fans per export — bounds a runaway sheet write

# The header is the export contract (spec §Outputs). Keep in lockstep with
# `_profile_row` below — column order is positional.
_HEADER = [
    "fan_name", "fan_id", "chat_id", "nickname", "short_bio", "bullet_points",
    "Q1", "Q2", "Q3", "Tease1", "Tease2", "Tease3",
    "total_spend", "message_count", "last_updated", "note_on_of",
]


class PushToSheetsError(RuntimeError):
    """Configuration / transport failure that should fail the run loudly."""


class PushToSheetsAuthError(PushToSheetsError):
    """No usable Google token and we refuse to open a browser in the worker."""


# ── Config resolution (payload → process-env → service/.env → default) ─────

def _env(name: str) -> str:
    """Setup → Keys store wins, then process env, then service/.env (same
    recovery llm_client uses for keys docker-compose injects as empty)."""
    try:
        from secrets_store import stored as _stored
        s = _stored(name)
        if s:
            return s
    except Exception:  # never let the key store crash the export
        pass
    val = (os.environ.get(name) or "").strip()
    if val:
        return val
    try:
        return (dotenv_values(_ENV_FILE).get(name) or "").strip()
    except Exception:  # pragma: no cover — never let config lookup crash the run
        return ""


@dataclass(frozen=True)
class _Cfg:
    spreadsheet_id: str
    sheet_tab: str
    credentials_file: str
    token_file: str
    token_json: str  # inline token JSON, "" when not supplied
    create_tab: bool


def _resolve_cfg(account_id: str, payload: dict) -> _Cfg:
    spreadsheet_id = (
        str(payload.get("spreadsheet_id") or "").strip()
        or _env("GOOGLE_SHEETS_SPREADSHEET_ID")
        or _DEFAULT_SPREADSHEET_ID
    )
    # Default to the single shared "Main" tab — that's the sheet the team actually
    # reads. A payload/env tab still overrides (e.g. GOOGLE_SHEETS_TAB="<account_id>"
    # to split multiple accounts/models into their own tabs without collisions).
    sheet_tab = (
        str(payload.get("sheet_tab") or "").strip()
        or _env("GOOGLE_SHEETS_TAB")
        or _DEFAULT_SHEET_TAB
    )
    credentials_file = _env("GOOGLE_SHEETS_CREDENTIALS_FILE") or str(
        _REPO_ROOT / "credentials.json"
    )
    token_file = _env("GOOGLE_SHEETS_TOKEN_FILE") or str(_REPO_ROOT / "token.json")
    create_tab = payload.get("create_tab")
    create_tab = True if create_tab is None else bool(create_tab)
    return _Cfg(
        spreadsheet_id=spreadsheet_id,
        sheet_tab=sheet_tab,
        credentials_file=credentials_file,
        token_file=token_file,
        token_json=_env("GOOGLE_SHEETS_TOKEN_JSON"),
        create_tab=create_tab,
    )


# ── DB seams (own session each — house pattern) ────────────────────────────

@dataclass
class _Row:
    """Detached snapshot of one profiled fan, joined with its fans row +
    live spend. Built under the load sessions so they close before the push."""
    fan_id: int
    fan_name: str
    nickname: str
    short_bio: str
    bullet_points: str
    q1: str
    q2: str
    q3: str
    tease1: str
    tease2: str
    tease3: str
    total_spend_cents: int
    message_count: int
    last_updated: str
    note_on_of: str


def _s(v: object) -> str:
    return "" if v is None else str(v)


async def _load_profiles(account_id: str, limit: int) -> list[FanProfile]:
    async with get_session() as s:
        return list(
            (await s.execute(
                select(FanProfile)
                .where(FanProfile.account_id == account_id)
                .order_by(FanProfile.fan_id)
                .limit(limit)
            )).scalars().all()
        )


async def _load_fans(account_id: str, fan_ids: list[int]) -> dict[int, Fan]:
    """fan_id → Fan, for the display name + last-touch columns. Own session.
    Chunked at 900 ids so a multi-thousand export never trips SQLite's
    bind-variable cap (999 on older builds)."""
    out: dict[int, Fan] = {}
    if not fan_ids:
        return out
    async with get_session() as s:
        for i in range(0, len(fan_ids), 900):
            chunk = fan_ids[i:i + 900]
            rows = (await s.execute(
                select(Fan)
                .where(Fan.account_id == account_id)
                .where(Fan.fan_id.in_(chunk))
            )).scalars().all()
            out.update({int(f.fan_id): f for f in rows})
    return out


async def _load_spend(account_id: str) -> dict[int, int]:
    """fan_id → SUM(amount_cents) over CLEARED transactions (the status='cleared'
    contract; pending payouts excluded). Own session. fan_id NULL rows (sub
    rebills) are dropped — they aren't attributable to a profiled fan."""
    async with get_session() as s:
        rows = (await s.execute(
            select(
                Transaction.fan_id,
                func.sum(Transaction.amount_cents),
            )
            .where(Transaction.account_id == account_id)
            .where(Transaction.status == "cleared")
            .where(Transaction.fan_id.is_not(None))
            .group_by(Transaction.fan_id)
        )).all()
    return {int(fid): int(total or 0) for fid, total in rows}


def _build_rows(
    profiles: list[FanProfile],
    fans: dict[int, Fan],
    spend: dict[int, int],
) -> list[_Row]:
    out: list[_Row] = []
    for p in profiles:
        fid = int(p.fan_id)
        fan = fans.get(fid)
        # Best available human label, then the profile's captured original name.
        fan_name = ""
        if fan is not None:
            fan_name = _s(fan.of_display_name) or _s(fan.of_username)
        fan_name = fan_name or _s(p.original_name) or str(fid)
        # Profile nickname is canonical; fall back to whatever label the fans row
        # carries so the column is rarely blank.
        nickname = _s(p.nickname)
        if not nickname and fan is not None:
            nickname = _s(fan.generated_nickname) or _s(fan.custom_nickname)
        # Profile freshness drives "last_updated"; fall back to the fans row.
        last_updated = ""
        if p.last_generated_at is not None:
            last_updated = p.last_generated_at.isoformat()
        elif fan is not None and fan.updated_at is not None:
            last_updated = fan.updated_at.isoformat()
        # bullet_points = the SAME useful-first fact line apply_profiles pushes to
        # the drawer note (Recent → Intr → Job → Rel → Kinks, then short_bio at the
        # bottom; EMPTY fields skipped; name/age/loc/spend omitted — they're in the
        # nickname / total_spend column). Derived from the fans-row facts (no
        # gen_info regen needed). A larger cap than the OF note so the bio fits.
        facts = {} if fan is None else {
            "hobbies": _s(fan.hobbies),
            "job": _s(fan.occupation),
            "relationship": _s(fan.relationship_status),
            "fetishes": _s(fan.fetishes),
            "recent_events": _s(fan.recent_events),
        }
        bullets = build_facts_note(facts, max_len=2000, short_bio=_s(p.short_bio))
        # "added" iff the note is actually on OF. We mirror `fans.notes` ONLY when the
        # OF notice push succeeds (and get_user syncs it from OF), so a non-empty
        # mirror == the note reached OnlyFans; non-subscribers (notice 404s) stay
        # "not added". The bullet line above is what SHOULD be there either way.
        note_on_of = "added" if (fan is not None and _s(fan.notes).strip()) else "not added"
        out.append(_Row(
            fan_id=fid,
            fan_name=fan_name,
            nickname=nickname,
            short_bio=_s(p.short_bio),
            bullet_points=bullets,
            q1=_s(p.q1), q2=_s(p.q2), q3=_s(p.q3),
            tease1=_s(p.tease1), tease2=_s(p.tease2), tease3=_s(p.tease3),
            total_spend_cents=int(spend.get(fid, 0)),
            message_count=int(p.message_count_at_gen or 0),
            last_updated=last_updated,
            note_on_of=note_on_of,
        ))
    return out


async def build_fan_data(account_id: str, limit: int = _DEFAULT_LIMIT) -> list[dict]:
    """The push_to_sheets export, as per-fan dicts — for read surfaces (the Stats
    'User data' tab) that want to SEE what gets pushed to the Google Sheet without
    a sheet round-trip. Same load + row build as `run`, so the columns stay in
    lockstep with `_HEADER`. total_spend is dollars (cents/100), like the sheet."""
    profiles = await _load_profiles(account_id, limit)
    fans = await _load_fans(account_id, [int(p.fan_id) for p in profiles])
    spend = await _load_spend(account_id)
    out: list[dict] = []
    for r in _build_rows(profiles, fans, spend):
        out.append({
            "account_id": account_id,
            "fan_name": r.fan_name, "fan_id": r.fan_id, "chat_id": r.fan_id,
            "nickname": r.nickname, "short_bio": r.short_bio,
            "bullet_points": r.bullet_points,
            "q1": r.q1, "q2": r.q2, "q3": r.q3,
            "tease1": r.tease1, "tease2": r.tease2, "tease3": r.tease3,
            "total_spend": round(r.total_spend_cents / 100.0, 2),
            "message_count": r.message_count,
            "last_updated": r.last_updated, "note_on_of": r.note_on_of,
        })
    return out


def _to_values(rows: list[_Row]) -> list[list[object]]:
    """Header + one list-of-cells per fan, column order == `_HEADER`. Numeric
    columns stay numeric (RAW input) so the sheet can sort/aggregate them; the
    OF chat id equals the fan/user id on this platform, so chat_id == fan_id."""
    values: list[list[object]] = [list(_HEADER)]
    for r in rows:
        values.append([
            r.fan_name, r.fan_id, r.fan_id, r.nickname, r.short_bio,
            r.bullet_points, r.q1, r.q2, r.q3, r.tease1, r.tease2, r.tease3,
            round(r.total_spend_cents / 100.0, 2), r.message_count, r.last_updated,
            r.note_on_of,
        ])
    return values


# ── Google Sheets transport (all blocking — runs in asyncio.to_thread) ─────

def _load_creds(cfg: _Cfg):
    """Cached token only — never an interactive flow. Refresh-in-place when
    expired, then persist back to the file (not when the token came from the
    inline env var). Raise if we can't get a valid credential without a browser."""
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
    except ImportError as e:  # pragma: no cover — surfaced as the run's error_text
        raise PushToSheetsError(
            "google-api-python-client / google-auth not installed — "
            "`pip install -r requirements.txt`"
        ) from e

    creds = None
    if cfg.token_json:
        import json
        creds = Credentials.from_authorized_user_info(
            json.loads(cfg.token_json), _SCOPES
        )
    elif os.path.exists(cfg.token_file):
        creds = Credentials.from_authorized_user_file(cfg.token_file, _SCOPES)

    if creds is None:
        raise PushToSheetsAuthError(
            f"no Google token (GOOGLE_SHEETS_TOKEN_JSON unset and {cfg.token_file} "
            "missing) — run `python -m automations.push_to_sheets --bootstrap` "
            "once locally to mint token.json"
        )
    if creds.valid:
        return creds
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        if not cfg.token_json:  # persist the refreshed token for next tick
            try:
                with open(cfg.token_file, "w") as fh:
                    fh.write(creds.to_json())
            except OSError:
                log.warning("push_to_sheets could not persist refreshed token to %s",
                            cfg.token_file, exc_info=True)
        return creds
    raise PushToSheetsAuthError(
        "Google token is invalid and has no refresh_token — re-run --bootstrap"
    )


def _export_sync(cfg: _Cfg, values: list[list[object]]) -> dict:
    """Build the service, ensure the tab exists, clear A:Z, write from A1.
    Fully blocking; the caller wraps it in asyncio.to_thread."""
    from googleapiclient.discovery import build

    creds = _load_creds(cfg)
    service = build("sheets", "v4", credentials=creds, cache_discovery=False)
    sheets = service.spreadsheets()

    created_tab = False
    meta = sheets.get(spreadsheetId=cfg.spreadsheet_id).execute()
    existing = {
        sh["properties"]["title"] for sh in meta.get("sheets", [])
    }
    if cfg.sheet_tab not in existing:
        if not cfg.create_tab:
            raise PushToSheetsError(
                f"tab {cfg.sheet_tab!r} not found in spreadsheet "
                f"{cfg.spreadsheet_id} and create_tab is off"
            )
        sheets.batchUpdate(
            spreadsheetId=cfg.spreadsheet_id,
            body={"requests": [
                {"addSheet": {"properties": {"title": cfg.sheet_tab}}}
            ]},
        ).execute()
        created_tab = True

    # A1-notation ranges must single-quote tab names that contain spaces/specials.
    rng = f"'{cfg.sheet_tab}'"
    sheets.values().clear(
        spreadsheetId=cfg.spreadsheet_id, range=f"{rng}!A:Z"
    ).execute()
    resp = sheets.values().update(
        spreadsheetId=cfg.spreadsheet_id,
        range=f"{rng}!A1",
        valueInputOption="RAW",
        body={"values": values},
    ).execute()
    return {
        "created_tab": created_tab,
        "updated_cells": int(resp.get("updatedCells", 0) or 0),
        "updated_rows": int(resp.get("updatedRows", 0) or 0),
    }


# ── The automation entry point ─────────────────────────────────────────────

@register("push_to_sheets")
async def run(account_id: str, payload: dict, *, run_id: int) -> dict:
    payload = payload or {}
    cfg = _resolve_cfg(account_id, payload)
    limit = int(payload.get("limit") or _DEFAULT_LIMIT)

    # 1) Read-only DB loads — each on its own session.
    profiles = await _load_profiles(account_id, limit)
    fan_ids = [int(p.fan_id) for p in profiles]
    fans = await _load_fans(account_id, fan_ids)
    spend = await _load_spend(account_id)

    rows = _build_rows(profiles, fans, spend)
    values = _to_values(rows)

    # 2) Push (blocking Google client off the event loop).
    result = await asyncio.to_thread(_export_sync, cfg, values)

    stats = {
        "profiles_seen": len(profiles),
        "rows_written": len(rows),
        "spreadsheet_id": cfg.spreadsheet_id,
        "sheet_tab": cfg.sheet_tab,
        **result,
    }
    log.info("push_to_sheets ok account=%s run_id=%s stats=%s",
             account_id, run_id, stats)
    return stats


# ── One-time interactive token mint (human runs this locally, never the worker) ─

def _bootstrap() -> None:
    """`python -m automations.push_to_sheets --bootstrap` — open the browser
    OAuth consent once and write token.json so the headless worker can refresh
    it thereafter. Paths come from the same env knobs as the automation."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    cfg = _resolve_cfg("__bootstrap__", {})
    if not os.path.exists(cfg.credentials_file):
        raise SystemExit(
            f"credentials.json not found at {cfg.credentials_file} — download the "
            "OAuth client (type 'Desktop') from the GCP console first, or set "
            "GOOGLE_SHEETS_CREDENTIALS_FILE."
        )
    flow = InstalledAppFlow.from_client_secrets_file(cfg.credentials_file, _SCOPES)
    creds = flow.run_local_server(port=0)
    with open(cfg.token_file, "w") as fh:
        fh.write(creds.to_json())
    print(f"wrote {cfg.token_file}")


if __name__ == "__main__":
    import sys

    if "--bootstrap" in sys.argv:
        _bootstrap()
    else:
        print("usage: python -m automations.push_to_sheets --bootstrap")
