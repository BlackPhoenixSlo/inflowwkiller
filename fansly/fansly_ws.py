"""
Fansly realtime WebSocket client.

Mirrors `service/of_ws.py` so the relay's pump (`server.py:_ws_pump_for_account`)
and the typing seam (`of_ws.emit_typing`) work against either platform with one
branch on construction. Every wire fact below was captured off live frames —
see `fansly/capture/RECIPES.md` §"Realtime socket" — not read off the JS bundle.

Protocol (wss://wsv3.fansly.com/?v=3, origin https://fansly.com):

  client ──> {"t":1,"d":"{\"token\":\"<session token>\",\"v\":3}"}
  server <── {"t":1,"d":"{\"session\":{...}}"}
  client ──> p                          ← keepalive, a BARE LITERAL, not JSON
  server <── {"t":2,"d":"{\"lastPing\":<ms>}"}

Everything else arrives as `t:10000` and is TRIPLE-encoded:

  {"t":10000,"d":"{\"serviceId\":N,\"event\":\"<JSON string>\"}"}
                   ^ d is a JSON string    ^ event is JSON *inside* that string

There is no subscribe/join frame: one connection is a per-account firehose. There
is also no resume protocol — `websocketSessionId` is newly minted each connect
and never sent back — so a dropped pump just re-auths with the same token.

We translate at the edge: `serviceId:5 type:1` (a chat message) is reshaped by
the shim's `of_message` and yielded as `{"api2_chat_message": <OF message>}`,
which is exactly the key `event_transcoder.transcode` routes on. Everything
downstream (transcode → persist → webhook → automation) then runs unmodified.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from collections import deque
from typing import Any, AsyncIterator

import websockets

log = logging.getLogger("fansly-ws")

WS_URL = "wss://wsv3.fansly.com/?v=3"

# Measured OUT-`p` gaps ranged 20–45s and are an idle timer reset by other
# traffic, not a fixed cadence (RECIPES §"Keepalive cadence"). Sending
# unconditionally at 18s is inside the floor of every observed window; do not
# try to reproduce the client's timer.
PING_INTERVAL_S = 18
RECONNECT_MIN_S = 1
RECONNECT_MAX_S = 30

# Frame types
T_AUTH = 1
T_PONG = 2
T_EVENT = 10000

# serviceId → what it carries (RECIPES: the SAME enum as REST notification
# codes, where `notification type == serviceId * 1000 + eventType`).
SVC_MESSAGE = 5

# Duplicate frames are normal on this socket and arrive ADJACENT, not hours
# apart, so a bounded ring is sufficient — an unbounded set on a pump that
# lives for weeks is a leak.
_DEDUPE_MAX = 512


class FanslyWebSocket:
    """Long-lived wsv3 connection that yields OF-shaped event dicts.

    Same public surface as `of_ws.OFWebSocket` — `events()`, `stop()`,
    `send_typing()` — so `server.py` and the typing registry treat both alike.
    """

    def __init__(self, client: Any):
        self.client = client
        self._stop = asyncio.Event()
        self._live_ws = None
        # Bounded (deque + set) dedupe on (serviceId, type, id).
        self._seen: set[tuple] = set()
        self._seen_order: deque[tuple] = deque()
        # Disconnect-window accounting, so the realtime gap rate is measurable.
        self._down_since: float | None = None

    # -- auth ---------------------------------------------------------------

    def _read_token(self) -> str:
        """The session token, re-read from the client's session on every
        connect. NOT via `me()` — the shim's `me()` returns an OF-user shape
        with no token at all (that mismatch is what crash-looped this pump).
        Re-reading per connect matters because a Fansly token can rotate; a
        pump that cached one at construct time would re-auth-fail forever."""
        session = getattr(self.client, "session", None)
        token = getattr(session, "token", None) if session is not None else None
        if not token:
            raise RuntimeError("fansly session has no token — cannot auth the socket")
        return str(token)

    # -- typing -------------------------------------------------------------

    async def send_typing(self, fan_id: int | str) -> bool:
        """Fansly's typing indicator is REST (`POST /message/typing`), not a
        socket frame — unlike OF, where it rides the pump. We still expose it
        here so the shared `of_ws.emit_typing` registry seam reaches it the
        same way for both platforms.

        The REST call is blocking, so it goes off the loop.
        """
        try:
            group_id = await asyncio.to_thread(self._resolve_group, fan_id)
            if not group_id:
                return False
            await asyncio.to_thread(self.client.typing, group_id)
            return True
        except Exception:
            log.debug("send_typing failed (fan=%s)", fan_id, exc_info=True)
            return False

    def _resolve_group(self, fan_id: int | str) -> str | None:
        """Fan accountId → groupId. The shim caches both directions off
        `list_chats`; `_as_group_id` also falls back to a live scan."""
        resolver = getattr(self.client, "_as_group_id", None)
        if resolver is None:
            return None
        return str(resolver(str(fan_id)) or "") or None

    # -- translation --------------------------------------------------------

    def _dedupe(self, key: tuple) -> bool:
        """True when `key` is new (and records it); False when already seen."""
        if key in self._seen:
            return False
        self._seen.add(key)
        self._seen_order.append(key)
        if len(self._seen_order) > _DEDUPE_MAX:
            self._seen.discard(self._seen_order.popleft())
        return True

    def _translate(self, frame: dict) -> dict | None:
        """One wire frame → an OF-shaped event dict, or None to drop it.

        Only frames we can express in OF's vocabulary are yielded. An
        untranslated frame yields NOTHING rather than a raw envelope: passing
        the triple-encoded shape downstream would make `transcode` log it as an
        unknown event type forever.
        """
        t = frame.get("t")
        if t != T_EVENT:
            return None

        # d is a JSON *string*; event is JSON again inside it.
        try:
            envelope = json.loads(frame.get("d") or "{}")
            service_id = envelope.get("serviceId")
            event = json.loads(envelope.get("event") or "{}")
        except (json.JSONDecodeError, TypeError):
            log.debug("undecodable t:10000 envelope")
            return None
        if not isinstance(event, dict):
            return None

        event_type = event.get("type")
        if service_id == SVC_MESSAGE and event_type == 1:
            return self._translate_message(event.get("message"))

        # Known-but-untranslated (typing announce, read receipts, presence,
        # follows) and everything else: silently ignored. Tips (serviceId 7)
        # and purchases (serviceId 2) decode by the same rule but no real one
        # has been observed on this account, so they are deliberately NOT
        # guessed at here — see the Non-goals in go_live_plan/01_ws_pump.md.
        return None

    def _translate_message(self, message: Any) -> dict | None:
        if not isinstance(message, dict):
            return None
        message_id = message.get("id")
        if not message_id:
            return None
        if not self._dedupe((SVC_MESSAGE, 1, str(message_id))):
            log.debug("dropping duplicate message frame %s", message_id)
            return None
        # The id and NOTHING else: this line is what distinguishes a frame the
        # pump carried from the identical row the 30 s poller would write, and
        # the relay log is not gitignored the way capture/out/ is.
        log.info("ws-pump inbound message frame id=%s", message_id)

        # Reshape with the shim's own reshaper rather than hand-building the
        # dict: it is the single place that turns snowflakes into strings,
        # normalizes createdAt (float epoch SECONDS on this socket), lifts a
        # KLIPY gif out of `content`, and gets the PPV lock semantics right.
        from fansly_shim import of_message
        account_id = str(getattr(self.client, "account_id", "") or "")
        # The socket frame carries bare `attachments` with no accountMedia[]
        # sidecar, so media cannot be resolved from it — `of_message` falls back
        # to the attachment count for `mediaCount`, which is all the transcoder
        # needs. The media objects themselves arrive with the poll's
        # `get_messages`, which is the system of record for display state.
        of_msg = of_message(message, account_id=account_id)
        return {"api2_chat_message": of_msg}

    # -- the loop -----------------------------------------------------------

    async def events(self) -> AsyncIterator[dict]:
        """Yield each translated event as it arrives, reconnecting with
        jittered exponential backoff. `.stop()` exits the loop promptly."""
        backoff = RECONNECT_MIN_S
        while not self._stop.is_set():
            try:
                # Reading the session token can touch disk; keep it off the loop
                # so a reconnect never stalls every other account's pump.
                token = await asyncio.to_thread(self._read_token)
                # NOTE: never log `token` or the t:1 frame body — the relay log
                # is not gitignored the way capture/out/ is.
                proxy_url = getattr(self.client, "proxy_url", None) or None
                log.info("connecting %s%s", WS_URL,
                         " via proxy" if proxy_url else " (direct — no proxy assigned)")
                async with websockets.connect(
                    WS_URL,
                    origin="https://fansly.com",
                    user_agent_header=getattr(self.client, "user_agent", None)
                    or getattr(getattr(self.client, "session", None), "user_agent", None),
                    max_size=8 * 1024 * 1024,
                    open_timeout=15,
                    close_timeout=5,
                    # Deliberately NOT passing ping_interval: the library default
                    # (RFC-6455 ping every 20s, drop on missed pong) is what
                    # detects a half-open TCP. The in-band bare `p` below is
                    # Fansly's APP-level keepalive and proves nothing about
                    # liveness. Without one of the two a zombie pump means
                    # realtime is silently dead.
                    proxy=proxy_url,
                ) as ws:
                    # Auth is in-band and `d` is a double-encoded JSON string.
                    await ws.send(json.dumps(
                        {"t": T_AUTH, "d": json.dumps({"token": token, "v": 3})}
                    ))
                    self._live_ws = ws
                    if self._down_since is not None:
                        log.info("reconnected after %.1fs of realtime downtime",
                                 time.monotonic() - self._down_since)
                        self._down_since = None
                    pinger = asyncio.create_task(_pinger(ws, self._stop))
                    stop_waiter = asyncio.create_task(self._stop.wait())
                    recv_task: asyncio.Task | None = None
                    try:
                        while not self._stop.is_set():
                            recv_task = asyncio.create_task(ws.recv())
                            done, _ = await asyncio.wait(
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
                                frame = json.loads(raw)
                            except json.JSONDecodeError:
                                # `p`/`pong` style bare literals are expected.
                                if raw.strip() not in ("p", "pong", ""):
                                    log.warning("non-JSON frame: %r", raw[:200])
                                continue
                            if not isinstance(frame, dict):
                                continue
                            event = self._translate(frame)
                            if event is not None:
                                yield event
                    finally:
                        self._live_ws = None
                        pinger.cancel()
                        if not stop_waiter.done():
                            stop_waiter.cancel()
                        # Cancelling this task (account removed / shutdown)
                        # suspends us at the `yield` with a recv still in
                        # flight. Retire it here, or every teardown logs a
                        # stray "Task exception was never retrieved".
                        if recv_task is not None and not recv_task.done():
                            recv_task.cancel()
                backoff = RECONNECT_MIN_S
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if self._down_since is None:
                    self._down_since = time.monotonic()
                # Jitter the backoff: with many Fansly accounts, one wsv3 blip
                # would otherwise reconnect them all in lockstep — a thundering
                # herd against the same endpoint.
                delay = backoff * (0.5 + random.random())
                log.warning("ws error %s — reconnecting in %.1fs", e, delay)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=delay)
                    return
                except asyncio.TimeoutError:
                    pass
                backoff = min(int(backoff * 2) or 1, RECONNECT_MAX_S)

    def stop(self) -> None:
        self._stop.set()


async def _pinger(ws, stop: asyncio.Event) -> None:
    """Send the app-level keepalive: a BARE literal `p`, not JSON. (Sending
    `{"act":"..."}` here — the OF shape — is not understood by wsv3.)"""
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=PING_INTERVAL_S)
            return
        except asyncio.TimeoutError:
            pass
        try:
            await ws.send("p")
        except Exception:
            return
