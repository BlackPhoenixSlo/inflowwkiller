"""
OnlyFans WebSocket client.

OF pushes real-time events (new chat messages, tips, subs, online presence,
counter updates, post expirations, …) over a single persistent WebSocket:

  wss://ws2.onlyfans.com/ws3/<n>          ← `wsUrl` field of /users/me
  Authenticated by a JWT (RS256) returned alongside as `wsAuthToken`.

Protocol shape (captured live 2026-05-17):

  client ──> {"act": "connect", "token": "<jwt>"}
  server <── {"connected": true, "v": "5.4.0"}
  ... then OF pushes events as JSON objects keyed by event type, e.g.:
    {"api2_chat_message": {responseType: "message", text, fromUser, ...}}
    {"chat_messages": 30, "count_priority_chat": 3, "unread_tips": 0}
    {"post_expire": "<post_id>"}
    {"new_message": {...}}   /  {"online_status": {...}}  / …

Usage:

  from of_ws import OFWebSocket
  ws = OFWebSocket.from_latest_session()
  async for event in ws.events():
      print(event)
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import AsyncIterator

import websockets

from of_client import OFClient

log = logging.getLogger("of-ws")

# OF's token TTL was ~24h on the capture we did; we refresh well before that
# just in case. Practically the JWT carries `exp` so we could parse it, but
# the calling code can also just reconnect any time.
PING_INTERVAL_S = 25         # OF sends `{"unread_tips":...}` style updates;
                              # no formal ping/pong needed, but a benign
                              # `get_onlines` keeps NATs happy.
RECONNECT_MIN_S = 1
RECONNECT_MAX_S = 30


class OFWebSocket:
    """Long-lived WS connection that yields parsed events."""

    def __init__(self, client: OFClient):
        self.client = client
        self._ws_url: str | None = None
        self._token: str | None = None
        self._stop = asyncio.Event()
        # The currently-connected socket, set while events() holds an open
        # connection and cleared on disconnect. Lets other tasks (e.g. the
        # automation layer) push outbound frames — like the "...is typing"
        # indicator — over the SAME authenticated, proxy-tunneled connection
        # instead of opening a second WS. None when not connected.
        self._live_ws = None

    async def send_typing(self, fan_id: int) -> bool:
        """Emit OF's typing indicator to `fan_id` over the live pump socket so
        the fan sees the "...is typing" bubble. OF auto-clears the indicator
        after a few seconds, so callers re-emit every ~2-3s for a longer hold.
        Returns False (no-op) when the socket isn't currently connected."""
        ws = self._live_ws
        if ws is None:
            return False
        try:
            await ws.send(json.dumps({"act": "typing", "typing_to": int(fan_id)}))
            return True
        except Exception:
            log.debug("send_typing failed (fan=%s)", fan_id, exc_info=True)
            return False

    @classmethod
    def from_latest_session(cls) -> "OFWebSocket":
        return cls(OFClient.from_latest_session())

    def _refresh_auth(self) -> None:
        """Fetch a fresh wsUrl + wsAuthToken from /users/me."""
        me = self.client.me()
        self._ws_url = me["wsUrl"]
        self._token = me["wsAuthToken"]

    async def events(self) -> AsyncIterator[dict]:
        """Async generator: yields each event dict as it arrives. Reconnects
        automatically on disconnect with exponential backoff. Caller can stop
        by calling `.stop()` (which causes the loop to exit on next iteration).

        The WebSocket is tunneled through the same per-account proxy as the
        REST traffic (when one is assigned). Without this, REST would egress
        from the proxy IP but the WS would egress direct from the host's WAN
        IP — OF would see "same user-id from two distinct IPs at once" and
        flag it as a shared-session anomaly.
        """
        backoff = RECONNECT_MIN_S
        while not self._stop.is_set():
            try:
                # _refresh_auth() does a blocking GET /users/me; run it off the
                # event loop so a (re)connect doesn't freeze every account's pump
                # and every HTTP handler (observed loop-stall during reconnects).
                await asyncio.to_thread(self._refresh_auth)
                assert self._ws_url and self._token
                proxy_url = getattr(self.client, "proxy_url", None) or None
                if proxy_url:
                    log.info("connecting %s via proxy %s",
                             self._ws_url, getattr(self.client, "proxy_label", None) or proxy_url)
                else:
                    log.info("connecting %s (direct — no proxy assigned)", self._ws_url)
                async with websockets.connect(
                    self._ws_url,
                    origin="https://onlyfans.com",
                    user_agent_header=self.client.user_agent,
                    additional_headers={
                        "Accept-Language": "sl-SI,sl;q=0.9",
                    },
                    max_size=8 * 1024 * 1024,
                    open_timeout=15,
                    close_timeout=5,
                    # `proxy=` default in websockets 16 is `True`, which reads
                    # HTTPS_PROXY env var. We want explicit control: account's
                    # proxy when assigned, otherwise direct (None disables).
                    proxy=proxy_url,
                ) as ws:
                    await ws.send(json.dumps({"act": "connect", "token": self._token}))
                    # Expose the open socket so send_typing()/other tasks can
                    # push outbound frames on it. Cleared in finally below.
                    self._live_ws = ws
                    # Background pinger for NAT keepalive
                    pinger = asyncio.create_task(_pinger(ws, self._stop))
                    stop_waiter = asyncio.create_task(self._stop.wait())
                    try:
                        # Race ws.recv() vs stop event so .stop() exits promptly
                        while not self._stop.is_set():
                            recv_task = asyncio.create_task(ws.recv())
                            done, pending = await asyncio.wait(
                                {recv_task, stop_waiter},
                                return_when=asyncio.FIRST_COMPLETED,
                            )
                            if stop_waiter in done:
                                recv_task.cancel()
                                break
                            raw = recv_task.result()
                            if isinstance(raw, bytes):
                                try:
                                    raw = raw.decode("utf-8")
                                except Exception:
                                    continue
                            try:
                                msg = json.loads(raw)
                            except json.JSONDecodeError:
                                log.warning("non-JSON frame: %r", raw[:200])
                                continue
                            yield msg
                    finally:
                        self._live_ws = None
                        pinger.cancel()
                        if not stop_waiter.done():
                            stop_waiter.cancel()
                # Clean disconnect — reset backoff
                backoff = RECONNECT_MIN_S
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("ws error %s — reconnecting in %ds", e, backoff)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                    return
                except asyncio.TimeoutError:
                    pass
                backoff = min(int(backoff * 2), RECONNECT_MAX_S)

    def stop(self) -> None:
        self._stop.set()


# ─── Live-pump registry ───────────────────────────────────────────────────
# server.py runs one OFWebSocket pump per account in the same event loop as the
# automation executor. It registers each live pump here so the automation layer
# can reach the open socket (e.g. to emit the typing indicator) without opening
# a second WS. Keyed by account_id (str).
_LIVE_PUMPS: dict[str, "OFWebSocket"] = {}


def register_pump(account_id: str, ws: "OFWebSocket") -> None:
    _LIVE_PUMPS[str(account_id)] = ws


def unregister_pump(account_id: str, ws: "OFWebSocket | None" = None) -> None:
    """Remove the pump for an account. If `ws` is given, only remove it when it
    is still the registered instance (avoids a restarted pump clobbering a newer
    registration)."""
    cur = _LIVE_PUMPS.get(str(account_id))
    if ws is None or cur is ws:
        _LIVE_PUMPS.pop(str(account_id), None)


async def emit_typing(account_id: str, fan_id: int) -> bool:
    """Fire the OF typing indicator from `account_id` to `fan_id` over that
    account's live pump socket. No-op (False) if the pump isn't connected."""
    ws = _LIVE_PUMPS.get(str(account_id))
    if ws is None:
        return False
    return await ws.send_typing(fan_id)


async def _pinger(ws, stop: asyncio.Event) -> None:
    """Send `{"act":"get_onlines"}` every PING_INTERVAL_S so the connection
    stays alive through NAT timeouts. OF accepts unknown acts silently."""
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=PING_INTERVAL_S)
            return
        except asyncio.TimeoutError:
            pass
        try:
            await ws.send(json.dumps({"act": "get_onlines"}))
        except Exception:
            return


# ─── CLI tailer ──────────────────────────────────────────────────────────

async def _tail(pretty: bool = True) -> None:
    """Print each event as it arrives. For ad-hoc debugging:
       ./venv/bin/python service/of_ws.py
    """
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ws = OFWebSocket.from_latest_session()
    print("[*] tailing OF events — Ctrl-C to stop")
    n = 0
    try:
        async for event in ws.events():
            n += 1
            key = next(iter(event)) if isinstance(event, dict) and event else "?"
            if pretty and isinstance(event, dict):
                print(f"\n[{n}] {key}")
                # Trim huge payloads
                preview = json.dumps(event, indent=2)
                if len(preview) > 1500:
                    preview = preview[:1500] + "…"
                print(preview)
            else:
                print(f"[{n}] {event}")
    except KeyboardInterrupt:
        ws.stop()
        print(f"\n[*] stopped after {n} events")


if __name__ == "__main__":
    asyncio.run(_tail())
