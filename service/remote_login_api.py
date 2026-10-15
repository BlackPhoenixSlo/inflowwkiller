"""service/remote_login_api.py — HTTP surface for the served remote-browser
login (Part C of the sanctioned direct-login upgrade).

Routes (all under /admin/login/remote except the refresh twin):
  POST /admin/login/remote/start          begin a login job → {id, ...}
  GET  /admin/login/remote/{id}           poll status (no secrets)
  POST /admin/login/remote/{id}/otp       submit a 2FA code the human read
  POST /admin/login/remote/{id}/cancel    cancel the job
  POST /admin/login/remote/{id}/input     replay human mouse/wheel/key events
  GET  /admin/login/remote/{id}/screen    SSE JPEG screencast (token via ?t=)
  POST /admin/session/refresh             re-harvest signing from a profile

SECURITY
  * The login path prefix is in employees._AUDIT_SKIP_PREFIXES, so the start
    body (which carries the password) is NEVER written to the audit log.
  * `remote_login.scrub()` strips the password from every job message/error.
  * Started-principal check on every per-job route: only the principal that
    started a job may poll/drive/cancel it (403 otherwise).
  * A proxy is bound to the account BEFORE its OFClient loads (done inside
    remote_login._capture_and_persist → one egress).

This module imports NO server internals (server imports this router, so the
dependency only goes one way); the post-bootstrap hooks are reached by
remote_login via a lazy `import server` at capture time.
"""
from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

import auth
import proxies as proxy_registry
import remote_login

log = logging.getLogger("of-relay.remote-login-api")

router = APIRouter()

# NOTE: the audit middleware skips this path via employees._AUDIT_SKIP_PREFIXES
# (passwords ride in the start body) — that tuple is the single source of truth.


def _principal() -> str | None:
    u = auth.get_request_user()
    return u.id if u is not None else None


def _assert_started_principal(job: remote_login.RemoteLoginJob) -> None:
    """Only the principal that started a job may see or drive it."""
    if _principal() != job.principal:
        raise HTTPException(status_code=403, detail="not your login job")


def _require_job(job_id: str) -> remote_login.RemoteLoginJob:
    job = remote_login.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="no such login job (expired?)")
    _assert_started_principal(job)
    return job


def _visible_proxy(label: str):
    """Resolve a proxy by label, 404 if the signed-in user can't see it
    (another user's private proxy). Shared defaults + own proxies are fine."""
    p = proxy_registry.get_by_label(label)
    if p is None:
        raise HTTPException(status_code=404, detail=f"no proxy with label {label!r}")
    u = auth.get_request_user()
    if u is not None:
        owner = p.get("user_id")
        if owner is not None and owner != u.id:
            raise HTTPException(status_code=404, detail=f"no proxy with label {label!r}")
    return p


class _StartBody(BaseModel):
    email: str
    password: str
    proxy_label: str | None = Field(None, description="Registry label of the proxy to route through")
    proxy: str | None = Field(None, description="Inline proxy line to upsert + use (alternative to proxy_label)")


@router.post("/admin/login/remote/start")
def remote_login_start(body: _StartBody = Body(...)) -> dict:
    if not body.email or not body.password:
        raise HTTPException(status_code=400, detail="email and password are required")

    principal = _principal()
    # Resolve the proxy: inline line upserts into the registry first.
    if body.proxy:
        try:
            entry = proxy_registry.upsert_from_line(body.proxy, user_id=principal)
        except (ValueError, proxy_registry.ProxyPermissionError) as e:
            raise HTTPException(status_code=400, detail=f"inline proxy: {e}")
        label = entry["label"]
        p = proxy_registry.get_by_label(label)
    elif body.proxy_label:
        label = body.proxy_label
        p = _visible_proxy(label)
    else:
        raise HTTPException(status_code=400, detail="proxy_label or proxy is required")

    proxy_url = proxy_registry.proxy_url(p)
    try:
        job = remote_login.start_login(
            email=body.email, password=body.password,
            proxy_label=label, proxy_url=proxy_url, principal=principal,
        )
    except remote_login.RemoteLoginConflict as e:
        raise HTTPException(status_code=409, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    return job.public()


@router.get("/admin/login/remote/{job_id}")
def remote_login_status(job_id: str) -> dict:
    return _require_job(job_id).public()


class _OtpBody(BaseModel):
    code: str


@router.post("/admin/login/remote/{job_id}/otp")
def remote_login_otp(job_id: str, body: _OtpBody = Body(...)) -> dict:
    job = _require_job(job_id)
    remote_login.submit_otp(job, body.code)
    return {"ok": True}


@router.post("/admin/login/remote/{job_id}/cancel")
def remote_login_cancel(job_id: str) -> dict:
    job = _require_job(job_id)
    remote_login.cancel(job)
    return {"ok": True}


class _InputBody(BaseModel):
    events: list[dict] = Field(default_factory=list)


@router.post("/admin/login/remote/{job_id}/input")
def remote_login_input(job_id: str, body: _InputBody = Body(...)) -> dict:
    job = _require_job(job_id)
    remote_login.submit_inputs(job, body.events)
    return {"ok": True, "queued": len(body.events)}


@router.get("/admin/login/remote/{job_id}/screen")
async def remote_login_screen(job_id: str, request: Request) -> StreamingResponse:
    """SSE JPEG screencast. EventSource can't set headers, so the share-token
    rides in `?t=` (handled by the gate) and the job is principal-checked here.
    Each message is `data: {"seq":N,"w":W,"h":H,"jpeg_b64":"..."}`."""
    job = _require_job(job_id)

    async def _stream():
        last_seq = -1
        # Open with the frame geometry so the client can size its canvas even
        # before the first JPEG lands.
        yield f'event: meta\ndata: {json.dumps({"w": job.frame_w, "h": job.frame_h})}\n\n'
        while True:
            if await request.is_disconnected():
                return
            seq, b64, w, h = job.latest_frame()
            if b64 is not None and seq != last_seq:
                last_seq = seq
                payload = json.dumps({"seq": seq, "w": w, "h": h, "jpeg_b64": b64})
                yield f"data: {payload}\n\n"
            if job.is_terminal():
                yield f'event: state\ndata: {json.dumps(job.public())}\n\n'
                return
            await asyncio.sleep(0.12)

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


class _RefreshBody(BaseModel):
    account_id: str


@router.post("/admin/session/refresh")
def session_refresh(body: _RefreshBody = Body(...)) -> dict:
    """Re-harvest signing rules + cookies for an account from its stored
    browser profile — no credentials. Returns a job (poll it like a login)."""
    auth.assert_account_owned(body.account_id)
    p = proxy_registry.get_for_account(body.account_id)
    if p is None:
        raise HTTPException(
            status_code=400,
            detail=f"account {body.account_id} has no proxy bound — bind one first",
        )
    proxy_url = proxy_registry.proxy_url(p)
    try:
        job = remote_login.start_refresh(
            account_id=body.account_id, proxy_label=p["label"],
            proxy_url=proxy_url, principal=_principal(),
        )
    except remote_login.RemoteLoginConflict as e:
        raise HTTPException(status_code=409, detail=str(e))
    return job.public()
