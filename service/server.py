"""
Step 4: HTTP relay over the signed OF client.

A thin FastAPI wrapper around OFClient. Endpoints mirror OF's URL structure so
the frontend can speak OF's shape directly — we just sign + forward + return.

  GET  /health                           — does the captured session still work?
  GET  /api/of/v2/users/me
  GET  /api/of/v2/chats?limit=10&offset=0&order=recent
  GET  /api/of/v2/chats/{chat_id}/messages?limit=10&before_id=...

Run from the repo root with whichever venv has curl_cffi + fastapi + uvicorn:
  ./venv/bin/uvicorn service.server:app --reload --port 8787
"""
from __future__ import annotations

import hashlib
import logging
import os
import sys
import threading
from pathlib import Path
from typing import Any

import json

import asyncio
import contextlib
from contextvars import ContextVar
from datetime import datetime, timedelta
from fastapi import Body, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import accounts as account_registry  # noqa: E402
import proxies as proxy_registry  # noqa: E402
import live_rev  # noqa: E402
import secrets_store  # noqa: E402  # UI-writable key store (Setup → Keys)
from of_client import OFClient, OFAPIError  # noqa: E402
from curl_cffi import requests as curl_requests  # noqa: E402  # proxy/network error types

# Phase A — SQL persistence + SSE broadcaster. Both are additive: the
# legacy WS subscribers in `_event_subscribers` keep working alongside.
from db import init_db as db_init  # noqa: E402
from db.repo import sync_from_disk as _sync_db_from_disk  # noqa: E402
from events import (  # noqa: E402
    handle_event as _sse_handle_event,
    sse_stream,
    start_persist_worker as _start_persist_worker,
    stop_persist_worker as _stop_persist_worker,
)
from employees import router as _employees_router, audit_middleware as _audit_middleware  # noqa: E402
from fans import router as _fans_router  # noqa: E402
from messages import router as _messages_router, sync_rest_media_dims  # noqa: E402
from stats import router as _stats_router  # noqa: E402
from transactions import router as _transactions_router  # noqa: E402
from vault import router as _vault_router  # noqa: E402
import vault_cache  # noqa: E402  # shared TTL cache for /api/of/v2/vault/* responses
import relay_cache  # noqa: E402  # in-process TTL cache for hot-path OF endpoints (Stage C will wire call-sites)
import relay_coalesce  # noqa: E402  # in-flight dedup for idempotent OF GETs (Stage B-2)
from posts import router as _posts_router  # noqa: E402
from lists import router as _lists_router  # noqa: E402
from automation_rules_api import router as _automation_rules_router  # noqa: E402
from automation_preview_api import router as _automation_preview_router  # noqa: E402
from funnels_api import router as _funnels_router  # noqa: E402
from nudge_config_api import router as _nudge_config_router  # noqa: E402
from webhook_config_api import router as _webhook_config_router  # noqa: E402
from autoreply_config_api import router as _autoreply_config_router  # noqa: E402
from tip_reward_config_api import router as _tip_reward_config_router  # noqa: E402
from scripts_api import router as _scripts_router  # noqa: E402
from style_config_api import router as _style_config_router  # noqa: E402
from account_config_api import router as _account_config_router  # noqa: E402
from auth import (  # noqa: E402
    router as _auth_router,
    admin_router as _auth_admin_router,
    impersonate_router as _auth_impersonate_router,
    session_middleware as _auth_session_middleware,
    get_request_user as _get_request_user,
    assert_account_owned,
    clamp_account_filter,
    COOKIE_NAME as _AUTH_COOKIE_NAME,
)
from chatters import (  # noqa: E402
    router as _chatters_router,
    admin_router as _chatters_admin_router,
    chatter_session_middleware as _chatter_session_middleware,
    get_request_chatter as _get_request_chatter,
    current_actor as _current_actor,
    allowed_folder_ids_for_chatter as _allowed_folder_ids_for_chatter,
    COOKIE_NAME as _CHATTER_COOKIE_NAME,
)


def _kick_db_sync(reason: str) -> None:
    """Fire-and-forget re-import of JSON files into SQL. Called after every
    mutating admin endpoint so the SQL mirror catches up within a few hundred
    ms of any change to accounts.py / proxies.py / session_bootstrap state.

    Wrapped in try/except + logged because a sync failure must NEVER bubble
    out of an admin response — the user's actual mutation already succeeded
    against the JSON write path."""
    import asyncio as _aio
    try:
        loop = _aio.get_running_loop()
    except RuntimeError:
        # Sync FastAPI handlers run in the threadpool; trampoline to the
        # main loop captured at startup.
        if _main_loop is None:
            return
        _main_loop.call_soon_threadsafe(
            lambda: _aio.create_task(_sync_db_from_disk(), name=f"db-sync-{reason}")
        )
        return
    loop.create_task(_sync_db_from_disk(), name=f"db-sync-{reason}")


def _redact(s: str) -> str:
    """Mask user:password in a connection URL before logging.
    Turns 'postgresql://user:pass@host/db' into 'postgresql://***@host/db'.
    No-op for SQLite paths and any URL without credentials."""
    import re as _re
    return _re.sub(r"://[^/@]+@", "://***@", s)

log = logging.getLogger("of-relay")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

# Run the one-shot legacy-layout migration before any client load — moves
# pre-multi-account sessions into accounts/<user_id>/. Idempotent.
account_registry.migrate_legacy()

# Promote any pre-existing proxy → session-file bindings to proxy → account_id
# bindings, so the new UI sees them and so re-captures don't silently lose
# the proxy. Idempotent — only fires for proxies that haven't been upgraded.
def _migrate_proxy_bindings_to_accounts() -> None:
    # Build a filename → account_id map by scanning each account dir.
    mapping: dict[str, str] = {}
    base = HERE / "sessions" / "accounts"
    if base.is_dir():
        for adir in base.iterdir():
            if not adir.is_dir():
                continue
            for sp in adir.glob("session_*.json"):
                mapping[sp.name] = adir.name
    if mapping:
        n = proxy_registry.migrate_legacy_assignments(mapping)
        if n:
            log.info("migrated %d legacy proxy assignment(s) → account_id", n)


_migrate_proxy_bindings_to_accounts()

app = FastAPI(title="OF Relay", version="0.1.0")

# Phase A: employees CRUD + audit log routes (read-only browse + admin).
# Middleware registered below so every mutating call gets logged.
app.include_router(_employees_router)
app.include_router(_fans_router)
app.include_router(_messages_router)
app.include_router(_stats_router)
app.include_router(_transactions_router)
app.include_router(_vault_router)
app.include_router(_posts_router)
app.include_router(_lists_router)
app.include_router(_automation_rules_router)
app.include_router(_automation_preview_router)
app.include_router(_funnels_router)
app.include_router(_nudge_config_router)
app.include_router(_webhook_config_router)
app.include_router(_autoreply_config_router)
app.include_router(_tip_reward_config_router)
app.include_router(_scripts_router)
app.include_router(_style_config_router)
app.include_router(_account_config_router)
app.include_router(_auth_router)
app.include_router(_auth_admin_router)
app.include_router(_auth_impersonate_router)
# Chatter principal — separate from User. /chatter/* is the chatter-facing
# auth surface; /admin/chatters/* is the owner-side link/unlink/invite UI.
# Order doesn't matter for routers; placed next to the auth pair so the
# two principals' surfaces sit together.
app.include_router(_chatters_router)
app.include_router(_chatters_admin_router)

# Audit middleware. Registered AFTER the share-token gate (below) on purpose
# — middlewares run in *reverse* registration order in starlette, so the
# share-token gate fires first, blocks unauthed requests, and only authed
# ones reach the audit writer. The decorator-registered middlewares below
# are added before this line, so we add this one via the imperative API to
# control ordering explicitly.
app.middleware("http")(_audit_middleware)

# Chatter session middleware — registered BEFORE the User session
# middleware so on the inbound path it runs AFTER auth (Starlette runs
# user-added middlewares in reverse registration order). That ordering
# lets the chatter middleware's admin-gate read `get_request_user()` and
# only fire when no User cookie is present, while still landing both
# contextvars before the audit middleware's post-block resolves them.
app.middleware("http")(_chatter_session_middleware)

# Friend-auth session middleware. Same ordering trick: registered here so
# it runs *inside* the share-token gate (gate fires first, rejects unauthed
# requests; only then does this middleware read the session cookie and
# populate _request_user for _resolve_account_id).
app.middleware("http")(_auth_session_middleware)


# Defense-in-depth: scan every request URL for any OF account_id the
# signed-in friend doesn't own (whether passed as ?account_id=… or as a
# numeric path segment). Catches per-account endpoints that haven't been
# individually wrapped with assert_account_owned(). False-positive
# domain: fan_ids and other numeric segments — only ids that actually
# match a real OF account row trigger the gate, so fan_ids that happen
# to share numeric shape do not.
@app.middleware("http")
async def _account_isolation_middleware(request: Request, call_next):
    # Pick the active principal's account_ids set. User wins precedence;
    # falls back to chatter's set-union if no user is signed in. Both
    # paths must be checked because the same account_id can be reached
    # via either cookie family.
    user = _get_request_user()
    if user is not None:
        allowed_ids: frozenset[str] | None = user.account_ids
    else:
        chatter = _get_request_chatter()
        allowed_ids = chatter.account_ids if chatter is not None else None
    if allowed_ids is None:
        return await call_next(request)
    # Cheap pre-filter — only /admin/* and /api/of/* paths can possibly
    # reference per-account data. /auth/*, /events, /img, /ws/* never do.
    path = request.url.path
    if not (path.startswith("/admin/") or path.startswith("/api/of/")):
        return await call_next(request)

    candidates: set[str] = set()
    qaid = request.query_params.get("account_id")
    if qaid:
        candidates.add(qaid)
    # Any numeric path segment in the OF account-id shape (5-12 digits).
    for seg in path.split("/"):
        if seg.isdigit() and 5 <= len(seg) <= 12:
            candidates.add(seg)
    if not candidates:
        return await call_next(request)

    all_ids = {a["id"] for a in account_registry.list_accounts()}
    for cand in candidates:
        if cand in all_ids and cand not in allowed_ids:
            return Response("not your account", status_code=403)
    return await call_next(request)

# ── Share-link gate ────────────────────────────────────────────
# Only requests carrying the token via ?t=... or the share_token cookie get
# through. The default is a stable test token so the share URL stays the
# same across `docker compose up`, plain `uvicorn ...`, and
# `service/run_public.sh`. To disable the gate entirely (truly local dev,
# no exposure), launch with SHARE_TOKEN= (empty string). /health is always
# open so cloudflared / load balancers can probe it.
SHARE_TOKEN = os.environ.get("SHARE_TOKEN", "").strip()
_SHARE_COOKIE = "share_token"


def _effective_share_token() -> str:
    """The live access token: a value set via Setup → Keys (secrets.json) wins,
    else the env/module default. Read per-request so the UI can set/clear the
    gate without a relay restart."""
    try:
        s = secrets_store.stored("SHARE_TOKEN")
        if s:
            return s
    except Exception:  # never let the gate crash on a bad store
        pass
    return SHARE_TOKEN


# Stash the current Request so deep helpers (_get_client) can pick up the
# X-Account-Id header without us having to add `request: Request` to every
# one of the ~60 endpoint signatures. Set by the middleware below.
_request_ctx: ContextVar[Request | None] = ContextVar("of_relay_request", default=None)


@app.middleware("http")
async def _account_context(request: Request, call_next):
    """Make the current Request available to non-endpoint code paths
    (notably _get_client → _resolve_account_id) via a ContextVar. The token
    is reset in `finally` so the contextvar doesn't leak between requests."""
    token = _request_ctx.set(request)
    try:
        return await call_next(request)
    finally:
        _request_ctx.reset(token)


@app.middleware("http")
async def _share_token_gate(request: Request, call_next):
    share_token = _effective_share_token()
    if not share_token:
        return await call_next(request)
    if request.url.path == "/health" or request.url.path == "/livez":
        return await call_next(request)
    # /auth/* must be reachable without the share-token so a brand-new
    # friend can register or sign in from the landing page. Same for
    # /chatter/* — a chatter following an owner-issued invite link
    # arrives without any cookie at all.
    if request.url.path.startswith("/auth/") or request.url.path.startswith("/chatter/"):
        return await call_next(request)
    if request.cookies.get(_SHARE_COOKIE) == share_token:
        return await call_next(request)
    # Anyone carrying a friend-auth OR chatter session cookie has already
    # cleared username+password — the legacy share-token gate is
    # redundant for them. (The session middlewares validate the HMAC +
    # DB row downstream.)
    if request.cookies.get(_AUTH_COOKIE_NAME) or request.cookies.get(_CHATTER_COOKIE_NAME):
        return await call_next(request)
    if request.query_params.get("t") == share_token:
        resp = await call_next(request)
        # Persist the token so subsequent fetch() calls (which won't carry ?t=)
        # still authenticate. 7-day TTL is plenty for an ad-hoc share session.
        resp.set_cookie(
            _SHARE_COOKIE, share_token,
            max_age=7 * 24 * 3600, httponly=True, samesite="lax",
        )
        return resp
    return Response("unauthorized — link missing or expired", status_code=401)


# ── Setup → Keys: UI-writable secret/key store ─────────────────────
# Lets a self-hoster paste their DeepSeek key, Google Sheets token, access
# password, etc. instead of editing the VPS .env. Values land in
# service/secrets.json and every consumer checks the store before the env, so
# they take effect on the next call with no restart. GET never returns raw
# secrets — only a masked status. Both sit under /admin/* (already gated).
@app.get("/admin/secrets")
def admin_secrets_status() -> dict[str, Any]:
    """Masked status of every UI-settable key (set?/source/hint — never the
    raw value)."""
    return {"keys": secrets_store.status()}


@app.put("/admin/secrets")
async def admin_secrets_set(request: Request) -> dict[str, Any]:
    """Set or clear keys. Body is a flat JSON object {KEY: value, …}; an empty
    string or null clears that key. Unknown keys are ignored. Returns the new
    masked status."""
    try:
        body = await request.json()
    except Exception:
        body = None
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="expected a JSON object of key→value")
    secrets_store.set_many({str(k): v for k, v in body.items()})
    return {"keys": secrets_store.status()}


# ── /tmp/of-api.log — dedicated single-file access log ─────────────
# Every HTTP request that makes it past the share-token gate gets one
# line here. Pure text (no binary bytes from upstream stream bodies
# the way uvicorn's mixed log can have), append-only, so the user can
# `tail -f /tmp/of-api.log` and see live API traffic without needing
# grep -a flags. The access timing is measured around `call_next` so
# it includes all our middleware + the handler + StreamingResponse
# generator startup.
_API_LOG_PATH = "/tmp/of-api.log"
import time as _time_mod


@app.middleware("http")
async def _api_access_log(request: Request, call_next):
    started = _time_mod.monotonic()
    response = None
    status = 0
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    except HTTPException as exc:
        status = exc.status_code
        raise
    except Exception:
        status = 500
        raise
    finally:
        ms = int((_time_mod.monotonic() - started) * 1000)
        # Strip the share token from the logged URL — even though the file
        # is local-only, no need to mirror it on every line.
        url = str(request.url)
        if "?t=" in url:
            url = url.split("?t=", 1)[0] + (
                "?" + url.split("?", 1)[1].split("&", 1)[1]
                if "&" in url.split("?", 1)[1] else ""
            )
        elif "&t=" in url:
            head, tail = url.split("&t=", 1)
            rest = tail.split("&", 1)[1] if "&" in tail else ""
            url = head + (("&" + rest) if rest else "")
        ts = datetime.utcnow().isoformat(timespec="milliseconds") + "Z"
        line = f"{ts} {request.method:6s} {status} {ms:5d}ms {url}\n"
        try:
            # Append synchronously — open+write+close is a single syscall
            # per line at this volume. Avoids the lock contention an
            # async aiofiles handle would buy us.
            with open(_API_LOG_PATH, "a", encoding="utf-8") as fh:
                fh.write(line)
        except Exception:
            pass


@app.middleware("http")
async def _persist_unhandled_errors(request: Request, call_next):
    """Catch unhandled server-side exceptions, persist a row in app_errors
    (so /admin/errors surfaces them), then re-raise so FastAPI's default
    500 response still fires. HTTPException is intentionally NOT caught —
    those are deliberate, structured replies, not bugs."""
    try:
        return await call_next(request)
    except HTTPException:
        raise
    except Exception as exc:
        import traceback as _tb
        try:
            from db.engine import get_session
            from db.models import AppError as _AE
            stack = "".join(_tb.format_exception(type(exc), exc, exc.__traceback__))[:16384]
            account_id = request.headers.get("x-account-id")
            emp_hdr = request.headers.get("x-employee-id")
            try:
                employee_id = int(emp_hdr) if emp_hdr else None
            except ValueError:
                employee_id = None
            async with get_session() as s:
                s.add(_AE(
                    source="server",
                    kind=type(exc).__name__[:64],
                    message=str(exc)[:4096],
                    stack=stack,
                    url=str(request.url)[:1024],
                    account_id=account_id[:64] if account_id else None,
                    employee_id=employee_id,
                    user_agent=(request.headers.get("user-agent") or "")[:512] or None,
                ))
                await s.commit()
        except Exception:
            # Don't let the logger crash the response path on top of the
            # original error — just log and move on.
            log.exception("failed to persist server-side error to app_errors")
        raise


# Lazy client pool — one OFClient per account_id. First request for an
# account loads its session; subsequent requests reuse the pooled client.
# Hot-reload via /admin/reload-session?account_id=... or by re-bootstrapping.
_clients: dict[str, OFClient] = {}

# ── Realtime event bus ─────────────────────────────────────────
# We connect ONCE to OF's WebSocket (wss://ws2.onlyfans.com/ws3/N) at startup
# and fan every event out to (a) browser clients on /ws/events, (b) configured
# webhook URLs, (c) any local subscribers (CLI tailer, future plugins).
# Subscribers are asyncio.Queue instances; if a queue is full, we drop the
# oldest message rather than block the producer.

_event_subscribers: set[asyncio.Queue] = set()
_event_stats: dict[str, Any] = {
    "received": 0,
    "by_type": {},
    "by_account": {},
    "started_at": None,
    "last_event_ts": None,
    "subscribers": 0,
}

# Webhooks: per-event-type URL list. Loaded from sessions/webhooks.json so
# config survives restarts. Each call POSTs the event JSON to every URL
# subscribed to that event type (or "*" wildcard).
_WEBHOOKS_FILE = HERE / "sessions" / "webhooks.json"


def _load_webhooks() -> dict[str, list[str]]:
    if not _WEBHOOKS_FILE.exists():
        return {}
    try:
        return json.loads(_WEBHOOKS_FILE.read_text())
    except Exception:
        return {}


def _save_webhooks(cfg: dict) -> None:
    _WEBHOOKS_FILE.parent.mkdir(exist_ok=True)
    _WEBHOOKS_FILE.write_text(json.dumps(cfg, indent=2))


async def _broadcast_event(event: dict) -> None:
    """Push an event to every subscriber queue + fire webhooks."""
    _event_stats["received"] += 1
    _event_stats["last_event_ts"] = __import__("time").time()
    if isinstance(event, dict):
        for k in event:
            if k.startswith("__"):  # don't tally our own meta-keys
                continue
            _event_stats["by_type"][k] = _event_stats["by_type"].get(k, 0) + 1
        aid = event.get("__account_id")
        if aid:
            _event_stats["by_account"][aid] = _event_stats["by_account"].get(aid, 0) + 1
    _event_stats["subscribers"] = len(_event_subscribers)

    dead: list[asyncio.Queue] = []
    for q in _event_subscribers:
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            # Drop oldest, push new — better than blocking the OF pump
            with contextlib.suppress(asyncio.QueueEmpty):
                q.get_nowait()
            try:
                q.put_nowait(event)
            except Exception:
                dead.append(q)
    for q in dead:
        _event_subscribers.discard(q)

    # Phase A: also fan into the SSE module + persist to event_inbox. Wrapped
    # in try/except because we never want a SQL hiccup to break the legacy
    # WS broadcast — those subscribers were already served above.
    try:
        await _sse_handle_event(event)
    except Exception:
        log.exception("SSE handle_event failed")

    # Webhooks — fire-and-forget HTTP POST, don't await
    cfg = _load_webhooks()
    if cfg:
        urls: set[str] = set()
        for kind in list(event.keys()) if isinstance(event, dict) else []:
            urls.update(cfg.get(kind, []))
        urls.update(cfg.get("*", []))
        for url in urls:
            asyncio.create_task(_post_webhook(url, event))


async def _post_webhook(url: str, event: dict) -> None:
    """Fire-and-forget POST. Errors logged but don't propagate."""
    try:
        # Use stdlib (no extra deps) via thread executor — keeps signed-client
        # curl_cffi clean. Short 5s timeout per request.
        import urllib.request, urllib.error
        body = json.dumps(event).encode("utf-8")
        req = urllib.request.Request(
            url, data=body, method="POST",
            headers={"Content-Type": "application/json", "User-Agent": "of-relay/1.0"},
        )
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None, lambda: urllib.request.urlopen(req, timeout=5).read()
        )
    except Exception as e:
        log.warning("webhook POST %s failed: %s", url, e)


# One WebSocket pump per account. Events are tagged with `__account_id` +
# `__account_name` before broadcast so subscribers (UI + webhooks) can route
# or filter by account. The pumps dict is mutated only by the helpers below.
_account_pumps: dict[str, asyncio.Task] = {}
_supervisor_task: asyncio.Task | None = None
_tx_ingest_task: asyncio.Task | None = None
# Automation executor supervisor (service/automation_executor.py) — consumes
# scheduled_jobs / automation_rules and runs the 12 automations. Cancelled in
# _stop_event_pumps alongside _tx_ingest_task.
_automation_exec_task: asyncio.Task | None = None
# W7 (0): seed the inbox with each account's chat links ONCE on startup so the
# bot has conversation links to react to; webhooks carry deltas after that.
# Guarded so a re-entered startup can't double-enqueue the scrape sweep.
_chat_links_seeded: bool = False
# The asyncio loop the app is running on. Captured at startup so sync FastAPI
# endpoints (which run in the threadpool, with NO running loop in the worker
# thread) can schedule pump start/stop via `call_soon_threadsafe`.
_main_loop: asyncio.AbstractEventLoop | None = None


async def _ws_pump_for_account(account_id: str) -> None:
    """Run the OF WS pump for one account until cancelled. OFWebSocket has
    its own reconnect-with-backoff loop, so this task is only torn down when
    the account is removed or the server is shutting down."""
    from of_ws import OFWebSocket, register_pump, unregister_pump
    while True:
        try:
            client = _load_client(account_id)
            meta = account_registry.get_account(account_id) or {}
            nickname = meta.get("nickname") or account_id
            color = meta.get("color")
            ws = OFWebSocket(client)
            # Publish this live pump so the automation layer can push outbound
            # frames (e.g. the typing indicator) on the same socket.
            register_pump(account_id, ws)
            log.info("ws-pump[%s]: connecting to OF", nickname)
            try:
                async for event in ws.events():
                    # Tag every event so multi-account subscribers can route.
                    # Use sentinel `__` keys so we never collide with real OF event names.
                    if isinstance(event, dict):
                        event = {
                            "__account_id": account_id,
                            "__account_name": nickname,
                            "__account_color": color,
                            **event,
                        }
                    await _broadcast_event(event)
            finally:
                unregister_pump(account_id, ws)
        except HTTPException as e:
            log.warning("ws-pump[%s]: %s — retry in 30s", account_id, e.detail)
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("ws-pump[%s] unexpected: %s — retry in 10s", account_id, e)
            await asyncio.sleep(10)


def _start_account_pump(account_id: str) -> None:
    """Spawn a pump for an account if one isn't already running.

    Safe to call from BOTH the event loop and from a worker thread (FastAPI
    sync endpoints run in the threadpool, where `asyncio.create_task` raises
    `RuntimeError: no running event loop`). From a thread we trampoline back
    to the main loop via `call_soon_threadsafe`.
    """
    try:
        asyncio.get_running_loop()
        _do_start_account_pump(account_id)
    except RuntimeError:
        if _main_loop is None:
            # Startup hasn't run yet; the supervisor / startup hook will
            # eventually spawn this pump. Drop silently rather than crash.
            return
        _main_loop.call_soon_threadsafe(_do_start_account_pump, account_id)


def _do_start_account_pump(account_id: str) -> None:
    """Inner: must be called from inside the event loop."""
    task = _account_pumps.get(account_id)
    if task and not task.done():
        return
    _account_pumps[account_id] = asyncio.create_task(
        _ws_pump_for_account(account_id), name=f"ws-pump-{account_id}",
    )


def _stop_account_pump(account_id: str) -> None:
    """Cancel an account's pump. `.cancel()` is documented to be safe from
    any thread, but the dict pop and the task reference both want the loop;
    we trampoline for symmetry with _start_account_pump."""
    try:
        asyncio.get_running_loop()
        _do_stop_account_pump(account_id)
    except RuntimeError:
        if _main_loop is None:
            return
        _main_loop.call_soon_threadsafe(_do_stop_account_pump, account_id)


def _do_stop_account_pump(account_id: str) -> None:
    task = _account_pumps.pop(account_id, None)
    if task and not task.done():
        task.cancel()


def _restart_account_pump(account_id: str) -> None:
    _stop_account_pump(account_id)
    _start_account_pump(account_id)


async def _pump_supervisor() -> None:
    """Reconcile the set of running pumps against the set of accounts every
    15s. Picks up newly-bootstrapped accounts without needing an explicit
    'start pump' call from the bootstrap endpoint, and reaps pumps for
    deleted accounts. Also restarts any pump that crashed unexpectedly."""
    while True:
        try:
            current_ids = {a["id"] for a in account_registry.list_accounts()
                           if a.get("has_session")}
            # Start missing
            for aid in current_ids:
                _start_account_pump(aid)
            # Stop pumps whose account vanished
            for aid in list(_account_pumps.keys()):
                if aid not in current_ids:
                    _stop_account_pump(aid)
                    continue
                task = _account_pumps[aid]
                if task.done() and not task.cancelled():
                    log.warning("ws-pump[%s] task exited — restarting", aid)
                    _restart_account_pump(aid)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("supervisor pass failed")
        await asyncio.sleep(15)


@app.on_event("startup")
async def _start_event_pumps() -> None:
    global _supervisor_task, _tx_ingest_task, _automation_exec_task, _main_loop
    _main_loop = asyncio.get_running_loop()
    _event_stats["started_at"] = __import__("time").time()

    # Bump the anyio thread pool. `/img` is a sync streaming endpoint —
    # each in-flight video / avatar fetch holds one thread for as long as
    # the browser keeps the stream open. With hover-preview, scrub seeks,
    # the chat-list avatars, vault thumbnails and notification polls all
    # sharing the default ~40-thread pool, the relay would exhaust threads
    # and start dropping requests (ECONNRESET storms on the Next side).
    # 200 gives plenty of headroom; each thread is cheap and idle when no
    # stream is active.
    try:
        import anyio
        anyio.to_thread.current_default_thread_limiter().total_tokens = 200
        log.info("anyio thread pool bumped to 200")
    except Exception:
        log.exception("could not bump anyio thread pool — sticking with default")

    # Phase A — bring up SQLite. Idempotent: creates tables on first boot,
    # no-ops on subsequent. Must precede pump spawn because the SSE writer
    # below tries to insert event_inbox rows from the first event onward.
    try:
        await db_init()
        log.info("db ready (DATABASE_URL=%s)", _redact(os.environ.get("DATABASE_URL", "(default)")))
    except Exception:
        log.exception("db_init failed — relay will run but SSE/inbox writes will error")

    # TEST-MODE GUARD — db init above already ran (prod-critical at startup);
    # everything BELOW is the spawn/network section (persist worker, WS pumps,
    # pump-supervisor, live_rev network refresh, evictors, tx-ingest,
    # automation-exec). Tests neither exercise the WS path nor want OF/network
    # round-trips, so skip the whole section. MUST stay AFTER db_init — moving
    # it above would leave the schema uninitialized. (Belt-and-suspenders: the
    # harness also no-ops this function and live_rev.refresh post-import.)
    if os.environ.get("CHATTERLY_TEST_MODE"):
        log.info("CHATTERLY_TEST_MODE=1 — skipping pump/supervisor/network spawns")
        return

    # Start the bounded persist worker BEFORE pumps spawn. Otherwise the
    # first burst of events takes the pre-startup fallback path in
    # events.handle_event (unbounded create_task), which is exactly what
    # this worker exists to avoid.
    _start_persist_worker()

    # Spawn pumps for every existing account with a session
    for meta in account_registry.list_accounts():
        if meta.get("has_session"):
            _start_account_pump(meta["id"])
    _supervisor_task = asyncio.create_task(_pump_supervisor(), name="pump-supervisor")
    log.info("event pumps started for %d account(s)", len(_account_pumps))

    # Probe the live OF build hash so the drift detector has a value ready
    # by the time the UI loads /admin/rev/drift. Network — run off-thread so
    # a slow CF response doesn't block FastAPI startup.
    asyncio.get_running_loop().run_in_executor(None, live_rev.refresh)

    # Periodic GC of `_storyboard_locks` — the prune cron clears the on-disk
    # storyboard dirs but the in-memory lock entries would otherwise leak
    # ~200 bytes per unique video ever hovered. Eviction is conservative:
    # only drops locks whose dir is gone AND whose `acquire(blocking=False)`
    # succeeds, so it never races with an active build.
    asyncio.create_task(_storyboard_evictor_loop(), name="storyboard-evictor")

    # Periodic TTL sweep of the on-disk /img cache. Keys are host+path
    # so entries are shared across all accounts/users — the sweep just
    # drops anything older than _IMG_CACHE_TTL_S by mtime.
    asyncio.create_task(_img_cache_evictor_loop(), name="img-cache-evictor")

    # Prune of `perf_events` so the client-side perflog ingest table
    # stays bounded. Retain window via PERFLOG_RETAIN_S (default 7d).
    asyncio.create_task(_perflog_evictor_loop(), name="perflog-evictor")

    # Cap DB growth: NULL out messages.raw_json older than _RAW_JSON_RETAIN_S
    # (default 60d). The payloads are a write-only migration buffer; left
    # unbounded they grew the DB to ~1.9 GB. Daily ticks.
    asyncio.create_task(_raw_json_evictor_loop(), name="raw-json-evictor")

    # Hard SIZE ceiling on the two raw-OF-payload columns (event_inbox +
    # messages.raw_json) so the SQLite file stays well under ~2 GB even when
    # VOLUME, not age, is the problem. Drives off logical SUM(LENGTH(...)),
    # evicts oldest-first down to PAYLOAD_BUDGET_BYTES (never breaching the
    # EVENT_INBOX_RETAIN_FLOOR_S floor), then returns freed pages to the OS.
    # Touches no fan rows. Hourly ticks. See _payload_size_cap_loop.
    asyncio.create_task(_payload_size_cap_loop(), name="payload-size-cap")

    # Phase F: poll OF's /api2/v2/payouts/transactions ledger so per-model
    # stats include subs / tips / PPV unlocks / paid posts. 10-min ticks,
    # sequential per-account refresh, parallel backfill (sem=4). See
    # service/transaction_ingest.py docstring.
    from transaction_ingest import start_supervisor as _start_tx_ingest
    _tx_ingest_task = asyncio.create_task(_start_tx_ingest(), name="tx-ingest-supervisor")

    # P3 — automation executor: consumes scheduled_jobs / automation_rules and
    # runs the 12 automations (scrape_chats ships as the reference). Mirrors the
    # supervisor pattern above; cancelled in _stop_event_pumps next to tx-ingest.
    from automation_executor import automation_supervisor as _automation_supervisor
    _automation_exec_task = asyncio.create_task(
        _automation_supervisor(), name="automation-exec",
    )

    # W7 (0) — seed chat links ONCE for all accounts: enqueue a one-shot
    # scrape_chats sweep per session-backed account so the inbox has the full
    # conversation list (the WS pump then carries deltas — no second bulk-pull).
    # Reuses the existing scrape_chats automation; the supervisor's first drain
    # (immediate at loop start) picks the jobs up. Fire-and-forget so N inserts
    # don't block startup.
    asyncio.create_task(_seed_chat_links_once(), name="w7-seed-chat-links")


async def _seed_chat_links_once() -> None:
    """Enqueue a one-shot scrape_chats job per active account (W7 part 0)."""
    global _chat_links_seeded
    if _chat_links_seeded:
        return
    _chat_links_seeded = True
    try:
        from automation_executor import enqueue_job, wake_supervisor
        n = 0
        for meta in account_registry.list_accounts():
            if not meta.get("has_session"):
                continue
            try:
                await enqueue_job(meta["id"], "scrape_chats", payload={})
                n += 1
            except Exception:
                log.warning("w7_seed_enqueue_failed account=%s", meta.get("id"), exc_info=True)
        if n:
            wake_supervisor()
        log.info("w7_chat_links_seeded accounts=%d", n)
    except Exception:
        log.warning("w7_seed_chat_links failed", exc_info=True)


@app.on_event("shutdown")
async def _stop_event_pumps() -> None:
    # CancelledError is not an "Exception" in 3.8+ (it's BaseException) so we
    # have to suppress it explicitly — otherwise FastAPI logs a noisy traceback
    # at every clean shutdown.
    if _supervisor_task and not _supervisor_task.done():
        _supervisor_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await _supervisor_task
    if _tx_ingest_task and not _tx_ingest_task.done():
        _tx_ingest_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await _tx_ingest_task
    if _automation_exec_task and not _automation_exec_task.done():
        _automation_exec_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await _automation_exec_task
    for task in list(_account_pumps.values()):
        if not task.done():
            task.cancel()
    for task in list(_account_pumps.values()):
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
    _account_pumps.clear()

    # Drain the persist worker last — pumps are already cancelled above, so
    # no new events are arriving. Anything still in the queue is dropped.
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await _stop_persist_worker()


def _load_client(account_id: str) -> OFClient:
    """Return the OFClient for `account_id`, loading it the first time.
    Raises a structured 503 if the account has no usable session yet."""
    cached = _clients.get(account_id)
    if cached is not None:
        return cached
    log.info("Loading account %s into OFClient", account_id)
    try:
        client = OFClient.from_account(account_id)
    except FileNotFoundError as e:
        raise HTTPException(
            status_code=503,
            detail={
                "error": "no_session",
                "account_id": account_id,
                "message": str(e),
                "remedy": ["POST /admin/session/bootstrap", "or ./venv/bin/python service/capture_session.py"],
            },
        ) from None
    except ValueError as e:
        raise HTTPException(
            status_code=503,
            detail={
                "error": "incomplete_signing_rules",
                "account_id": account_id,
                "message": str(e),
                "remedy": ["./venv/bin/python service/extract_rules.py"],
            },
        ) from None
    _clients[account_id] = client
    log.info("OFClient ready (account=%s user_id=%s rev=%s)",
             account_id, client.user_id, client.x_of_rev)
    return client


def _resolve_account_id(request: Request | None) -> str:
    """Pick the account_id for this request, in priority order:
       1. `X-Account-Id` request header (UI sends this)
       2. `?account_id=...` query param (curl-friendly)
       3. the currently active account (fallback for un-aware callers)
    Raises 404 if the explicit id doesn't exist, 503 if no active account,
    403 if the signed-in user doesn't own the resolved account.

    Ownership gate: if a session cookie identifies a signed-in friend (see
    auth.session_middleware), the resolved account_id MUST appear in their
    `user_accounts` set. Background paths with no request user (cron, WS
    pump callbacks) bypass the check — they're trusted system code. This
    is the single choke point for cross-user data isolation.
    """
    explicit: str | None = None
    if request is not None:
        explicit = request.headers.get("x-account-id") or request.query_params.get("account_id")
    if explicit:
        if account_registry.get_account(explicit) is None:
            raise HTTPException(status_code=404, detail=f"Unknown account_id {explicit!r}")
        aid = explicit
    else:
        aid = account_registry.get_active_account_id() or ""
        if not aid:
            raise HTTPException(
                status_code=503,
                detail={
                    "error": "no_active_account",
                    "message": "No account has a captured session yet.",
                    "remedy": ["POST /admin/session/bootstrap"],
                },
            )

    user = _get_request_user()
    if user is not None:
        if aid not in user.account_ids:
            raise HTTPException(status_code=403, detail="not your account")
    else:
        # Chatter principal: account_ids is the SET UNION across every
        # owner that linked this chatter (see service/chatters.py).
        chatter = _get_request_chatter()
        if chatter is not None and aid not in chatter.account_ids:
            raise HTTPException(status_code=403, detail="not your account")
    return aid


def _get_client(request: Request | None = None) -> OFClient:
    """Resolve the account for `request` (or — if not passed — the request
    captured by the middleware contextvar, falling back to the active
    account) and return its OFClient."""
    if request is None:
        request = _request_ctx.get()
    return _load_client(_resolve_account_id(request))


def _invalidate_client(account_id: str) -> None:
    """Drop a pooled OFClient so the next request reloads from disk.
    Used after re-bootstrap or proxy reassignment."""
    _clients.pop(account_id, None)


def _link_account_to_request_user(account_id: str) -> None:
    """If a signed-in friend just captured an OF session, INSERT a
    user_accounts row linking the two so it shows up in their
    ScopeSwitcher + passes the ownership gate. No-op if the request is
    unauthed (founder pre-auth curl, internal cron). Idempotent —
    duplicate inserts silently rolled back."""
    user = _get_request_user()
    if user is None:
        return

    async def _link() -> None:
        from db.engine import get_session
        from db.models import UserAccount
        from sqlalchemy.exc import IntegrityError
        try:
            async with get_session() as s:
                s.add(UserAccount(user_id=user.id, account_id=account_id))
                try:
                    await s.flush()
                except IntegrityError:
                    await s.rollback()
        except Exception:
            log.exception("failed to link account %s to user %s", account_id, user.id)
        # Refresh the in-process snapshot so the SAME request (sync handler
        # called us, then returns to the user) sees the new account_id in
        # its set on any follow-up _resolve_account_id call.
        try:
            user.account_ids = frozenset(user.account_ids | {account_id})  # type: ignore[misc]
        except Exception:
            pass

    # The bootstrap handler is sync; bounce into the event loop.
    try:
        loop = _main_loop
        if loop is None:
            return
        import asyncio as _asyncio
        fut = _asyncio.run_coroutine_threadsafe(_link(), loop)
        # Wait briefly so the row is visible by the time we return; capped
        # so a stuck DB can't hang the bootstrap response.
        fut.result(timeout=5)
    except Exception:
        log.exception("link account → user dispatch failed")


# ── Per-account OF call priority lanes ────────────────────────────────
#
# All OF API calls for one account share a single curl_cffi.requests.Session
# routed through one proxy IP. curl_cffi Sessions are NOT thread-safe and
# OF + the proxy provider cap concurrent connections per source IP — so
# firing 8 parallel /users/list batches behind a 11s /chats/messages call
# starves anything else (like a vault picker open) that tries to land
# during that window.
#
# We solve this with two semaphores per account:
#   * `total`      — hard concurrency cap so we never exceed what the
#                    proxy/OF can serve cleanly
#   * `background` — sub-cap so bulk enrichment can't fill every slot
#
# Background calls acquire BOTH; user calls only the total cap. Result:
# user-initiated work always has at least (total - background) reserved
# slots and jumps the queue ahead of any waiting background call.
#
# Priority is signalled by the `X-Priority: background` request header.
# Anything else (including missing) is treated as user.
_ACCOUNT_LANE_TOTAL = 5
_ACCOUNT_LANE_BACKGROUND = 2
_account_lanes_lock = threading.Lock()
_account_lanes: dict[str, tuple[threading.BoundedSemaphore, threading.BoundedSemaphore]] = {}
_priority_total_waits: int = 0
_priority_background_waits: int = 0


def _lanes_for(account_id: str) -> tuple[threading.BoundedSemaphore, threading.BoundedSemaphore]:
    """Return (total, background) semaphores for `account_id`, creating
    them on first access."""
    with _account_lanes_lock:
        pair = _account_lanes.get(account_id)
        if pair is None:
            pair = (
                threading.BoundedSemaphore(_ACCOUNT_LANE_TOTAL),
                threading.BoundedSemaphore(_ACCOUNT_LANE_BACKGROUND),
            )
            _account_lanes[account_id] = pair
        return pair


def _current_priority() -> str:
    """Read X-Priority off the current request. Defaults to 'user' when
    the header is missing or the call originates outside a request (e.g.
    background tasks). The only value that changes behaviour is the exact
    string 'background'."""
    req = _request_ctx.get()
    if req is None:
        return "user"
    return (req.headers.get("x-priority") or "user").lower()


@contextlib.contextmanager
def _priority_lane(account_id: str | None):
    """Acquire the right semaphore(s) for the current request's priority.
    Background callers also hold the background sub-semaphore, so user
    callers always have at least (total - background) reserved slots."""
    global _priority_total_waits, _priority_background_waits
    if not account_id:
        yield
        return
    total, background = _lanes_for(account_id)
    priority = _current_priority()
    bg_held = False
    if priority == "background":
        _priority_background_waits += 1
        background.acquire()
        bg_held = True
    _priority_total_waits += 1
    total.acquire()
    try:
        yield
    finally:
        try: total.release()
        except ValueError: pass
        if bg_held:
            try: background.release()
            except ValueError: pass


def _proxy(call):
    """Translate OFAPIError + curl_cffi proxy/network errors into a structured
    502 so the frontend can react, and log a one-liner instead of a 200-line
    traceback for transient proxy failures (cf-tunnel 403s, timeouts, DNS).

    Also serializes the upstream call through the per-account priority
    lane, so a bulk background enrichment can't starve a user-initiated
    fetch (vault open, chat click)."""
    try:
        aid: str | None = None
        req = _request_ctx.get()
        if req is not None:
            try:
                aid = _resolve_account_id(req)
            except HTTPException:
                aid = None
        with _priority_lane(aid):
            return call()
    except OFAPIError as e:
        r = e.response
        status = r.status_code if r is not None else 500
        body = r.text[:2000] if r is not None else str(e)
        log.warning("upstream error: %s %s", status, body[:200])
        # 502 = bad gateway: we tried, OF didn't like it.
        raise HTTPException(
            status_code=502,
            detail={"upstream_status": status, "upstream_body": body},
        )
    except curl_requests.exceptions.ProxyError as e:
        # of_client._http_call already logged the structured details
        # (account, proxy label, endpoint). Surface a clean 502 to the UI.
        log.warning("proxy_unreachable: %s", str(e)[:200])
        raise HTTPException(
            status_code=502,
            detail={"upstream_status": "proxy_unreachable", "upstream_body": str(e)[:500]},
        )
    except curl_requests.exceptions.Timeout as e:
        log.warning("upstream_timeout: %s", str(e)[:200])
        raise HTTPException(
            status_code=504,
            detail={"upstream_status": "timeout", "upstream_body": str(e)[:500]},
        )
    except curl_requests.exceptions.RequestException as e:
        log.warning("upstream_network_error: %s %s", type(e).__name__, str(e)[:200])
        raise HTTPException(
            status_code=502,
            detail={"upstream_status": "network", "upstream_body": str(e)[:500]},
        )


# ── Endpoints ──────────────────────────────────────────────────

@app.get("/livez")
def livez():
    """Process liveness probe. Returns 200 as long as FastAPI is running.

    Distinct from /health which reports session/account readiness and can
    return 503 when no OF account is loaded yet. The Docker HEALTHCHECK
    directive aims this endpoint so a relay with zero captured sessions
    (i.e. a brand-new install before paste-cURL bootstrap) is still
    marked healthy — otherwise app.depends_on.relay:service_healthy
    deadlocks fresh deploys, since the UI that creates the first session
    lives behind that very dependency. Always cheap, no side effects, no
    auth required (same exemption as /health from the share-token gate)."""
    return {"ok": True}


@app.get("/health")
def health(request: Request, all_accounts: bool = Query(False, description="Probe every account, not just the requested one")):
    """Never raises 500. Always returns a JSON body the frontend can act on.

    By default reports the health of the account resolved from
    `X-Account-Id` / `?account_id=` / active. Pass `?all_accounts=1` to get
    the full per-account snapshot, which the UI uses to flag any expired
    sessions in the switcher dropdown."""
    if all_accounts:
        rows: list[dict[str, Any]] = []
        for meta in account_registry.list_accounts():
            aid = meta["id"]
            row: dict[str, Any] = {
                "account_id": aid, "nickname": meta.get("nickname"),
                "color": meta.get("color"),
                "has_session": meta.get("has_session", False),
            }
            if not meta.get("has_session"):
                row["ok"] = False
                row["error"] = "no_session"
                rows.append(row)
                continue
            try:
                c = _load_client(aid)
                me = c.me()
                row.update({"ok": True, "user_id": me.get("id"), "name": me.get("name"),
                            "proxy": {"label": c.proxy_label, "url": c.proxy_url}})
            except OFAPIError as e:
                r = e.response
                row.update({"ok": False, "error": "upstream",
                            "upstream_status": r.status_code if r else None,
                            "upstream_body": (r.text[:300] if r else str(e))})
            except HTTPException as e:
                row.update({"ok": False,
                            "error": (e.detail.get("error") if isinstance(e.detail, dict) else "error"),
                            "detail": e.detail})
            except Exception as e:
                row.update({"ok": False, "error": f"{type(e).__name__}: {e}"})
            rows.append(row)
        return {"ok": all(r.get("ok") for r in rows) if rows else False,
                "accounts": rows}

    try:
        client = _get_client(request)
    except HTTPException as e:
        return JSONResponse(status_code=e.status_code, content={"ok": False, **(e.detail if isinstance(e.detail, dict) else {"detail": e.detail})})
    try:
        me = client.me()
    except OFAPIError as e:
        r = e.response
        return JSONResponse(status_code=502, content={
            "ok": False,
            "error": "upstream",
            "upstream_status": r.status_code if r else None,
            "upstream_body": (r.text[:500] if r else str(e)),
            "proxy": {"label": client.proxy_label, "url": client.proxy_url},
        })
    return {
        "ok": True,
        "account_id": client.account_id,
        "user_id": me.get("id"),
        "name": me.get("name"),
        "proxy": {
            "label": client.proxy_label,
            "url": client.proxy_url,
            "egress_ip": client.egress_ip(),
        },
    }


@app.get("/api/of/v2/users/me")
def users_me() -> dict[str, Any]:
    return _proxy(lambda: _get_client().me())


# ── Image proxy ────────────────────────────────────────────────
# OF's CDN signs URLs with `AWS:SourceIp=<egress IP>/32` — the IP of the
# proxy that fetched the API response. The user's browser doesn't share
# that IP, so the image fetch fails on any account whose session uses a
# different egress than the user's home IP. We tunnel the fetch back
# through the account's HTTP client so the source IP matches.
#
# Restricted to known OF CDN hosts to keep this from becoming an SSRF
# foot-gun. Caches at the browser via Cache-Control passthrough.

_ALLOWED_CDN_SUFFIXES = (
    ".onlyfans.com",
    ".ofcdn.com",
    ".mycdn.dev",  # OF's media subdomains
)

# In-flight /img stream counters. Each active proxy_image generator
# increments _img_active on first chunk and decrements in its finally
# block. Read via /admin/streams so the user can see whether hover-
# preview is actually blowing the budget or it's something else (e.g.,
# a stalled upstream holding a thread). Pure counters, no lock — Python
# int += is atomic enough for monotonic stats.
_img_active: int = 0
_img_high_water: int = 0
_img_total_started: int = 0
_img_total_aborted: int = 0

# Video-stream concurrency cap. /img Range requests (= browser video
# streaming) get a dedicated semaphore so a user hovering through
# many video tiles can't drain uvicorn's threadpool and lock up the
# whole relay. Sized for "watching 1 vid + 1 preload sibling + a
# little headroom for the brief overlap when switching tiles". JPEG
# thumbnail loads bypass this cap entirely (no Range header).
# Past the cap, late arrivals wait up to _VIDEO_STREAM_WAIT_S for a
# slot, then 503 — the browser <video> element retries Range requests
# naturally so a transient 503 just costs a few hundred ms.
_VIDEO_STREAM_CAP = 6
_VIDEO_STREAM_WAIT_S = 2.0
_video_stream_sem = threading.BoundedSemaphore(_VIDEO_STREAM_CAP)
_video_stream_503s: int = 0

# Stable handle for /img assets. OF CDN URLs include a signed Policy +
# Signature + Key-Pair-Id that rotate per upstream fetch — the SAME
# physical image looks like a different URL each time we re-list a chat.
# Browser HTTP cache keys by full URL, so signature rotation gives a
# 0% hit rate across re-fetches of the same chat.
#
# Fix: hash the stable identity of the asset (host + path, scoped by
# account), keep an in-memory hash → most-recent-signed-URL map, and
# expose /img/by-hash/<h> that the frontend can use as a stable cache
# key once it's seen the hash via /admin/img-cache/stats or via the
# response header X-Img-Hash from /img?u=...
#
# TTL on entries matches OF's signed-URL lifetime (~1h). After expiry,
# /img/by-hash/<h> returns 410 and the frontend falls back to /img?u=
# with a fresh URL from the next /messages payload.
_IMG_HASH_TTL_S = 60 * 60
_IMG_HASH_CAP = 50_000
_img_hash_lock = threading.Lock()
_img_hash_map: dict[str, tuple[str, float]] = {}   # hash -> (signed_url, ts_seen)


def _img_stable_hash(account_id: str, u: str) -> str:
    """SHA-1 (truncated to 20 hex chars) over the stable identity of an
    OF CDN asset. Drops the query string (Policy/Signature/Key-Pair-Id),
    lowercases the host, scopes by account so two accounts can't collide
    on a shared asset that might be re-signed differently."""
    from urllib.parse import urlparse as _urlparse
    parsed = _urlparse(u)
    base = (parsed.hostname or "").lower() + (parsed.path or "")
    return hashlib.sha1(f"{account_id}\0{base}".encode("utf-8")).hexdigest()[:20]


def _img_hash_remember(account_id: str, u: str) -> str:
    """Record (or refresh) the hash → signed-URL mapping. Returns the hash.
    Naive size-cap: when over _IMG_HASH_CAP, drop oldest 10% by ts."""
    h = _img_stable_hash(account_id, u)
    now = _time_mod.monotonic()
    with _img_hash_lock:
        _img_hash_map[h] = (u, now)
        if len(_img_hash_map) > _IMG_HASH_CAP:
            # Sort once, drop the bottom 10% in one pass — cheaper than
            # per-eviction in steady state. Triggered rarely (only at cap).
            ordered = sorted(_img_hash_map.items(), key=lambda kv: kv[1][1])
            for k, _ in ordered[: len(ordered) // 10]:
                _img_hash_map.pop(k, None)
    return h


# ── On-disk image cache ───────────────────────────────────────────────
#
# Persistent cache for /img bytes, keyed by hostname+path (signature
# stripped) so it's shared across all accounts/users. Motivation:
#
#   - OF CDN URLs carry a Policy/Signature query string that rotates
#     every ~hours. The browser HTTP cache keys on the full URL, so
#     the moment OF hands us a freshly-signed URL for the same physical
#     asset (chat refetch, vault re-list, signature expiry) the browser
#     cache misses even within the same session.
#   - This cache keys on host+path → those re-signed URLs become disk
#     hits. Same physical image, served from disk, no upstream call.
#   - User A and user B viewing the same chat/profile both hit the same
#     bytes — cross-session sharing without a CDN round-trip each.
#
# Only stores SMALL FULL responses. Range requests (browser <video>) and
# 206 partials are never cached — partials would poison the cache, and
# videos are huge. _IMG_CACHE_MAX_BYTES caps per-file size as a safety
# net even when Content-Length isn't trustworthy.
_IMG_CACHE_DIR = Path(os.environ.get("IMG_CACHE_DIR", "/tmp/of-relay-img-cache"))
_IMG_CACHE_MAX_BYTES = int(os.environ.get("IMG_CACHE_MAX_BYTES", str(2 * 1024 * 1024)))
_IMG_CACHE_TTL_S = int(os.environ.get("IMG_CACHE_TTL_S", str(7 * 24 * 60 * 60)))
_IMG_CACHE_EVICT_INTERVAL_S = 30 * 60
_img_cache_hits: int = 0
_img_cache_misses: int = 0
_img_cache_writes: int = 0
_img_cache_skipped_partial: int = 0
_img_cache_skipped_too_big: int = 0
_img_cache_write_errors: int = 0


def _img_cache_key(u: str) -> str:
    """Account-agnostic SHA-1 over host+path. Same physical asset → same
    key, regardless of which account fetched it or how OF signed the URL."""
    from urllib.parse import urlparse as _urlparse
    parsed = _urlparse(u)
    base = (parsed.hostname or "").lower() + (parsed.path or "")
    return hashlib.sha1(base.encode("utf-8")).hexdigest()


def _img_cache_paths(h: str) -> tuple[Path, Path]:
    """Returns (bytes_path, content_type_path). Sharded into 2-char dirs
    so any one dir doesn't grow unbounded (matters for ext4/some FS)."""
    sub = _IMG_CACHE_DIR / h[:2]
    return sub / f"{h}.bin", sub / f"{h}.ct"


def _img_cache_lookup(u: str) -> tuple[Path, str] | None:
    """Returns (bytes_path, content_type) if a complete cache entry exists,
    else None. A complete entry has BOTH .bin and .ct present — half-written
    state is invisible (writers use unique .tmp paths and atomic rename)."""
    h = _img_cache_key(u)
    bin_p, ct_p = _img_cache_paths(h)
    if not bin_p.is_file() or not ct_p.is_file():
        return None
    try:
        ct = ct_p.read_text(encoding="utf-8", errors="replace").strip() or "application/octet-stream"
    except OSError:
        return None
    return bin_p, ct


def _img_cache_touch(bin_p: Path, ct_p: Path) -> None:
    """Refresh an entry's mtime on a cache HIT so the TTL evictor ages it by
    last-ACCESS, not last-WRITE. Without this a frequently-served image (a
    popular vault preview shared by many messages) is evicted at
    _IMG_CACHE_TTL_S even while in active use, forcing a CDN refetch and a
    visible tile flicker. Throttled: only bumps when the entry is already
    older than one evict interval, so a hot file costs at most ~one utime per
    interval — not one per request. Best-effort; never raises into the
    request path."""
    try:
        now = _time_mod.time()
        if now - bin_p.stat().st_mtime < _IMG_CACHE_EVICT_INTERVAL_S:
            return  # touched recently — skip the syscall
        os.utime(bin_p, (now, now))
        try:
            os.utime(ct_p, (now, now))
        except OSError:
            pass
    except OSError:
        pass


def _img_cache_write(u: str, content_type: str, data: bytes) -> bool:
    """Atomically write `data` for `u` to the cache. Unique .tmp filename
    + rename means concurrent writers for the same hash don't corrupt
    each other (last writer wins, identical bytes either way). Returns
    True on success."""
    global _img_cache_writes, _img_cache_write_errors
    h = _img_cache_key(u)
    bin_p, ct_p = _img_cache_paths(h)
    try:
        bin_p.parent.mkdir(parents=True, exist_ok=True)
        suffix = f".tmp.{os.getpid()}.{threading.get_ident()}"
        tmp_bin = bin_p.with_suffix(bin_p.suffix + suffix)
        tmp_ct = ct_p.with_suffix(ct_p.suffix + suffix)
        tmp_bin.write_bytes(data)
        tmp_ct.write_text(content_type, encoding="utf-8")
        os.replace(tmp_bin, bin_p)
        os.replace(tmp_ct, ct_p)
        _img_cache_writes += 1
        return True
    except OSError:
        _img_cache_write_errors += 1
        # Best-effort cleanup of leftover .tmp files; ignore errors.
        for p in (tmp_bin, tmp_ct):  # type: ignore[possibly-unbound]
            try: p.unlink()
            except (OSError, NameError): pass
        return False


def _img_cache_evict_once() -> int:
    """Walk the cache dir and delete entries older than _IMG_CACHE_TTL_S
    (by mtime). Returns the number of (bin, ct) pairs removed. Cheap on
    a few-tens-of-thousands of files; if the cache grows past that we'd
    want a smarter index, but this is fine for current expected size."""
    if not _IMG_CACHE_DIR.exists():
        return 0
    import time as _t
    cutoff = _t.time() - _IMG_CACHE_TTL_S
    removed = 0
    # Pinned just-sent media (Wave 2.1) is protected from TTL eviction
    # during its warm-up window so the local copy always outlives OF's
    # CDN cold-start. Prune expired pins first, then snapshot the surviving
    # key set so we can skip those files below.
    _pin_prune_expired()
    with _pin_lock:
        protected = set(_pinned_keys)
    try:
        for sub in _IMG_CACHE_DIR.iterdir():
            if not sub.is_dir():
                continue
            for f in sub.iterdir():
                try:
                    if f.stem in protected:
                        continue
                    if f.stat().st_mtime < cutoff:
                        f.unlink()
                        if f.suffix == ".bin":
                            removed += 1
                except OSError:
                    continue
    except OSError:
        return removed
    return removed


async def _img_cache_evictor_loop() -> None:
    """Periodic TTL sweep. Same shape as _storyboard_evictor_loop."""
    while True:
        try:
            await asyncio.sleep(_IMG_CACHE_EVICT_INTERVAL_S)
            removed = await asyncio.to_thread(_img_cache_evict_once)
            if removed:
                log.info("img cache evicted: %d entries (TTL=%ds)", removed, _IMG_CACHE_TTL_S)
        except asyncio.CancelledError:
            return
        except Exception:
            log.warning("img cache evictor cycle failed", exc_info=True)


# ── Pinned outbound media (Wave 2.1) ──────────────────────────────────
#
# When WE send an image, the frontend renders it instantly from a local
# blob (useSendMessage `_localPreview`). After OF reconciles the message,
# the bubble swaps to /img?u=<of-cdn-url>. That CDN URL can be COLD for
# the first seconds/minutes after upload — OF's edge hasn't warmed the
# object yet — so a just-sent image can flip to a slow/blank state.
#
# Fix: right after a successful send, fetch the freshly-signed media URLs
# server-side ONCE and write the bytes into the on-disk /img cache under
# their host+path key — the SAME key the reconciled /img?u= request will
# resolve to (signature stripped, see `_img_cache_key`). The reconciled
# bubble then disk-HITs our local copy instead of racing OF's cold edge.
#
# We track the pin by MEDIA ID so (a) we never re-fetch the same media
# twice and (b) the TTL evictor leaves the entry alone during the warm-up
# window — guaranteeing the local copy outlives the CDN cold-start
# regardless of cache pressure. After the window, normal TTL reclaims it.
_IMG_PIN_WARM_S = int(os.environ.get("IMG_PIN_WARM_S", str(24 * 60 * 60)))
_IMG_PIN_MAX_URLS = 6                 # cap variants warmed per media (full/thumb/preview/…)
_pin_lock = threading.Lock()
# media_id -> {"keys": set[str], "pinned_at": float (monotonic)}
_pinned_media: dict[str, dict[str, Any]] = {}
_pinned_keys: set[str] = set()        # union of pinned cache-key hashes (fast evictor skip)
_img_pins_added: int = 0
_img_pins_fetched: int = 0
_img_pins_fetch_errors: int = 0
_img_pins_skipped_existing: int = 0


def _pin_prune_expired() -> None:
    """Drop pins past the warm-up window so normal TTL can reclaim their
    bytes, and rebuild the flat key set from survivors. Cheap; safe to
    call on every evictor pass."""
    now = _time_mod.monotonic()
    with _pin_lock:
        stale = [m for m, p in _pinned_media.items()
                 if now - p["pinned_at"] > _IMG_PIN_WARM_S]
        if not stale:
            return
        for m in stale:
            _pinned_media.pop(m, None)
        _pinned_keys.clear()
        for p in _pinned_media.values():
            _pinned_keys.update(p["keys"])


def _media_urls_from_result(result: Any) -> list[tuple[str, list[str]]]:
    """Extract [(media_id, [absolute CDN urls])] from an OF send response.
    Tolerant of shape drift: walks a top-level `media` list (the
    /chats/{id}/messages send echo) and pulls every `files.*.url` plus any
    top-level src/preview/thumb url on the item. Returns [] for shapes
    without per-message media (mass/queue echoes), so it's a no-op there."""
    out: list[tuple[str, list[str]]] = []
    if not isinstance(result, dict):
        return out
    media = result.get("media")
    if not isinstance(media, list):
        return out
    for it in media:
        if not isinstance(it, dict):
            continue
        mid = it.get("id")
        if mid is None:
            continue
        urls: list[str] = []
        files = it.get("files")
        if isinstance(files, dict):
            for variant in files.values():
                if isinstance(variant, dict):
                    u = variant.get("url")
                    if isinstance(u, str) and u.startswith("http"):
                        urls.append(u)
        for k in ("full", "src", "preview", "thumb", "squareThumb"):
            u = it.get(k)
            if isinstance(u, str) and u.startswith("http"):
                urls.append(u)
        if urls:
            out.append((str(mid), urls))
    return out


@contextlib.contextmanager
def _priority_lane_background(account_id: str):
    """Acquire the per-account total + background semaphores (background
    priority) for a server-side fetch with no request context. Mirrors
    `_priority_lane`'s background branch so a pin fetch can never starve a
    user-initiated OF call."""
    total, background = _lanes_for(account_id)
    background.acquire()
    total.acquire()
    try:
        yield
    finally:
        try: total.release()
        except ValueError: pass
        try: background.release()
        except ValueError: pass


def _pin_one_media_blocking(account_id: str, media_id: str, urls: list[str]) -> None:
    """Fetch each variant's bytes (via the account's OF client) and write
    them into the on-disk /img cache under their host+path key, then record
    the pin keyed by media id. Runs in a worker thread (blocking curl).
    Best-effort — never raises out."""
    global _img_pins_fetched, _img_pins_fetch_errors, _img_pins_added, _img_pins_skipped_existing
    from urllib.parse import urlparse as _urlparse
    # Dedup by cache key (variants can share a path) and cap how many we warm.
    seen: dict[str, str] = {}   # cache_key -> url
    for u in urls:
        try:
            host = (_urlparse(u).hostname or "").lower()
        except Exception:
            continue
        if not any(host.endswith(s) for s in _ALLOWED_CDN_SUFFIXES):
            continue
        seen.setdefault(_img_cache_key(u), u)
        if len(seen) >= _IMG_PIN_MAX_URLS:
            break
    if not seen:
        return
    try:
        client = _load_client(account_id)
    except Exception:
        return
    pinned: set[str] = set()
    for k, u in seen.items():
        # Already on disk (e.g. re-sending a vault asset we've served
        # before) → no fetch, just pin the existing entry so the evictor
        # protects it for the warm-up window.
        if _img_cache_lookup(u) is not None:
            pinned.add(k)
            _img_pins_skipped_existing += 1
            continue
        try:
            with _priority_lane_background(account_id):
                r = client.http.get(u, timeout=client.timeout_s, stream=False)
            if r.status_code != 200:
                _img_pins_fetch_errors += 1
                continue
            data = r.content or b""
            # Honour the same size cap as the live cache — a >2MB asset
            # wouldn't be disk-cached on reconcile anyway, so pinning it
            # would just be dead weight the serve path can't use.
            if not (0 < len(data) <= _IMG_CACHE_MAX_BYTES):
                continue
            ct = r.headers.get("Content-Type", "application/octet-stream")
            if _img_cache_write(u, ct, data):
                pinned.add(k)
                _img_pins_fetched += 1
        except Exception:
            _img_pins_fetch_errors += 1
            continue
    if not pinned:
        return
    now = _time_mod.monotonic()
    with _pin_lock:
        entry = _pinned_media.get(media_id)
        if entry is None:
            entry = {"keys": set(), "pinned_at": now}
            _pinned_media[media_id] = entry
        entry["keys"] |= pinned
        entry["pinned_at"] = now
        _pinned_keys.update(pinned)
    _img_pins_added += 1


def _pin_sent_media(account_id: str, result: Any) -> None:
    """Fire-and-forget from a send handler: warm + pin the on-disk /img
    cache with the bytes of any media in an outbound send response, so the
    reconciled bubble serves a local copy instead of OF's cold edge.
    Reserves the media id under the lock immediately so concurrent/rapid
    sends of the same asset don't double-fetch. Never blocks the response;
    never raises."""
    try:
        items = _media_urls_from_result(result)
    except Exception:
        return
    if not items:
        return
    _pin_prune_expired()
    now = _time_mod.monotonic()
    fresh: list[tuple[str, list[str]]] = []
    with _pin_lock:
        for media_id, urls in items:
            if media_id in _pinned_media:
                continue
            # Reserve now (empty key set) so a second send of the same media
            # in-flight is deduped; the worker fills in the keys on success.
            _pinned_media[media_id] = {"keys": set(), "pinned_at": now}
            fresh.append((media_id, urls))
    for media_id, urls in fresh:
        try:
            asyncio.create_task(
                asyncio.to_thread(_pin_one_media_blocking, account_id, media_id, urls)
            )
        except Exception:
            log.debug("pin dispatch failed (media_id=%s)", media_id, exc_info=True)


@app.get("/admin/streams")
def admin_streams() -> dict:
    """Live view of the /img stream budget. `active` = currently streaming
    right now; `high_water` = peak since startup. If active sits near the
    thread-pool limit while you scroll, that's the bottleneck. If it stays
    low but you still see ECONNRESETs, look elsewhere (Next dev proxy,
    upstream OF, network)."""
    return {
        "active": _img_active,
        "high_water": _img_high_water,
        "total_started": _img_total_started,
        "total_aborted": _img_total_aborted,
        "video_stream_cap": _VIDEO_STREAM_CAP,
        "video_stream_503s": _video_stream_503s,
    }


# ── /admin/errors ───────────────────────────────────────────────────
# Persisted error log. The frontend POSTs unhandled errors here via
# useErrorReporter; the server's exception middleware writes 500s
# here too. GET returns recent rows for the badge + admin viewer.

class _AppErrorBody(BaseModel):
    source: str = "browser"             # "browser" | "server" (clients should send "browser")
    kind: str = "error"                 # short category — "unhandledrejection", "react-render", …
    message: str
    stack: str | None = None
    url: str | None = None
    account_id: str | None = None
    employee_id: int | None = None
    user_agent: str | None = None
    # Free-form bag of extra fields the client wants persisted. Stored
    # as a JSON blob; we don't enforce a schema.
    context: dict[str, Any] | None = None


@app.post("/admin/errors")
async def admin_errors_create(body: _AppErrorBody = Body(...)) -> dict[str, Any]:
    """Persist a single error report. Never raises — if we can't write
    we still 200 so the reporter loop doesn't error-on-error."""
    from db.engine import get_session
    from db.models import AppError
    try:
        async with get_session() as s:
            row = AppError(
                source=(body.source or "browser")[:32],
                kind=(body.kind or "error")[:64],
                message=(body.message or "")[:4096],
                stack=body.stack[:16384] if body.stack else None,
                url=body.url[:1024] if body.url else None,
                account_id=body.account_id[:64] if body.account_id else None,
                employee_id=body.employee_id,
                user_agent=body.user_agent[:512] if body.user_agent else None,
                context_json=json.dumps(body.context)[:16384] if body.context else None,
            )
            s.add(row)
            await s.commit()
            return {"ok": True, "id": row.id}
    except Exception:
        log.exception("failed to persist app_error")
        return {"ok": False}


@app.delete("/admin/errors")
async def admin_errors_clear(
    since_hours: int | None = Query(None, ge=1, le=720, description="Only clear rows within this window; omit to clear all"),
    source: str | None = Query(None, description="Filter by 'browser' or 'server'"),
) -> dict[str, Any]:
    """Dismiss errors — used by the TopNav badge's Clear button. Deletes
    rows matching the same filter the GET uses so the badge count drops
    to zero immediately."""
    from db.engine import get_session
    from db.models import AppError
    from sqlalchemy import delete as sa_delete
    async with get_session() as s:
        stmt = sa_delete(AppError)
        if since_hours is not None:
            cutoff = datetime.utcnow() - timedelta(hours=since_hours)
            stmt = stmt.where(AppError.occurred_at >= cutoff)
        if source:
            stmt = stmt.where(AppError.source == source)
        result = await s.execute(stmt)
        await s.commit()
        return {"ok": True, "deleted": result.rowcount or 0}


@app.get("/admin/errors")
async def admin_errors_list(
    limit: int = Query(50, ge=1, le=500),
    since_hours: int = Query(24, ge=1, le=720),
    source: str | None = Query(None, description="Filter by 'browser' or 'server'"),
) -> dict[str, Any]:
    """Recent errors, newest first. `since_hours` defaults to 24h so the
    TopNav badge can count today's errors cheaply."""
    from db.engine import get_session
    from db.models import AppError
    from sqlalchemy import select, and_
    cutoff = datetime.utcnow() - timedelta(hours=since_hours)
    async with get_session() as s:
        stmt = select(AppError).where(AppError.occurred_at >= cutoff)
        if source:
            stmt = stmt.where(AppError.source == source)
        stmt = stmt.order_by(AppError.occurred_at.desc()).limit(limit)
        rows = (await s.execute(stmt)).scalars().all()
        return {
            "count": len(rows),
            "since_hours": since_hours,
            "list": [
                {
                    "id": r.id,
                    "occurred_at": r.occurred_at.isoformat() + "Z",
                    "source": r.source,
                    "kind": r.kind,
                    "message": r.message,
                    "stack": r.stack,
                    "url": r.url,
                    "account_id": r.account_id,
                    "employee_id": r.employee_id,
                    "user_agent": r.user_agent,
                    "context": json.loads(r.context_json) if r.context_json else None,
                }
                for r in rows
            ],
        }


# ── /admin/perflog — client-side timing log ingest ────────────────
# Frontend `app/lib/perfLog.ts` batches PerfEvents and POSTs them here so
# we can aggregate "tab open → call asked → call delivered" timing across
# all chatters. Append-only. Pruned every `_PERFLOG_EVICT_INTERVAL_S` by
# `_perflog_evictor_loop()` so the table stays bounded.

_PERFLOG_RETAIN_S = int(os.environ.get("PERFLOG_RETAIN_S", str(7 * 24 * 60 * 60)))
_PERFLOG_EVICT_INTERVAL_S = 60 * 60
_PERFLOG_BATCH_MAX = 500
_PERFLOG_META_MAX_BYTES = 4 * 1024


class _PerfLogEventBody(BaseModel):
    opId: str
    kind: str
    phase: str
    ts: float  # client epoch ms (may be sub-ms float from performance.timeOrigin)
    tabId: str | None = None
    meta: dict[str, Any] | None = None


class _PerfLogIngestBody(BaseModel):
    tabId: str
    parentTabId: str | None = None
    employeeId: int | None = None
    accountId: str | None = None
    events: list[_PerfLogEventBody]


@app.post("/admin/perflog/ingest")
async def admin_perflog_ingest(request: Request, body: _PerfLogIngestBody = Body(...)) -> dict[str, Any]:
    """Persist a batch of client-side perf events. Never raises — if we
    can't write we still 200 so the client's flush loop doesn't error-on-
    error. Sent via `navigator.sendBeacon` on page unload, plain
    `fetch(..., keepalive: true)` otherwise.

    Soft-tags rows with the requesting employee + account from headers
    when the body didn't carry them, so analytics queries can filter on
    "who's been seeing slow loads"."""
    from db.engine import get_session
    from db.models import PerfEventRow

    if not body.events:
        return {"ok": True, "inserted": 0}
    events = body.events[:_PERFLOG_BATCH_MAX]

    # Best-effort identity backfill from headers (employee picker writes
    # X-Employee-Id on every relayed call; X-Account-Id rides scope).
    hdr_employee_raw = request.headers.get("x-employee-id")
    try:
        hdr_employee_id: int | None = int(hdr_employee_raw) if hdr_employee_raw else None
    except ValueError:
        hdr_employee_id = None
    hdr_account_id = request.headers.get("x-account-id") or None

    employee_id = body.employeeId if body.employeeId is not None else hdr_employee_id
    account_id = body.accountId if body.accountId else hdr_account_id

    try:
        async with get_session() as s:
            for e in events:
                meta_str: str | None = None
                if e.meta is not None:
                    try:
                        meta_str = json.dumps(e.meta, default=str)[:_PERFLOG_META_MAX_BYTES]
                    except (TypeError, ValueError):
                        meta_str = None
                # The body tabId is per-batch; fall back to per-event if a
                # client ever wants to ship cross-tab batches (today they
                # don't — but keeping the schema honest).
                tab_id = (e.tabId or body.tabId)[:64]
                s.add(PerfEventRow(
                    tab_id=tab_id,
                    parent_tab_id=body.parentTabId[:64] if body.parentTabId else None,
                    op_id=(e.opId or "")[:128],
                    kind=(e.kind or "")[:64],
                    phase=(e.phase or "")[:32],
                    client_ts_ms=int(e.ts),
                    employee_id=employee_id,
                    account_id=account_id[:64] if account_id else None,
                    meta_json=meta_str,
                ))
            await s.commit()
        return {"ok": True, "inserted": len(events)}
    except Exception:
        log.exception("perflog ingest failed")
        return {"ok": False}


@app.get("/admin/perflog/recent")
async def admin_perflog_recent(
    limit: int = Query(200, ge=1, le=2000),
    since_minutes: int = Query(60, ge=1, le=24 * 60 * 7),
    kind: str | None = Query(None, description="Filter by event kind (exact match)"),
    tab_id: str | None = Query(None, description="Filter to a single tab session"),
    op_id: str | None = Query(None, description="Filter to a single op id (all phases)"),
) -> dict[str, Any]:
    """Recent perfLog events, newest first. Powers ad-hoc inspection of
    timing across chatters — e.g. histogram of `vault.media` request→
    delivered deltas, or per-tab "open → first useful paint" times."""
    from db.engine import get_session
    from db.models import PerfEventRow
    from sqlalchemy import select

    cutoff = datetime.utcnow() - timedelta(minutes=since_minutes)
    async with get_session() as s:
        stmt = select(PerfEventRow).where(PerfEventRow.received_at >= cutoff)
        if kind:
            stmt = stmt.where(PerfEventRow.kind == kind)
        if tab_id:
            stmt = stmt.where(PerfEventRow.tab_id == tab_id)
        if op_id:
            stmt = stmt.where(PerfEventRow.op_id == op_id)
        stmt = stmt.order_by(PerfEventRow.received_at.desc()).limit(limit)
        rows = (await s.execute(stmt)).scalars().all()
        return {
            "count": len(rows),
            "since_minutes": since_minutes,
            "list": [
                {
                    "id": r.id,
                    "tab_id": r.tab_id,
                    "parent_tab_id": r.parent_tab_id,
                    "op_id": r.op_id,
                    "kind": r.kind,
                    "phase": r.phase,
                    "client_ts_ms": r.client_ts_ms,
                    "received_at": r.received_at.isoformat() + "Z",
                    "employee_id": r.employee_id,
                    "account_id": r.account_id,
                    "meta": json.loads(r.meta_json) if r.meta_json else None,
                }
                for r in rows
            ],
        }


# ── messages.raw_json retention ───────────────────────────────────────
#
# `raw_json` stores the full OF message payload per row (avg ~8 KB, mostly
# the media[] array nobody reads back — it's only a migration safety-net).
# Left unbounded it grew the DB to ~1.9 GB. We keep a recent window so a
# future backfill still has fresh payloads to read, and NULL anything older.
#
# This is a NULL-out, not a row delete — message rows stay intact, only the
# heavy column is cleared. auto_vacuum is off, so freed pages return to the
# freelist and are reused by new inserts; the file plateaus rather than
# growing. To actually shrink the file on disk, run VACUUM manually.
_RAW_JSON_RETAIN_S = int(os.environ.get("RAW_JSON_RETAIN_S", str(60 * 24 * 60 * 60)))
_RAW_JSON_EVICT_INTERVAL_S = int(os.environ.get("RAW_JSON_EVICT_INTERVAL_S", str(24 * 60 * 60)))


async def _raw_json_evictor_loop() -> None:
    """Periodic NULL-out of messages.raw_json older than _RAW_JSON_RETAIN_S.
    Caps DB growth (the payloads are a write-only migration buffer). Runs
    daily by default; tune via RAW_JSON_RETAIN_S / RAW_JSON_EVICT_INTERVAL_S."""
    from db.engine import get_session
    from db.models import Message
    from sqlalchemy import update as sa_update
    while True:
        try:
            await asyncio.sleep(_RAW_JSON_EVICT_INTERVAL_S)
            cutoff = datetime.utcnow() - timedelta(seconds=_RAW_JSON_RETAIN_S)
            async with get_session() as s:
                res = await s.execute(
                    sa_update(Message)
                    .where(Message.created_at < cutoff, Message.raw_json.isnot(None))
                    .values(raw_json=None)
                )
                await s.commit()
                if (res.rowcount or 0) > 0:
                    log.info("raw_json evictor cleared %d rows (retain=%ds)", res.rowcount, _RAW_JSON_RETAIN_S)
        except asyncio.CancelledError:
            return
        except Exception:
            log.warning("raw_json evictor cycle failed", exc_info=True)


async def _perflog_evictor_loop() -> None:
    """Periodic prune of perf_events older than _PERFLOG_RETAIN_S. Cheap —
    indexed on received_at. Runs hourly by default; tune via env."""
    from db.engine import get_session
    from db.models import PerfEventRow
    from sqlalchemy import delete as sa_delete
    while True:
        try:
            await asyncio.sleep(_PERFLOG_EVICT_INTERVAL_S)
            cutoff = datetime.utcnow() - timedelta(seconds=_PERFLOG_RETAIN_S)
            async with get_session() as s:
                res = await s.execute(
                    sa_delete(PerfEventRow).where(PerfEventRow.received_at < cutoff)
                )
                await s.commit()
                if (res.rowcount or 0) > 0:
                    log.info("perflog evictor pruned %d rows (retain=%ds)", res.rowcount, _PERFLOG_RETAIN_S)
        except asyncio.CancelledError:
            return
        except Exception:
            log.warning("perflog evictor cycle failed", exc_info=True)


# ── raw-OF-payload SIZE cap (event_inbox + messages.raw_json) ─────────
#
# Two columns hoard raw OnlyFans payloads we receive but read back only for
# a short window:
#   • event_inbox.payload_json — the raw WS/SSE event firehose. Every event
#     is already transcoded into permanent tables (DMs → messages, subs →
#     fans, tips → transactions); the envelope is re-read ONLY for SSE
#     Last-Event-ID reconnect replay (minutes–hours) and provider_event_id
#     idempotency. It was NEVER pruned → the real leak.
#   • messages.raw_json — the full OF message JSON (mostly the media[] array
#     nobody reads back). The real message lives in its own columns. The
#     time-based _raw_json_evictor_loop (60d) already trims it; this size
#     cap is the hard ceiling on top, for when volume — not age — is the
#     problem.
#
# The time-based evictors above bound the trailing window; THIS bounds the
# absolute footprint so the SQLite file stays well under ~2 GB.
#
# Two facts shape the design:
#   1. auto_vacuum is OFF on the prod DB, so DELETE / NULL only moves pages
#      to the freelist — the FILE plateaus at its high-water mark, it does
#      not shrink. So a "if file > 2 GB, delete" cap would never shrink
#      anything. We therefore drive the cap off LOGICAL content bytes
#      (SUM(LENGTH(...))), and reclaim disk separately via incremental_vacuum
#      once the DB has been converted to auto_vacuum=INCREMENTAL (a one-time
#      full VACUUM — gated behind PAYLOAD_VACUUM_ON_START so it only runs on
#      a deliberately-scheduled off-peak restart).
#   2. We must lose ZERO fan data. The cap NEVER deletes messages/fans/
#      transactions ROWS — it deletes spent event_inbox envelopes and NULLs
#      the duplicate raw_json column, and NEVER touches anything newer than
#      the retention floor (the last EVENT_INBOX_RETAIN_FLOOR_S stays intact
#      for debugging + SSE replay).
#
# Default budget 1.5 GB of payload bytes keeps total DB < 2 GB (the rest —
# message columns, fans, txns, indexes — is ~0.4 GB and grows slowly).
_PAYLOAD_BUDGET_BYTES = int(os.environ.get("PAYLOAD_BUDGET_BYTES", str(1_500_000_000)))
_PAYLOAD_RETAIN_FLOOR_S = int(os.environ.get("EVENT_INBOX_RETAIN_FLOOR_S", str(2 * 24 * 60 * 60)))
_PAYLOAD_CAP_INTERVAL_S = int(os.environ.get("PAYLOAD_CAP_INTERVAL_S", str(60 * 60)))
_PAYLOAD_EVICT_BATCH = int(os.environ.get("PAYLOAD_EVICT_BATCH", "2000"))
# One-time conversion of an existing auto_vacuum=NONE DB to INCREMENTAL +
# the heavy full VACUUM that makes it take effect. Off by default; flip on
# for a single off-peak restart (via a deploy), then unset. After conversion,
# incremental_vacuum each tick returns freed pages to the OS for free.
_PAYLOAD_VACUUM_ON_START = os.environ.get("PAYLOAD_VACUUM_ON_START", "0") == "1"
# Pages reclaimed per tick once auto_vacuum=INCREMENTAL. Bounded so each tick
# holds the write lock only briefly (default ~80 MB at 4 KiB pages).
_PAYLOAD_INCR_VACUUM_PAGES = int(os.environ.get("PAYLOAD_INCR_VACUUM_PAGES", "20000"))

# VACUUM / incremental_vacuum are whole-DB write locks; never let two run at
# once, and never overlap a full VACUUM with an eviction pass.
_payload_vacuum_lock = asyncio.Lock()


def _floor_cutoff_str(floor_s: int) -> str:
    """Retention-floor cutoff as the fixed-width string SQLAlchemy stores
    DateTime in ('YYYY-MM-DD HH:MM:SS.ffffff'), so a raw `received_at < ?`
    comparison sorts chronologically. Rows newer than this are never touched."""
    return (datetime.utcnow() - timedelta(seconds=floor_s)).strftime("%Y-%m-%d %H:%M:%S.%f")


async def _payload_bytes(conn) -> tuple[int, int]:
    """(event_inbox.payload_json bytes, messages.raw_json bytes) via SUM(LENGTH).
    This is the LOGICAL content size the cap is driven off — not file size."""
    ev = (await conn.exec_driver_sql(
        "SELECT COALESCE(SUM(LENGTH(payload_json)), 0) FROM event_inbox"
    )).scalar() or 0
    rj = (await conn.exec_driver_sql(
        "SELECT COALESCE(SUM(LENGTH(raw_json)), 0) FROM messages"
    )).scalar() or 0
    return int(ev), int(rj)


async def evict_payloads_once(
    *,
    budget_bytes: int | None = None,
    floor_s: int | None = None,
    batch: int | None = None,
) -> dict[str, int]:
    """Size-driven eviction of raw OF payloads. SQLite only (no-op elsewhere —
    Postgres autovacuums and has no rowid).

    Frees logical payload bytes until
        SUM(LENGTH(event_inbox.payload_json)) + SUM(LENGTH(messages.raw_json))
    is under `budget_bytes`, deleting the OLDEST event_inbox rows first (the
    real leak, lowest value — a spent envelope), then NULLing the OLDEST
    messages.raw_json — but NEVER touching any row whose timestamp is within
    `floor_s` of now. Eviction happens in autocommit batches so the write
    lock is released between batches and the WS pump keeps flowing.

    Touches ONLY the two raw envelope columns; messages / fans / transactions
    ROWS are left fully intact. Idempotent: a no-op once under budget, and a
    no-op once everything older than the floor is gone (it will not breach the
    floor to reach budget — it logs that it's floor-bound instead).

    Returns a summary dict (also used by the test to assert before/after)."""
    from db.engine import engine, _is_sqlite
    budget = _PAYLOAD_BUDGET_BYTES if budget_bytes is None else budget_bytes
    floor = _PAYLOAD_RETAIN_FLOOR_S if floor_s is None else floor_s
    bsize = _PAYLOAD_EVICT_BATCH if batch is None else batch
    if not _is_sqlite:
        return {"skipped": 1}

    cutoff = _floor_cutoff_str(floor)
    deleted_events = nulled_messages = freed = 0

    async with engine.connect() as conn:
        await conn.execution_options(isolation_level="AUTOCOMMIT")
        ev0, rj0 = await _payload_bytes(conn)
        before = ev0 + rj0
        over = before - budget

        if over > 0:
            # 1) event_inbox — delete oldest spent envelopes older than floor.
            while freed < over:
                row = (await conn.exec_driver_sql(
                    "SELECT COALESCE(SUM(LENGTH(payload_json)), 0), COUNT(*) FROM "
                    "(SELECT payload_json FROM event_inbox "
                    " WHERE received_at < ? ORDER BY rowid ASC LIMIT ?)",
                    (cutoff, bsize),
                )).first()
                nbytes, cnt = int(row[0] or 0), int(row[1] or 0)
                if cnt == 0:
                    break  # nothing left older than the floor
                await conn.exec_driver_sql(
                    "DELETE FROM event_inbox WHERE rowid IN "
                    "(SELECT rowid FROM event_inbox WHERE received_at < ? "
                    " ORDER BY rowid ASC LIMIT ?)",
                    (cutoff, bsize),
                )
                deleted_events += cnt
                freed += nbytes
                await asyncio.sleep(0)  # yield between batches

            # 2) messages.raw_json — NULL oldest duplicates older than floor.
            while freed < over:
                row = (await conn.exec_driver_sql(
                    "SELECT COALESCE(SUM(LENGTH(raw_json)), 0), COUNT(*) FROM "
                    "(SELECT raw_json FROM messages "
                    " WHERE raw_json IS NOT NULL AND created_at < ? "
                    " ORDER BY rowid ASC LIMIT ?)",
                    (cutoff, bsize),
                )).first()
                nbytes, cnt = int(row[0] or 0), int(row[1] or 0)
                if cnt == 0:
                    break
                await conn.exec_driver_sql(
                    "UPDATE messages SET raw_json = NULL WHERE rowid IN "
                    "(SELECT rowid FROM messages WHERE raw_json IS NOT NULL "
                    " AND created_at < ? ORDER BY rowid ASC LIMIT ?)",
                    (cutoff, bsize),
                )
                nulled_messages += cnt
                freed += nbytes
                await asyncio.sleep(0)

        ev1, rj1 = await _payload_bytes(conn)

    after = ev1 + rj1
    summary = {
        "before_bytes": before,
        "after_bytes": after,
        "budget_bytes": budget,
        "freed_bytes": freed,
        "deleted_events": deleted_events,
        "nulled_messages": nulled_messages,
        "event_bytes_after": ev1,
        "raw_json_bytes_after": rj1,
        "floor_bound": int(after > budget),  # 1 = floor stopped us short of budget
    }
    return summary


async def _maybe_convert_to_incremental_autovacuum() -> None:
    """One-time: convert an existing auto_vacuum=NONE DB to INCREMENTAL so the
    freelist can be returned to the OS. The conversion itself requires a full
    VACUUM (rewrites the whole file — a heavy, whole-DB write lock), so it is
    gated behind PAYLOAD_VACUUM_ON_START and runs at most once per boot. No-op
    if already INCREMENTAL or not SQLite."""
    from db.engine import engine, _is_sqlite
    if not _is_sqlite or not _PAYLOAD_VACUUM_ON_START:
        return
    async with _payload_vacuum_lock:
        try:
            async with engine.connect() as conn:
                await conn.execution_options(isolation_level="AUTOCOMMIT")
                mode = (await conn.exec_driver_sql("PRAGMA auto_vacuum")).scalar()
                if int(mode or 0) == 2:
                    log.info("payload-cap: auto_vacuum already INCREMENTAL")
                    return
                log.warning("payload-cap: converting auto_vacuum→INCREMENTAL via full VACUUM "
                            "(heavy, whole-DB lock) — this runs once")
                await conn.exec_driver_sql("PRAGMA auto_vacuum=INCREMENTAL")
                await conn.exec_driver_sql("VACUUM")
                mode2 = (await conn.exec_driver_sql("PRAGMA auto_vacuum")).scalar()
                log.warning("payload-cap: auto_vacuum now=%s (2=INCREMENTAL)", mode2)
        except Exception:
            log.warning("payload-cap: auto_vacuum conversion failed", exc_info=True)


async def _reclaim_freelist() -> None:
    """Return up to _PAYLOAD_INCR_VACUUM_PAGES freelist pages to the OS. Cheap
    and bounded (unlike full VACUUM); only does anything once the DB is in
    auto_vacuum=INCREMENTAL mode — otherwise it is a silent no-op. Guarded by
    the vacuum lock so it never overlaps the one-time full VACUUM."""
    from db.engine import engine, _is_sqlite
    if not _is_sqlite:
        return
    async with _payload_vacuum_lock:
        try:
            async with engine.connect() as conn:
                await conn.execution_options(isolation_level="AUTOCOMMIT")
                if int((await conn.exec_driver_sql("PRAGMA auto_vacuum")).scalar() or 0) != 2:
                    return  # NONE/FULL mode: incremental_vacuum is a no-op
                free0 = int((await conn.exec_driver_sql("PRAGMA freelist_count")).scalar() or 0)
                if free0 <= 0:
                    return
                await conn.exec_driver_sql(f"PRAGMA incremental_vacuum({_PAYLOAD_INCR_VACUUM_PAGES})")
                free1 = int((await conn.exec_driver_sql("PRAGMA freelist_count")).scalar() or 0)
                if free0 != free1:
                    log.info("payload-cap: reclaimed %d freelist pages (%d→%d)",
                             free0 - free1, free0, free1)
        except Exception:
            log.warning("payload-cap: incremental_vacuum failed", exc_info=True)


async def _payload_size_cap_loop() -> None:
    """Periodic size cap on the raw OF payload columns. Each tick: evict down
    to PAYLOAD_BUDGET_BYTES (honoring the retention floor), then return freed
    pages to the OS. Hourly by default; tune via PAYLOAD_* env vars."""
    await _maybe_convert_to_incremental_autovacuum()
    while True:
        try:
            await asyncio.sleep(_PAYLOAD_CAP_INTERVAL_S)
            summary = await evict_payloads_once()
            if summary.get("freed_bytes"):
                log.info(
                    "payload-cap: %d→%d MB (budget %d MB) — deleted %d events, "
                    "nulled %d raw_json%s",
                    summary["before_bytes"] // 1_000_000,
                    summary["after_bytes"] // 1_000_000,
                    summary["budget_bytes"] // 1_000_000,
                    summary["deleted_events"],
                    summary["nulled_messages"],
                    " (floor-bound)" if summary.get("floor_bound") else "",
                )
            await _reclaim_freelist()
        except asyncio.CancelledError:
            return
        except Exception:
            log.warning("payload-cap cycle failed", exc_info=True)


# ── /admin/chats/recent — instant-load seed for the inbox ──────────
# Returns the most recent rows from the LOCAL `chats` table (populated
# by the WS transcoder) joined to `fans` for display names + avatars.
# Sub-50ms because everything is indexed in SQLite — no OF call.
# Used by the frontend to render an instant 10-25 row chat list while
# the live OF `/chats` fetch is still in flight, so users can click a
# conversation immediately instead of waiting for the cold OF round-trip.

@app.get("/admin/chats/recent")
async def admin_chats_recent(
    limit: int = Query(25, ge=1, le=100),
    account_id: str | None = Query(None, description="Filter to one account; omit for all"),
) -> dict[str, Any]:
    from db.engine import get_session
    from db.models import Chat as _Chat, Fan as _Fan
    from sqlalchemy import select
    account_ids = clamp_account_filter(account_id)
    async with get_session() as s:
        stmt = (
            select(_Chat, _Fan)
            .join(_Fan, (_Fan.account_id == _Chat.account_id) & (_Fan.fan_id == _Chat.fan_id), isouter=True)
            .where(_Chat.hidden_locally == False)  # noqa: E712
            .order_by(_Chat.last_message_at.desc().nullslast())
            .limit(limit)
        )
        if account_ids is not None:
            if not account_ids:
                return {"list": [], "source": "local-db"}
            stmt = stmt.where(_Chat.account_id.in_(account_ids))
        rows = (await s.execute(stmt)).all()
        out: list[dict[str, Any]] = []
        for chat, fan in rows:
            out.append({
                "__accountId": chat.account_id,
                "withUser": {
                    "id": chat.fan_id,
                    "name": fan.of_display_name if fan else None,
                    "username": fan.of_username if fan else None,
                    "avatar": fan.avatar_url if fan else None,
                },
                "lastMessage": {
                    "id": chat.last_message_id,
                    "text": chat.last_message_preview or "",
                    "createdAt": chat.last_message_at.isoformat() + "Z" if chat.last_message_at else None,
                } if chat.last_message_id else None,
                "hasUnread": (chat.unread_count or 0) > 0,
                "unreadMessagesCount": chat.unread_count or 0,
            })
        return {"list": out, "source": "local-db"}


@app.get("/img")
def proxy_image(request: Request, u: str = Query(..., description="Absolute OF CDN URL")):
    """Fetch `u` via the requesting account's OF client (so the source IP
    matches the URL's signed Policy) and stream the bytes back."""
    from urllib.parse import urlparse
    parsed = urlparse(u)
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(status_code=400, detail="bad scheme")
    host = (parsed.hostname or "").lower()
    if not any(host.endswith(s) for s in _ALLOWED_CDN_SUFFIXES):
        raise HTTPException(status_code=400, detail="host not allowed")

    range_hdr = request.headers.get("range") or request.headers.get("Range")

    # Disk-cache fast path: only safe when the client wants the WHOLE
    # asset (no Range). Cached bytes are account-agnostic — once we have
    # them, the upstream signature is irrelevant for serving them again.
    # Skipped on Range because we don't store partials.
    if not range_hdr:
        global _img_cache_hits, _img_cache_misses
        hit = _img_cache_lookup(u)
        if hit is not None:
            _img_cache_hits += 1
            bin_p, ct_cached = hit
            # Keep popular entries warm: age by last-access, not last-write,
            # so a hot vault preview isn't evicted out from under live use.
            _img_cache_touch(*_img_cache_paths(_img_cache_key(u)))
            cache_control = "public, max-age=604800, stale-while-revalidate=86400, immutable"
            return FileResponse(
                str(bin_p),
                media_type=ct_cached,
                headers={"Cache-Control": cache_control, "X-Img-Cache": "HIT"},
            )
        _img_cache_misses += 1

    client = _get_client(request)

    # Forward the browser's Range header so video <video> tags can seek.
    # Without this, the upstream always returns 200 with the whole file and
    # the browser can't jump past whatever's buffered — long videos can't
    # be scrubbed, and `currentTime` writes stall.
    upstream_headers: dict[str, str] = {}
    sem_held = False
    if range_hdr:
        upstream_headers["Range"] = range_hdr
        # Video streaming lane — bounded by _VIDEO_STREAM_CAP. JPEG
        # thumbnails (no Range header) bypass entirely so the grid stays
        # snappy even when the cap is saturated.
        if not _video_stream_sem.acquire(timeout=_VIDEO_STREAM_WAIT_S):
            global _video_stream_503s
            _video_stream_503s += 1
            raise HTTPException(
                status_code=503,
                detail="too many video streams in flight; retry shortly",
            )
        sem_held = True

    try:
        r = client.http.get(
            u, timeout=client.timeout_s, stream=True, headers=upstream_headers or None,
        )
    except Exception as e:
        if sem_held:
            try: _video_stream_sem.release()
            except ValueError: pass
        raise HTTPException(status_code=502, detail=f"upstream fetch failed: {e}")
    # 206 (Partial Content) is the normal Range response — treat it as ok.
    if r.status_code not in (200, 206):
        if sem_held:
            try: _video_stream_sem.release()
            except ValueError: pass
        raise HTTPException(status_code=r.status_code, detail="upstream non-2xx")

    ct = r.headers.get("Content-Type", "application/octet-stream")
    # OF's CDN sends short Cache-Control because the signed Policy expires,
    # but the IMAGE BYTES themselves don't expire. We override upstream's
    # header to keep the browser HTTP cache (and a future Service Worker)
    # holding the asset for 7 days — matches the disk-cache TTL above so
    # the browser doesn't re-fetch bytes we still have on disk. Once we've
    # fetched the bytes, the upstream signature is irrelevant for serving
    # them again. SWR window of 1 day lets the SW serve a stale copy
    # instantly while silently refreshing in the background.
    cache_control = "public, max-age=604800, stale-while-revalidate=86400, immutable"
    out_headers = {"Cache-Control": cache_control}
    # Stable hash exposed back to the browser so the frontend can pivot
    # to /img/by-hash/<h> on subsequent renders — same image, same key,
    # even after OF rotates the upstream signature.
    account_id_hdr = request.headers.get("x-account-id") or ""
    if account_id_hdr:
        try:
            h_stable = _img_hash_remember(account_id_hdr, u)
            out_headers["X-Img-Hash"] = h_stable
        except Exception:
            # Hashing should never fail (urlparse + sha1), but if it does
            # we just skip the header — the proxy still works.
            pass
    # Pass through the bits the <video> element relies on to seek. Without
    # Accept-Ranges Chrome won't even attempt a Range request; without
    # Content-Range it can't tell what slice it got back.
    for h in ("Content-Range", "Accept-Ranges", "Content-Length", "ETag", "Last-Modified"):
        v = r.headers.get(h)
        if v:
            out_headers[h] = v
    if "Accept-Ranges" not in out_headers:
        # If upstream didn't advertise but we got 206/200, hint that we
        # support ranges so the browser will try one next time.
        out_headers["Accept-Ranges"] = "bytes"

    # Wrap iter_content so a browser-side abort (the common case — user
    # hovers a new tile, the old <video> unmounts, the TCP socket dies)
    # cleanly closes the upstream connection instead of bubbling up as
    # an unhandled ChunkedEncodingError / ConnectionResetError that the
    # worker would otherwise log as a 500 and tear down the thread.
    # Also enforces a hard max-stream-age so a `/img` stream can't pin a
    # worker thread for the full lifetime of an mp4 — uvicorn's graceful
    # shutdown was taking 2+ minutes because in-flight streams blocked on
    # iter_content() and never noticed the shutdown signal. After
    # _MAX_STREAM_SEC we bail; the browser will reissue a Range request
    # for the rest, which is what `<video>` elements do natively anyway.
    _MAX_STREAM_SEC = 60
    import time as _time
    started_mono = _time.monotonic()

    # Tee bytes to the disk cache only when the upstream response is a
    # complete 200 (not 206), the request had no Range, and Content-Length
    # is either unknown or under the cap. We additionally enforce the cap
    # while streaming — past it, we drop the buffer rather than allocate
    # unbounded memory for a video that slipped past the host filter.
    out_headers["X-Img-Cache"] = "MISS" if not range_hdr else "BYPASS"
    cache_buffer: bytearray | None = None
    if not range_hdr and r.status_code == 200:
        try:
            advertised = int(r.headers.get("Content-Length") or 0)
        except ValueError:
            advertised = 0
        if 0 < advertised <= _IMG_CACHE_MAX_BYTES or advertised == 0:
            cache_buffer = bytearray()
        else:
            global _img_cache_skipped_too_big
            _img_cache_skipped_too_big += 1
    elif r.status_code == 206:
        global _img_cache_skipped_partial
        _img_cache_skipped_partial += 1

    def _safe_iter():
        global _img_active, _img_high_water, _img_total_started, _img_total_aborted
        global _img_cache_skipped_too_big
        _img_active += 1
        _img_total_started += 1
        if _img_active > _img_high_water:
            _img_high_water = _img_active
        aborted = False
        completed = False
        local_buf = cache_buffer
        try:
            for chunk in r.iter_content(chunk_size=64 * 1024):
                if _time.monotonic() - started_mono > _MAX_STREAM_SEC:
                    # Hit our cap — close upstream so the worker thread
                    # is reusable. The browser will Range-request the
                    # tail if it still wants more bytes.
                    break
                if chunk:
                    if local_buf is not None:
                        if len(local_buf) + len(chunk) > _IMG_CACHE_MAX_BYTES:
                            # Asset grew past the cap mid-stream — drop
                            # the buffer (don't cache), keep streaming.
                            _img_cache_skipped_too_big += 1
                            local_buf = None
                        else:
                            local_buf.extend(chunk)
                    yield chunk
            else:
                completed = True
        except (ConnectionResetError, BrokenPipeError, GeneratorExit):
            # Client went away — silent, this is normal browser behavior
            # for unmounting video elements.
            aborted = True
        except Exception:
            aborted = True
            log.warning("upstream stream error mid-flight (u=%s)", u[:120], exc_info=True)
        finally:
            if aborted:
                _img_total_aborted += 1
            _img_active = max(0, _img_active - 1)
            if sem_held:
                try: _video_stream_sem.release()
                except ValueError: pass
            try:
                r.close()
            except Exception:
                pass
            # Only persist when the full upstream body landed cleanly.
            # Aborted/truncated bodies must never enter the cache.
            if completed and not aborted and local_buf is not None and len(local_buf) > 0:
                try:
                    _img_cache_write(u, ct, bytes(local_buf))
                except Exception:
                    log.warning("img cache write failed (u=%s)", u[:120], exc_info=True)

    return StreamingResponse(
        _safe_iter(),
        media_type=ct,
        status_code=r.status_code,
        headers=out_headers,
    )


@app.get("/img/by-hash/{h}")
def proxy_image_by_hash(request: Request, h: str):
    """Stable-URL alias for /img. Resolves the hash to the most recent
    signed URL we've seen for it, then delegates to proxy_image. Returns
    410 if the signed URL has aged past _IMG_HASH_TTL_S — at that point
    the upstream signature is likely expired and the frontend should
    fetch a fresh URL from /messages (or whatever surface produced this
    asset originally) and call /img?u=... directly.

    The browser's Cache-Control window (2 days) is much longer than the
    upstream signature window (~1h), so most hits never reach the server
    at all — only first-sight requests touch us here."""
    with _img_hash_lock:
        entry = _img_hash_map.get(h)
    if not entry:
        raise HTTPException(
            status_code=404,
            detail="unknown img hash; refetch the producing endpoint for a fresh signed URL",
        )
    u, ts = entry
    if _time_mod.monotonic() - ts > _IMG_HASH_TTL_S:
        # Drop the stale entry while we're here.
        with _img_hash_lock:
            _img_hash_map.pop(h, None)
        raise HTTPException(
            status_code=410,
            detail="signed URL expired; refetch the producing endpoint for a fresh one",
        )
    return proxy_image(request, u=u)


@app.get("/admin/img-cache/disk")
def admin_img_cache_disk() -> dict:
    """Disk cache stats. `hit_rate` is the headline number — if it stays
    near zero, the cache isn't paying for itself; if it climbs past ~0.4
    we're saving meaningful upstream calls. `bytes` is a `du`-equivalent
    of the cache root so we can see growth over time."""
    total_bytes = 0
    entry_count = 0
    try:
        if _IMG_CACHE_DIR.exists():
            for sub in _IMG_CACHE_DIR.iterdir():
                if not sub.is_dir():
                    continue
                for f in sub.iterdir():
                    try:
                        st = f.stat()
                        total_bytes += st.st_size
                        if f.suffix == ".bin":
                            entry_count += 1
                    except OSError:
                        continue
    except OSError:
        pass
    total = _img_cache_hits + _img_cache_misses
    hit_rate = (_img_cache_hits / total) if total else 0.0
    return {
        "dir": str(_IMG_CACHE_DIR),
        "entries": entry_count,
        "bytes": total_bytes,
        "hits": _img_cache_hits,
        "misses": _img_cache_misses,
        "writes": _img_cache_writes,
        "hit_rate": round(hit_rate, 3),
        "skipped_partial": _img_cache_skipped_partial,
        "skipped_too_big": _img_cache_skipped_too_big,
        "write_errors": _img_cache_write_errors,
        "max_bytes_per_entry": _IMG_CACHE_MAX_BYTES,
        "ttl_seconds": _IMG_CACHE_TTL_S,
        # Wave 2.1 pinned-outbound-media stats.
        "pins_active": len(_pinned_media),
        "pinned_keys": len(_pinned_keys),
        "pins_added": _img_pins_added,
        "pins_fetched": _img_pins_fetched,
        "pins_skipped_existing": _img_pins_skipped_existing,
        "pins_fetch_errors": _img_pins_fetch_errors,
        "pin_warm_seconds": _IMG_PIN_WARM_S,
    }


@app.get("/admin/img-cache/stats")
def admin_img_cache_stats() -> dict:
    """Live view of the hash → signed-URL map. `entries` counts distinct
    images we've ever proxied since boot; `freshest_age_s` shows how
    recently we saw activity. Used to gauge whether /img/by-hash is
    actually doing work (entries climbing == hits expected to follow)."""
    now = _time_mod.monotonic()
    with _img_hash_lock:
        n = len(_img_hash_map)
        if n:
            ages = [now - ts for _, ts in _img_hash_map.values()]
            oldest_age = int(max(ages))
            freshest_age = int(min(ages))
        else:
            oldest_age = 0
            freshest_age = 0
    return {
        "entries": n,
        "cap": _IMG_HASH_CAP,
        "ttl_seconds": _IMG_HASH_TTL_S,
        "oldest_age_s": oldest_age,
        "freshest_age_s": freshest_age,
    }


@app.get("/admin/_debug/relay-cache-stats")
def admin_relay_cache_stats() -> dict[str, Any]:
    """Hit/miss counters + entry counts for the in-process relay_cache.

    Empty `namespaces` is expected until Stage C wires call-sites into
    `relay_cache.get_or_fetch(...)`. Useful afterwards for tuning TTLs:
    a namespace with 0 hits after a warm-up window is either keyed wrong
    (per-employee data cached account-wide?) or has every caller passing
    `?refresh=1`.
    """
    return relay_cache.stats()


# ── Video scrub storyboard ────────────────────────────────────────────
#
# /img/scrub?u=<signed-video-url>&i=<0..11>
#
# Replaces the streaming hover-preview pipeline (two <video> elements
# pulling Range bytes through /img) with 12 pre-extracted JPGs at evenly
# spaced timestamps (every 8.33%). First request for a video triggers
# a one-time ffmpeg extraction: download the mp4, extract 12 frames,
# write to disk, drop the mp4. Subsequent requests serve directly from
# disk and are immutable cacheable.
#
# Motivation: streaming videos through /img pinned uvicorn threadpool
# slots for up to 60s each. Five users hovering through video tiles
# could deadlock the relay. Storyboard frames are ~20KB JPGs served
# from disk — never blocks the upstream pool past the one-time build.

# Configurable cache root. /tmp is fine on macOS (cleared at boot, so
# eviction handles itself). In Docker /tmp is per-container and goes
# away on restart — point STORYBOARD_DIR at a mounted volume in prod
# so storyboards survive across container restarts.
_STORYBOARD_DIR = Path(os.environ.get("STORYBOARD_DIR", "/tmp/of-relay-storyboard"))
_STORYBOARD_FRAMES = 12
_STORYBOARD_WIDTH = 400
# Drop from 90s → 30s. Past 30s Next dev's HTTP proxy and most browsers
# have already given up; holding the uvicorn worker thread any longer
# just leaks the slot for nothing. The client retries naturally on the
# next hover cycle so failing fast is cheaper than queueing zombies.
_STORYBOARD_BUILD_TIMEOUT_S = 30
_storyboard_locks: dict[str, threading.Lock] = {}
_storyboard_locks_lock = threading.Lock()
_storyboard_total_built: int = 0
_storyboard_total_served: int = 0
_storyboard_build_failures: int = 0
# Per-hover-session cancel signal. Set by POST /img/scrub/cancel when the
# user moves off a video before its download finishes. The download loop
# polls this every chunk and bails IF it hasn't already crossed the
# "almost done" threshold below — past that point we let the bytes
# finish landing so the next hover gets a warm cache.
#
# Keyed by `session_id` (client-generated UUID per hover) rather than by
# video hash on purpose: a previous fix used hash-keyed events, which let
# a late cancel POST from a prior hover abort a brand-new build for the
# same video (re-hover within ~100-300ms). With session-keyed events,
# the new hover gets a new id → fresh event → the stale cancel POST
# finds no entry and is a no-op.
_storyboard_cancel_events: dict[str, threading.Event] = {}
_storyboard_cancel_events_lock = threading.Lock()
# If the cancel arrives after this fraction of the bytes has landed,
# we finish the download anyway — the remaining bandwidth is cheap and
# the cached storyboard will be there next time the user hovers this
# video. Lower = more cache built (more wasted bytes on truly-abandoned
# videos); higher = less wasted bandwidth (more re-download next time).
# 0.80 was the user's call after measuring the tradeoff.
_STORYBOARD_CANCEL_KEEP_THRESHOLD = 0.80
_storyboard_total_cancelled: int = 0
_storyboard_total_cancel_ignored: int = 0


def _storyboard_register_session(session_id: str) -> threading.Event:
    """Reserve a cancel event for an active build's session. Called once
    at the start of a download; the event is dropped at end of build."""
    with _storyboard_cancel_events_lock:
        ev = threading.Event()
        _storyboard_cancel_events[session_id] = ev
        return ev


def _storyboard_release_session(session_id: str) -> None:
    """Drop the session's cancel event after the build finishes (success
    or failure). Late cancel POSTs that arrive after this find no entry
    and become a no-op, which is the desired behaviour."""
    with _storyboard_cancel_events_lock:
        _storyboard_cancel_events.pop(session_id, None)


def _storyboard_signal_cancel(session_id: str) -> bool:
    """Set the cancel event for `session_id` if a build is active. Returns
    True when a live session was found, False otherwise (stale cancel)."""
    with _storyboard_cancel_events_lock:
        ev = _storyboard_cancel_events.get(session_id)
    if ev is None:
        return False
    ev.set()
    return True


def _storyboard_lock_for(h: str) -> threading.Lock:
    with _storyboard_locks_lock:
        lk = _storyboard_locks.get(h)
        if lk is None:
            lk = threading.Lock()
            _storyboard_locks[h] = lk
        return lk


def _storyboard_evict_locks_once() -> int:
    """Drop `_storyboard_locks` entries for hashes whose on-disk
    storyboard dir is gone — the prune-storyboards cron deletes those
    dirs on TTL, but the in-memory lock dict would otherwise grow
    without bound (≈200 bytes per unique video ever hovered).

    Safety: we only evict a lock when (a) the dir is missing AND (b)
    `lock.acquire(blocking=False)` succeeds, meaning no thread is
    currently building. We then re-check the dir under the lock and
    only pop from the dict if the same lock instance is still mapped.
    Returns the number of entries evicted (for the periodic logger)."""
    existing: set[str] = set()
    try:
        if _STORYBOARD_DIR.exists():
            existing = {p.name for p in _STORYBOARD_DIR.iterdir() if p.is_dir()}
    except OSError:
        return 0
    with _storyboard_locks_lock:
        candidates = [h for h in _storyboard_locks.keys() if h not in existing]
    evicted = 0
    for h in candidates:
        with _storyboard_locks_lock:
            lk = _storyboard_locks.get(h)
        if lk is None:
            continue
        if not lk.acquire(blocking=False):
            continue  # active build → leave it alone
        try:
            if (_STORYBOARD_DIR / h).is_dir():
                continue  # someone resurrected it between our snapshot and now
            with _storyboard_locks_lock:
                if _storyboard_locks.get(h) is lk:
                    _storyboard_locks.pop(h, None)
                    evicted += 1
        finally:
            lk.release()
    return evicted


async def _storyboard_evictor_loop() -> None:
    """Background task spawned at startup. Runs the lock evictor every
    30 minutes — matches the cadence of the prune-storyboards cron so
    the in-memory dict catches up shortly after the disk cleanup."""
    while True:
        try:
            await asyncio.sleep(30 * 60)
            evicted = await asyncio.to_thread(_storyboard_evict_locks_once)
            if evicted:
                log.info("storyboard locks evicted: %d", evicted)
        except asyncio.CancelledError:
            return
        except Exception:
            log.warning("storyboard evictor cycle failed", exc_info=True)


def _video_path_hash(u: str) -> str:
    """SHA-1 of host+path (signature stripped, account-agnostic). Same
    physical video → same hash → same on-disk frames, regardless of
    which account fetched it or how OF signed the URL today."""
    from urllib.parse import urlparse as _urlparse
    parsed = _urlparse(u)
    base = (parsed.hostname or "").lower() + (parsed.path or "")
    return hashlib.sha1(base.encode("utf-8")).hexdigest()[:20]


_ALL_FRAME_INDICES = tuple(range(_STORYBOARD_FRAMES))


def _storyboard_frame_path(h: str, i: int) -> Path:
    return _STORYBOARD_DIR / h / f"{i}.jpg"


def _storyboard_all_frames_present(h: str) -> bool:
    return all(_storyboard_frame_path(h, i).is_file() for i in _ALL_FRAME_INDICES)


def _storyboard_download(
    request: Request, u: str, src_path: Path, session_id: str | None,
) -> bool:
    """Pull the mp4 into src_path via the right account's HTTP client (so
    the URL's source-IP signature matches). Returns False on failure or
    when the client cancelled before 90% of the bytes had landed.

    The cancel hook lets us bail mid-download when the user moves on to
    another video. Past `_STORYBOARD_CANCEL_KEEP_THRESHOLD` we ignore
    the cancel and finish the bytes anyway — the next hover gets a
    warm cache and the bandwidth we already burned isn't wasted.

    `session_id` scopes the cancel to one hover session. If empty/None
    (old client, no sid supplied), the build runs without cancellation
    support — a missed cancel is strictly better than the alternative,
    which was aborting unrelated builds."""
    global _storyboard_build_failures, _storyboard_total_cancelled, _storyboard_total_cancel_ignored
    # Fresh, session-scoped event. Drop it on exit (finally) so a late
    # cancel POST arriving after the build wraps up is a no-op — not a
    # poison flag for the next session that happens to reuse this id.
    cancel_event = _storyboard_register_session(session_id) if session_id else None
    try:
        client = _get_client(request)
        r = client.http.get(u, timeout=client.timeout_s, stream=True)
        if r.status_code not in (200, 206):
            log.warning("storyboard download non-2xx %d for %s", r.status_code, u[:120])
            _storyboard_build_failures += 1
            return False
        total_bytes = 0
        try:
            total_bytes = int(r.headers.get("content-length") or 0)
        except (TypeError, ValueError):
            total_bytes = 0
        bytes_done = 0
        with src_path.open("wb") as f:
            for chunk in r.iter_content(chunk_size=256 * 1024):
                if cancel_event is not None and cancel_event.is_set():
                    # Past the keep threshold? Honour the bytes we already
                    # paid for and finish the file — bumping the ignored
                    # counter so we can see the cap is working.
                    fraction = (bytes_done / total_bytes) if total_bytes > 0 else 0.0
                    if total_bytes > 0 and fraction >= _STORYBOARD_CANCEL_KEEP_THRESHOLD:
                        _storyboard_total_cancel_ignored += 1
                        cancel_event.clear()  # treat the rest of the dl as committed
                    else:
                        # Truly abort — drop the partial mp4, signal failure.
                        _storyboard_total_cancelled += 1
                        try: r.close()
                        except Exception: pass
                        return False
                if chunk:
                    f.write(chunk)
                    bytes_done += len(chunk)
        try: r.close()
        except Exception: pass
        return True
    except Exception:
        log.warning("storyboard download failed for %s", u[:120], exc_info=True)
        _storyboard_build_failures += 1
        try: src_path.unlink(missing_ok=True)
        except Exception: pass
        return False
    finally:
        if session_id:
            _storyboard_release_session(session_id)


def _storyboard_probe_duration(src_path: Path) -> float:
    """ffprobe → duration in seconds, 0 on failure."""
    import subprocess
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "error",
             "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1",
             str(src_path)],
            capture_output=True, text=True, timeout=10, check=True,
        )
        return float(probe.stdout.strip())
    except Exception:
        return 0.0


def _storyboard_extract_batch(
    src_path: Path, dest: Path, indices: tuple[int, ...], dur: float,
) -> bool:
    """Extract all `indices` frames in a SINGLE ffmpeg invocation, using
    repeated `-ss TIME -i SRC -frames:v 1 OUT` blocks. ffmpeg reopens the
    file per `-i` (keyframe seek is fast on local disk) but the process
    spawn happens only once per batch — cuts pass-1 latency by ~4x for a
    4-frame batch vs. one subprocess per frame. Frames write directly to
    their final 0-based name.

    Tolerates partial success: if ffmpeg returns non-zero (common when
    the final frame's seek lands at/past EOF on short clips with
    rounded-up duration metadata) but at least one jpg landed on disk,
    we treat the batch as good. Returns False only if zero frames came
    out — that's the case where the source mp4 is unreadable or the
    timeout fired."""
    import subprocess
    # Stay at least 100ms before the encoded EOF — ffmpeg can't seek past
    # the last decodable frame, and short clips whose container metadata
    # rounds the duration up (e.g. "4.0s" for a 3.93s file) would otherwise
    # blow up on the last index.
    safe_dur = max(0.1, dur - 0.1)
    args: list[str] = ["ffmpeg", "-y", "-nostdin", "-loglevel", "error"]
    for pos, i in enumerate(indices):
        # Mid-segment seek (i + 0.5) lands away from cut boundaries where
        # an I-frame might be black or a hard transition.
        ts = (i + 0.5) * dur / _STORYBOARD_FRAMES
        ts = min(ts, safe_dur)
        out_path = dest / f"{i}.jpg"
        args += [
            "-ss", f"{ts:.4f}", "-i", str(src_path),
            "-map", f"{pos}:v:0",
            "-frames:v", "1",
            "-vf", f"scale={_STORYBOARD_WIDTH}:-2",
            "-q:v", "5",
            str(out_path),
        ]
    rc = -1
    try:
        proc = subprocess.run(
            args, check=False, capture_output=True,
            timeout=_STORYBOARD_BUILD_TIMEOUT_S,
        )
        rc = proc.returncode
    except Exception:
        log.warning(
            "storyboard batch extract crashed for %s indices=%s",
            dest.name, indices, exc_info=True,
        )

    landed = [i for i in indices if (dest / f"{i}.jpg").is_file()]
    if not landed:
        log.warning(
            "storyboard batch extract produced no frames for %s "
            "(rc=%s indices=%s dur=%.3f)",
            dest.name, rc, indices, dur,
        )
        return False
    if len(landed) < len(indices):
        # Backfill missing indices by copying the nearest neighbour so the
        # storyboard is always complete-shaped after a build. Without this
        # any missing frame would trigger a fresh download+extract on the
        # next request and fail the same way — an infinite loop.
        import shutil
        for i in indices:
            if (dest / f"{i}.jpg").is_file():
                continue
            nearest = min(landed, key=lambda j: abs(j - i))
            try:
                shutil.copyfile(dest / f"{nearest}.jpg", dest / f"{i}.jpg")
            except Exception:
                log.warning(
                    "storyboard backfill copy %s→%s failed for %s",
                    nearest, i, dest.name, exc_info=True,
                )
        log.info(
            "storyboard partial extract for %s: %d/%d frames (rc=%s dur=%.3f, backfilled rest)",
            dest.name, len(landed), len(indices), rc, dur,
        )
    return True


def _ensure_storyboard_frame(
    request: Request, u: str, h: str, i: int,
    *, hint_duration: float | None = None,
    session_id: str | None = None,
) -> bool:
    """Make sure frame `i` is on disk. Returns True if it is now.

    Single-pass build: any caller that finds the storyboard missing
    downloads the source mp4 and extracts all 12 frames in one batched
    ffmpeg invocation. We tried a two-phase (4-then-8) split but the
    measured breakdown showed ffmpeg is ~230ms total vs ~3-7s for the
    download — splitting saved nothing meaningful and added background-
    thread coordination.

    `hint_duration` (seconds) lets the caller skip ffprobe entirely —
    VaultMedia carries duration in the vault listing so the client
    passes it through. The worst case if it's wrong is mis-spaced
    scrub frames; we sanity-check it's positive."""
    global _storyboard_total_built, _storyboard_build_failures
    dest = _STORYBOARD_DIR / h
    frame_path = _storyboard_frame_path(h, i)

    if frame_path.is_file():
        return True

    lock = _storyboard_lock_for(h)
    if not lock.acquire(timeout=_STORYBOARD_BUILD_TIMEOUT_S):
        return False
    try:
        # Another concurrent caller may have produced our frame while we
        # waited on the lock.
        if frame_path.is_file():
            return True

        dest.mkdir(parents=True, exist_ok=True)
        src_path = dest / "source.tmp"

        # Download mp4 (skipped if a prior failed build left it around).
        if not src_path.is_file():
            if not _storyboard_download(request, u, src_path, session_id):
                # On cancel/error, drop the partial bytes — incomplete
                # mp4s aren't useful for extraction.
                try: src_path.unlink(missing_ok=True)
                except Exception: pass
                return False

        if hint_duration and hint_duration > 0:
            dur = hint_duration
        else:
            dur = _storyboard_probe_duration(src_path)
        if dur <= 0:
            _storyboard_build_failures += 1
            try: src_path.unlink(missing_ok=True)
            except Exception: pass
            return False

        if not _storyboard_extract_batch(src_path, dest, _ALL_FRAME_INDICES, dur):
            _storyboard_build_failures += 1
            # Drop the source mp4 on extract failure. The previous code
            # kept it around so a follow-up call could skip the download
            # and re-attempt extraction, but if the file is the cause of
            # the failure (corrupt download, codec ffmpeg can't decode)
            # leaving it on disk pins us into an infinite-retry loop on
            # this video — every subsequent hover hits the same broken
            # mp4 and fails the same way. Re-downloading is cheap
            # relative to a permanently-broken cache entry.
            try: src_path.unlink(missing_ok=True)
            except Exception: pass
            return False

        try: src_path.unlink(missing_ok=True)
        except Exception: pass
        _storyboard_total_built += 1
        return frame_path.is_file()
    finally:
        lock.release()


@app.get("/img/scrub")
def proxy_image_scrub(
    request: Request,
    u: str = Query(..., description="Signed OF video URL (.mp4)"),
    i: int = Query(..., ge=0, lt=_STORYBOARD_FRAMES, description="Frame index 0..11"),
    dur: float | None = Query(None, gt=0, description="Video duration in seconds (from VaultMedia.duration). When supplied, the relay skips its own ffprobe."),
    sid: str | None = Query(None, description="Per-hover session id. The matching /img/scrub/cancel POST carries the same value; the server scopes cancellation to this session so a stale cancel from a previous hover can't abort a fresh build."),
):
    """Returns the i-th frame of the 12-frame storyboard for video `u`.
    First call builds; subsequent calls serve from disk."""
    from urllib.parse import urlparse as _urlparse
    parsed = _urlparse(u)
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(status_code=400, detail="bad scheme")
    host = (parsed.hostname or "").lower()
    if not any(host.endswith(s) for s in _ALLOWED_CDN_SUFFIXES):
        raise HTTPException(status_code=400, detail="host not allowed")

    h = _video_path_hash(u)
    ok = _ensure_storyboard_frame(request, u, h, i, hint_duration=dur, session_id=sid)
    if not ok:
        raise HTTPException(status_code=502, detail="storyboard build failed")
    frame_path = _storyboard_frame_path(h, i)
    if not frame_path.is_file():
        raise HTTPException(status_code=404, detail=f"frame {i} not extracted")

    # Bump the dir's mtime so the prune script's find -mtime treats this
    # as "last used now". Cheap (one syscall) — does NOT touch each frame
    # file, just the parent dir, which is all the prune script looks at.
    try:
        os.utime(_STORYBOARD_DIR / h, None)
    except OSError:
        pass

    global _storyboard_total_served
    _storyboard_total_served += 1
    return FileResponse(
        frame_path,
        media_type="image/jpeg",
        headers={
            "Cache-Control": "public, max-age=604800, immutable",
            "X-Img-Hash": h,
        },
    )


@app.post("/img/scrub/cancel")
def cancel_storyboard_build(
    u: str = Query(..., description="Signed OF video URL whose in-flight storyboard build should be cancelled"),
    sid: str | None = Query(None, description="Per-hover session id from the matching /img/scrub request. Cancellation is scoped to this session — a missing or unknown sid is a no-op (intentional: stale cancels from a previous hover must not abort fresh builds for the same video)."),
):
    """Signal the in-flight download for hover session `sid` to abort,
    unless it's already past `_STORYBOARD_CANCEL_KEEP_THRESHOLD` (in
    which case we let it finish so the bytes aren't wasted). Idempotent —
    calling with a sid that has no in-flight build is a no-op.

    `u` is still validated for hygiene but isn't used to route the cancel
    anymore — that would re-introduce the hash-keyed race where a late
    POST from a stale hover aborted a fresh hover of the same video.

    Browser fires this on hover end (useEffect cleanup in VaultPicker).
    Fire-and-forget; the browser doesn't wait for or read the response."""
    from urllib.parse import urlparse as _urlparse
    parsed = _urlparse(u)
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(status_code=400, detail="bad scheme")
    host = (parsed.hostname or "").lower()
    if not any(host.endswith(s) for s in _ALLOWED_CDN_SUFFIXES):
        raise HTTPException(status_code=400, detail="host not allowed")
    if not sid:
        return {"ok": True, "ignored": "no-sid"}
    found = _storyboard_signal_cancel(sid)
    return {"ok": True, "found": found}


@app.get("/admin/coalesce/stats")
def admin_coalesce_stats() -> dict:
    """In-flight request coalescer activity (Stage B-2).

    `coalesced_hits` per namespace = waiters that joined an already-
    in-flight upstream call instead of dispatching a fresh one. A
    non-zero value means the coalescer is doing useful work in prod.
    `first_calls` = the leading caller for each namespace. Ratio
    `coalesced_hits / (first_calls + coalesced_hits)` is the hit rate."""
    return relay_coalesce.stats()


@app.get("/admin/storyboard/stats")
def admin_storyboard_stats() -> dict:
    """How the scrub-frame storyboard cache is doing."""
    try:
        n_videos = sum(1 for p in _STORYBOARD_DIR.iterdir() if p.is_dir()) if _STORYBOARD_DIR.exists() else 0
    except Exception:
        n_videos = 0
    return {
        "cached_videos": n_videos,
        "frames_per_video": _STORYBOARD_FRAMES,
        "total_built": _storyboard_total_built,
        "total_served": _storyboard_total_served,
        "build_failures": _storyboard_build_failures,
        "total_cancelled": _storyboard_total_cancelled,
        "total_cancel_ignored": _storyboard_total_cancel_ignored,
        "cancel_keep_threshold": _STORYBOARD_CANCEL_KEEP_THRESHOLD,
    }


@app.get("/api/of/v2/chats")
async def list_chats(
    limit: int = Query(10, ge=1, le=50),
    offset: int = Query(0, ge=0),
    order: str = Query("recent"),
    filter: str | None = Query(None, description="OF chat filter: 'unread'|'pinned'|'priority'"),
    list_id: str | None = Query(None, description="Show only chats whose fan is in this custom list (folder)"),
    query: str | None = Query(None, description="Free-text search by fan name/username (OF param: ?query=)"),
    # Legacy alias from the first cut of the chat-search UI — the real OF param
    # is `query=`. We accept both so any deep-linked URLs from earlier builds
    # don't silently 400.
    search: str | None = Query(None, description="Alias for `query` (back-compat)"),
    refresh: bool = Query(False, alias="refresh"),
) -> dict[str, Any]:
    # BC1: 10min TTL relay_cache, invalidated by api2_chat_message in
    # event_transcoder.py (see relay_cache.invalidate("list_chats", ...)).
    # ?refresh=1 bypasses the cache but still warms it for the next caller.
    aid = _resolve_account_id(_request_ctx.get())
    q = query or search
    coalesce_key = ("list_chats", aid, limit, offset, order, filter, list_id, q)
    async def _fetch():
        return await relay_coalesce.coalesce(coalesce_key, lambda: asyncio.to_thread(
            _proxy, lambda: _get_client().list_chats(
                limit=limit, offset=offset, order=order, filter=filter,
                list_id=list_id, query=q,
            ),
        ))
    return await relay_cache.get_or_fetch(
        "list_chats", aid, (limit, offset, order, filter, list_id, q),
        ttl_seconds=600.0, fetcher=_fetch, bypass=refresh,
    )


@app.get("/api/of/v2/chats/folders")
def chat_folders(
    limit: int = Query(10, ge=1, le=50),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    """Fan-lists the user has pinned (or can pin) to the chat sidebar.
    Mirrors OF's `/lists?filter=can_pin_chat&isChat=true&format=infinite`."""
    return _proxy(lambda: _get_client().chat_folders(limit=limit, offset=offset))


class _PinChatBody(BaseModel):
    pinned: bool


@app.patch("/api/of/v2/lists/{list_id}/pin-chat")
def pin_list_to_chat(list_id: int, body: _PinChatBody = Body(...)) -> dict[str, Any]:
    """Pin or unpin a fan list as a chat-sidebar folder.
    Maps to PATCH /lists/{id} with `{"isPinnedToChat": <bool>}`."""
    return _proxy(lambda: _get_client().set_list_pinned_to_chat(list_id, body.pinned))


@app.get("/api/of/v2/chats/{chat_id}/messages")
async def get_messages(
    chat_id: int,
    limit: int = Query(10, ge=1, le=100),
    order: str = Query("desc"),
    before_id: int | None = Query(None, description="Paginate older: pass last message id"),
    refresh: bool = Query(False, alias="refresh"),
) -> dict[str, Any]:
    # BC2: cache ONLY paginated history (before_id non-None). Head page
    # (before_id=None) is where new messages land — we have no
    # per-message WS invalidator, so head MUST go through to OF every
    # time. Coalescing still applies on both paths.
    aid = _resolve_account_id(_request_ctx.get())
    coalesce_key = ("get_messages", aid, chat_id, before_id, limit, order)
    async def _fetch():
        return await relay_coalesce.coalesce(coalesce_key, lambda: asyncio.to_thread(
            _proxy, lambda: _get_client().get_messages(
                chat_id, limit=limit, order=order, before_id=before_id,
            ),
        ))
    if before_id is None:
        # Head page: skip cache entirely. New inbound DMs arrive via the
        # WS pump + SSE; caching the head would mask them for ~TTL.
        payload = await _fetch()
    else:
        # Paginated tail: history is immutable, 10min TTL bounds memory.
        payload = await relay_cache.get_or_fetch(
            "get_messages_tail", aid, (chat_id, before_id, limit),
            ttl_seconds=600.0, fetcher=_fetch, bypass=refresh,
        )
    # Media render-stability (18_chat_render_stability §1.1): persist any OF
    # files/info width+height into message_media (the primary dims source)
    # and surface width/height at each media item's top level so the client
    # sizes the skeleton without parsing OF's nested files. Best-effort —
    # a media hiccup must never break loading the chat. Idempotent, so a
    # cache hit re-running it is harmless.
    if isinstance(payload, dict) and aid:
        try:
            await sync_rest_media_dims(str(aid), int(chat_id), payload.get("list") or [])
        except Exception:  # noqa: BLE001
            log.debug("sync_rest_media_dims failed for chat %s", chat_id, exc_info=True)
    return payload


@app.get("/api/of/v2/chats/{chat_id}/messages/all")
def get_all_messages(
    chat_id: int,
    page_size: int = Query(10, ge=1, le=10),  # OF caps the page at ~10; don't go higher
    delay_ms: int = Query(300, ge=0, le=5000),
    max_pages: int | None = Query(None, ge=1, le=1000),
    from_id: int | None = Query(None, description="Resume cursor: only fetch messages older than this id"),
) -> dict[str, Any]:
    """Blocking: paginates the full chat history server-side, returns one JSON.
    Fine for small/medium chats; large chats can take minutes — prefer /stream."""
    msgs = _proxy(lambda: _get_client().get_all_messages(
        chat_id, page_size=page_size, delay_s=delay_ms / 1000, max_pages=max_pages,
        from_id=from_id,
    ))
    return {"count": len(msgs), "list": msgs}


@app.get("/api/of/v2/chats/{chat_id}/messages/stream")
def stream_all_messages(
    chat_id: int,
    page_size: int = Query(10, ge=1, le=10),  # OF caps the page at ~10; don't go higher
    delay_ms: int = Query(300, ge=0, le=5000),
    max_pages: int | None = Query(None, ge=1, le=1000),
    from_id: int | None = Query(None, description="Resume cursor: only stream messages older than this id"),
):
    """NDJSON stream: one JSON object per page, flushed as it's fetched.
    Each line: {"page": N, "count": k, "hasMore": bool, "messages": [...]}.
    Lets the frontend render as messages arrive instead of waiting for the
    whole chat. Errors come through as a final {"error": "..."} line."""
    client = _get_client()

    def gen():
        try:
            for page in client.iter_messages(
                chat_id, page_size=page_size,
                delay_s=delay_ms / 1000, max_pages=max_pages,
                from_id=from_id,
            ):
                yield json.dumps({
                    "page": page["page"],
                    "count": len(page["messages"]),
                    "hasMore": page["hasMore"],
                    "messages": page["messages"],
                }) + "\n"
        except OFAPIError as e:
            r = e.response
            yield json.dumps({
                "error": "upstream",
                "upstream_status": r.status_code if r else None,
                "upstream_body": (r.text[:1000] if r else str(e)),
            }) + "\n"

    return StreamingResponse(gen(), media_type="application/x-ndjson")


# ── Users ──────────────────────────────────────────────────────
# Order matters: the static /users/list route must be declared BEFORE the
# dynamic /users/{...} route, otherwise FastAPI would match "list" as a
# username and never reach the batch endpoint.

@app.get("/api/of/v2/users/list")
async def list_users(
    ids: list[int] = Query(..., description="One or more user ids: ?ids=1&ids=2"),
    view: str = Query("m", pattern="^[mx]$",
                      description="'m' = chat-list view (default), 'x' = extended"),
) -> dict[str, Any]:
    """Batch-fetch up to ~50 user profiles in one request. Returns OF's native
    shape: a dict keyed by user-id string, e.g. {"117183": {...}}.

    Quirk: when OF can't resolve ANY of the requested ids (deleted accounts,
    typos, etc.) it returns `[]` instead of `{}`. Normalize to dict so the
    response validator stays happy and callers don't have to type-guard.

    Augments each entry with `customNickname` from our local Fan table when
    one is set. Read-only join — never creates Fan stub rows. Lets the chat
    list / group panes / popouts all surface team-set nicknames without a
    separate fetch.

    The OF httpx call is blocking; we run it via `asyncio.to_thread` so the
    event loop stays responsive while the upstream request is in flight.
    Direct `_proxy(...)` from this `async def` would freeze SSE, webhooks,
    and concurrent requests for the duration of the upstream call."""
    aid = _resolve_account_id(_request_ctx.get())
    # IDs must be sorted in the key — two requests with [1,2,3] vs [3,2,1]
    # should coalesce. The OF response is keyed by user-id string anyway,
    # so per-waiter slicing is order-insensitive.
    key = ("list_users", aid, view, tuple(sorted(ids)))

    async def _fetch():
        resp = await asyncio.to_thread(
            _proxy, lambda: _get_client().list_users(ids, view=view)
        )
        if isinstance(resp, list):
            resp = {}
        # Overlay runs INSIDE the fetcher so coalesced waiters see the
        # same already-stitched dict (Stage B-2 bug guard #3 — moves
        # mutation inside the one shared execution).
        try:
            await _sync_of_user_overlay(aid, resp)
        except Exception:
            log.exception("custom_nickname stitch failed for /users/list")
        return resp

    return await relay_coalesce.coalesce(key, _fetch)


async def _sync_of_user_overlay(
    account_id: str, users: dict[str, Any]
) -> None:
    """Reconcile OF's per-fan `displayName`/`notice` with our local mirror
    (`Fan.custom_nickname` / `Fan.notes`), in both directions:

      • OF → local: whenever a chatter edits the values directly on
        onlyfans.com, the next OF refetch flips our local row to match.
        OF wins; this is the read-back leg of the bidirectional sync.

      • local → response: stitches the (now-fresh) `customNickname` into
        each entry of the dict so legacy consumers reading that field
        get the same value the chat-list rail/group pane already pick.

    `users` is mutated in place. Empty string from OF means "cleared",
    normalised to NULL locally. Missing local rows are skipped here —
    /users/list already auto-creates fan stubs via the WS pump, and we
    don't want this read path to create rows for arbitrary id lookups."""
    if not users:
        return
    fan_ids: list[int] = []
    for key in users.keys():
        try:
            fan_ids.append(int(key))
        except (TypeError, ValueError):
            continue
    if not fan_ids:
        return

    from db.engine import get_session
    from db.models import Fan
    from sqlalchemy import select as _select, update as _update

    async with get_session() as s:
        rows = (await s.execute(
            _select(Fan.fan_id, Fan.custom_nickname, Fan.notes)
            .where(Fan.account_id == account_id, Fan.fan_id.in_(fan_ids))
        )).all()
        local_by_id: dict[int, tuple[str | None, str | None]] = {
            int(fid): (nick, notes) for fid, nick, notes in rows
        }

        dirty = False
        for key, entry in users.items():
            if not isinstance(entry, dict):
                continue
            try:
                fid = int(key)
            except (TypeError, ValueError):
                continue
            of_display = entry.get("displayName")
            of_notice = entry.get("notice")
            local = local_by_id.get(fid)
            if local is None:
                continue
            local_nick, local_notes = local
            norm_display = of_display if of_display else None
            norm_notice = of_notice if of_notice else None
            patch: dict[str, Any] = {}
            if of_display is not None and norm_display != local_nick:
                patch["custom_nickname"] = norm_display
            if of_notice is not None and norm_notice != local_notes:
                patch["notes"] = norm_notice
            if patch:
                patch["updated_at"] = datetime.utcnow()
                await s.execute(
                    _update(Fan)
                    .where(Fan.account_id == account_id, Fan.fan_id == fid)
                    .values(**patch)
                )
                dirty = True
                local_by_id[fid] = (
                    patch.get("custom_nickname", local_nick),
                    patch.get("notes", local_notes),
                )
        if dirty:
            await s.commit()

        for fid, (nick, _notes) in local_by_id.items():
            if not nick:
                continue
            entry = users.get(str(fid))
            if isinstance(entry, dict):
                entry["customNickname"] = nick


# NOTE: /users/{user_id_or_username} is declared at the very end of the user-family
# routes (see "Catch-all single user lookup" further down) so static routes like
# /users/list, /users/search, /users/{id}/posts can match first.


# ── Send a message ─────────────────────────────────────────────

class SendMessageBody(BaseModel):
    text: str = Field(..., description="Message body. Plain text or HTML.")
    locked_text: bool = Field(False, description="Lock the text behind the PPV price.")
    price: float = Field(0, ge=0, description="PPV price; 0 = free message.")
    # Either: ints (existing vault ids) or dicts (fresh-upload claim objects
    # like {processId, host, name, extra}). OF accepts both in mediaFiles.
    media_files: list[int | dict] = Field(default_factory=list, description="Vault media ids OR fresh-upload claim objects {processId,host,name,extra}")
    # Vault media ids that ride along UNLOCKED (free preview) when `price > 0`.
    # Must be a subset of `media_files`'s numeric ids; fresh-claim dicts can't
    # appear here because they have no id until OF resolves them.
    previews: list[int] = Field(default_factory=list, description="Subset of media_files (by vault id) sent unlocked when price > 0")
    is_forward: bool = False
    reply_to_message_id: int | None = Field(
        None,
        description="OF native quote-reply target id — receiver renders the quoted message above this one.",
    )
    # Numeric OF user ids of creators to tag in this message. Picker UI
    # populates these from GET /api/of/v2/self/tagged-friend-users; OF
    # rejects ids that aren't on the caller's tagged-friend list with 400.
    tagged_users: list[int] = Field(
        default_factory=list,
        description="OF user ids of creators to tag (sent as `userTags`).",
    )
    # Giphy GIF id (e.g. "0ndspgUFbm9iQtyX2Q"). Forwarded as top-level
    # `giphyId` in the OF body — sibling of `mediaFiles`, not nested in it.
    # OF accepts one GIF per send and echoes it back on the response.
    giphy_id: str | None = Field(
        None,
        description="Giphy GIF id (string). Sent as top-level `giphyId`; one per message.",
    )


# Mass-message route MUST come before /chats/{chat_id}/messages below, otherwise
# FastAPI matches `chat_id="messages"` and 422s on int validation.
#
# `_MassMessageBody` MUST be defined here, ABOVE the route that annotates it.
# With `from __future__ import annotations` the body annotation is a string, so
# FastAPI/Pydantic build a deferred TypeAdapter from a ForwardRef at decoration
# time — if the class is defined later in the file nothing ever resolves it, and
# `/openapi.json` blows up with "is not fully defined" (PydanticUserError).
class _MassMessageBody(BaseModel):
    text: str
    user_lists: list[int] = Field(default_factory=list)
    included_users: list[int] = Field(default_factory=list)
    excluded_users: list[int] = Field(default_factory=list)
    price: float = Field(0, ge=0)
    locked_text: bool = False
    # Mirror SendMessageBody: int = vault id, dict = fresh-upload claim
    # (the same payload shape /upload returns in `send_with`).
    media_files: list[int | dict] = Field(default_factory=list)
    # Plumbed end-to-end in Work Unit O (Phase 6). Accepted here so a
    # forward-compat client can already send it without 422; the value is
    # currently ignored downstream because `of_client.send_mass_message`
    # doesn't yet thread it to OF.
    previews: list[int] = Field(default_factory=list)
    scheduled_date: str | None = None


async def _open_mass_run(
    request: Request,
    *,
    user_lists: list,
    included_users: list[int],
    excluded_users: list[int],
    funnel_id: int | None = None,
) -> tuple[str, int | None, int | None]:
    """Resolve account + X-Employee-Id and mint a `mass_runs` row so every
    per-fan outbound row downstream can reference it. Returns
    `(account_id, employee_id, mass_run_id)` — `mass_run_id` may be None if
    the mint failed (the broadcast still proceeds; attribution degrades to
    NULL, same fallback as the single-fan path)."""
    account_id = _resolve_account_id(request)
    # Resolves header (owner) OR chatter mirror Employee under the
    # account's owner. Chatters never carry X-Employee-Id and the header
    # is the only way the old path knew "who sent this"; without this
    # branch every chatter send showed up as Automation in the per-
    # employee stats table.
    from employees import resolve_outbound_employee_id
    employee_id = await resolve_outbound_employee_id(request, account_id)

    audience_filter = json.dumps({
        "user_lists": user_lists,
        "included_users": included_users,
        "excluded_users": excluded_users,
    })
    mass_run_id: int | None = None
    try:
        from db.engine import get_session
        from db.models import MassRun
        async with get_session() as s:
            run = MassRun(
                account_id=str(account_id),
                funnel_id=int(funnel_id) if funnel_id is not None else None,
                started_by_employee_id=employee_id,
                audience_filter=audience_filter,
                recipient_count=len(included_users),
                status="running",
            )
            s.add(run)
            await s.flush()
            mass_run_id = int(run.id)
    except Exception:
        log.exception("mass_runs mint failed (account=%s)", account_id)
    return account_id, employee_id, mass_run_id


async def _close_mass_run(
    *,
    account_id: str,
    employee_id: int | None,
    mass_run_id: int | None,
    text: str,
    price: float,
    result: Any,
    recipient_ids: list[int] | None = None,
) -> None:
    """After OF returns, persist a `messages` row per recipient of the
    broadcast. Two sources, handled in order:

      1. **Echoed ids.** OF's `/messages/queue` shape varies — some
         installations echo `{messages: [{userId, id, ...}]}`. For each
         echoed pair we write the REAL row (`write_outbound_attribution`) and
         drop any optimistic placeholder for that recipient.
      2. **Optimistic rows.** For the explicit recipients OF did NOT echo
         (`recipient_ids`, i.e. the `userIds` audience), we write an optimistic
         placeholder row at send time — direction='out', `mass_run_id` set,
         a synthetic `message_id` + `temp_id` — so the broadcast shows up in
         the chat cache immediately instead of never. The WS pump skips
         outbound events, so without this the row would otherwise never land.

    List-based audiences (`userLists`) aren't expanded here, so their members
    aren't in `recipient_ids`; they still rely on the (future) WS-pump
    reconciler keyed on `mass_run_id`. The `mass_runs` row is already minted
    as that reconciler's anchor."""
    if mass_run_id is None:
        return
    from attribution import (
        reconcile_mass_placeholder,
        write_mass_optimistic_rows,
        write_outbound_attribution,
    )
    from event_transcoder import _parse_iso, _to_cents

    created_at = (
        (_parse_iso(result.get("createdAt")) if isinstance(result, dict) else None)
        or datetime.utcnow()
    )
    price_cents = _to_cents(price)

    # ── 1) Real rows for any per-fan ids OF echoed in the response ──────
    msgs = result.get("messages") if isinstance(result, dict) else None
    per_fan_ids: list[tuple[int, int]] = []
    if isinstance(msgs, list):
        for m in msgs:
            if not isinstance(m, dict):
                continue
            fid_raw = m.get("userId") or m.get("toUserId") or m.get("recipientId")
            mid_raw = m.get("id") or m.get("messageId")
            try:
                if fid_raw is not None and mid_raw is not None:
                    per_fan_ids.append((int(fid_raw), int(mid_raw)))
            except (TypeError, ValueError):
                continue

    echoed_fans: set[int] = set()
    for fan_id, msg_id in per_fan_ids:
        echoed_fans.add(fan_id)
        try:
            await write_outbound_attribution(
                account_id=account_id,
                fan_id=fan_id,
                message_id=msg_id,
                sent_by_employee_id=employee_id,
                body=text or "",
                price_cents=price_cents,
                created_at=created_at,
                mass_run_id=mass_run_id,
            )
            # If an optimistic placeholder for this recipient already landed
            # (e.g. a retry, or a future async reconciler), drop it so the
            # real row is the only one left.
            await reconcile_mass_placeholder(
                account_id=account_id, fan_id=fan_id, mass_run_id=mass_run_id,
            )
        except Exception:
            log.exception(
                "mass attribution write failed (account=%s run=%s fan=%s)",
                account_id, mass_run_id, fan_id,
            )

    # ── 2) Optimistic placeholders for the explicit recipients not echoed ─
    pending: list[int] = []
    for raw in (recipient_ids or []):
        try:
            fid = int(raw)
        except (TypeError, ValueError):
            continue
        if fid not in echoed_fans:
            pending.append(fid)
    if pending:
        await write_mass_optimistic_rows(
            account_id=account_id,
            fan_ids=pending,
            mass_run_id=mass_run_id,
            sent_by_employee_id=employee_id,
            body=text or "",
            price_cents=price_cents,
            created_at=created_at,
        )

    # ── 3) Close the run. reply_mass_funnel only anchors on status='ok'
    #      runs, and the Mass Messages tab joins the broadcast cache on
    #      queue_id — mirror send_mass_message's close step.
    queue_id = result.get("id") if isinstance(result, dict) else None
    try:
        from db.engine import get_session
        from db.models import MassRun
        async with get_session() as s:
            mr = await s.get(MassRun, mass_run_id)
            if mr is not None:
                mr.status = "ok"
                mr.completed_at = datetime.utcnow()
                if queue_id is not None:
                    mr.queue_id = int(queue_id)
    except Exception:
        log.exception("mass run close failed (account=%s run=%s)", account_id, mass_run_id)


@app.post("/api/of/v2/chats/messages")
async def of_send_mass(
    request: Request,
    body: _MassMessageBody = Body(...),
) -> dict[str, Any]:
    """Broadcast a message to many fans.

    Mints a `mass_runs` row up front (capturing account_id + X-Employee-Id +
    audience filter) so per-fan outbound rows written below all reference
    it. After OF returns, per-fan attribution rows are written here — the
    WS pump intentionally skips outbound chat_message events, so this is
    the single producer of outbound persistence for broadcasts."""
    account_id, employee_id, mass_run_id = await _open_mass_run(
        request,
        user_lists=body.user_lists,
        included_users=body.included_users,
        excluded_users=body.excluded_users,
    )
    result = await asyncio.to_thread(
        _proxy,
        lambda: _get_client().send_mass_message(
            text=body.text,
            user_lists=body.user_lists,
            included_users=body.included_users,
            excluded_users=body.excluded_users,
            price=body.price,
            locked_text=body.locked_text,
            media_files=body.media_files,
            previews=body.previews,
            scheduled_date=body.scheduled_date,
        ),
    )
    await _close_mass_run(
        account_id=account_id,
        employee_id=employee_id,
        mass_run_id=mass_run_id,
        text=body.text,
        price=body.price,
        result=result,
        recipient_ids=body.included_users,
    )
    if mass_run_id is not None and isinstance(result, dict):
        result.setdefault("_mass_run_id", mass_run_id)
    return result

@app.post("/api/of/v2/chats/{chat_id}/messages")
async def send_message(
    request: Request,
    chat_id: int,
    body: SendMessageBody = Body(...),
) -> dict[str, Any]:
    """Send a message to a fan. `chat_id` is the fan's user id (same id used
    everywhere else in this API).

    After OF returns 200, immediately writes a `messages` row stamped with
    `sent_by_employee_id` from the `X-Employee-Id` header (or the system
    Automation employee when the header is absent). The transcoder skips
    outbound chat_message events on purpose; this is the single producer
    of outbound rows."""
    # Extract context BEFORE _proxy — its lambda has no request scope.
    try:
        account_id = _resolve_account_id(request)
    except HTTPException:
        # _resolve_account_id already shaped the error; let it propagate.
        raise
    # Same chatter-aware resolution as the mass-message path above. For
    # owner sessions this reads X-Employee-Id; for chatter sessions it
    # looks up (chatter, account_owner) → Employee, materialising the
    # mirror row on first hit so the message bubble's "Sent by" label
    # reads the chatter's display name instead of "Automation."
    from employees import resolve_outbound_employee_id
    employee_id = await resolve_outbound_employee_id(request, account_id)

    # Bridge sync _proxy from this async handler — copied from the
    # /users/list pattern at server.py around line 2388.
    result = await asyncio.to_thread(
        _proxy,
        lambda: _get_client().send_message(
            chat_id,
            text=body.text,
            locked_text=body.locked_text,
            price=body.price,
            media_files=body.media_files,
            previews=body.previews,
            is_forward=body.is_forward,
            reply_to_message_id=body.reply_to_message_id,
            tagged_users=body.tagged_users,
            giphy_id=body.giphy_id,
        ),
    )

    # Best-effort attribution write. Never raise — the OF send already
    # succeeded and the frontend reconcile depends on the response.
    try:
        from attribution import write_outbound_attribution
        from event_transcoder import _parse_iso, _to_cents
        msg_id = result.get("id") if isinstance(result, dict) else None
        if msg_id:
            await write_outbound_attribution(
                account_id=account_id,
                fan_id=int(chat_id),
                message_id=int(msg_id),
                sent_by_employee_id=employee_id,
                body=str(result.get("text") or ""),
                price_cents=_to_cents(body.price),
                created_at=_parse_iso(result.get("createdAt")) or datetime.utcnow(),
            )
    except Exception:
        log.exception("attribution write failed (chat_id=%s)", chat_id)

    # Wave 2.1 — pin the just-sent image bytes in the on-disk /img cache so
    # the bubble keeps serving a local copy after OF reconciles, instead of
    # racing OF's cold CDN edge. Fire-and-forget; never blocks this response.
    try:
        _pin_sent_media(account_id, result)
    except Exception:
        log.debug("pin sent media dispatch failed (chat_id=%s)", chat_id, exc_info=True)

    return result


# ── Dashboard / init ───────────────────────────────────────────

@app.get("/api/of/v2/init")
async def of_init(
    refresh: bool = Query(False, alias="refresh"),
) -> dict[str, Any]:
    """Bootstrap payload OF loads on every page (notification counts, configs, feature flags).

    BC3: 5min TTL relay_cache, invalidated by api2_chat_message /
    paidMessage / messageUnlock / purchase in event_transcoder.py.
    """
    aid = _resolve_account_id(_request_ctx.get())
    coalesce_key = ("init", aid)
    async def _fetch():
        return await relay_coalesce.coalesce(coalesce_key, lambda: asyncio.to_thread(
            _proxy, lambda: _get_client().init(),
        ))
    return await relay_cache.get_or_fetch(
        "init", aid, (),
        ttl_seconds=300.0, fetcher=_fetch, bypass=refresh,
    )

@app.get("/api/of/v2/labels")
async def of_labels() -> dict[str, Any]:
    """Fan labels (colored tags)."""
    aid = _resolve_account_id(_request_ctx.get())
    key = ("labels", aid)
    async def _fetch():
        return await asyncio.to_thread(_proxy, lambda: _get_client().labels())
    return await relay_coalesce.coalesce(key, _fetch)

@app.get("/api/of/v2/users/notifications")
def of_notifications(
    limit: int = Query(10, ge=1, le=50),
    offset: int = Query(0, ge=0),
    type: str | None = Query(None, description="'all','tips','subscribes','comments','mentions'"),
):
    """Notifications feed. Captured-correct path is /users/notifications (not /notifications)."""
    return _proxy(lambda: _get_client().notifications(limit=limit, offset=offset, type=type))

@app.get("/api/of/v2/users/notifications/count")
async def of_notifications_count(
    refresh: bool = Query(False, alias="refresh"),
):
    """Per-category badge counts (all, subscribed, purchases, tip).

    BC4: 30s TTL relay_cache (kept short — this is a badge the user is
    staring at). Invalidated by the same event set as BC3.
    """
    aid = _resolve_account_id(_request_ctx.get())
    coalesce_key = ("notifications_count", aid)
    async def _fetch():
        return await relay_coalesce.coalesce(coalesce_key, lambda: asyncio.to_thread(
            _proxy, lambda: _get_client().notifications_count(),
        ))
    return await relay_cache.get_or_fetch(
        "notifications_count", aid, (),
        ttl_seconds=30.0, fetcher=_fetch, bypass=refresh,
    )

@app.get("/api/of/v2/users/notifications/settings/tabs-order")
def of_notification_tabs_order():
    """Saved order of notification tabs in OF UI."""
    return _proxy(lambda: _get_client().notification_tabs_order())

@app.get("/api/of/v2/users/me/settings")
async def of_my_settings(
    refresh: bool = Query(False, alias="refresh"),
):
    """Full settings dump (account, payments, streaming keys, etc.).

    BC5: 5min TTL relay_cache. Invalidated explicitly when our own write
    endpoints touch settings (PATCH /users/me, PATCH
    /users/me/reply-on-subscribe) — those handlers call
    `relay_cache.invalidate("my_settings", aid)` after a successful upstream
    write.

    NOT WS-event-driven: a chatter editing settings on onlyfans.com (outside
    this app) won't trigger invalidation. **5 minutes is the upper bound on
    staleness** in that case; callers can force a fresh fetch via ?refresh=1.
    """
    aid = _resolve_account_id(_request_ctx.get())
    coalesce_key = ("my_settings", aid)
    async def _fetch():
        return await relay_coalesce.coalesce(coalesce_key, lambda: asyncio.to_thread(
            _proxy, lambda: _get_client().my_settings(),
        ))
    return await relay_cache.get_or_fetch(
        "my_settings", aid, (),
        ttl_seconds=300.0, fetcher=_fetch, bypass=refresh,
    )

@app.get("/api/of/v2/users/hints")
def of_hints():
    """UI hints (onboarding banners)."""
    return _proxy(lambda: _get_client().hints())

@app.get("/api/of/v2/stories/map")
def of_stories_map():
    """Story geo-analytics map data."""
    return _proxy(lambda: _get_client().stories_map())

@app.get("/api/of/v2/streams/feed")
def of_streams_feed():
    """Live streams feed."""
    return _proxy(lambda: _get_client().streams_feed())

@app.get("/api/of/v2/streams/reminder")
def of_streams_reminders():
    """My stream reminders."""
    return _proxy(lambda: _get_client().streams_reminders())

@app.get("/api/of/v2/payouts/requests")
def of_payout_requests(limit: int = Query(10, ge=1, le=50), offset: int = Query(0, ge=0)):
    """Payout request history (Statements > Payout requests tab)."""
    return _proxy(lambda: _get_client().payout_requests(limit=limit, offset=offset))

@app.get("/api/of/v2/subscriptions/count/all")
def of_subscription_counts_all():
    """Full subscription breakdown (active/expired/blocked/etc) + subscribers + bookmarks."""
    return _proxy(lambda: _get_client().subscription_counts_all())

@app.get("/api/of/v2/users/promotions")
def of_my_promotions():
    """My outbound promotions (alt path of /promotions)."""
    return _proxy(lambda: _get_client().my_promotions())

@app.get("/api/of/v2/vault/media/types")
def of_vault_media_types():
    """Allowed vault media MIME types."""
    return _proxy(lambda: _get_client().vault_media_types())

@app.get("/api/of/v2/vault/media/processing")
def of_vault_media_processing():
    """Uploads currently being processed."""
    return _proxy(lambda: _get_client().vault_media_processing())

@app.get("/api/of/v2/users/posts/on-this-day")
def of_posts_on_this_day():
    """Memories: my old posts from this date in past years."""
    return _proxy(lambda: _get_client().posts_on_this_day())


# ── Users / search ─────────────────────────────────────────────

@app.get("/api/of/v2/users/search")
def of_search_users(q: str = Query(..., min_length=1), limit: int = Query(10, ge=1, le=50)):
    """Search creators by name/username (OF's `/users?q=` under the hood)."""
    return _proxy(lambda: _get_client().search_users(q, limit=limit))

@app.get("/api/of/v2/posts/tagged-friend-users")
async def of_tagged_friend_users(
    search: str | None = Query(None, max_length=100),
    limit: int = Query(20, ge=1, le=50),
    offset: int = Query(0, ge=0),
    filter: str = Query("all"),
    sort: str = Query("date:desc"),
    skip_users: str = Query("all"),
    refresh: bool = Query(False, alias="refresh"),
):
    """Creators eligible to be tagged in posts AND chat messages — same
    endpoint OF web uses for the chat composer's @-tag picker. Search is
    forwarded as `name=` to OF (captured live 2026-05-23).

    BC6: 5min TTL relay_cache, exact-tuple key (NO prefix-collapse). A
    user typing "abc" then "abcd" intentionally produces two cache
    entries — OF's response shape differs per substring.
    """
    aid = _resolve_account_id(_request_ctx.get())
    coalesce_key = ("tagged_friend_users", aid, search, limit, offset, filter, sort, skip_users)
    async def _fetch():
        return await relay_coalesce.coalesce(coalesce_key, lambda: asyncio.to_thread(
            _proxy, lambda: _get_client().tagged_friend_users(
                search=search, limit=limit, offset=offset,
                filter=filter, sort=sort, skip_users=skip_users,
            ),
        ))
    return await relay_cache.get_or_fetch(
        "tagged_friend_users", aid,
        (search, limit, offset, filter, sort, skip_users),
        ttl_seconds=300.0, fetcher=_fetch, bypass=refresh,
    )

@app.get("/api/of/v2/users/{user_id}/posts")
def of_user_posts(user_id: str, limit: int = Query(10, ge=1, le=50),
                  type: str | None = Query(None, description="'photo'|'video'|'audio'")):
    """Posts owned by another user (creator). `user_id` accepts numeric id or 'me'.
    `type` filters by media."""
    return _proxy(lambda: _get_client().user_posts(user_id, limit=limit, type=type))

# /posts/bookmarks declared earlier above (before /posts/{post_id}) so static
# path wins over the dynamic one.

@app.get("/api/of/v2/users/{user_id}/stories")
def of_user_stories(user_id: str):
    """Active stories for a creator. `user_id` accepts numeric id or 'me'."""
    return _proxy(lambda: _get_client().user_stories(user_id))

@app.get("/api/of/v2/users/{user_id}/stories/highlights")
def of_user_highlights(user_id: str):
    """Highlights for a creator. `user_id` accepts numeric id or 'me'."""
    return _proxy(lambda: _get_client().user_highlights(user_id))

@app.get("/api/of/v2/users/{user_id}/posts/photos")
def of_user_photos(user_id: str, limit: int = Query(10, ge=1, le=50), offset: int = Query(0, ge=0)):
    """Photo posts by a creator."""
    return _proxy(lambda: _get_client().user_photos(user_id, limit=limit, offset=offset))

@app.get("/api/of/v2/users/{user_id}/posts/videos")
def of_user_videos(user_id: str, limit: int = Query(10, ge=1, le=50), offset: int = Query(0, ge=0)):
    """Video posts by a creator."""
    return _proxy(lambda: _get_client().user_videos(user_id, limit=limit, offset=offset))

@app.get("/api/of/v2/users/{user_id}/labels")
def of_user_labels_on(user_id: str):
    """Labels (creator content tags) applied to this user's posts."""
    return _proxy(lambda: _get_client().user_labels_on(user_id))

@app.get("/api/of/v2/users/{user_id}/social/buttons")
def of_user_social(user_id: str):
    """Creator's social-media link buttons."""
    return _proxy(lambda: _get_client().user_social_buttons(user_id))

@app.get("/api/of/v2/users/{user_id}/links")
def of_user_links(user_id: str):
    """Creator's outbound links list."""
    return _proxy(lambda: _get_client().user_links(user_id))


# ── Subscribers / counts ───────────────────────────────────────

@app.get("/api/of/v2/subscribers")
def of_subscribers(
    type: str = Query("active", pattern="^(active|expired|all|attention|muted|recent)$"),
    limit: int = Query(10, ge=1, le=50),
    offset: int = Query(0, ge=0),
    online: int | None = Query(None, ge=0, le=1, description="1 = only fans currently online"),
):
    """Fans subscribed to me. `type` filters by subscription state.
    `online=1` adds OF's `filter[online]=1` (the "Subscribers · online" list)."""
    return _proxy(lambda: _get_client().subscribers(
        type=type, limit=limit, offset=offset,
        online=(bool(online) if online is not None else None),
    ))

@app.get("/api/of/v2/subscriptions/count")
def of_subscription_counts():
    """Counts of active/expired/etc — used by the dashboard pill."""
    return _proxy(lambda: _get_client().subscription_counts())


# ── Posts ──────────────────────────────────────────────────────

@app.get("/api/of/v2/posts")
def of_list_posts(limit: int = Query(10, ge=1, le=50), offset: int = Query(0, ge=0)):
    """My own posts."""
    return _proxy(lambda: _get_client().list_posts(limit=limit, offset=offset))

# Static path BEFORE the dynamic /posts/{post_id} below.
@app.get("/api/of/v2/posts/bookmarks")
def of_bookmarks_top(limit: int = Query(10, ge=1, le=50), offset: int = Query(0, ge=0)):
    """Posts I've bookmarked."""
    return _proxy(lambda: _get_client().bookmarked_posts(limit=limit, offset=offset))

# Posts shortcuts — declared BEFORE /posts/{post_id: int} to avoid the int-validation
# trap on static second segments like "chart" / "top".
@app.get("/api/of/v2/posts/chart")
def of_posts_chart(start: str | None = Query(None), end: str | None = Query(None)):
    """Post-performance time-series (Statistics > Engagement chart)."""
    return _proxy(lambda: _get_client().posts_chart(start=start, end=end))

@app.get("/api/of/v2/posts/top")
def of_posts_top(
    start: str | None = Query(None),
    end: str | None = Query(None),
    by: str = Query("purchases", description="purchases|likes|comments|views"),
    limit: int = Query(10, ge=1, le=50),
):
    """Top-performing posts in a date range."""
    return _proxy(lambda: _get_client().posts_top(start=start, end=end, by=by, limit=limit))

@app.get("/api/of/v2/posts/{post_id}")
def of_get_post(post_id: int):
    """Single post detail."""
    return _proxy(lambda: _get_client().get_post(post_id))

@app.get("/api/of/v2/posts/{post_id}/comments")
def of_post_comments(post_id: int, limit: int = Query(20, ge=1, le=50), offset: int = Query(0, ge=0)):
    """Comments on a post."""
    return _proxy(lambda: _get_client().post_comments(post_id, limit=limit, offset=offset))


# ── Vault ──────────────────────────────────────────────────────

@app.get("/api/of/v2/vault/media")
async def of_vault_media(
    limit: int = Query(24, ge=1, le=100),
    offset: int = Query(0, ge=0),
    type: str = Query("all", pattern="^(all|photo|video|gif|audio)$",
                      description="OF uses `type=`, not `filter=` — guessing wrong silently returns everything"),
    list_id: int | None = Query(None),
    sort: str = Query("desc"),
    field: str = Query("recent"),
    refresh: bool = Query(False, description="Bypass the shared server cache; forces a fresh OF fetch"),
):
    """My vault items. Filter by media kind via `type=`.

    Reads from the shared server-side cache (vault_response_cache) keyed
    on (account_id, query_key). All employees on the same OF account
    share the cache, so the second open is instant. Cache is invalidated
    on uploads/deletes and ages out by TTL for external edits.
    """
    aid = _resolve_account_id(_request_ctx.get())
    # Chatter Access (step 4): folder-visibility gate. Evaluate the deny
    # BEFORE touching vault_cache — a cached list_id=None "All" entry must
    # never reach a restricted chatter. SOFT deny (empty payload matching
    # OF's exact shape, {"list": [], "hasMore": False}) so the frontend's
    # .map() never throws; never raise.
    allowed_folders = await _allowed_folder_ids_for_chatter(aid)
    if allowed_folders is not None and (
        list_id is None or list_id not in allowed_folders
    ):
        return {"list": [], "hasMore": False}
    key = f"media:type={type}|list={list_id}|offset={offset}|limit={limit}|sort={sort}|field={field}"
    if not refresh:
        cached = await vault_cache.get(aid, key)
        if cached is not None:
            return cached
    result = await asyncio.to_thread(
        _proxy,
        lambda: _get_client().vault_media(
            limit=limit, offset=offset, type=type, list_id=list_id, sort=sort, field=field,
        ),
    )
    await vault_cache.put(aid, key, result)
    return result


@app.get("/api/of/v2/vault/media/{media_id}")
async def of_vault_media_by_id(media_id: int) -> dict[str, Any]:
    """One vault item by id — resolves a bare media id back to a media object
    (with `files.thumb` etc. for thumbnails). Powers the Brain panel's saved
    per-slot images (`time_images` stores only ids), which the paginated list
    can't surface unless the item happens to land on a loaded page.

    Owner-gated: the only caller is the owner-only Brain editor, and gating to
    the account owner keeps this from becoming a folder-restriction bypass for
    restricted chatters (the list route soft-denies per folder; a by-id read
    can't be folder-scoped, so we require ownership instead). Cached server-side
    via vault_cache like the list route, so repeat loads are instant and shared.
    """
    aid = _resolve_account_id(_request_ctx.get())
    assert_account_owned(aid)
    key = f"media-by-id:{media_id}"
    cached = await vault_cache.get(aid, key)
    if cached is not None:
        return cached
    result = await asyncio.to_thread(
        _proxy, lambda: _get_client().vault_media_by_id(media_id),
    )
    await vault_cache.put(aid, key, result)
    return result


# ── Fan lists ──────────────────────────────────────────────────

@app.get("/api/of/v2/lists")
def of_get_lists(limit: int = Query(20, ge=1, le=50), offset: int = Query(0, ge=0)):
    """My custom fan lists + built-in ones like 'fans', 'recent'."""
    return _proxy(lambda: _get_client().get_lists(limit=limit, offset=offset))

@app.get("/api/of/v2/lists/{list_id}")
def of_get_list(list_id: str):
    """List metadata. Built-in names like 'fans' work too."""
    return _proxy(lambda: _get_client().get_list(list_id))

@app.get("/api/of/v2/lists/{list_id}/users")
def of_list_users_in(list_id: str, limit: int = Query(20, ge=1, le=50), offset: int = Query(0, ge=0)):
    """Fans in a list."""
    return _proxy(lambda: _get_client().list_users_in(list_id, limit=limit, offset=offset))


# ── Money ──────────────────────────────────────────────────────

@app.post("/admin/vault/cache/invalidate")
async def admin_vault_cache_invalidate() -> dict[str, Any]:
    """Drop every cached vault response for the current account. The picker's
    Refresh button calls this so both vault-media AND vault-lists go upstream
    on the next fetch (the `?refresh=1` flag only covers a single endpoint)."""
    aid = _resolve_account_id(_request_ctx.get())
    deleted = await vault_cache.invalidate(aid)
    return {"ok": True, "account_id": aid, "deleted": deleted}


@app.get("/api/of/v2/vault/lists")
async def of_vault_lists(view: str = Query("main"), limit: int = Query(10, ge=1, le=50),
                         offset: int = Query(0, ge=0),
                         refresh: bool = Query(False, description="Bypass the shared server cache")):
    """Vault folders. Note OF's required `view` param value is `main`.
    Cached server-side via vault_cache (same invalidation semantics as
    /api/of/v2/vault/media)."""
    aid = _resolve_account_id(_request_ctx.get())
    # Chatter Access (step 4): compute the folder gate BEFORE the cache read
    # so a cached unfiltered payload is never handed straight to a restricted
    # chatter. None ⇒ no restriction (owner, or unrestricted chatter).
    allowed_folders = await _allowed_folder_ids_for_chatter(aid)
    key = f"lists:view={view}|limit={limit}|offset={offset}"
    result = None
    if not refresh:
        result = await vault_cache.get(aid, key)
    if result is None:
        result = await asyncio.to_thread(
            _proxy,
            lambda: _get_client().vault_lists(view=view, limit=limit, offset=offset),
        )
        # Cache the UNFILTERED payload — owners + other chatters share this
        # entry. Filtering happens per-response below, never written back.
        await vault_cache.put(aid, key, result)
    if allowed_folders is not None and isinstance(result, dict):
        # Filter on READ only — shallow-copy so the cached object keeps its
        # full folder list for owners/unrestricted chatters.
        filtered = dict(result)
        filtered["list"] = [
            f for f in (result.get("list") or [])
            if f.get("id") in allowed_folders
        ]
        return filtered
    return result

@app.get("/api/of/v2/payouts/balances")
def of_payout_balances():
    """Current balance + breakdown."""
    return _proxy(lambda: _get_client().payout_balances())

@app.get("/api/of/v2/payouts/check-receive")
def of_payout_check_receive():
    """Eligibility for next payout (banking complete, KYC done, etc)."""
    return _proxy(lambda: _get_client().payout_check_receive())

@app.get("/api/of/v2/users/settings/chat")
def of_settings_chat():
    """Default-chat / autoresponder / welcome-message settings."""
    return _proxy(lambda: _get_client().settings_chat())

@app.get("/api/of/v2/users/settings/post")
def of_settings_post():
    """Default-post / watermark settings."""
    return _proxy(lambda: _get_client().settings_post())

@app.get("/api/of/v2/schedules/later/chat")
def of_schedules_later_chat(limit: int = Query(10, ge=1, le=50)):
    """Chats scheduled for the future (newer API than /messages/queue)."""
    return _proxy(lambda: _get_client().schedules_later_chat(limit=limit))

@app.get("/api/of/v2/schedules/later/post")
def of_schedules_later_post(limit: int = Query(10, ge=1, le=50)):
    """Posts scheduled for the future."""
    return _proxy(lambda: _get_client().schedules_later_post(limit=limit))

@app.get("/api/of/v2/schedules")
def of_schedules(
    publish_date: str | None = Query(None, description="ISO yyyy-mm-dd"),
    publish_date_end: str | None = Query(None, description="ISO yyyy-mm-dd"),
    time_zone: str = Query("Europe/Ljubljana"),
    limit: int = Query(20, ge=1, le=50),
):
    """Calendar view of scheduled chats + posts in a date range."""
    return _proxy(lambda: _get_client().schedules(
        publish_date=publish_date, publish_date_end=publish_date_end,
        time_zone=time_zone, limit=limit,
    ))

@app.get("/api/of/v2/schedules/counters")
def of_schedule_counters(
    publish_date: str = Query(..., description="ISO yyyy-mm-dd"),
    publish_date_end: str = Query(..., description="ISO yyyy-mm-dd"),
    time_zone: str = Query("Europe/Ljubljana"),
):
    """Counts of scheduled items per day in a date range."""
    return _proxy(lambda: _get_client().schedule_counters(
        publish_date=publish_date, publish_date_end=publish_date_end, time_zone=time_zone,
    ))

@app.get("/api/of/v2/payouts/transactions")
def of_transactions(
    limit: int = Query(20, ge=1, le=50),
    offset: int = Query(0, ge=0),
    type: str | None = Query(None),
    start: str | None = Query(None, description="ISO yyyy-mm-dd"),
    end: str | None = Query(None, description="ISO yyyy-mm-dd"),
):
    """Earning transactions (tips/subs/messages/posts)."""
    return _proxy(lambda: _get_client().transactions(
        limit=limit, offset=offset, type=type, start=start, end=end,
    ))

@app.get("/api/of/v2/payouts/stats")
def of_earning_stats(
    by: str = Query("day", pattern="^(day|week|month)$"),
    start: str | None = Query(None),
    end: str | None = Query(None),
):
    """Time-series earnings totals."""
    return _proxy(lambda: _get_client().earning_stats(by=by, start=start, end=end))


# ── Stories ────────────────────────────────────────────────────

@app.get("/api/of/v2/users/me/stories")
def of_my_stories():
    """My active stories."""
    return _proxy(lambda: _get_client().my_stories())


# ── Catch-all single user lookup ───────────────────────────────
# Declared AFTER every static /users/... route so it doesn't shadow them.

@app.get("/api/of/v2/users/{user_id_or_username}")
async def get_user(user_id_or_username: str) -> dict[str, Any]:
    """Fetch a single user profile by numeric id or username.

    Runs the OF→local sync of `displayName`/`notice` (same logic as
    /users/list) so that opening the drawer for a fan whose name was
    edited directly on onlyfans.com flips our local `Fan.custom_nickname`
    /`Fan.notes` to match. Without this, the chat-list rail keeps
    showing the stale value because enrichWithUsers short-circuits to
    the local `by-ids` lookup for any fan we already know."""
    aid = _resolve_account_id(_request_ctx.get())
    key = ("get_user", aid, user_id_or_username)

    async def _fetch():
        resp = await asyncio.to_thread(
            _proxy, lambda: _get_client().get_user(user_id_or_username)
        )
        if not isinstance(resp, dict):
            return resp
        # Overlay inside the fetcher — see /users/list rationale.
        fid = resp.get("id")
        if isinstance(fid, int):
            try:
                await _sync_of_user_overlay(aid, {str(fid): resp})
            except Exception:
                log.exception("custom_nickname stitch failed for /users/{id}")
        return resp

    return await relay_coalesce.coalesce(key, _fetch)


# ── Promotions / Trials ────────────────────────────────────────

@app.get("/api/of/v2/promotions")
def of_promotions():
    """My subscription promotions."""
    return _proxy(lambda: _get_client().promotions())

@app.get("/api/of/v2/promotions/trial")
def of_trials():
    """Trial-link campaigns (same endpoint with `type=trial`)."""
    return _proxy(lambda: _get_client().trials())


# ── Chat extras ────────────────────────────────────────────────

@app.get("/api/of/v2/chats/{chat_id}")
def of_get_chat(chat_id: int):
    """Full chat detail. Richer than the per-item shape from /chats."""
    return _proxy(lambda: _get_client().get_chat(chat_id))

@app.get("/api/of/v2/messages/queue")
def of_scheduled_messages(limit: int = Query(10, ge=1, le=50), offset: int = Query(0, ge=0)):
    """Messages scheduled for future delivery."""
    return _proxy(lambda: _get_client().scheduled_messages(limit=limit, offset=offset))

@app.get("/api/of/v2/users/me/stats/messages/group")
async def of_mass_message_history(
    request: Request,
    startDate: str = Query(..., description="YYYY-MM-DD HH:MM:SS"),
    endDate: str = Query(..., description="YYYY-MM-DD HH:MM:SS"),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    """Past mass-message broadcasts (the /statistics/engagement/messages
    feed). Each item carries the queue `id` — pass to DELETE
    /messages/queue/{id} to unsend the broadcast from EVERY recipient
    (no edit window for mass sends, per OF's `unsendSeconds=1000000`).

    Write-through to `mass_broadcast_cache` on every successful fetch so
    the /admin/mass-messages read path can serve subsequent opens off
    SQLite without an OF roundtrip."""
    import mass_broadcast_cache as cache
    resp = await asyncio.to_thread(_proxy, lambda: _get_client().mass_message_history(
        start_date=startDate, end_date=endDate, limit=limit, offset=offset,
    ))
    # Best-effort write-through. Don't let a cache miss surface as a 502
    # — the upstream succeeded, the operator should still see the data.
    if isinstance(resp, dict):
        items = resp.get("items") or []
        try:
            account_id = _resolve_account_id(request)
            await cache.upsert_many(account_id, items)
        except Exception as e:
            log.warning("mass_broadcast_cache upsert failed: %s", e)
    return resp


@app.get("/admin/mass-messages")
async def admin_mass_messages_list(
    request: Request,
    account_id: str | None = Query(None, description="OF account id; defaults to X-Account-Id"),
    days_back: int = Query(30, ge=1, le=730),
    limit: int = Query(100, ge=1, le=500),
):
    """Read past mass broadcasts for ONE account from the local cache.

    Fast path for the Settings → Mass messages tab — no OF roundtrip,
    snappy across reloads. Pair with POST /admin/mass-messages/refresh
    to force a fresh pull from OF and re-warm the cache.

    Safety net: if the cache hasn't been refreshed within TTL_SECONDS
    (1 day), pull from OF once before serving so the UI doesn't drift
    silently when no-one clicks Refresh."""
    import mass_broadcast_cache as cache
    aid = account_id or _resolve_account_id(request)
    if await cache.is_stale(aid):
        try:
            end = datetime.utcnow()
            start = end - timedelta(days=days_back)
            fmt = "%Y-%m-%d %H:%M:%S"
            client = _load_client(aid)

            def _fetch():
                # Run the blocking OF round-trip (and the per-account lane
                # acquire) OFF the event loop so a stale-cache safety-net
                # refresh can't freeze vault-open / chat-click for everyone.
                with _priority_lane(aid):
                    return client.mass_message_history(
                        start_date=start.strftime(fmt),
                        end_date=end.strftime(fmt),
                        limit=limit, offset=0,
                    )
            resp = await asyncio.to_thread(_fetch)
            items = (resp.get("items") if isinstance(resp, dict) else None) or []
            await cache.upsert_many(aid, items)
        except Exception as e:
            # Don't fail the read just because the safety-net refresh failed;
            # serve whatever we have and let the user hit Refresh manually.
            log.warning("mass_broadcast_cache safety-net refresh failed for %s: %s", aid, e)
    since = datetime.utcnow() - timedelta(days=days_back)
    items = await cache.list_for_account(aid, since=since, limit=limit)
    return {"account_id": aid, "days_back": days_back, "items": items}


@app.post("/admin/mass-messages/refresh")
async def admin_mass_messages_refresh(
    request: Request,
    account_id: str | None = Query(None),
    days_back: int = Query(30, ge=1, le=730),
    limit: int = Query(100, ge=1, le=500),
):
    """Pull from OF + upsert into the cache + return the fresh rows.

    The frontend calls this when the user clicks Refresh. Done as a
    single HTTP call (vs. fetching from OF and then re-reading the cache)
    so the UI gets the new state in one roundtrip."""
    import mass_broadcast_cache as cache
    aid = account_id or _resolve_account_id(request)
    end = datetime.utcnow()
    start = end - timedelta(days=days_back)
    fmt = "%Y-%m-%d %H:%M:%S"
    # Resolve the right client by hand — _proxy() relies on the request
    # context's X-Account-Id, but for this endpoint we accept it as a
    # query param too. _load_client() bypasses the context resolver.
    client = _load_client(aid)
    try:
        def _fetch():
            # Blocking OF round-trip + lane acquire run OFF the event loop;
            # this manual Refresh must not freeze every other request.
            with _priority_lane(aid):
                return client.mass_message_history(
                    start_date=start.strftime(fmt),
                    end_date=end.strftime(fmt),
                    limit=limit, offset=0,
                )
        resp = await asyncio.to_thread(_fetch)
    except OFAPIError as e:
        r = e.response
        status = r.status_code if r is not None else 500
        body = r.text[:2000] if r is not None else str(e)
        raise HTTPException(
            status_code=502,
            detail={"upstream_status": status, "upstream_body": body},
        )
    items = (resp.get("items") if isinstance(resp, dict) else None) or []
    written = await cache.upsert_many(aid, items)
    since = datetime.utcnow() - timedelta(days=days_back)
    rows = await cache.list_for_account(aid, since=since, limit=limit)
    return {"account_id": aid, "days_back": days_back, "written": written, "items": rows}


# ── Writes (UNVERIFIED — code-only, no live test from auto run) ───
# Every endpoint below maps to a known OF API path inferred from the cracked
# extension + OnlyFansAPI/OFAuth docs. Shapes are likely correct but each one
# needs a single careful live test before you trust it in a UI.

# Pydantic bodies for FastAPI's auto-validation + Swagger forms.
class _TextBody(BaseModel):
    text: str = Field(..., min_length=1)

class _NameBody(BaseModel):
    name: str = Field(..., min_length=1)

class _ScheduleMessageBody(BaseModel):
    text: str
    scheduled_date: str = Field(..., description="ISO 8601, e.g. 2026-06-01T18:00:00+00:00")
    price: float = Field(0, ge=0)
    locked_text: bool = False
    # Same shape as immediate-send: numeric vault ids OR fresh-upload claim
    # dicts. OF accepts both in mediaFiles on the queue endpoint too.
    media_files: list[int | dict] = Field(default_factory=list)
    previews: list[int] = Field(default_factory=list)
    tagged_users: list[int] = Field(default_factory=list)

class _ScheduledSendBody(BaseModel):
    """Near-term (≤15 min) deferred 1:1 send, fired by OUR executor (the
    `scheduled_send` automation), not OF's queue. Survives reload + is shared
    cross-chatter because it's a DB row, not an in-browser timer."""
    fan_id: int = Field(..., description="Fan's OF user id (the chat peer).")
    run_at: str = Field(..., description="ISO 8601 fire time, e.g. 2026-06-13T15:38:00Z")
    text: str = ""
    price: float = Field(0, ge=0)
    locked_text: bool = False
    media_files: list[int | dict] = Field(default_factory=list)
    previews: list[int] = Field(default_factory=list)
    tagged_users: list[int] = Field(default_factory=list)
    giphy_id: str | None = None


class _CreatePostBody(BaseModel):
    text: str
    # int = existing vault id; dict = fresh-upload claim payload from
    # the /upload endpoint's `send_with`. OF accepts mixed.
    media_files: list[int | dict] = Field(default_factory=list)
    price: float = Field(0, ge=0)
    posted_at: str | None = None
    # OF user ids of creators to @-tag in the post (sent as `userTags`).
    tagged_users: list[int] = Field(default_factory=list)
    # Giphy id (e.g. "0ndspgUFbm9iQtyX2Q") — forwarded as top-level
    # `giphyId` to OF. Same wire shape as chat sends.
    giphy_id: str | None = Field(
        None,
        description="Giphy GIF id forwarded to OF as top-level `giphyId`.",
    )

class _EditPostBody(BaseModel):
    text: str | None = None
    price: float | None = None
    media_files: list[int | dict] | None = None

class _TipBody(BaseModel):
    user_id: int
    amount: float = Field(..., gt=0)
    message: str = ""


# Chat-message writes ---------------------------------------------
# These hit /messages/{id}/... directly on OF (no /chats/{cid} prefix). All
# four like/unlike/pin/unpin VERIFIED LIVE; unsend works but is DESTRUCTIVE.

class _UnsendMessageBody(BaseModel):
    """OF web sends `{withUserId: <fanId>}` on every message unsend. Forwarding
    it lets OF return the parent `queue` object in the response — needed so
    the UI can offer "also unsend from everyone" on mass-message bubbles."""
    withUserId: int | None = None


@app.delete("/api/of/v2/messages/{message_id}")
async def of_unsend_message(request: Request, message_id: int):
    """Unsend (delete) a chat message. Works inside OF's edit window only.

    The OF endpoint expects an optional `{"withUserId": <fan_id>}` body.
    We accept the body if present (FastAPI's `Body(...)` would 400 on
    empty bodies, so we parse defensively)."""
    with_user_id: int | None = None
    try:
        raw = await request.body()
        if raw:
            import json as _json
            parsed = _json.loads(raw)
            if isinstance(parsed, dict) and parsed.get("withUserId") is not None:
                with_user_id = int(parsed["withUserId"])
    except Exception:
        with_user_id = None
    return await asyncio.to_thread(_proxy, lambda: _get_client().unsend_message(message_id, with_user_id=with_user_id))

@app.post("/api/of/v2/messages/{message_id}/like")
def of_like_message(message_id: int):
    """Like a message you received (can't like your own)."""
    return _proxy(lambda: _get_client().like_message(message_id))

@app.delete("/api/of/v2/messages/{message_id}/like")
def of_unlike_message(message_id: int):
    return _proxy(lambda: _get_client().unlike_message(message_id))

@app.post("/api/of/v2/messages/{message_id}/pin/user/{user_id}")
def of_pin_message(message_id: int, user_id: int):
    """Pin a message inside a chat. Path mirrors OF's wire shape
    (`/messages/{msg}/pin/user/{chat_partner}`) — the shorter `/pin` form
    didn't reliably pin to the chat's visible pinned list."""
    return _proxy(lambda: _get_client().pin_message(message_id, user_id))

@app.delete("/api/of/v2/messages/{message_id}/pin/user/{user_id}")
def of_unpin_message(message_id: int, user_id: int):
    return _proxy(lambda: _get_client().unpin_message(message_id, user_id))

# Chat-level actions (VERIFIED LIVE) — mark read/unread, mute, hide.
# Static path BEFORE dynamic /chats/{chat_id}/... to avoid `chat_id="mark-as-read"`
# int-validation 422.

@app.post("/api/of/v2/chats/mark-as-read")
def of_mark_all_chats_read():
    """Mark EVERY chat as read (bulk no-arg endpoint)."""
    return _proxy(lambda: _get_client().mark_all_chats_read())

@app.post("/api/of/v2/chats/{chat_id}/mark-as-read")
def of_mark_chat_read(chat_id: int):
    """Mark chat as read."""
    return _proxy(lambda: _get_client().mark_chat_read(chat_id))

@app.delete("/api/of/v2/chats/{chat_id}/mark-as-read")
def of_mark_chat_unread(chat_id: int):
    """Mark chat as unread (same path as mark-read but DELETE)."""
    return _proxy(lambda: _get_client().mark_chat_unread(chat_id))

@app.post("/api/of/v2/chats/{chat_id}/mute")
def of_mute_chat(chat_id: int):
    """Mute notifications for a chat."""
    return _proxy(lambda: _get_client().mute_chat(chat_id))

@app.delete("/api/of/v2/chats/{chat_id}/mute")
def of_unmute_chat(chat_id: int):
    return _proxy(lambda: _get_client().unmute_chat(chat_id))

@app.post("/api/of/v2/chats/{chat_id}/hide")
def of_hide_chat(chat_id: int):
    """Hide chat from inbox (chat still exists; fan can still message)."""
    return _proxy(lambda: _get_client().hide_chat(chat_id))

@app.delete("/api/of/v2/chats/{chat_id}/hide")
def of_unhide_chat(chat_id: int):
    return _proxy(lambda: _get_client().unhide_chat(chat_id))

@app.get("/api/of/v2/chats/{chat_id}/media")
def of_chat_media(
    chat_id: int,
    limit: int = Query(20, ge=1, le=50),
    offset: int = Query(0, ge=0),
    last_id: str | None = Query(None, description="Cursor: pass the previous response's nextLastId."),
    skip_users: str | None = Query(None, description="Set to 'all' to drop the per-message fromUser blob (matches OF's purchased-tab call)."),
    opened: int | None = Query(None, ge=0, le=1, description="opened=1 filters to messages the fan has VIEWED (includes free)."),
    purchased: int | None = Query(None, ge=0, le=1, description="purchased=1 filters to PPVs the fan actually paid for (OF's Purchased tab)."),
    from_user: int | None = Query(None, description="Filter to messages from one specific user; pair with purchased=1 + the creator's user id to get only outbound paid messages (the canonical Sales feed)."),
):
    """Only messages in this chat that contain media.

    `purchased=1` + `from_user=<creator_user_id>` + `skip_users=all`
    mirrors OF's per-fan PURCHASED gallery (the /gallery/purchased URL
    in the web UI) and is the canonical source for the fan-drawer Sales
    section. `opened=1` is a different filter — items the fan has
    viewed, including free messages — kept for the "any viewed" case."""
    return _proxy(lambda: _get_client().chat_media(
        chat_id,
        limit=limit,
        offset=offset,
        last_id=last_id,
        skip_users=skip_users,
        opened=opened,
        purchased=purchased,
        from_user=from_user,
    ))

@app.get("/api/of/v2/chats/{chat_id}/messages/search")
def of_search_chat(chat_id: int, query: str = Query(..., min_length=1, description="Substring to match against message text.")):
    """Search the FULL history of one chat. Returns a list of matching
    message IDs (newest first) — OF's native chat-search endpoint. The
    caller is responsible for fetching previews / paging to load the
    matched message bodies if they aren't in the local cache yet.

    OF param is `query` (not `text` — that 200s with an empty list)."""
    return _proxy(lambda: _get_client().search_chat(chat_id, query))

@app.post("/api/of/v2/users/{user_id}/block")
def of_block_user(user_id: int):
    """Block a user (hides chat + prevents future messages from them)."""
    return _proxy(lambda: _get_client().block_user(user_id))

@app.delete("/api/of/v2/users/{user_id}/block")
def of_unblock_user(user_id: int):
    return _proxy(lambda: _get_client().unblock_user(user_id))

@app.post("/api/of/v2/users/{user_id}/restrict")
def of_restrict_user(user_id: int):
    """Restrict a user (their content is hidden from your feed)."""
    return _proxy(lambda: _get_client().restrict_user(user_id))

@app.delete("/api/of/v2/users/{user_id}/restrict")
def of_unrestrict_user(user_id: int):
    return _proxy(lambda: _get_client().unrestrict_user(user_id))

@app.get("/api/of/v2/stories")
def of_stories_feed():
    """Stories feed (creators I follow)."""
    return _proxy(lambda: _get_client().stories_feed())

@app.get("/api/of/v2/stories/archive")
def of_stories_archive(limit: int = Query(20, ge=1, le=50), offset: int = Query(0, ge=0)):
    """My own archived (expired) stories."""
    return _proxy(lambda: _get_client().my_stories_archive(limit=limit, offset=offset))

@app.get("/api/of/v2/giphy/proxy/gifs/trending")
def of_gif_trending(limit: int = Query(10, ge=1, le=50), offset: int = Query(0, ge=0)):
    """Trending GIFs via OF's Giphy proxy."""
    return _proxy(lambda: _get_client().gif_trending(limit=limit, offset=offset))

@app.get("/api/of/v2/giphy/proxy/gifs/search")
def of_gif_search(q: str = Query(..., min_length=1),
                  limit: int = Query(10, ge=1, le=50), offset: int = Query(0, ge=0)):
    """Search Giphy via OF's proxy."""
    return _proxy(lambda: _get_client().gif_search(q, limit=limit, offset=offset))

# ── Message templates (welcome + saved replies) ───────────────

class _TemplateBody(BaseModel):
    text: str
    template: str | None = Field(None, description="'reply_on_subscribe' = welcome message, otherwise a saved reply")
    media_files: list[int | dict] = Field(default_factory=list)
    price: float = Field(0, ge=0)
    locked_text: bool = False

class _TemplatePatchBody(BaseModel):
    text: str | None = None
    media_files: list[int | dict] | None = None
    price: float | None = None
    locked_text: bool | None = None
    template: str | None = None  # carried for completeness; OF preserves slot on update

@app.get("/api/of/v2/messages/templates")
def of_message_templates(template: str | None = Query(None, description="filter, e.g. 'reply_on_subscribe'")):
    """Saved replies + welcome message. `template=reply_on_subscribe` filters to the welcome message."""
    return _proxy(lambda: _get_client().message_templates(template=template))

@app.post("/api/of/v2/messages/templates")
def of_create_template(body: _TemplateBody = Body(...)):
    """Create a saved reply or set the welcome message (`template=reply_on_subscribe`)."""
    return _proxy(lambda: _get_client().create_template(
        body.text, template=body.template, media_files=body.media_files,
        price=body.price, locked_text=body.locked_text,
    ))

# ── Welcome message slot (reply_on_subscribe) ──────────────────
# Slot-specific upsert + delete + the `replyOnSubscribe` master toggle.
# OF's web client doesn't go through the generic /messages/templates
# create/PUT for the welcome — it POSTs/DELETEs the slot URL directly
# and uses the extended body shape, so we mirror that here.
#
# IMPORTANT: declared BEFORE the generic /messages/templates/{id} routes
# so FastAPI's first-match-wins ordering routes /reply_on_subscribe here
# rather than treating it as a numeric template id.

class _WelcomeTemplateBody(BaseModel):
    text: str
    media_files: list[int | dict] = Field(default_factory=list)
    price: float = Field(0, ge=0)
    locked_text: bool = False
    previews: list[int] = Field(default_factory=list)

class _ReplyOnSubscribeBody(BaseModel):
    enabled: bool

@app.post("/api/of/v2/messages/templates/reply_on_subscribe")
def of_upsert_welcome_template(body: _WelcomeTemplateBody = Body(...)):
    """Create or replace the welcome message (`reply_on_subscribe`)."""
    return _proxy(lambda: _get_client().upsert_welcome_template(
        body.text, media_files=body.media_files, price=body.price,
        locked_text=body.locked_text, previews=body.previews,
    ))

@app.delete("/api/of/v2/messages/templates/reply_on_subscribe")
def of_delete_welcome_template():
    """Delete the welcome message slot."""
    return _proxy(lambda: _get_client().delete_welcome_template())

@app.patch("/api/of/v2/users/me/reply-on-subscribe")
def of_set_reply_on_subscribe(body: _ReplyOnSubscribeBody = Body(...)):
    """Toggle the master `replyOnSubscribe` flag on /users/me."""
    result = _proxy(lambda: _get_client().set_reply_on_subscribe(body.enabled))
    # BC5 invalidation: this write changes /users/me/settings.replyOnSubscribe.
    aid = _resolve_account_id(_request_ctx.get())
    relay_cache.invalidate("my_settings", aid)
    return result

@app.put("/api/of/v2/messages/templates/{template_id}")
def of_update_template(template_id: str, body: _TemplatePatchBody = Body(...)):
    """Edit a template."""
    return _proxy(lambda: _get_client().update_template(
        template_id, text=body.text, media_files=body.media_files,
        price=body.price, locked_text=body.locked_text,
    ))

@app.delete("/api/of/v2/messages/templates/{template_id}")
def of_delete_template(template_id: str):
    """Delete a saved reply / welcome message."""
    return _proxy(lambda: _get_client().delete_template(template_id))

# ── Subscription bundles ──────────────────────────────────────

class _BundleBody(BaseModel):
    months: int = Field(..., ge=1, le=24)
    price: float = Field(..., ge=0)
    discount: int | None = Field(None, ge=0, le=100)

@app.get("/api/of/v2/subscriptions/bundles")
def of_subscription_bundles():
    """Multi-month bundle pricing config."""
    return _proxy(lambda: _get_client().subscription_bundles())

@app.post("/api/of/v2/subscriptions/bundles")
def of_create_bundle(body: _BundleBody = Body(...)):
    return _proxy(lambda: _get_client().create_bundle(
        months=body.months, price=body.price, discount=body.discount,
    ))

@app.delete("/api/of/v2/subscriptions/bundles/{bundle_id}")
def of_delete_bundle(bundle_id: int):
    return _proxy(lambda: _get_client().delete_bundle(bundle_id))

# ── Promotion campaigns: write side ───────────────────────────

class _PromoBody(BaseModel):
    price: float = Field(..., ge=0)
    subscribe_counts: int = Field(..., ge=1)
    subscribe_days: int = Field(0, ge=0)
    message: str = ""
    type: str = "all"

@app.post("/api/of/v2/promotions")
def of_create_promo(body: _PromoBody = Body(...)):
    """Create a promo campaign. Touches public-facing offers; double-check before exposing."""
    return _proxy(lambda: _get_client().create_promo(
        price=body.price, subscribe_counts=body.subscribe_counts,
        subscribe_days=body.subscribe_days, message=body.message, type=body.type,
    ))

@app.delete("/api/of/v2/promotions/{promo_id}")
def of_delete_promo(promo_id: int):
    return _proxy(lambda: _get_client().delete_promo(promo_id))

# ── Tracking links (/campaigns) ──────────────────────────────

class _TrackingLinkBody(BaseModel):
    name: str
    code: int | None = None

@app.get("/api/of/v2/campaigns")
def of_tracking_links():
    """Tracking link list with countSubscribers + countTransitions stats."""
    return _proxy(lambda: _get_client().tracking_links())

@app.post("/api/of/v2/campaigns")
def of_create_tracking_link(body: _TrackingLinkBody = Body(...)):
    return _proxy(lambda: _get_client().create_tracking_link(
        name=body.name, code=body.code,
    ))

@app.delete("/api/of/v2/campaigns/{campaign_id}")
def of_delete_tracking_link(campaign_id: int):
    return _proxy(lambda: _get_client().delete_tracking_link(campaign_id))

# ── Free-trial links (/trials) ───────────────────────────────

class _TrialLinkBody(BaseModel):
    name: str
    subscribe_days: int = Field(7, ge=1, le=365)
    subscribe_counts: int = Field(1, ge=1)
    expired_at: str | None = None

@app.get("/api/of/v2/trials")
def of_trial_links():
    """Free-trial link list with claim/click counts."""
    return _proxy(lambda: _get_client().trial_links())

@app.post("/api/of/v2/trials")
def of_create_trial_link(body: _TrialLinkBody = Body(...)):
    return _proxy(lambda: _get_client().create_trial_link(
        name=body.name, subscribe_days=body.subscribe_days,
        subscribe_counts=body.subscribe_counts, expired_at=body.expired_at,
    ))

@app.delete("/api/of/v2/trials/{trial_id}")
def of_delete_trial_link(trial_id: int):
    return _proxy(lambda: _get_client().delete_trial_link(trial_id))

# ── Profile writes (bio, display name, etc.) ─────────────────

@app.patch("/api/of/v2/users/me")
def of_update_profile(body: dict = Body(...)):
    """PATCH /users/me — update profile fields. Body is any JSON dict; keys
    match /users/me response shape. Confirmed working live for `about`."""
    result = _proxy(lambda: _get_client().update_profile(**body))
    # BC5 invalidation: many /users/me fields are mirrored under
    # /users/me/settings, so any profile patch may change the cached blob.
    aid = _resolve_account_id(_request_ctx.get())
    relay_cache.invalidate("my_settings", aid)
    return result

# ── Analytics (Statistics > Earnings/Engagement/Reach tabs) ────────
# All accept startDate/endDate as ISO with space (OF spec). Default = 30d window.

@app.get("/api/of/v2/earnings/chart")
def of_earnings_chart(start: str | None = Query(None), end: str | None = Query(None)):
    """Earnings time-series for the Statistics → Earnings chart."""
    return _proxy(lambda: _get_client().earnings_chart(start=start, end=end))

# /posts/chart and /posts/top declared above /posts/{post_id} in the file
# (see "Posts shortcuts" section) so they don't get shadowed by the int-typed
# dynamic route. Routes themselves live there; nothing here.

@app.get("/api/of/v2/users/me/stats/overview")
def of_my_stats_overview(start: str | None = Query(None), end: str | None = Query(None),
                          by: str = Query("visitors")):
    """My stats overview (visitors/engagement)."""
    return _proxy(lambda: _get_client().my_stats_overview(start=start, end=end, by=by))

@app.get("/api/of/v2/users/me/stats/top/post")
def of_my_stats_top_posts(start: str | None = Query(None), end: str | None = Query(None),
                          limit: int = Query(10, ge=1, le=50)):
    """My best-performing posts across types."""
    return _proxy(lambda: _get_client().my_stats_top_posts(start=start, end=end, limit=limit))

@app.get("/api/of/v2/users/me/profile/stats")
def of_my_profile_stats(start: str | None = Query(None), end: str | None = Query(None),
                         limit: int = Query(10, ge=1, le=50)):
    """Visitor source breakdown (Statistics → Reach)."""
    return _proxy(lambda: _get_client().my_profile_stats(start=start, end=end, limit=limit))

@app.get("/api/of/v2/users/me/start-date-model")
def of_my_start_date():
    """When I became a creator — used to range-cap charts."""
    return _proxy(lambda: _get_client().my_start_date())

@app.get("/api/of/v2/payouts/chargebacks/ratio")
def of_chargebacks_ratio(start: str | None = Query(None), end: str | None = Query(None)):
    """Chargeback-rate risk metric."""
    return _proxy(lambda: _get_client().chargebacks_ratio(start=start, end=end))

# ── Bookmarks (Collections → Bookmarks tab) ────────────────────────

@app.get("/api/of/v2/posts/bookmarks/all")
def of_bookmarks_all(limit: int = Query(10, ge=1, le=50), offset: int = Query(0, ge=0)):
    """Paginated bookmarks feed (the OF UI tab)."""
    return _proxy(lambda: _get_client().bookmarks_all(limit=limit, offset=offset))

@app.get("/api/of/v2/posts/bookmarks/categories")
def of_bookmark_categories(limit: int = Query(10, ge=1, le=50), offset: int = Query(0, ge=0)):
    """Bookmark folders / category tabs."""
    return _proxy(lambda: _get_client().bookmark_categories(limit=limit, offset=offset))

# ── Referrals ──────────────────────────────────────────────────────

@app.get("/api/of/v2/payments/referrals/balance")
def of_referrals_balance():
    """Referral earnings balance."""
    return _proxy(lambda: _get_client().referrals_balance())

@app.get("/api/of/v2/payouts/requests/referral")
def of_referral_payouts(limit: int = Query(10, ge=1, le=50), offset: int = Query(0, ge=0)):
    """Referral payout history."""
    return _proxy(lambda: _get_client().referral_payouts(limit=limit, offset=offset))

# ── Stories with map param + notification transports ──────────────

@app.get("/api/of/v2/users/settings/notifications/transports")
def of_notification_transports():
    """Notification channel settings (push/email/SMS)."""
    return _proxy(lambda: _get_client().notification_transports())

class _StoriesItemsBody(BaseModel):
    map: dict[str, int] = Field(..., description="{user_id: story_id} mapping")

@app.post("/api/of/v2/stories/items/lookup")
def of_stories_items(body: _StoriesItemsBody = Body(...)):
    """Look up story items for multiple users. OF's underlying call is
    `GET /stories/items?map[uid]=sid&...` but the query string gets unwieldy,
    so this proxy takes a JSON body. POST with `{"map": {"117183": 116351527}}`."""
    return _proxy(lambda: _get_client().stories_items({int(k): int(v) for k, v in body.map.items()}))

# Scheduled / mass --------------------------------------------------

@app.post("/api/of/v2/chats/{chat_id}/messages/scheduled")
def of_schedule_message(chat_id: int, body: _ScheduleMessageBody = Body(...)):
    """Schedule a single message for future delivery.
    Under the hood: POST /api2/v2/messages/queue with userIds=[chat_id]."""
    return _proxy(lambda: _get_client().schedule_message(
        chat_id, text=body.text, scheduled_date=body.scheduled_date,
        price=body.price, locked_text=body.locked_text, media_files=body.media_files,
        previews=body.previews,
        tagged_users=body.tagged_users,
    ))


# ── Near-term scheduled sends (executor-fired, survive reload, cross-chatter) ──
#
# These replace the old in-browser local-wait timer. A row in `scheduled_jobs`
# (kind="scheduled_send") is enqueued here; the executor's `scheduled_send`
# automation fires it at `run_at`. Distinct from the OF-queue path above
# (`/messages/scheduled`), which is for longer delays and lives on OF's servers.

def _run_at_naive_utc(iso: str) -> datetime:
    """Parse an ISO fire-time to a NAIVE-UTC datetime — the form the rest of the
    schema uses (everything compares against `datetime.utcnow()`).

    NB: we parse with `fromisoformat`, NOT event_transcoder._parse_iso — the
    latter *strips* the offset (treats the wall-clock as-is) rather than
    converting it, so a `+02:00` time would fire 2h late. The UI only sends `Z`
    strings, but normalizing correctly costs nothing and is robust to other
    callers."""
    from datetime import timezone
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail=f"invalid run_at: {iso!r}")
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


@app.post("/api/of/v2/scheduled-sends")
async def create_scheduled_send(request: Request, body: _ScheduledSendBody = Body(...)):
    """Enqueue a near-term deferred 1:1 send. Resolves the scheduling chatter's
    employee id so the eventual bubble is attributed to them, then inserts a
    `scheduled_jobs` row the executor will fire at `run_at`."""
    account_id = _resolve_account_id(request)
    from employees import resolve_outbound_employee_id
    from automation_executor import enqueue_job

    employee_id = await resolve_outbound_employee_id(request, account_id)
    run_at = _run_at_naive_utc(body.run_at)
    payload = {
        "fan_id": body.fan_id,
        "text": body.text,
        "price": body.price,
        "locked_text": body.locked_text,
        "media_files": body.media_files,
        "previews": body.previews,
        "tagged_users": body.tagged_users,
        "giphy_id": body.giphy_id,
        "sent_by_employee_id": employee_id,
    }
    job_id = await enqueue_job(
        account_id, "scheduled_send",
        payload=payload, run_at=run_at, created_by_employee_id=employee_id,
    )
    return {"job_id": job_id, "fan_id": body.fan_id, "run_at": run_at.isoformat() + "Z"}


@app.get("/api/of/v2/scheduled-sends")
async def list_scheduled_sends(request: Request, fan_id: int | None = None):
    """List this account's pending/running near-term scheduled sends — optionally
    scoped to one fan. The chat thread renders these as ghost bubbles; any
    chatter on the account sees the same rows."""
    from db.engine import get_session
    from db.models import ScheduledJob
    from sqlalchemy import select

    account_id = _resolve_account_id(request)
    async with get_session() as s:
        rows = (await s.execute(
            select(ScheduledJob)
            .where(
                ScheduledJob.account_id == account_id,
                ScheduledJob.kind == "scheduled_send",
                ScheduledJob.status.in_(("pending", "running")),
            )
            .order_by(ScheduledJob.run_at)
        )).scalars().all()

    out: list[dict[str, Any]] = []
    for r in rows:
        try:
            p = json.loads(r.payload_json or "{}")
        except Exception:
            p = {}
        if fan_id is not None and int(p.get("fan_id") or 0) != int(fan_id):
            continue
        out.append({
            "job_id": r.id,
            "fan_id": p.get("fan_id"),
            "run_at": (r.run_at.isoformat() + "Z") if r.run_at else None,
            "status": r.status,
            "text": p.get("text") or "",
            "price": p.get("price") or 0,
            "locked_text": bool(p.get("locked_text")),
            "media_count": len(p.get("media_files") or []),
            "giphy_id": p.get("giphy_id"),
        })
    return {"list": out}


@app.delete("/api/of/v2/scheduled-sends/{job_id}")
async def cancel_scheduled_send(request: Request, job_id: int):
    """Cancel a near-term scheduled send before it fires. Atomic: only a still
    `pending` job is cancellable — once the executor has CLAIMED it (`running`)
    it's already going out, so we report it as too-late rather than lying."""
    from db.engine import get_session
    from db.models import ScheduledJob
    from sqlalchemy import update

    account_id = _resolve_account_id(request)
    async with get_session() as s:
        res = await s.execute(
            update(ScheduledJob)
            .where(
                ScheduledJob.id == job_id,
                ScheduledJob.account_id == account_id,
                ScheduledJob.kind == "scheduled_send",
                ScheduledJob.status == "pending",
            )
            .values(status="cancelled")
        )
    cancelled = bool(res.rowcount)
    if not cancelled:
        return {"cancelled": False, "reason": "not_pending"}
    return {"cancelled": True, "job_id": job_id}


class _MassScheduleBody(BaseModel):
    text: str
    # Optional — omit for an immediate broadcast. /messages/queue is OF's
    # send-now path when no scheduledDate is in the body; with one it
    # becomes the scheduled-send path.
    scheduled_date: str | None = Field(
        None, description="ISO 8601 (e.g. 2026-06-01T18:00:00+00:00); omit to send now",
    )
    user_ids: list[int] = Field(default_factory=list)
    user_lists: list[str] = Field(default_factory=list, description="List ids or built-in names like 'fans'")
    excluded_users: list[int] = Field(default_factory=list)
    excluded_user_lists: list[str] = Field(default_factory=list, description="List ids/names to exclude from the audience")
    price: float = Field(0, ge=0)
    locked_text: bool = False
    # int = existing vault id; dict = fresh-upload claim payload from
    # the /upload endpoint's `send_with`. Same shape send_message accepts.
    media_files: list[int | dict] = Field(default_factory=list)
    # Vault ids that ride along unlocked when price > 0. Must be a subset
    # of `media_files`'s numeric ids.
    previews: list[int] = Field(default_factory=list)
    # OF user ids of creators to @-tag in the broadcast (sent as `userTags`).
    # Same picker contract as direct chat sends — ids must come from
    # /api/of/v2/posts/tagged-friend-users or OF will 400.
    tagged_users: list[int] = Field(default_factory=list)
    # Giphy id — forwarded as top-level `giphyId` to OF. Multi-GIF mass
    # broadcasts fan out client-side as separate calls.
    giphy_id: str | None = Field(
        None,
        description="Giphy GIF id forwarded to OF as top-level `giphyId`.",
    )
    # Native audience filter — forwarded to OF as the top-level `filters` object.
    # `online_only=True` (or `filters={"online": 1}`) targets fans currently
    # online at send time. Combine with `user_lists=["fans"]` to blast every
    # online fan. Mirrors OF web's online broadcast.
    online_only: bool = False
    filters: dict | None = None
    # DB-sourced audience: inject the fan ids we exchanged a message with in the
    # last `recent_chat_hours` (newest first, capped to `recent_chat_limit`).
    # Resolved server-side from the local `messages` table and MERGED into
    # `user_ids` — no OF call needed. Bots + blacklisted fans are dropped.
    recent_chat_hours: float | None = Field(
        None, gt=0, description="Target fans chatted with in the last N hours (from local DB)",
    )
    recent_chat_limit: int | None = Field(
        None, gt=0, description="Cap the recent-chat audience to the N most-recent fans",
    )
    # DB-sourced EXCLUSIONS — drop fans from the audience based on recent
    # activity (resolved server-side, merged into `excluded_users`).
    #   exclude_replied_hours: fans we sent an OUTBOUND message to in the last
    #     N hours (i.e. "already replied") — don't re-blast them.
    #   exclude_inbound_hours: fans who messaged us INBOUND in the last N hours
    #     — the extra "2h inbound" filter.
    exclude_replied_hours: float | None = Field(
        None, gt=0, description="Exclude fans we replied to (outbound) in the last N hours",
    )
    exclude_inbound_hours: float | None = Field(
        None, gt=0, description="Exclude fans who messaged us (inbound) in the last N hours",
    )
    # Target fans with UNREAD messages (the OF "Unread" inbox tab), capped at
    # `unread_limit`. Resolved via OF `GET /chats?filter=unread` and MERGED into
    # `user_ids`. Great for "reply blast" to fans waiting on us.
    unread_limit: int | None = Field(
        None, gt=0, le=1000, description="Target up to N fans with unread messages",
    )
    # Auto-unsend: N hours after an immediate broadcast goes out, unsend it from
    # every recipient. OF has no native timer, so we enqueue a one-shot
    # `unsend_messages` job at send_time + N hours (the A12 automation calls
    # DELETE /messages/queue/{id} — the forever-window mass unsend). Ignored for
    # scheduled sends (unsending a future send is just a cancel).
    unsend_after_hours: float | None = Field(
        None, gt=0, le=8760, description="Auto-unsend the broadcast N hours after it sends",
    )
    # Funnel anchor — stamps the minted `mass_runs` row with this
    # `mass_message_funnels.id` so the reply_mass_funnel automation discovers
    # repliers and walks the funnel's reply/PPV steps. Send-now only (a
    # scheduled run is closed by OF later, so it never reaches 'ok' here).
    funnel_id: int | None = Field(
        None, gt=0, description="mass_message_funnels.id to walk repliers through",
    )


@app.post("/api/of/v2/messages/queue")
async def of_send_or_schedule_mass(
    request: Request,
    body: _MassScheduleBody = Body(...),
):
    """POST /messages/queue — broadcasts a mass message to many fans.
    With `scheduled_date` it's a scheduled send; without, it's immediate.
    `user_ids` for explicit fans OR `user_lists` for list-based audience.

    Same attribution-write contract as `of_send_mass` (the legacy
    /chats/messages path): mint a `mass_runs` row up front, then walk the
    OF response for per-fan ids and write attribution rows. This is the
    path the Next UI broadcast composer actually hits."""
    # DB-sourced audience: merge "chatted in the last N hours" fan ids into the
    # explicit user_ids before anything else, so mass-run attribution + the OF
    # send both see the full recipient set.
    if (body.recent_chat_hours or body.exclude_replied_hours
            or body.exclude_inbound_hours or body.unread_limit):
        from audiences import resolve_mass_audience
        _aid = _resolve_account_id(request)
        resolved = await resolve_mass_audience(
            _aid,
            included_users=body.user_ids,
            excluded_users=body.excluded_users,
            recent_chat_hours=body.recent_chat_hours,
            recent_chat_limit=body.recent_chat_limit,
            exclude_replied_hours=body.exclude_replied_hours,
            exclude_inbound_hours=body.exclude_inbound_hours,
            unread_limit=body.unread_limit,
            client=_get_client(),  # for the OF "Unread" inbox lookup
        )
        body.user_ids = resolved["included_users"]
        body.excluded_users = resolved["excluded_users"]
    # Excluded ids must leave `user_ids` HERE too — of_client subtracts them
    # from the wire `userIds` (OF has no per-user exclude field), so if we left
    # them in `body.user_ids` the mass_runs recipient_count + the optimistic
    # `messages` rows _close_mass_run writes would be PHANTOM (rows for fans OF
    # never messaged), and those outbound rows would then poison the contact
    # guard. Mirror the of_client subtraction so our record matches the send.
    if body.excluded_users and body.user_ids:
        _excl = {int(x) for x in body.excluded_users}
        body.user_ids = [u for u in body.user_ids if int(u) not in _excl]
    # OF cannot exclude individual ids from a list/online audience — the
    # `excludedUsers` body field doesn't exist (verified live 2026-06-12: it was
    # silently dropped and excluded fans got the blast). Mirror the ids into the
    # per-account Auto_Exclude OF list, which OF honors via `excludedLists`.
    # Explicit `user_ids` need no list — they're already stripped just above.
    # Done under the shared per-account broadcast lock so a concurrent
    # automation send can't rewrite the list mid-sync. Errors bubble up (500) —
    # a broadcast without its guard is the bug itself. (Known minor gap: the
    # lock is released before the send below, so a manual composer blast firing
    # in the same instant as an automation tick on the SAME account can race on
    # Auto_Exclude membership; automation<->automation sends hold it through.)
    if body.excluded_users and (body.user_lists or body.online_only or body.filters):
        from audiences import broadcast_lock, ensure_exclude_list
        _aid2 = _resolve_account_id(request)
        async with broadcast_lock(_aid2):
            auto_lid = await ensure_exclude_list(
                _aid2, body.excluded_users, client=_get_client())
        if auto_lid is not None and auto_lid not in (body.excluded_user_lists or []):
            body.excluded_user_lists = [*(body.excluded_user_lists or []), auto_lid]
    account_id, employee_id, mass_run_id = await _open_mass_run(
        request,
        user_lists=body.user_lists,
        included_users=body.user_ids,
        excluded_users=body.excluded_users,
        funnel_id=body.funnel_id,
    )
    client = _get_client()
    if body.scheduled_date:
        # Scheduled sends don't produce per-fan rows now — OF fires the
        # actual messages later. The mass_run_id is stamped on `mass_runs`
        # so the WS-pump reconciler (TODO inside _close_mass_run) can match
        # the eventual arrivals.
        result = await asyncio.to_thread(
            _proxy,
            lambda: client.schedule_mass_message(
                text=body.text, scheduled_date=body.scheduled_date,
                user_ids=body.user_ids, user_lists=body.user_lists,
                excluded_users=body.excluded_users,
                excluded_user_lists=body.excluded_user_lists,
                price=body.price,
                locked_text=body.locked_text, media_files=body.media_files,
                previews=body.previews,
                tagged_users=body.tagged_users,
                giphy_id=body.giphy_id,
                filters=body.filters,
                online_only=body.online_only,
            ),
        )
    else:
        result = await asyncio.to_thread(
            _proxy,
            lambda: client.send_mass_message(
                text=body.text,
                user_lists=body.user_lists,
                included_users=body.user_ids,
                excluded_users=body.excluded_users,
                excluded_user_lists=body.excluded_user_lists,
                price=body.price,
                locked_text=body.locked_text,
                media_files=body.media_files,
                previews=body.previews,
                tagged_users=body.tagged_users,
                giphy_id=body.giphy_id,
                filters=body.filters,
                online_only=body.online_only,
            ),
        )
        await _close_mass_run(
            account_id=account_id,
            employee_id=employee_id,
            mass_run_id=mass_run_id,
            text=body.text,
            price=body.price,
            result=result,
            recipient_ids=body.user_ids,
        )
        # Auto-unsend timer: schedule a one-shot `unsend_messages` job for the
        # forever-window mass unsend (DELETE /messages/queue/{id}) at send_time
        # + N hours. Picked up by the automation supervisor when due.
        queue_id = result.get("id") if isinstance(result, dict) else None
        if body.unsend_after_hours and queue_id:
            from automation_executor import enqueue_job
            run_at = datetime.utcnow() + timedelta(hours=body.unsend_after_hours)
            job_id = await enqueue_job(
                account_id, "unsend_messages",
                payload={"targets": [{"queue_id": int(queue_id),
                                      "mass_run_id": mass_run_id}]},
                run_at=run_at,
                created_by_employee_id=employee_id,
            )
            if isinstance(result, dict):
                result["_auto_unsend_at"] = run_at.isoformat() + "Z"
                result["_auto_unsend_job_id"] = job_id
    if mass_run_id is not None and isinstance(result, dict):
        result.setdefault("_mass_run_id", mass_run_id)
    return result

@app.delete("/api/of/v2/messages/queue/{queue_id}")
async def of_cancel_scheduled(request: Request, queue_id: int):
    """Cancel a previously scheduled message or unsend a mass broadcast.

    Both code paths share OF's wire endpoint:
      • scheduled (future) → cancel before it fires
      • mass broadcast (past) → unsend from EVERY recipient (no window)

    On success we flip `is_canceled=True` in the local mass-broadcast
    cache so the Settings → Mass messages list reflects the new state
    without waiting for the next refresh."""
    import mass_broadcast_cache as cache
    resp = await asyncio.to_thread(_proxy, lambda: _get_client().cancel_scheduled(queue_id))
    try:
        account_id = _resolve_account_id(request)
        await cache.mark_canceled(account_id, queue_id)
    except Exception as e:
        # Cache update is best-effort — the upstream cancel already
        # succeeded, the user shouldn't see a 500.
        log.warning("mass_broadcast_cache mark_canceled failed: %s", e)
    return resp

# Posts writes ------------------------------------------------------

@app.post("/api/of/v2/posts")
def of_create_post(body: _CreatePostBody = Body(...)):
    return _proxy(lambda: _get_client().create_post(
        text=body.text, media_files=body.media_files,
        price=body.price, posted_at=body.posted_at,
        tagged_users=body.tagged_users,
        giphy_id=body.giphy_id,
    ))

@app.put("/api/of/v2/posts/{post_id}")
def of_edit_post(post_id: int, body: _EditPostBody = Body(...)):
    return _proxy(lambda: _get_client().edit_post(
        post_id, text=body.text, price=body.price, media_files=body.media_files,
    ))

@app.delete("/api/of/v2/posts/{post_id}")
def of_delete_post(post_id: int):
    return _proxy(lambda: _get_client().delete_post(post_id))

@app.post("/api/of/v2/posts/{post_id}/like")
def of_like_post(post_id: int):
    return _proxy(lambda: _get_client().like_post(post_id))

@app.delete("/api/of/v2/posts/{post_id}/like")
def of_unlike_post(post_id: int):
    return _proxy(lambda: _get_client().unlike_post(post_id))

@app.post("/api/of/v2/posts/{post_id}/comments")
def of_comment_post(post_id: int, body: _TextBody = Body(...)):
    return _proxy(lambda: _get_client().comment_on_post(post_id, body.text))

@app.post("/api/of/v2/posts/{post_id}/pin")
def of_pin_post(post_id: int):
    return _proxy(lambda: _get_client().pin_post(post_id))

@app.delete("/api/of/v2/posts/{post_id}/pin")
def of_unpin_post(post_id: int):
    return _proxy(lambda: _get_client().unpin_post(post_id))

# List writes -------------------------------------------------------

@app.post("/api/of/v2/lists")
def of_create_list(body: _NameBody = Body(...)):
    return _proxy(lambda: _get_client().create_list(body.name))

# list_id is typed `str` (not `int`) so built-in lists like 'fans', 'recent',
# 'bookmarks' work alongside custom numeric ids. OF's API accepts both.

@app.patch("/api/of/v2/lists/{list_id}")
def of_rename_list(list_id: str, body: _NameBody = Body(...)):
    """Rename a list. OF uses PATCH (not PUT) — verified live."""
    return _proxy(lambda: _get_client().rename_list(list_id, body.name))

@app.delete("/api/of/v2/lists/{list_id}")
def of_delete_list(list_id: str):
    return _proxy(lambda: _get_client().delete_list(list_id))

@app.post("/api/of/v2/lists/{list_id}/users/{user_id}")
def of_add_user_to_list(list_id: str, user_id: int):
    return _proxy(lambda: _get_client().add_user_to_list(list_id, user_id))

@app.delete("/api/of/v2/lists/{list_id}/users/{user_id}")
def of_remove_user_from_list(list_id: str, user_id: int):
    return _proxy(lambda: _get_client().remove_user_from_list(list_id, user_id))

# Labels writes -----------------------------------------------------

@app.post("/api/of/v2/labels/{label_id}/users/{user_id}")
def of_add_label_to_user(label_id: int, user_id: int):
    return _proxy(lambda: _get_client().add_label_to_user(label_id, user_id))

@app.delete("/api/of/v2/labels/{label_id}/users/{user_id}")
def of_remove_label_from_user(label_id: int, user_id: int):
    return _proxy(lambda: _get_client().remove_label_from_user(label_id, user_id))

# Subscriber tools — FOUND via Playwright UI capture.
# OF uses ONE endpoint PUT /subscriptions/{id} with body containing either
# `notice` (fan note) or `displayName` (custom nickname) — or both.

class _NoteBody(BaseModel):
    note: str

class _CustomNameBody(BaseModel):
    name: str = Field(..., description="Custom display name; empty string clears it")

class _SubscriptionPatchBody(BaseModel):
    notice: str | None = None
    displayName: str | None = None

def _bust_chat_list_cache() -> None:
    """Drop the account's cached `list_chats` so a nickname/note change shows on the
    inbox rail immediately instead of after the 10-min TTL. The chat list's per-fan
    `displayName` is overlaid from the local mirror / OF, so a stale cached page keeps
    rendering the old name (the "<name>/Whale" drawers stayed stale after a gen_info
    rewrite). The drawer's own read (get_user) is live + coalesced, so it needs no bust."""
    try:
        aid = _resolve_account_id(_request_ctx.get())
        relay_cache.invalidate("list_chats", aid)
    except Exception as e:  # noqa: BLE001 — cache bust must never break the write response
        log.warning("relay_cache: chat-list invalidate after subscription write failed: %s", e)

@app.put("/api/of/v2/subscriptions/{user_id}/note")
def of_set_fan_note(user_id: int, body: _NoteBody = Body(...)):
    """Set the creator-side private note on a fan. Send empty string to clear."""
    result = _proxy(lambda: _get_client().set_fan_note(user_id, body.note))
    _bust_chat_list_cache()
    return result

@app.put("/api/of/v2/subscriptions/{user_id}/custom-name")
def of_set_fan_custom_name(user_id: int, body: _CustomNameBody = Body(...)):
    """Set the custom nickname for a fan. Send empty string to clear."""
    result = _proxy(lambda: _get_client().set_fan_custom_name(user_id, body.name))
    _bust_chat_list_cache()
    return result

@app.put("/api/of/v2/subscriptions/{user_id}")
def of_update_subscription(user_id: int, body: _SubscriptionPatchBody = Body(...)):
    """Generic update: set notice and/or displayName together in one call."""
    payload = {k: v for k, v in body.model_dump().items() if v is not None}
    result = _proxy(lambda: _get_client().update_subscription(user_id, **payload))
    _bust_chat_list_cache()
    return result

# Media upload -----------------------------------------------------
# Three-step flow: dedupe-hash check → POST /upload/signed/create →
# PUT bytes to presigned S3 URL → returns vault media id we can send in
# subsequent /chats/{id}/messages or /posts with mediaFiles=[id].

from fastapi import UploadFile, File

@app.post("/api/of/v2/upload")
async def of_upload_media(file: UploadFile = File(...)):
    """Upload a file to OF's vault. Multipart form field name: `file`.
    Returns {media_id, deduped, size, filename, ...}.
    The returned `media_id` is usable as mediaFiles=[id] in send/post calls
    after OF's transcoder finishes (typically 5-30s for images)."""
    import tempfile, os, shutil
    suffix = os.path.splitext(file.filename or "upload.bin")[1] or ".bin"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        shutil.copyfileobj(file.file, tmp)
        tmp_path = tmp.name
    try:
        result = _proxy(lambda: _get_client().upload_media(
            tmp_path,
            content_type=file.content_type,
        ))
        # New vault item — every cached page is now potentially stale.
        # Drop the whole account's cache so the next picker open re-fetches.
        try:
            aid = _resolve_account_id(_request_ctx.get())
            await vault_cache.invalidate(aid)
        except Exception as e:  # noqa: BLE001 — cache invalidation must never block the upload response
            log.warning("vault_cache: invalidate-on-upload failed: %s", e)
        return result
    finally:
        try: os.unlink(tmp_path)
        except OSError: pass


# Stories writes ----------------------------------------------------
# /stories does NOT accept a vault media id — even "pick from vault" in the
# UI re-uploads bytes through the convert pipeline. So we fetch the image
# (a vault item's files.full.url, or any CDN/image url), re-upload via
# upload_media(), then POST the convert claim to /stories. See
# of_client.post_story_from_url for the captured flow.

class _CreateStoryBody(BaseModel):
    media_url: str | None = Field(
        None, description="Image URL to download + repost (vault files.full.url / any CDN url)")
    media_files: list | None = Field(
        None, description="Already-prepared mediaFiles (send_with from POST /upload)")
    watermark_text: str | None = None
    # Optional overlays — replayed at the captured editor coordinates.
    caption: str | None = Field(None, description="Text overlay")
    mention: str | None = Field(None, description="@username tag (with or without @)")
    question: str | None = Field(None, description="Question sticker prompt")

@app.post("/api/of/v2/stories")
async def of_create_story(body: _CreateStoryBody = Body(...)):
    """Publish an image as a story, optionally with a caption / @mention tag /
    question sticker (placed at the captured editor coordinates).

    Two input modes:
      • media_url  → download bytes, re-upload through OF's convert pipeline,
                     then post (the "from vault / from CDN" path).
      • media_files → already-prepared claim from POST /api/of/v2/upload
                     (its `send_with`); posted as-is.
    """
    if body.media_files:
        return _proxy(lambda: _get_client().create_story(
            body.media_files,
            caption=body.caption, mention=body.mention, question=body.question,
        ))
    if not body.media_url:
        raise HTTPException(400, "provide media_url or media_files")
    return _proxy(lambda: _get_client().post_story_from_url(
        body.media_url, watermark_text=body.watermark_text,
        caption=body.caption, mention=body.mention, question=body.question,
    ))

@app.post("/api/of/v2/stories/upload")
async def of_create_story_upload(file: UploadFile = File(...)):
    """Publish a directly-uploaded file as a story. Multipart field: `file`."""
    import tempfile, os, shutil
    suffix = os.path.splitext(file.filename or "story.jpg")[1] or ".jpg"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        shutil.copyfileobj(file.file, tmp)
        tmp_path = tmp.name
    try:
        return _proxy(lambda: _get_client().post_story(
            tmp_path, content_type=file.content_type,
        ))
    finally:
        try: os.unlink(tmp_path)
        except OSError: pass


# Tips (MOVES MONEY — not auto-tested) ------------------------------

@app.post("/api/of/v2/tips")
def of_send_tip(body: _TipBody = Body(...)):
    """Send a tip. Touches real money — verify carefully before exposing in UI."""
    return _proxy(lambda: _get_client().send_tip(body.user_id, body.amount, message=body.message))


# ── Admin ──────────────────────────────────────────────────────

@app.post("/admin/reload-session")
def reload_session(account_id: str | None = Query(None)) -> dict[str, Any]:
    """Hot-swap an account's OFClient (re-read its latest session from disk).
    `account_id` defaults to the currently-active account."""
    aid = account_id or account_registry.get_active_account_id()
    if not aid:
        raise HTTPException(status_code=503, detail="no active account")
    _invalidate_client(aid)
    c = _load_client(aid)
    return {"ok": True, "account_id": aid, "user_id": c.user_id, "x_of_rev": c.x_of_rev}


@app.get("/admin/rev/live")
def admin_rev_live(refresh: bool = Query(False, description="Force a fresh probe instead of using the cached value")) -> dict[str, Any]:
    """Current OF frontend build hash (x-of-rev) as seen by an unauthenticated
    homepage fetch. Cached in-process with a 10-minute TTL; pass `?refresh=1`
    to bust the cache. UI uses this to flag sessions stuck at an old rev."""
    snap = live_rev.refresh(force=True) if refresh else live_rev.get()
    return snap


@app.get("/admin/rev/drift")
def admin_rev_drift() -> dict[str, Any]:
    """Per-account drift report. For each account with a captured session,
    compare its stored x-of-rev against the live homepage probe. The UI's
    'session stale — re-capture required' banner reads this endpoint on
    page load and after any /admin/reload-session call."""
    live_snap = live_rev.get()
    live = live_snap.get("rev")
    rows: list[dict[str, Any]] = []
    any_drift = False
    for meta in account_registry.list_accounts():
        aid = meta["id"]
        if not meta.get("has_session"):
            rows.append({
                "account_id": aid, "nickname": meta.get("nickname"),
                "has_session": False, "session_rev": None,
                "drift": False, "stale": True, "reason": "no_session",
            })
            continue
        sp = account_registry.latest_session_path(aid)
        try:
            s = json.loads(sp.read_text()) if sp else {}
        except Exception:
            s = {}
        session_rev = (s.get("headers") or {}).get("x_of_rev")
        cmp = live_rev.compare(session_rev)
        row = {
            "account_id": aid,
            "nickname": meta.get("nickname"),
            "has_session": True,
            "session_rev": session_rev,
            "live_rev": cmp.get("live_rev"),
            "live_known": cmp.get("live_known"),
            "drift": cmp.get("drift", False),
            "stale": cmp.get("drift", False),
            "captured_at": s.get("captured_at"),
        }
        if cmp.get("drift"):
            any_drift = True
        rows.append(row)
    return {
        "live_rev": live,
        "live_known": bool(live),
        "live_fetched_at": live_snap.get("fetched_at"),
        "live_error": live_snap.get("error"),
        "any_drift": any_drift,
        "accounts": rows,
    }


@app.get("/admin/session/status")
def session_status(account_id: str | None = Query(None)) -> dict[str, Any]:
    """Snapshot of an account's current session. Defaults to the active one."""
    aid = account_id or account_registry.get_active_account_id()
    if not aid:
        return {"loaded": False, "remedy": "No account yet — POST /admin/session/bootstrap"}
    sp = account_registry.latest_session_path(aid)
    if not sp:
        return {"loaded": False, "account_id": aid,
                "remedy": f"Account {aid} has no captured session yet"}
    s = json.loads(sp.read_text())
    return {
        "loaded": True,
        "account_id": aid,
        "session_file": sp.name,
        "captured_at": s.get("captured_at"),
        "user_id": s.get("headers", {}).get("user_id"),
        "x_of_rev": s.get("headers", {}).get("x_of_rev"),
        "profile_id": s.get("profile_id"),
        "cookies_count": len(s.get("cookies", [])),
    }


class _BootstrapBody(BaseModel):
    mode: str = Field(..., description="'incogniton-default' | 'incogniton-custom' | 'paste-curl' | 'playwright-proxy'")
    profile_id: str | None = Field(None, description="Required for mode=incogniton-custom")
    curl: str | None = Field(None, description="Required for mode=paste-curl — `curl …` from DevTools")
    static_param_override: str | None = Field(None, description="Optional override for an unfamiliar OF revision")
    # Multi-account additions: optional hint/nickname and whether to flip the
    # newly-captured account to active. account_id is a hint only — the real
    # account is always determined by the captured user_id (OF's identity).
    account_id: str | None = Field(None, description="Hint: re-capture for this account (uses its stored Incogniton profile / proxy)")
    nickname: str | None = Field(None, description="Friendly label persisted to the account meta")
    make_active: bool = Field(True, description="Flip the relay's active account to the newly-captured one")
    # playwright-proxy mode: which proxy (from the registry) to route the
    # capture browser through. The new account auto-inherits this binding.
    proxy_label: str | None = Field(None, description="Required for mode=playwright-proxy unless account_id already has a proxy bound")


@app.post("/admin/session/bootstrap")
def bootstrap_session(body: _BootstrapBody = Body(...)) -> dict[str, Any]:
    """Set up an OF session. Modes:

    - **incogniton-default**: capture via the default Incogniton profile id
      (env INCOGNITON_PROFILE_ID or hard-coded fallback).
    - **incogniton-custom**: capture via a profile id you pass in.
    - **paste-curl**: paste a `curl ...` command copied from DevTools
      (right-click a `/api2/v2/*` request → Copy as cURL). We extract
      cookies + signed-header sample, fetch 2313.js, derive rules.

    Each captured session is written into its account's directory
    (sessions/accounts/<user_id>/). Pass `account_id` to re-capture an
    existing one (uses the stored Incogniton profile if you don't override).
    `nickname` is the friendly label shown in the UI switcher."""
    import session_bootstrap
    try:
        if body.mode == "incogniton-default":
            path = session_bootstrap.run_incogniton(
                None, account_id=body.account_id, nickname=body.nickname,
                make_active=body.make_active,
            )
        elif body.mode == "incogniton-custom":
            if not body.profile_id:
                raise HTTPException(status_code=400, detail="profile_id required for incogniton-custom")
            path = session_bootstrap.run_incogniton(
                body.profile_id, account_id=body.account_id, nickname=body.nickname,
                make_active=body.make_active,
            )
        elif body.mode == "paste-curl":
            if not body.curl:
                raise HTTPException(status_code=400, detail="curl text required for paste-curl")
            path = session_bootstrap.from_curl(
                body.curl,
                static_param_override=body.static_param_override,
                account_id=body.account_id, nickname=body.nickname,
                make_active=body.make_active,
            )
        elif body.mode == "playwright-proxy":
            if not body.proxy_label and not body.account_id:
                raise HTTPException(
                    status_code=400,
                    detail="playwright-proxy mode requires either proxy_label or an account_id "
                           "that already has a proxy assigned",
                )
            path = session_bootstrap.run_playwright_proxy(
                body.proxy_label,
                account_id=body.account_id,
                nickname=body.nickname,
                make_active=body.make_active,
            )
        else:
            raise HTTPException(status_code=400, detail=f"unknown mode: {body.mode}")
    except session_bootstrap.CurlParseError as e:
        raise HTTPException(status_code=400, detail=f"curl parse error: {e}")
    except HTTPException:
        raise
    except Exception as e:
        log.exception("bootstrap failed")
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")

    # Resolve which account dir we ended up in (path = .../accounts/<id>/session_*.json)
    actual_aid = path.parent.name
    _invalidate_client(actual_aid)
    c = _load_client(actual_aid)
    # Kick the WS pump too so the new cookies are used immediately.
    _restart_account_pump(actual_aid)
    # Phase A: mirror the new on-disk state into SQL. Fire-and-forget so a
    # slow DB doesn't delay the user's confirmation.
    _kick_db_sync(f"bootstrap-{actual_aid}")
    # Link the captured OF account to the signed-in friend, so it shows up
    # in their ScopeSwitcher + passes the ownership gate. No-op if unauthed
    # (founder running curl flows pre-auth) — the account remains visible
    # in that path because _resolve_account_id skips the gate when there's
    # no request user.
    _link_account_to_request_user(actual_aid)
    return {
        "ok": True,
        "session_file": path.name,
        "account_id": actual_aid,
        "user_id": c.user_id,
        "x_of_rev": c.x_of_rev,
    }


@app.post("/admin/session/wipe-fresh-browser-buckets")
def wipe_fresh_browser_buckets() -> dict[str, Any]:
    """Delete every `service/sessions/browser_profiles/fresh-*/` directory.

    Each click of "Launch browser via proxy" (no `account_id` hint) creates
    a fresh-<ts>-<uuid> bucket holding that Chromium's persistent profile
    (cookies, localStorage, captcha-solved flag). The captured session is
    already adopted into `sessions/accounts/<user_id>/` before we return —
    so the bucket itself is disposable. This endpoint frees the disk and
    guarantees no leftover logged-in state is sitting around.

    Untouched: account-id-bucketed dirs (e.g. `446300082/`) and the
    legacy `unbound/` / proxy-label dirs (`hu-1`, `hu-3`, ...). Those are
    still re-usable for warmed-up re-captures.
    """
    import shutil
    base = Path(__file__).resolve().parent / "sessions" / "browser_profiles"
    if not base.exists():
        return {"ok": True, "wiped": [], "skipped": []}
    wiped: list[str] = []
    errors: list[dict[str, str]] = []
    for entry in base.iterdir():
        if not entry.is_dir():
            continue
        if not entry.name.startswith("fresh-"):
            continue
        try:
            shutil.rmtree(entry)
            wiped.append(entry.name)
        except OSError as e:
            errors.append({"bucket": entry.name, "error": str(e)})
    return {"ok": not errors, "wiped": wiped, "errors": errors}


# ── Multi-account admin ───────────────────────────────────────

@app.get("/admin/accounts")
def admin_accounts_list(response: Response) -> dict[str, Any]:
    """List every account the signed-in user owns + the currently-active one
    (clamped to the user's set). The UI populates its switcher from this.

    Account lists are PER-PRINCIPAL and must never be cached — a response
    fetched in one auth state must not be replayed in another — hence
    `no-store`. An UNauthenticated caller gets an EMPTY list, never the full
    multi-tenant registry: that fallback used to leak every owner's models,
    get cached by the client, and then 403 on save ("account X is not one of
    yours"). Sign in to get the real, `user_accounts`-scoped list."""
    response.headers["Cache-Control"] = "no-store"
    user = _get_request_user()
    if user is None:
        # Anonymous caller — expose nothing. Authentication yields the
        # real, scoped list; leaking the registry here poisoned client
        # caches across owners.
        return {"accounts": [], "active_account_id": None}
    all_accounts = [
        a for a in account_registry.list_accounts() if a.get("id") in user.account_ids
    ]
    active = account_registry.get_active_account_id()
    if active not in user.account_ids:
        active = all_accounts[0]["id"] if all_accounts else None
    return {
        "accounts": all_accounts,
        "active_account_id": active,
    }


class _ActivateBody(BaseModel):
    account_id: str | None


@app.post("/admin/accounts/active")
def admin_accounts_activate(body: _ActivateBody = Body(...)) -> dict[str, Any]:
    """Flip which account is the default (no X-Account-Id header → this one)."""
    if body.account_id is not None and account_registry.get_account(body.account_id) is None:
        raise HTTPException(status_code=404, detail=f"unknown account {body.account_id!r}")
    account_registry.set_active_account_id(body.account_id)
    _kick_db_sync("accounts-active")
    return {"ok": True, "active_account_id": body.account_id}


class _AccountUpdateBody(BaseModel):
    nickname: str | None = None
    color: str | None = None
    incogniton_profile_id: str | None = None


@app.patch("/admin/accounts/{account_id}")
def admin_accounts_update(account_id: str, body: _AccountUpdateBody = Body(...)) -> dict[str, Any]:
    """Rename / recolor / re-link Incogniton profile. Doesn't touch sessions."""
    if account_registry.get_account(account_id) is None:
        raise HTTPException(status_code=404, detail=f"unknown account {account_id!r}")
    meta = account_registry.upsert_account(
        account_id,
        nickname=body.nickname,
        color=body.color,
        incogniton_profile_id=body.incogniton_profile_id,
    )
    _kick_db_sync(f"accounts-update-{account_id}")
    return {"ok": True, "account": meta}


@app.delete("/admin/accounts/{account_id}")
def admin_accounts_remove(account_id: str) -> dict[str, Any]:
    """Permanently remove an account dir (sessions + meta). Stops its WS pump
    and drops its pooled client. Cannot be undone."""
    if account_registry.get_account(account_id) is None:
        raise HTTPException(status_code=404, detail=f"unknown account {account_id!r}")
    _stop_account_pump(account_id)
    _invalidate_client(account_id)
    ok = account_registry.delete_account(account_id)
    _kick_db_sync(f"accounts-delete-{account_id}")
    return {"ok": ok}


# ── Proxy registry admin ──────────────────────────────────────
# Phase-1 (DC testing): creds are returned in plaintext on purpose so the UI
# can show host:port:user:pass. Move to keychain in Phase 2.

class _ProxyBody(BaseModel):
    label: str
    host: str
    port: int
    username: str | None = None
    password: str | None = None
    scheme: str = "http"
    notes: str = ""


class _AssignBody(BaseModel):
    label: str
    # Legacy: bind to a specific session file. Kept for back-compat.
    session_file: str | None = Field(None, description="LEGACY: session_*.json filename; prefer account_id")
    # Preferred: bind to an OF account (survives re-captures).
    account_id: str | None = Field(None, description="OF account id to bind this proxy to (null to unassign)")


@app.get("/admin/proxies")
def admin_proxies_list() -> dict[str, Any]:
    """Return proxies + the account list the UI populates its 'assign to'
    picker from. Each proxy is enriched with `assigned_accounts` (a list of
    {id, nickname, color}). A proxy may serve many accounts — the UI flags
    >1 as a shared-egress warning because OF can correlate same-IP behaviour.

    Per-user filtering: a signed-in friend sees only proxies they own
    (`user_id == them`) plus shared defaults (`user_id is None`). The
    `accounts` picker list and each proxy's `assigned_accounts` are
    clamped to accounts the viewer owns so we don't leak nicknames of
    other friends' accounts via the proxy chip render path.

    `assigned_account` (singular, deprecated) and `assigned_account_id`
    (legacy scalar) are mirrored from the list's first entry so older
    callers keep working until they migrate."""
    user = _get_request_user()
    proxies_list = proxy_registry.list_proxies()
    accounts = account_registry.list_accounts()
    if user is not None:
        accounts = [a for a in accounts if a["id"] in user.account_ids]
    by_aid = {a["id"]: a for a in accounts}
    enriched = []
    for p in proxies_list:
        if user is not None:
            pu = p.get("user_id")
            if pu is not None and pu != user.id:
                continue
        out = dict(p)
        ids = proxy_registry._ids_on(p)
        bound = [
            {
                "id": aid,
                "nickname": by_aid[aid].get("nickname"),
                "color": by_aid[aid].get("color"),
            }
            for aid in ids if aid in by_aid
        ]
        out["assigned_accounts"] = bound
        out["assigned_account_ids"] = [b["id"] for b in bound]
        # Legacy singular: mirror first entry so old consumers don't break.
        out["assigned_account"] = bound[0] if bound else None
        out["assigned_account_id"] = bound[0]["id"] if bound else None
        out["url"] = proxy_registry.proxy_url(p)
        enriched.append(out)
    return {
        "proxies": enriched,
        # Slim account list shaped the way the picker wants it.
        "accounts": [{
            "id": a["id"], "nickname": a.get("nickname"),
            "color": a.get("color"), "has_session": a.get("has_session", False),
        } for a in accounts],
    }


def _assert_proxy_owned_or_mine(label: str) -> dict[str, Any]:
    """Look up a proxy by label and verify the signed-in user may mutate it.

    Shared defaults (`user_id is None`) are read-only for friends — they
    can't reassign or delete the 3 hu-* entries we ship by default.
    Friend-owned proxies (`user_id == me`) are freely mutable by that
    friend. Unauthed requests (cron, share-token bootstrap) bypass.
    """
    p = proxy_registry.get_by_label(label)
    if p is None:
        raise HTTPException(status_code=404, detail=f"no proxy with label {label!r}")
    user = _get_request_user()
    if user is None:
        return p
    pu = p.get("user_id")
    if pu is None:
        raise HTTPException(status_code=403, detail="shared default proxy is read-only")
    if pu != user.id:
        raise HTTPException(status_code=403, detail="not your proxy")
    return p


def _assert_proxy_assignable(label: str) -> dict[str, Any]:
    """Visibility check for binding/unbinding an account to a proxy.

    Distinct from `_assert_proxy_owned_or_mine`, which gates mutation of the
    proxy *entity* (upsert/delete) and keeps shared defaults read-only.
    Attaching YOUR OWN account to a shared-default proxy is the whole point
    of shipping shared defaults, so it's allowed here — account ownership is
    enforced separately by `assert_account_owned`, so a friend can only ever
    bind/unbind an account they own. We still 404 another friend's private
    proxy (they can't see it, so they can't bind to it by guessing a label).
    Unauthed requests (cron, share-token bootstrap) bypass.
    """
    p = proxy_registry.get_by_label(label)
    if p is None:
        raise HTTPException(status_code=404, detail=f"no proxy with label {label!r}")
    user = _get_request_user()
    if user is None:
        return p
    pu = p.get("user_id")
    if pu is not None and pu != user.id:
        raise HTTPException(status_code=404, detail=f"no proxy with label {label!r}")
    return p


@app.post("/admin/proxies")
def admin_proxies_upsert(body: _ProxyBody = Body(...)) -> dict[str, Any]:
    payload = body.model_dump()
    user = _get_request_user()
    existing = proxy_registry.get_by_label(payload.get("label", ""))
    if user is not None:
        if existing is not None:
            eu = existing.get("user_id")
            if eu is None:
                raise HTTPException(status_code=403, detail="shared default proxy is read-only")
            if eu != user.id:
                raise HTTPException(status_code=403, detail="not your proxy")
            # Preserve owner — don't let a payload field reassign ownership.
            payload["user_id"] = eu
        else:
            # New proxy created by a signed-in friend → tag with their id.
            payload["user_id"] = user.id
    try:
        entry = proxy_registry.upsert(payload)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    _kick_db_sync(f"proxy-upsert-{body.label}")
    return {"ok": True, "proxy": entry}


@app.delete("/admin/proxies/{label}")
def admin_proxies_remove(label: str) -> dict[str, Any]:
    _assert_proxy_owned_or_mine(label)
    ok = proxy_registry.remove(label)
    if not ok:
        raise HTTPException(status_code=404, detail=f"no proxy with label {label!r}")
    # Proxy assignment is per session-file, but a session-file is owned by
    # exactly one account → invalidating the whole pool is the simplest
    # correct option (one network round-trip on next request per account).
    _clients.clear()
    _kick_db_sync(f"proxy-delete-{label}")
    return {"ok": True}


@app.post("/admin/proxies/assign")
def admin_proxies_assign(body: _AssignBody = Body(...)) -> dict[str, Any]:
    """Bind a proxy to an account (preferred) or to a specific session file
    (legacy). Pass `account_id: null` to unassign the account binding.

    Whichever scheme is used, all pooled clients are dropped so the affected
    account picks up the new proxy on its next request."""
    _assert_proxy_assignable(body.label)
    if body.account_id is not None:
        assert_account_owned(body.account_id)
    try:
        # If the caller specified account_id (even as null), treat as account
        # binding — even an explicit null is a "please unassign" signal.
        if body.account_id is not None or "account_id" in body.model_fields_set:
            entry = proxy_registry.assign_account(body.label, body.account_id)
        else:
            entry = proxy_registry.assign(body.label, body.session_file)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    _clients.clear()
    _kick_db_sync(f"proxy-assign-{body.label}")
    return {"ok": True, "proxy": entry}


class _UnbindBody(BaseModel):
    account_id: str


@app.post("/admin/proxies/{label}/unbind")
def admin_proxies_unbind(label: str, body: _UnbindBody = Body(...)) -> dict[str, Any]:
    """Remove a single account from a proxy's binding list. The account
    falls back to direct egress until reassigned. Distinct from `/assign`
    (which now appends rather than replaces) — needed because multi-binding
    made the old `account_id: null` unbind gesture ambiguous (unbind WHICH
    account from the list?)."""
    _assert_proxy_assignable(label)
    assert_account_owned(body.account_id)
    try:
        entry = proxy_registry.unbind_account(label, body.account_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    _clients.clear()
    _kick_db_sync(f"proxy-unbind-{label}-{body.account_id}")
    return {"ok": True, "proxy": entry}


@app.post("/admin/proxies/{label}/test")
def admin_proxies_test(label: str) -> dict[str, Any]:
    # Read-only probe — fine to run against any proxy the user can see
    # (own + shared defaults). Just blocks "test some other friend's
    # proxy by label" since they can't see it in the first place.
    user = _get_request_user()
    p = proxy_registry.get_by_label(label)
    if p is None:
        raise HTTPException(status_code=404, detail=f"no proxy with label {label!r}")
    if user is not None:
        pu = p.get("user_id")
        if pu is not None and pu != user.id:
            raise HTTPException(status_code=404, detail=f"no proxy with label {label!r}")
    result = proxy_registry.probe(p)
    proxy_registry.record_verification(
        label, ip=result.get("ip"), geo=result.get("geo"), ok=result.get("ok", False),
    )
    return result


# ── Realtime: WS fan-out, stats, webhook config ────────────────

@app.get("/events", include_in_schema=False)
async def sse_events(request: Request, scope: str = Query("all")):
    """Server-Sent Events stream for the new browser app.

    Mirror of `/ws/events` but over SSE so the client uses native
    EventSource (one-way, auto-reconnect, plays well with corporate
    proxies). Scope grammar:
      ?scope=all                    — every event from every account
      ?scope=model:<account_id>     — only that account

    Phase B will add `Last-Event-ID` replay from event_inbox so a
    reconnecting client catches up on missed events. Phase A: starts
    from "now."
    """
    return StreamingResponse(
        sse_stream(request, scope),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",  # tell nginx/cloudflare to flush, not buffer
            "Connection": "keep-alive",
        },
    )


@app.websocket("/ws/events")
async def ws_events(websocket: WebSocket) -> None:
    """Browser-facing WebSocket. Every OF event that arrives over any account
    pump is forwarded as a JSON text frame.

    Optional `?account_id=...` query param filters to that account only.
    Without it, the subscriber receives events from every account (with
    the `__account_id` / `__account_name` tags intact).
    """
    await websocket.accept()
    only_account = websocket.query_params.get("account_id") or None
    q: asyncio.Queue = asyncio.Queue(maxsize=500)
    _event_subscribers.add(q)
    try:
        await websocket.send_json({
            "__ready": True,
            "subscribers": len(_event_subscribers),
            "filter_account_id": only_account,
            "accounts": [{"id": a["id"], "nickname": a["nickname"], "color": a.get("color")}
                         for a in account_registry.list_accounts()],
        })
        while True:
            event = await q.get()
            if only_account and isinstance(event, dict) \
                    and event.get("__account_id") not in (None, only_account):
                continue
            await websocket.send_json(event)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.warning("ws client error: %s", e)
    finally:
        _event_subscribers.discard(q)


@app.get("/admin/events/stats")
def event_stats() -> dict[str, Any]:
    """How many events the pumps have seen, broken down by type + account.
    Useful to confirm each account's WS is alive without subscribing."""
    pumps = {
        aid: {"running": not t.done(), "cancelled": t.cancelled() if t.done() else False}
        for aid, t in _account_pumps.items()
    }
    return {
        **_event_stats,
        "subscribers": len(_event_subscribers),
        "pumps": pumps,
        "pumps_running": sum(1 for p in pumps.values() if p["running"]),
        "supervisor_running": (_supervisor_task is not None and not _supervisor_task.done()),
    }


class _WebhookBody(BaseModel):
    url: str = Field(..., description="HTTP(S) URL to POST every matched event as JSON.")
    event_types: list[str] = Field(
        default_factory=lambda: ["*"],
        description="Event keys to match (e.g. ['api2_chat_message','new_message']). '*' = all events.",
    )


@app.get("/admin/webhooks")
def list_webhooks() -> dict[str, list[str]]:
    """Return the per-event-type → [url,...] config."""
    return _load_webhooks()


@app.post("/admin/webhooks")
def add_webhook(body: _WebhookBody = Body(...)) -> dict[str, Any]:
    """Register a webhook URL for one or more event types ('*' = catch-all)."""
    cfg = _load_webhooks()
    for kind in body.event_types or ["*"]:
        urls = cfg.setdefault(kind, [])
        if body.url not in urls:
            urls.append(body.url)
    _save_webhooks(cfg)
    return {"ok": True, "config": cfg}


@app.delete("/admin/webhooks")
def delete_webhook(url: str = Query(...), event_type: str = Query("*")) -> dict[str, Any]:
    """Remove a webhook URL from one event type, or '*' to scrub it from all."""
    cfg = _load_webhooks()
    if event_type == "*":
        for kind in list(cfg.keys()):
            cfg[kind] = [u for u in cfg[kind] if u != url]
            if not cfg[kind]:
                del cfg[kind]
    else:
        if event_type in cfg:
            cfg[event_type] = [u for u in cfg[event_type] if u != url]
            if not cfg[event_type]:
                del cfg[event_type]
    _save_webhooks(cfg)
    return {"ok": True, "config": cfg}


# ── Saved replies (local-only "templates" minus welcome) ──────────────
# OF's /messages/templates rejects creates for everything except the
# welcome slot. We keep regular saved replies in the local DB and let
# the UI merge them with OF's welcome at render time.

class _SavedReplyBody(BaseModel):
    title: str | None = None
    text: str
    price: float = Field(0, ge=0)
    locked_text: bool = False
    # Vault-media references, persisted as JSON so the editor can
    # re-render thumbs without a second fetch.
    media: list[dict] = Field(default_factory=list)
    # OF user ids of creators to @-tag when this template is applied.
    # Persisted so the picker can re-hydrate tagging without re-querying
    # OF's tagged-friend-users endpoint on every pick.
    tagged_users: list[int] = Field(default_factory=list)
    # Subset of `media`'s vault ids that ride along UNLOCKED when `price > 0`.
    # Order does not matter; OF treats this as a set.
    previews: list[int] = Field(default_factory=list)
    # Optional Giphy id stored on the template. NULL/None = no GIF. When set,
    # the picker seeds the composer's picked-GIF state on pick so the GIF
    # rides along on send.
    gif_id: str | None = None
    # Cached animated preview URL so the picker chip can render without a
    # fresh Giphy roundtrip.
    gif_url: str | None = None
    # Optional script grouping: when both `script_id` (free-text name) and
    # `script_step` (1-based order) are set, sending this template advances
    # a per-chat cursor; the composer then surfaces the next step in the
    # same script as a one-tap suggestion bubble. NULL on either field opts
    # the template out of the script flow.
    script_id: str | None = None
    script_step: int | None = None


def _serialize_saved_reply(row: Any) -> dict[str, Any]:
    return {
        "id": row.id,
        "account_id": row.account_id,
        "title": row.title,
        "text": row.text,
        "price": (row.price_cents or 0) / 100,
        "locked_text": bool(row.locked_text),
        "media": json.loads(row.media_json or "[]"),
        "tagged_users": json.loads(getattr(row, "tagged_users_json", None) or "[]"),
        "previews": json.loads(getattr(row, "previews_json", None) or "[]"),
        "gif_id": getattr(row, "gif_id", None),
        "gif_url": getattr(row, "gif_url", None),
        "script_id": getattr(row, "script_id", None),
        "script_step": getattr(row, "script_step", None),
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


@app.get("/admin/saved-replies")
async def admin_saved_replies_list(account_id: str = Query(...)) -> dict[str, Any]:
    """Saved replies for one account. Sorted newest-edit first."""
    assert_account_owned(account_id)
    from db.engine import get_session
    from db.models import SavedReply
    from sqlalchemy import select
    async with get_session() as s:
        result = await s.execute(
            select(SavedReply)
            .where(SavedReply.account_id == account_id)
            .order_by(SavedReply.updated_at.desc()),
        )
        rows = result.scalars().all()
        return {"list": [_serialize_saved_reply(r) for r in rows]}


@app.post("/admin/saved-replies")
async def admin_saved_replies_create(
    body: _SavedReplyBody = Body(...),
    account_id: str = Query(...),
) -> dict[str, Any]:
    """Create a new saved reply for an account."""
    assert_account_owned(account_id)
    from db.engine import get_session
    from db.models import SavedReply
    async with get_session() as s:
        row = SavedReply(
            account_id=account_id,
            title=body.title,
            text=body.text,
            price_cents=int(round(body.price * 100)),
            locked_text=body.locked_text,
            media_json=json.dumps(body.media),
            tagged_users_json=json.dumps([int(u) for u in body.tagged_users]),
            previews_json=json.dumps([int(p) for p in body.previews]),
            gif_id=body.gif_id or None,
            gif_url=body.gif_url or None,
            script_id=body.script_id or None,
            script_step=body.script_step,
        )
        s.add(row)
        await s.commit()
        await s.refresh(row)
        return _serialize_saved_reply(row)


@app.put("/admin/saved-replies/{reply_id}")
async def admin_saved_replies_update(
    reply_id: int,
    body: _SavedReplyBody = Body(...),
) -> dict[str, Any]:
    """Replace a saved reply's contents wholesale (no PATCH semantics — UI
    submits the full draft on every save, matching the editor's mental model)."""
    from db.engine import get_session
    from db.models import SavedReply
    from datetime import datetime as _dt
    async with get_session() as s:
        row = await s.get(SavedReply, reply_id)
        if not row:
            raise HTTPException(status_code=404, detail="saved reply not found")
        # Defense-in-depth: the row's account_id must be in the active
        # principal's allowed set. Without this, a chatter linked to
        # @alice could PUT a reply that belongs to @bob just by guessing
        # the reply_id.
        assert_account_owned(row.account_id)
        row.title = body.title
        row.text = body.text
        row.price_cents = int(round(body.price * 100))
        row.locked_text = body.locked_text
        row.media_json = json.dumps(body.media)
        row.tagged_users_json = json.dumps([int(u) for u in body.tagged_users])
        row.previews_json = json.dumps([int(p) for p in body.previews])
        row.gif_id = body.gif_id or None
        row.gif_url = body.gif_url or None
        row.script_id = body.script_id or None
        row.script_step = body.script_step
        row.updated_at = _dt.utcnow()
        await s.commit()
        await s.refresh(row)
        return _serialize_saved_reply(row)


@app.delete("/admin/saved-replies/{reply_id}")
async def admin_saved_replies_delete(reply_id: int) -> dict[str, Any]:
    from db.engine import get_session
    from db.models import SavedReply
    async with get_session() as s:
        row = await s.get(SavedReply, reply_id)
        if row is not None:
            # Same row-based ownership check as PUT.
            assert_account_owned(row.account_id)
        if not row:
            raise HTTPException(status_code=404, detail="saved reply not found")
        await s.delete(row)
        await s.commit()
        return {"ok": True}


# ── Vault sends + per-fan history ─────────────────────────────────────
# Powers the picker's "you sent this to her 2 weeks ago / she bought it"
# badges. Frontend writes here right after a successful chat-send (better
# than wrapping the OF send proxy — keeps that path a clean mirror, and
# lets us decide per-call whether to track, e.g. don't track mass-sends).

class _VaultSendBody(BaseModel):
    account_id: str
    fan_id: int
    media_ids: list[int] = Field(default_factory=list)
    message_id: int | None = None
    price_cents: int = Field(0, ge=0, description="PPV price in cents (0 = free send)")


@app.post("/admin/vault/sends")
async def admin_vault_sends_create(body: _VaultSendBody = Body(...)) -> dict[str, Any]:
    """Record one vault-send row per media id. Returns the inserted count.

    Skips negative ids (fresh-claim placeholders the optimistic bubble
    used before the real vault id was assigned) — those slip through if
    the caller doesn't filter, and we shouldn't pollute history with them."""
    from db.engine import get_session
    from db.models import VaultSend
    real_ids = [mid for mid in body.media_ids if isinstance(mid, int) and mid > 0]
    if not real_ids:
        return {"ok": True, "inserted": 0}
    async with get_session() as s:
        for mid in real_ids:
            s.add(VaultSend(
                account_id=body.account_id,
                fan_id=body.fan_id,
                media_id=mid,
                message_id=body.message_id,
                price_cents=body.price_cents,
                # was_purchased flips later when a matching transaction
                # lands (see Transaction ingest path). Default null = unknown.
            ))
        await s.commit()
    return {"ok": True, "inserted": len(real_ids)}


class _VaultBackfillItem(BaseModel):
    message_id: int
    media_ids: list[int] = Field(default_factory=list)
    price_cents: int = Field(0, ge=0)
    # OF reports `isOpened=true` on a PPV once the fan has paid; we mirror
    # that as `was_purchased`. None for free sends — leave the column null
    # so the UI can distinguish "definitely not paid" vs "wasn't paid for".
    was_purchased: bool | None = None


class _VaultBackfillBody(BaseModel):
    account_id: str
    fan_id: int
    items: list[_VaultBackfillItem] = Field(default_factory=list)


@app.post("/admin/vault/sends/backfill")
async def admin_vault_sends_backfill(body: _VaultBackfillBody = Body(...)) -> dict[str, Any]:
    """Bulk-insert vault-send rows from chat history. The frontend calls
    this whenever a chat opens — every outgoing message with media that
    DOESN'T already have a vault_send row gets one. Idempotent: existing
    rows are detected via (account_id, fan_id, media_id, message_id) and
    skipped, so it's safe to fire on every chat-open without dupes.

    This is how we cover historical sends (pre-tracking), without
    requiring a one-off backfill job."""
    from db.engine import get_session
    from db.models import VaultSend
    from sqlalchemy import select
    if not body.items:
        return {"ok": True, "inserted": 0, "skipped": 0}

    # Pull every existing (media_id, message_id) for this fan in one query
    # so the dupe check is O(rows) instead of O(items × DB roundtrip).
    seen_msg_ids = {it.message_id for it in body.items if it.message_id}
    inserted = 0
    skipped = 0
    async with get_session() as s:
        existing: set[tuple[int, int]] = set()
        if seen_msg_ids:
            res = await s.execute(
                select(VaultSend.media_id, VaultSend.message_id)
                .where(
                    VaultSend.account_id == body.account_id,
                    VaultSend.fan_id == body.fan_id,
                    VaultSend.message_id.in_(seen_msg_ids),
                ),
            )
            for mid, msg in res.all():
                if msg is not None:
                    existing.add((mid, msg))

        for item in body.items:
            real_ids = [mid for mid in item.media_ids if isinstance(mid, int) and mid > 0]
            for mid in real_ids:
                if (mid, item.message_id) in existing:
                    skipped += 1
                    continue
                s.add(VaultSend(
                    account_id=body.account_id,
                    fan_id=body.fan_id,
                    media_id=mid,
                    message_id=item.message_id,
                    price_cents=item.price_cents,
                    was_purchased=item.was_purchased,
                ))
                existing.add((mid, item.message_id))
                inserted += 1
        if inserted:
            await s.commit()
    return {"ok": True, "inserted": inserted, "skipped": skipped}


def _parse_of_iso(s: Any) -> datetime | None:
    """OF's postedAt is "2026-04-12T17:33:11+00:00"-ish. Returns a naive
    UTC datetime (matches the rest of our DB convention) or None."""
    if not isinstance(s, str):
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


@app.get("/admin/vault/wall-media")
async def admin_vault_wall_media(
    request: Request,
    account_id: str = Query(...),
    pages: int = Query(5, ge=1, le=20, description="Max OF /posts pages to walk per call."),
    limit: int = Query(50, ge=1, le=50, description="Posts per page."),
    force: bool = Query(False, description="Force a re-scan from the top, ignoring the cached watermarks. UI's '↻ refresh' button."),
) -> dict[str, Any]:
    """Aggregate every vault media id ever posted on this model's wall,
    backed by SQLite (`wall_media` + `wall_scan_state`) so subsequent
    calls are O(diff-since-last-scan) instead of the old 5-page
    (~250-post) re-walk every hour.

    Two operating modes (state per-account in `wall_scan_state`):
      • Backfill: walks BACKWARD from `oldest_post_published_at` using
        OF's beforePublishTime cursor. Used until we hit the bottom of
        the post feed; `pages` per call so creators with thousands of
        posts amortize the walk over several picker opens.
      • Refresh: once `fully_backfilled=True`, walks FORWARD from the
        top and stops at the post matching `newest_post_published_at`.
        Usually a single page round-trip — creators rarely post 50
        wall items between picker opens.

    Returns the UNION of every media_id we've ever recorded for this
    account plus scan status. `has_more=true` means DB coverage is
    incomplete (older history remains) — the frontend uses it to
    optionally trigger another fetch in the background.

    The OF httpx calls (`client.me()`, `client.user_posts(...)`) are
    blocking; we run each via `asyncio.to_thread` so the event loop
    stays responsive — the up-to-5 sequential page walks otherwise
    froze SSE / webhooks for the full duration (observed 13.5s in the
    wild). `request.is_disconnected()` checks between pages so a folder
    switch mid-walk frees the per-account proxy slot promptly."""
    from db.engine import get_session
    from db.models import WallMedia, WallScanState
    from sqlalchemy import select
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    client = _load_client(account_id)
    me_resp = await asyncio.to_thread(client.me)
    my_id = me_resp.get("id")
    if not isinstance(my_id, int):
        raise HTTPException(status_code=500, detail="Could not resolve current user id from /users/me")

    # 1. Load scan state; honor `force` by wiping watermarks (rows stay —
    #    re-walking just re-confirms what we know via on_conflict_do_nothing).
    async with get_session() as s:
        state = (await s.execute(
            select(WallScanState).where(WallScanState.account_id == account_id)
        )).scalar_one_or_none()
        if force and state is not None:
            state.newest_post_published_at = None
            state.oldest_post_published_at = None
            state.fully_backfilled = False
            await s.flush()
        # Snapshot the values we need outside the session (state row is
        # detached after the `async with` exits).
        prev_newest = state.newest_post_published_at if state else None
        prev_oldest = state.oldest_post_published_at if state else None
        prev_backfilled = bool(state.fully_backfilled) if state else False
        prev_total = int(state.scanned_posts_total) if state else 0

    # 2. Pick mode + cursor. Three cases:
    #    • first   — never scanned this account before, walk forward from top.
    #    • refresh — fully covered already, walk forward and stop at watermark.
    #    • backfill — middle: walk backward from oldest known post.
    if prev_newest is None and prev_oldest is None:
        mode = "first"
        before: str | None = None
        stop_forward_at: datetime | None = None
    elif prev_backfilled:
        mode = "refresh"
        before = None
        stop_forward_at = prev_newest
    else:
        mode = "backfill"
        before = (
            f"{prev_oldest.timestamp():.6f}"
            if prev_oldest is not None else None
        )
        stop_forward_at = None

    # 3. Walk pages, accumulating new (account, media, post, published_at).
    new_rows: list[dict[str, Any]] = []
    seen_media_in_call: set[int] = set()  # dedupe within this call's batch
    scanned = 0
    page_newest_ts: datetime | None = None
    page_oldest_ts: datetime | None = None
    hit_known_top = False
    of_has_more = False
    for _ in range(pages):
        if await request.is_disconnected():
            raise HTTPException(status_code=499, detail="client disconnected")
        resp = await asyncio.to_thread(
            client.user_posts,
            my_id,
            limit=limit,
            skip_users="all",
            format="infinite",
            before_publish_time=before,
        )
        posts = resp.get("list", []) if isinstance(resp, dict) else []
        if not posts:
            break
        scanned += len(posts)
        for post in posts:
            pid = post.get("id")
            posted_at = _parse_of_iso(post.get("postedAt") or post.get("postedAtPrecise"))
            if posted_at and (page_newest_ts is None or posted_at > page_newest_ts):
                page_newest_ts = posted_at
            if posted_at and (page_oldest_ts is None or posted_at < page_oldest_ts):
                page_oldest_ts = posted_at
            # Refresh-mode stop: posts older-than-or-equal-to the watermark
            # are already in our DB. Don't enqueue their media (we have
            # them) and flag the outer loop to bail.
            if mode == "refresh" and stop_forward_at and posted_at and posted_at <= stop_forward_at:
                hit_known_top = True
                continue
            for m in (post.get("media") or []):
                mid = m.get("id")
                if isinstance(mid, int) and mid > 0 and mid not in seen_media_in_call:
                    seen_media_in_call.add(mid)
                    new_rows.append({
                        "account_id": account_id,
                        "media_id": mid,
                        "post_id": int(pid) if isinstance(pid, int) else None,
                        "post_published_at": posted_at,
                    })
        of_has_more = bool(resp.get("hasMore"))
        if hit_known_top:
            break
        if not of_has_more:
            break
        before = resp.get("tailMarker")
        if not before:
            break

    # 4. Persist new rows + scan state, then read the full union back.
    now = datetime.utcnow()
    new_fully_backfilled = prev_backfilled
    if mode == "first":
        new_fully_backfilled = not of_has_more
    elif mode == "backfill" and not of_has_more:
        new_fully_backfilled = True
    # refresh mode never flips back to False — force=True is the only path
    # that resets the watermarks.

    # Merge watermarks: extend in whichever direction this call walked.
    new_newest = page_newest_ts
    if prev_newest is not None and (new_newest is None or prev_newest > new_newest):
        new_newest = prev_newest
    new_oldest = page_oldest_ts
    if prev_oldest is not None and (new_oldest is None or prev_oldest < new_oldest):
        new_oldest = prev_oldest

    new_total = prev_total + scanned

    async with get_session() as s:
        # Ensure the parent accounts row exists — wall_media + wall_scan_state
        # both FK to accounts.id. Without import_legacy ever running for this
        # account, the FK insert blows up. Idempotent ON CONFLICT no-op.
        from db.models import Account
        await s.execute(
            sqlite_insert(Account)
            .values(id=account_id, is_active_default=False)
            .on_conflict_do_nothing(index_elements=["id"])
        )
        if new_rows:
            await s.execute(
                sqlite_insert(WallMedia)
                .values(new_rows)
                .on_conflict_do_nothing(index_elements=["account_id", "media_id"])
            )
        await s.execute(
            sqlite_insert(WallScanState)
            .values(
                account_id=account_id,
                newest_post_published_at=new_newest,
                oldest_post_published_at=new_oldest,
                fully_backfilled=new_fully_backfilled,
                last_scan_at=now,
                scanned_posts_total=new_total,
            )
            .on_conflict_do_update(
                index_elements=["account_id"],
                set_={
                    "newest_post_published_at": new_newest,
                    "oldest_post_published_at": new_oldest,
                    "fully_backfilled": new_fully_backfilled,
                    "last_scan_at": now,
                    "scanned_posts_total": new_total,
                },
            )
        )
        all_ids_rows = (await s.execute(
            select(WallMedia.media_id).where(WallMedia.account_id == account_id)
        )).scalars().all()

    return {
        "media_ids": sorted({int(x) for x in all_ids_rows}),
        "scanned_posts": scanned,
        "new_media_count": len(new_rows),
        "has_more": not new_fully_backfilled,
        "fully_backfilled": new_fully_backfilled,
        "mode": mode,
        "last_scan_at": now.isoformat() + "Z",
        "newest_post_published_at": new_newest.isoformat() + "Z" if new_newest else None,
        "oldest_post_published_at": new_oldest.isoformat() + "Z" if new_oldest else None,
    }


@app.get("/admin/vault/fan-history")
async def admin_vault_fan_history(
    account_id: str = Query(...),
    fan_id: int = Query(...),
) -> dict[str, Any]:
    """Per-media send history for ONE fan. Drives the vault-picker's
    'sent / purchased / unseen' badges with an O(1) lookup per tile.

    Returns:
      by_media: { "<media_id>": { send_count, last_sent_at,
                                  last_price_cents, was_purchased,
                                  last_purchase_at, total_paid_cents } }

    `was_purchased` mirrors the column on vault_sends — true once a
    transaction with the same message_id is observed.
    `last_purchase_at` / `total_paid_cents` come from joining the
    transactions table (kind in {message, ppv}) on message_id."""
    from db.engine import get_session
    from db.models import Transaction, VaultSend
    from sqlalchemy import select
    async with get_session() as s:
        # All sends to this fan, newest first.
        sends_res = await s.execute(
            select(VaultSend)
            .where(VaultSend.account_id == account_id, VaultSend.fan_id == fan_id)
            .order_by(VaultSend.sent_at.desc()),
        )
        sends = sends_res.scalars().all()
        if not sends:
            return {"by_media": {}}

        # Pull transactions referenced by any of our sends so we can mark
        # was_purchased + sum paid amounts. Filter to message-bearing txns
        # to skip subscription rebills + tips.
        msg_ids = {s.message_id for s in sends if s.message_id is not None}
        tx_by_msg: dict[int, list[Transaction]] = {}
        if msg_ids:
            tx_res = await s.execute(
                select(Transaction)
                .where(
                    Transaction.account_id == account_id,
                    Transaction.fan_id == fan_id,
                    Transaction.message_id.in_(msg_ids),
                ),
            )
            for t in tx_res.scalars().all():
                if t.message_id is None:
                    continue
                tx_by_msg.setdefault(t.message_id, []).append(t)

        by_media: dict[str, dict[str, Any]] = {}
        for snd in sends:
            entry = by_media.setdefault(str(snd.media_id), {
                "send_count": 0,
                "last_sent_at": None,
                "last_price_cents": 0,
                "was_purchased": False,
                "last_purchase_at": None,
                "total_paid_cents": 0,
            })
            entry["send_count"] += 1
            # Sends came back newest-first, so the first one we see is
            # the most recent — only fill these when they're still empty.
            if entry["last_sent_at"] is None:
                entry["last_sent_at"] = snd.sent_at.isoformat() if snd.sent_at else None
                entry["last_price_cents"] = snd.price_cents or 0
            # was_purchased: row flag OR any matching transaction.
            if snd.was_purchased:
                entry["was_purchased"] = True
            if snd.message_id and snd.message_id in tx_by_msg:
                for t in tx_by_msg[snd.message_id]:
                    entry["was_purchased"] = True
                    entry["total_paid_cents"] += t.amount_cents or 0
                    iso = t.occurred_at.isoformat() if t.occurred_at else None
                    if iso and (entry["last_purchase_at"] is None or iso > entry["last_purchase_at"]):
                        entry["last_purchase_at"] = iso
        return {"by_media": by_media}


# ── /admin/ingest/transactions/* — Phase F supervisor admin surface ─
# Read/write surface over `transaction_scan_history` (the per-account
# watermark for the OF payouts/transactions ingest job) plus a few
# observability queries on `transactions`. Sits next to the other
# /admin/* routes; share-token gate fires via middleware so no
# per-route auth dep is needed.

def _tx_ingest_tier(minutes_since_scan: float | None, current_status: str) -> str:
    """Health tier per synthesis §7.
    green:  scanned within 30m AND status in {idle, ok, refreshing, backfilling}
    yellow: 30 < minutes_since_scan <= 360
    red:    minutes_since_scan > 360 OR status in {paused, error, never_scanned}

    Note: 'refreshing'/'backfilling' here are the in-flight steady states
    (tick START stamps them, _mark_success then stamps 'ok'). A row
    sitting in those states still counts as green if the timestamp is
    fresh — it means a scan is actively running.
    """
    if current_status in ("paused", "error", "never_scanned"):
        return "red"
    if minutes_since_scan is None:
        return "red"
    if minutes_since_scan <= 30 and current_status in (
        "idle", "ok", "refreshing", "backfilling",
    ):
        return "green"
    if minutes_since_scan <= 360:
        return "yellow"
    return "red"


_TIER_RANK = {"green": 0, "yellow": 1, "red": 2}


@app.get("/admin/ingest/transactions/health")
async def admin_ingest_tx_health() -> dict[str, Any]:
    from db.engine import get_session
    from db.models import Account, TransactionScanHistory
    from sqlalchemy import select
    now = datetime.utcnow()
    # Scope to the signed-in principal's account_ids union (same rule as
    # /admin/accounts and the picker). Unauthed callers (internal curl /
    # cron) still see every row. Without this filter the Ingest health
    # banner would list models the chatter has no permission to act on.
    user = _get_request_user()
    chatter = _get_request_chatter() if user is None else None
    if user is not None:
        allowed_ids: frozenset[str] | None = user.account_ids
    elif chatter is not None:
        allowed_ids = chatter.account_ids
    else:
        allowed_ids = None
    async with get_session() as s:
        acct_rows = (await s.execute(select(Account.id, Account.nickname))).all()
        hist_rows = (await s.execute(select(TransactionScanHistory))).scalars().all()

    if allowed_ids is not None:
        acct_rows = [r for r in acct_rows if r[0] in allowed_ids]

    hist_by_acct = {h.account_id: h for h in hist_rows}
    accounts_out: list[dict[str, Any]] = []
    worst = "green"
    for aid, nick in acct_rows:
        h = hist_by_acct.get(aid)
        if h is None:
            entry = {
                "account_id": aid,
                "display_name": nick,
                "last_scan_at": None,
                "current_status": "never_scanned",
                "last_error": None,
                "consecutive_failures": 0,
                "paused_until": None,
                "rows_inserted_total": 0,
                "rows_patched_total": 0,
                "fully_backfilled": False,
                "minutes_since_scan": None,
                "tier": "red",
            }
        else:
            mins = (
                (now - h.last_scan_at).total_seconds() / 60.0
                if h.last_scan_at is not None else None
            )
            tier = _tx_ingest_tier(mins, h.current_status or "idle")
            entry = {
                "account_id": aid,
                "display_name": nick,
                "last_scan_at": h.last_scan_at.isoformat() if h.last_scan_at else None,
                "current_status": h.current_status,
                "last_error": h.last_error,
                "consecutive_failures": int(h.consecutive_failures or 0),
                "paused_until": h.paused_until.isoformat() if h.paused_until else None,
                "rows_inserted_total": int(h.rows_inserted_total or 0),
                "rows_patched_total": int(h.rows_patched_total or 0),
                "fully_backfilled": bool(h.fully_backfilled),
                "minutes_since_scan": round(mins, 2) if mins is not None else None,
                "tier": tier,
            }
        accounts_out.append(entry)
        if _TIER_RANK[entry["tier"]] > _TIER_RANK[worst]:
            worst = entry["tier"]

    return {
        "overall_status": worst,
        "as_of": now.isoformat(),
        "accounts": accounts_out,
    }


@app.get("/admin/ingest/transactions/{account_id}/progress")
async def admin_ingest_tx_progress(account_id: str) -> dict[str, Any]:
    from db.engine import get_session
    from db.models import Account, TransactionScanHistory
    from sqlalchemy import select
    now = datetime.utcnow()
    async with get_session() as s:
        acct = (await s.execute(
            select(Account.id).where(Account.id == account_id)
        )).first()
        if acct is None:
            raise HTTPException(status_code=404, detail="account not found")
        h = (await s.execute(
            select(TransactionScanHistory)
            .where(TransactionScanHistory.account_id == account_id)
        )).scalar_one_or_none()

    if h is None:
        return {
            "account_id": account_id,
            "current_status": "never_scanned",
            "fully_backfilled": False,
            "backfill_floor": None,
            "oldest_seen_occurred_at": None,
            "newest_seen_occurred_at": None,
            "rows_inserted_total": 0,
            "rows_patched_total": 0,
            "percent_complete": 0.0,
            "eta_seconds": None,
        }

    percent = 1.0 if h.fully_backfilled else 0.0
    eta_seconds: int | None = None
    if not h.fully_backfilled and h.oldest_seen_occurred_at and h.backfill_floor:
        total_span = (now - h.backfill_floor).total_seconds()
        done_span = (now - h.oldest_seen_occurred_at).total_seconds()
        if total_span > 0:
            percent = max(0.0, min(1.0, done_span / total_span))
            # Crude ETA: if we've covered `done_span` since the run started,
            # extrapolate the remaining at the same rate. We don't have a
            # run-start timestamp here, so use `last_scan_at` as the proxy.
            if h.last_scan_at and percent > 0:
                elapsed = (now - h.last_scan_at).total_seconds()
                if elapsed > 0 and percent < 1.0:
                    eta_seconds = int(elapsed * (1 - percent) / percent)

    return {
        "account_id": account_id,
        "current_status": h.current_status,
        "fully_backfilled": bool(h.fully_backfilled),
        "backfill_floor": h.backfill_floor.isoformat() if h.backfill_floor else None,
        "oldest_seen_occurred_at": (
            h.oldest_seen_occurred_at.isoformat() if h.oldest_seen_occurred_at else None
        ),
        "newest_seen_occurred_at": (
            h.newest_seen_occurred_at.isoformat() if h.newest_seen_occurred_at else None
        ),
        "rows_inserted_total": int(h.rows_inserted_total or 0),
        "rows_patched_total": int(h.rows_patched_total or 0),
        "percent_complete": percent,
        "eta_seconds": eta_seconds,
    }


class _TxRunBody(BaseModel):
    mode: str = Field("refresh", pattern="^(refresh|backfill)$")


@app.post("/admin/ingest/transactions/{account_id}/run", status_code=202)
async def admin_ingest_tx_run(
    account_id: str,
    body: _TxRunBody | None = Body(None),
) -> dict[str, Any]:
    from db.engine import get_session
    from db.models import Account, TransactionScanHistory
    from sqlalchemy import select
    mode = (body.mode if body else "refresh") or "refresh"
    async with get_session() as s:
        acct = (await s.execute(
            select(Account.id).where(Account.id == account_id)
        )).first()
        if acct is None:
            raise HTTPException(status_code=404, detail="account not found")
        h = (await s.execute(
            select(TransactionScanHistory)
            .where(TransactionScanHistory.account_id == account_id)
        )).scalar_one_or_none()
        if h is not None and h.current_status in ("backfilling", "refreshing"):
            # Stale-age escape valve. last_scan_at is stamped at tick
            # START (transaction_ingest.run_one_tick), so its age ==
            # how long the row has been parked at this status. A real
            # in-flight tick holds an asyncio.Lock + finishes well under
            # _STALE_INFLIGHT_S; anything older is a tombstone from a
            # process restart or cancelled task that escaped both the
            # try/finally and the boot sweep — we must NOT keep locking
            # the user's manual Refresh out forever.
            from transaction_ingest import _STALE_INFLIGHT_S  # type: ignore[import-not-found]
            age_s = (
                (datetime.utcnow() - h.last_scan_at).total_seconds()
                if h.last_scan_at is not None
                else _STALE_INFLIGHT_S + 1
            )
            if age_s < _STALE_INFLIGHT_S:
                raise HTTPException(
                    status_code=409,
                    detail=f"already in-flight: {h.current_status}",
                )
            log.warning(
                "admin_ingest_tx_run_proceed_after_stale account=%s status=%s age_s=%.0f",
                account_id, h.current_status, age_s,
            )

    # Lock peek removed: run_one_tick now stamps current_status to
    # "refreshing"/"backfilling" at the START of the tick (inside the
    # session that took the lock), so the upstream current_status check
    # at line 4714 catches in-flight scans without needing to import
    # supervisor internals here. See library/db_data/17_round2_synthesis.md.
    queued_at = datetime.utcnow()

    async def _trigger() -> None:
        try:
            from transaction_ingest import run_one_tick  # type: ignore[import-not-found]
        except ImportError:
            log.info("admin_ingest_tx_run: transaction_ingest not yet available; no-op")
            return
        try:
            await run_one_tick(account_id, mode=mode)
        except Exception:
            log.warning("run_one_tick failed for %s", account_id, exc_info=True)

    asyncio.create_task(_trigger(), name=f"tx-ingest-trigger-{account_id}")

    return {"accepted": True, "mode": mode, "queued_at": queued_at.isoformat()}


class _TxBackfillBody(BaseModel):
    # `days` accepts int OR the literal string "all" (cold-start, walk to 1970).
    days: Any = 90


@app.post("/admin/ingest/transactions/{account_id}/backfill", status_code=202)
async def admin_ingest_tx_backfill(
    account_id: str,
    body: _TxBackfillBody = Body(...),
) -> dict[str, Any]:
    from db.engine import get_session
    from db.models import Account, TransactionScanHistory
    from sqlalchemy import select
    days_raw = body.days
    if isinstance(days_raw, str) and days_raw.lower() == "all":
        backfill_floor = datetime(1970, 1, 1)
    else:
        try:
            days_int = int(days_raw)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="days must be int or 'all'")
        if days_int <= 0:
            raise HTTPException(status_code=400, detail="days must be positive")
        backfill_floor = datetime.utcnow() - timedelta(days=days_int)

    async with get_session() as s:
        acct = (await s.execute(
            select(Account.id).where(Account.id == account_id)
        )).first()
        if acct is None:
            raise HTTPException(status_code=404, detail="account not found")
        h = (await s.execute(
            select(TransactionScanHistory)
            .where(TransactionScanHistory.account_id == account_id)
        )).scalar_one_or_none()
        if h is None:
            h = TransactionScanHistory(account_id=account_id)
            s.add(h)
        h.fully_backfilled = False
        h.backfill_floor = backfill_floor
        await s.commit()

    return {"accepted": True, "backfill_floor": backfill_floor.isoformat()}


class _TxPauseBody(BaseModel):
    duration_hours: int = Field(1, ge=1, le=24 * 30)


@app.post("/admin/ingest/transactions/{account_id}/pause", status_code=202)
async def admin_ingest_tx_pause(
    account_id: str,
    body: _TxPauseBody = Body(...),
) -> dict[str, Any]:
    from db.engine import get_session
    from db.models import Account, TransactionScanHistory
    from sqlalchemy import select
    paused_until = datetime.utcnow() + timedelta(hours=body.duration_hours)
    async with get_session() as s:
        acct = (await s.execute(
            select(Account.id).where(Account.id == account_id)
        )).first()
        if acct is None:
            raise HTTPException(status_code=404, detail="account not found")
        h = (await s.execute(
            select(TransactionScanHistory)
            .where(TransactionScanHistory.account_id == account_id)
        )).scalar_one_or_none()
        if h is None:
            h = TransactionScanHistory(account_id=account_id)
            s.add(h)
        h.paused_until = paused_until
        await s.commit()
    return {"accepted": True, "paused_until": paused_until.isoformat()}


@app.post("/admin/ingest/transactions/{account_id}/resume", status_code=202)
async def admin_ingest_tx_resume(account_id: str) -> dict[str, Any]:
    """Clear `paused_until` so the supervisor picks this account back up
    on the next tick. Idempotent — calling on an already-unpaused row
    is a no-op."""
    from db.engine import get_session
    from db.models import Account, TransactionScanHistory
    from sqlalchemy import select
    async with get_session() as s:
        acct = (await s.execute(
            select(Account.id).where(Account.id == account_id)
        )).first()
        if acct is None:
            raise HTTPException(status_code=404, detail="account not found")
        h = (await s.execute(
            select(TransactionScanHistory)
            .where(TransactionScanHistory.account_id == account_id)
        )).scalar_one_or_none()
        if h is not None:
            h.paused_until = None
            if h.current_status == "paused":
                h.current_status = "ok"
            await s.commit()
    return {"accepted": True}


@app.get("/admin/ingest/transactions/unknown-kinds")
async def admin_ingest_tx_unknown_kinds(
    limit: int = Query(50, ge=1, le=200),
) -> dict[str, Any]:
    from db.engine import get_session
    from db.models import Transaction
    from sqlalchemy import select
    async with get_session() as s:
        rows = (await s.execute(
            select(
                Transaction.id,
                Transaction.account_id,
                Transaction.occurred_at,
                Transaction.amount_cents,
                Transaction.currency,
                Transaction.provider_transaction_id,
                Transaction.description,
            )
            .where(Transaction.kind == "unknown")
            .order_by(Transaction.occurred_at.desc())
            .limit(limit)
        )).all()
    return {
        "rows": [
            {
                "id": int(r.id),
                "account_id": r.account_id,
                "occurred_at": r.occurred_at.isoformat() if r.occurred_at else None,
                "amount_cents": int(r.amount_cents or 0),
                "currency": r.currency,
                "provider_transaction_id": r.provider_transaction_id,
                "description": r.description,
            }
            for r in rows
        ]
    }


@app.get("/admin/ingest/transactions/recent-changes")
async def admin_ingest_tx_recent_changes(
    since: str = Query(..., description="ISO date or datetime"),
    limit: int = Query(200, ge=1, le=1000),
) -> dict[str, Any]:
    from db.engine import get_session
    from db.models import Transaction
    from sqlalchemy import or_, select
    # Mirror stats._parse_date's end-of-day rule for bare dates so the
    # admin UI behaves consistently. For `since` we want start-of-day on
    # bare-date input, which is what `fromisoformat` already gives us.
    try:
        since_dt = datetime.fromisoformat(since.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"invalid since: {e}") from e

    async with get_session() as s:
        rows = (await s.execute(
            select(
                Transaction.id,
                Transaction.account_id,
                Transaction.fan_id,
                Transaction.kind,
                Transaction.amount_cents,
                Transaction.status,
                Transaction.occurred_at,
                Transaction.cleared_at,
                Transaction.provider_transaction_id,
                Transaction.description,
            )
            .where(
                or_(
                    Transaction.status.in_(("chargedback", "refunded")),
                    (Transaction.status == "cleared")
                    & (Transaction.cleared_at.is_not(None))
                    & (Transaction.cleared_at >= since_dt),
                )
            )
            .order_by(Transaction.occurred_at.desc())
            .limit(limit)
        )).all()
    return {
        "rows": [
            {
                "id": int(r.id),
                "account_id": r.account_id,
                "fan_id": int(r.fan_id) if r.fan_id is not None else None,
                "kind": r.kind,
                "amount_cents": int(r.amount_cents or 0),
                "status": r.status,
                "occurred_at": r.occurred_at.isoformat() if r.occurred_at else None,
                "cleared_at": r.cleared_at.isoformat() if r.cleared_at else None,
                "provider_transaction_id": r.provider_transaction_id,
                "description": r.description,
            }
            for r in rows
        ]
    }


@app.get("/admin/ingest/transactions/orphan-tips")
async def admin_ingest_tx_orphan_tips(
    account_id: str | None = Query(None),
    from_: str | None = Query(None, alias="from"),
    to: str | None = Query(None),
    include_assigned: bool = Query(False),
    limit: int = Query(100, ge=1, le=500),
) -> dict[str, Any]:
    """Tip transactions that fall into the 'Unattributed' bucket in the
    per-employee view: kind in tip family, status='cleared', and no
    chatter-attributed outbound message within the 7-day lookback. With
    `include_assigned=true` ALSO returns tips that already have a
    manual `attributed_employee_id` set (so the UI can offer reassign
    / clear). Each row's `attributed_employee_id` is null for true
    orphans, populated for previously-assigned tips.

    Optional `from` / `to` (ISO date) scope to the dashboard's window."""
    from db.engine import get_session
    from sqlalchemy import text as sql_text
    # LEFT JOIN employees so we return the chatter's display_name with the
    # row — the UI used to look this up against the current owner's roster
    # and fell back to "Employee {id}" when the employee was scoped out
    # (e.g. transferred). The DB has the canonical name; surface it here.
    sql = """
      SELECT t.id, t.account_id, t.fan_id, t.amount_cents, t.occurred_at,
             t.description, t.kind, t.attributed_employee_id,
             e.display_name AS attributed_employee_name
        FROM transactions t
        LEFT JOIN employees e ON e.id = t.attributed_employee_id
       WHERE t.kind IN ('tip', 'tip_post', 'tip_stream')
         AND t.status = 'cleared'
         AND t.message_id IS NULL
         AND NOT EXISTS (
           SELECT 1 FROM messages m
            WHERE m.account_id = t.account_id
              AND m.fan_id = t.fan_id
              AND m.direction = 'out'
              AND m.sent_by_employee_id IS NOT NULL
              AND m.created_at <= t.occurred_at
              AND m.created_at > datetime(t.occurred_at, '-7 days')
         )
    """
    params: dict[str, Any] = {"lim": limit}
    if not include_assigned:
        sql += " AND t.attributed_employee_id IS NULL"
    if account_id:
        sql += " AND t.account_id = :aid"
        params["aid"] = account_id
    if from_:
        sql += " AND t.occurred_at >= :start"
        params["start"] = from_
    if to:
        # End-of-day for bare ISO dates so "to=YYYY-MM-DD" includes that day.
        sql += " AND t.occurred_at <= :end"
        params["end"] = f"{to} 23:59:59" if len(to) == 10 else to
    sql += " ORDER BY t.occurred_at DESC LIMIT :lim"

    async with get_session() as s:
        rows = (await s.execute(sql_text(sql), params)).all()
    return {
        "rows": [
            {
                "id": int(r.id),
                "account_id": r.account_id,
                "fan_id": int(r.fan_id) if r.fan_id is not None else None,
                "kind": r.kind,
                "amount_cents": int(r.amount_cents or 0),
                "occurred_at": r.occurred_at if isinstance(r.occurred_at, str)
                                else (r.occurred_at.isoformat() if r.occurred_at else None),
                "description": r.description,
                "attributed_employee_id": (
                    int(r.attributed_employee_id)
                    if r.attributed_employee_id is not None else None
                ),
                "attributed_employee_name": r.attributed_employee_name,
            }
            for r in rows
        ]
    }


@app.post("/admin/ingest/transactions/{tx_id}/attribute")
async def admin_ingest_tx_attribute(
    tx_id: int,
    payload: dict[str, Any] = Body(...),
) -> dict[str, Any]:
    """Manually assign (or clear) the employee a transaction counts
    toward in the per-employee revenue view. Pass `{"employee_id": <int>}`
    to assign, or `{"employee_id": null}` to clear. Updates
    `transactions.attributed_employee_id`; the view's COALESCE makes it
    win over the standalone 7-day lookback."""
    from db.engine import get_session
    from db.models import Employee, Transaction
    from sqlalchemy import select, update as sql_update

    raw_eid = payload.get("employee_id", "__missing__")
    if raw_eid == "__missing__":
        raise HTTPException(status_code=400, detail="employee_id required (or null)")
    employee_id: int | None
    if raw_eid is None:
        employee_id = None
    else:
        try:
            employee_id = int(raw_eid)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="employee_id must be an int or null")

    async with get_session() as s:
        tx = (await s.execute(
            select(Transaction.id, Transaction.kind, Transaction.account_id, Transaction.amount_cents)
            .where(Transaction.id == tx_id)
        )).first()
        if tx is None:
            raise HTTPException(status_code=404, detail=f"transaction {tx_id} not found")

        if employee_id is not None:
            emp = (await s.execute(
                select(Employee.id).where(Employee.id == employee_id)
            )).first()
            if emp is None:
                raise HTTPException(status_code=400, detail=f"employee {employee_id} not found")

        await s.execute(
            sql_update(Transaction)
            .where(Transaction.id == tx_id)
            .values(attributed_employee_id=employee_id)
        )
        await s.commit()

    log.info(
        "admin_ingest_tx_attribute tx=%d employee=%s kind=%s account=%s amount=%d",
        tx_id, employee_id, tx.kind, tx.account_id, tx.amount_cents,
    )
    return {
        "id": tx_id,
        "account_id": tx.account_id,
        "kind": tx.kind,
        "amount_cents": int(tx.amount_cents or 0),
        "attributed_employee_id": employee_id,
    }


# ── Static UI ──────────────────────────────────────────────────
# Mounted AFTER all API routes so route precedence is correct.
# StaticFiles(html=True) serves /ui/ → web/index.html automatically.

@app.get("/", include_in_schema=False)
def root_redirect():
    return RedirectResponse("/ui/")


# Belt-and-suspenders cache-bust: when the user updates web/app.js or
# index.html, we want every browser to refetch on next load. Without this
# header, browsers (especially behind Tailscale's funnel) hold onto JS for
# hours and the user sees ancient code despite hard-refreshing. Bypassing
# the cache for static assets is fine — they're tiny.
@app.middleware("http")
async def _no_cache_ui(request: Request, call_next):
    resp = await call_next(request)
    if request.url.path.startswith("/ui/"):
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
    return resp


_WEB_DIR = HERE.parent / "web"
if _WEB_DIR.is_dir():
    app.mount("/ui", StaticFiles(directory=_WEB_DIR, html=True), name="ui")
else:
    log.warning("web/ folder not found at %s — UI disabled", _WEB_DIR)
