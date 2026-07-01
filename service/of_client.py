"""
OnlyFans HTTP client.

Loads a captured session (from capture_session.py + extract_rules.py) and
wraps `requests.Session` with the headers OF expects on every signed call:
  Sign, Time   — generated per request via of_signer.sign()
  User-ID, X-BC, X-OF-Rev, User-Agent  — pinned from the captured session
  X-Hash       — fetched once from cdn2.onlyfans.com/hash/?u={user_id}
  App-Token    — hardcoded (OF web client constant)

  client = OFClient.from_latest_session()
  client.me()
  client.list_chats(limit=10)
  client.get_messages(chat_id="...")
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from curl_cffi import requests  # Chrome TLS fingerprint via libcurl-impersonate

import accounts as account_registry
import live_rev
import proxies as proxy_registry
from of_signer import sign

# Dedicated logger so proxy/upstream errors are filterable from the rest
# of the relay log noise: `grep "of-client" /tmp/of-relay.log`.
log_of = logging.getLogger("of-client")


def _short_path(url: str) -> str:
    """Strip scheme/host/query so log lines stay terse: /api2/v2/chats."""
    try:
        s = urlsplit(url)
        return s.path or url
    except Exception:
        return url[:120]

# curl_cffi impersonation profile. "chrome" picks the latest stable Chrome JA3.
# Pin to a specific version (e.g. "chrome131") if OF starts blocking newer ones.
IMPERSONATE = "chrome"

APP_TOKEN = "33d57ade8c02dbc5a333db99ff9ae26a"   # OF web app constant
API_BASE = "https://onlyfans.com/api2/v2"
HASH_BASE = "https://cdn2.onlyfans.com/hash"
DEFAULT_TIMEOUT_S = 30

HERE = Path(__file__).resolve().parent
SESSIONS_DIR = HERE / "sessions"


class OFAPIError(Exception):
    """Non-2xx from OF (or upstream Cloudflare). Carries the raw response."""
    def __init__(self, message: str, response=None):
        super().__init__(message)
        self.response = response


def _wrap_text_as_p(text: str) -> str:
    """OF web's chat textarea is a rich-text editor that always emits a
    `<p>…</p>` wrapper. Plain-text sends to fans work without wrapping,
    but creator-to-creator sends 400 unless wrapped — match OF web's
    body exactly here. Multi-paragraph text becomes one `<p>` with
    `<br>` separators, mirroring what the OF editor produces."""
    if not text:
        return text
    stripped = text.strip()
    # Already HTML? Don't double-wrap.
    if stripped.startswith("<p") or stripped.startswith("<P"):
        return text
    # Internal newlines → <br>, then wrap the whole thing.
    escaped = (
        stripped
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("\n", "<br />")
    )
    return f"<p>{escaped}</p>"


class OFClient:
    def __init__(self, session_dict: dict, *, proxies: dict | None = None,
                 proxy_label: str | None = None,
                 timeout_s: int = DEFAULT_TIMEOUT_S):
        h = session_dict["headers"]
        rules = session_dict["signing"]["rules"]
        for k in ("static_param", "start", "end", "checksum_indexes", "checksum_constant"):
            if rules.get(k) in (None, ""):
                raise ValueError(
                    f"Session signing.rules missing/empty: {k}. "
                    f"Run: ./venv/bin/python service/extract_rules.py"
                )

        self.user_id = str(h["user_id"])
        self.x_bc = h["x_bc"]
        self.x_of_rev = h["x_of_rev"]
        self.user_agent = h["user_agent"]
        self.rules = rules
        self.timeout_s = timeout_s
        self.proxy_label = proxy_label
        self.proxy_url: str | None = (proxies or {}).get("https") or (proxies or {}).get("http")
        # The account this client belongs to. For single-account callers this
        # is identical to user_id; for multi-account, this is the account_id
        # (which we currently key by user_id, but they are semantically
        # distinct so the server can rename / re-key without touching OFClient).
        self.account_id = str(h["user_id"])

        # curl_cffi.requests.Session takes impersonate at construction; every
        # subsequent .get()/.post() uses that TLS profile + matching default headers.
        self.http = requests.Session(impersonate=IMPERSONATE)
        if proxies:
            self.http.proxies.update(proxies)

        # Replay every cookie Playwright captured (auth_id, sess, fp, etc.)
        for c in session_dict.get("cookies", []):
            self.http.cookies.set(
                c["name"], c["value"],
                domain=c.get("domain"),
                path=c.get("path", "/"),
            )

        # Fetched lazily on first signed call (it's a single GET to the CDN).
        self._x_hash: str | None = None

    # ── Constructors ────────────────────────────────────────────

    @classmethod
    def from_session_file(cls, path: str | Path, **kw) -> "OFClient":
        """Loads a session file. Auto-attaches its assigned proxy from the
        registry unless caller passes an explicit `proxies=` (escape hatch).

        Proxy lookup order:
          1. Per-account binding (assigned_account_id == path.parent.name)
             — new scheme; survives re-captures.
          2. Per-session-file binding (assigned_session == path.name)
             — legacy; remains supported for back-compat.
        """
        path = Path(path)
        data = json.loads(path.read_text())
        if "proxies" not in kw and "proxy_label" not in kw:
            # Per-account dirs live at sessions/accounts/<id>/session_*.json
            # so the immediate parent dir name is the account_id.
            account_id = path.parent.name
            p = proxy_registry.get_for_account(account_id)
            if p is None:
                p = proxy_registry.get_for_session(path.name)
            if p is not None:
                kw["proxies"] = proxy_registry.proxies_dict_for_requests(p)
                kw["proxy_label"] = p["label"]
        return cls(data, **kw)

    @classmethod
    def from_account(cls, account_id: str, **kw) -> "OFClient":
        """Load the OFClient that serves a specific account. The session lives
        at sessions/accounts/<account_id>/latest.json."""
        path = account_registry.latest_session_path(account_id)
        if path is None:
            raise FileNotFoundError(
                f"Account {account_id!r} has no captured session. "
                f"Run the bootstrap (incogniton-default | paste-curl) for this account first."
            )
        return cls.from_session_file(path, **kw)

    @classmethod
    def from_latest_session(cls, **kw) -> "OFClient":
        """Legacy entry point: resolves the *currently-active* account and
        loads its session. Kept so single-account callers keep working."""
        aid = account_registry.get_active_account_id()
        if not aid:
            raise FileNotFoundError(
                "No account has a captured session yet. "
                "POST /admin/session/bootstrap or run capture_session.py."
            )
        return cls.from_account(aid, **kw)

    # ── Egress probe ────────────────────────────────────────────

    def egress_ip(self, timeout_s: int = 10) -> str | None:
        """Returns the IP that OF would see for this client (proxy egress or
        direct WAN). Goes through the same curl_cffi session so we measure
        what real OF calls measure, not Python's default route."""
        try:
            r = self._proxy_retry(
                lambda: self.http.get("https://api.ipify.org", timeout=timeout_s),
                what="egress-ip")
            if r.ok:
                return r.text.strip()
        except Exception:
            pass
        return None

    # ── Internal: x-hash ────────────────────────────────────────

    def _ensure_x_hash(self) -> str:
        if self._x_hash:
            return self._x_hash
        # This warmup fires on the FIRST signed request of every fresh client
        # (and _make_client builds a new client every automation tick), so it
        # must get the same CONNECT-403 retry as real OF calls — otherwise a
        # single datacenter proxy blip fails the whole tick/job before
        # _http_call's _proxy_retry is ever reached (it runs inside
        # _signed_headers, ahead of the wrapped request).
        r = self._proxy_retry(lambda: self.http.get(
            f"{HASH_BASE}/?u={self.user_id}",
            headers={
                "Accept": "application/json, text/plain, */*",
                "Referer": "https://onlyfans.com/",
                "User-Agent": self.user_agent,
            },
            timeout=self.timeout_s,
        ), what="x-hash")
        r.raise_for_status()
        self._x_hash = r.text.strip()
        return self._x_hash

    # ── Core signed HTTP ────────────────────────────────────────

    def _build_url(self, path_or_url: str, params: dict | None) -> str:
        url = path_or_url if path_or_url.startswith("http") else f"https://onlyfans.com{path_or_url}"
        if params:
            # Bake params into the URL so the signed path matches what's sent.
            from urllib.parse import urlencode, urlparse, urlunparse, parse_qsl
            p = urlparse(url)
            q = parse_qsl(p.query, keep_blank_values=True)
            q.extend(params.items())
            url = urlunparse(p._replace(query=urlencode(q, doseq=True)))
        return url

    def _signed_headers(self, url: str) -> dict:
        """Build the full header set for a signed request. Sign is fresh per call."""
        sig = sign(url, self.user_id, self.rules)
        return {
            "Accept": "application/json, text/plain, */*",
            "App-Token": APP_TOKEN,
            "Sign": sig["sign"],
            "Time": sig["time"],
            "User-ID": self.user_id,
            "X-BC": self.x_bc,
            "X-Hash": self._ensure_x_hash(),
            "X-OF-Rev": self.x_of_rev,
            "User-Agent": self.user_agent,
            "Referer": "https://onlyfans.com/",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        }

    def get(self, path_or_url: str, *, params: dict | None = None) -> requests.Response:
        """Signed GET. `path_or_url` may be a full URL or an /api2/v2/... path."""
        url = self._build_url(path_or_url, params)
        return self._http_call("GET", url)

    def _http_call(self, method: str, url: str, *,
                   json_body: dict | None = None) -> requests.Response:
        """All outbound OF calls funnel here so failures (proxy 403, TLS, DNS,
        timeout) get a single structured log line with the context we need to
        debug: which account, which proxy, which OF endpoint.

        Also the single place we passively observe OF's current build hash:
        every response carries `x-of-rev` in the headers, which feeds the
        global live-rev cache for drift detection (see live_rev.py)."""
        headers = self._signed_headers(url)
        if json_body is not None:
            headers["Content-Type"] = "application/json"
        try:
            # Datacenter providers (e.g. IPRoyal DC) sporadically 403 on CONNECT
            # under load; the same IP works again ~400ms later. _proxy_retry()
            # retries a few times against the SAME self.http session (sticky IP,
            # no rotation) — shared with every raw self.http call below.
            def _do() -> requests.Response:
                if method == "GET":
                    return self.http.get(url, headers=headers, timeout=self.timeout_s)
                return self.http.request(method, url, json=json_body,
                                         headers=headers, timeout=self.timeout_s)
            r = self._proxy_retry(_do, what=f"{method} {_short_path(url)}")
            # Opportunistic rev capture. OF echoes their current build in
            # `x-of-rev` on most /api2/v2/* responses; note() is a no-op
            # if the header's missing or unchanged so this is cheap on
            # every call. If this ever raises we want to know — it's the
            # only way the global drift detector gets fresh data.
            try:
                rev_hdr = r.headers.get("x-of-rev") or r.headers.get("X-OF-Rev")
                if rev_hdr:
                    live_rev.note(rev_hdr, source=f"response:{self.account_id}")
            except Exception:
                log_of.warning(
                    "live_rev.note failed account=%s url=%s",
                    self.account_id, _short_path(url), exc_info=True,
                )
            return r
        except requests.exceptions.ProxyError as e:
            log_of.warning(
                "proxy_error account=%s proxy=%s(%s) method=%s url=%s err=%s",
                self.account_id, self.proxy_label or "—",
                self._proxy_ip(), method, _short_path(url), str(e)[:200],
            )
            raise
        except requests.exceptions.Timeout as e:
            log_of.warning(
                "timeout account=%s proxy=%s method=%s url=%s err=%s",
                self.account_id, self.proxy_label or "—", method,
                _short_path(url), str(e)[:200],
            )
            raise
        except Exception as e:
            log_of.warning(
                "http_error account=%s proxy=%s method=%s url=%s err=%s",
                self.account_id, self.proxy_label or "—", method,
                _short_path(url), str(e)[:200],
            )
            raise

    def _proxy_ip(self) -> str:
        """Extract host:port from the proxy URL for logging without leaking creds."""
        if not self.proxy_url:
            return "direct"
        try:
            # http://user:pass@host:port → host:port
            after_at = self.proxy_url.split("@", 1)[-1]
            return after_at
        except Exception:
            return "?"

    def _proxy_retry(self, fn, *, tries: int = 3, pause_s: float = 0.4, what: str = ""):
        """Run a raw curl_cffi call `fn`, retrying the transient datacenter
        CONNECT 403 (IPRoyal DC blips ~400ms under load). Retries run against
        the SAME self.http session — no proxy lookup, no rebuild, no rotation —
        so OF still sees one sticky IP. This is the single retry policy: both
        _http_call AND the handful of raw self.http.get/post calls (CDN media
        download, /users/list, convert upload) funnel through it, so one
        half-second proxy blip can no longer hard-fail a request."""
        for attempt in range(tries):
            try:
                return fn()
            except requests.exceptions.ProxyError as e:
                if attempt < tries - 1:
                    log_of.info(
                        "proxy_error_retry account=%s proxy=%s(%s) attempt=%d/%d %s err=%s",
                        self.account_id, self.proxy_label or "—", self._proxy_ip(),
                        attempt + 1, tries, what, str(e)[:200],
                    )
                    time.sleep(pause_s)
                    continue
                raise

    def get_json(self, path_or_url: str, **kw) -> Any:
        r = self.get(path_or_url, **kw)
        if not r.ok:
            raise OFAPIError(
                f"{r.status_code} for {r.url}\n{r.text[:500]}",
                response=r,
            )
        return r.json()

    def request(self, method: str, path_or_url: str, *,
                json_body: dict | None = None,
                params: dict | None = None) -> requests.Response:
        """Generic signed request. `method` in {GET, POST, PUT, PATCH, DELETE}.
        OF's sign algorithm is body-independent, so the same headers work for all methods."""
        url = self._build_url(path_or_url, params)
        return self._http_call(method.upper(), url, json_body=json_body)

    def request_json(self, method: str, path_or_url: str, **kw) -> Any:
        """request() + raise OFAPIError on non-2xx + .json() the success body.
        Treats 204 No Content as None (some OF writes return empty bodies)."""
        r = self.request(method, path_or_url, **kw)
        if not r.ok:
            raise OFAPIError(f"{r.status_code} for {r.url}\n{r.text[:500]}", response=r)
        if r.status_code == 204 or not r.text:
            return None
        return r.json()

    # Convenience: same surface as before so existing callers don't break.
    def post(self, path_or_url: str, **kw) -> requests.Response:
        return self.request("POST", path_or_url, **kw)

    def post_json(self, path_or_url: str, **kw) -> Any:
        return self.request_json("POST", path_or_url, **kw)

    def put_json(self, path_or_url: str, **kw) -> Any:
        return self.request_json("PUT", path_or_url, **kw)

    def delete_json(self, path_or_url: str, **kw) -> Any:
        return self.request_json("DELETE", path_or_url, **kw)

    # ── Endpoints ───────────────────────────────────────────────

    def me(self) -> dict:
        """GET /api2/v2/users/me — sanity-check the session (no chat needed)."""
        return self.get_json(f"{API_BASE}/users/me")

    def list_chats(self, *, limit: int = 10, order: str = "recent",
                   offset: int = 0, filter: str | None = None,
                   list_id: int | str | None = None,
                   query: str | None = None,
                   skip_users: str | None = "all") -> dict:
        """GET /api2/v2/chats — list conversations.

        OF UI uses three orthogonal filters:
          - `filter`: one of {unread, pinned, priority} or None for all.
          - `list_id`: when set, only chats whose fan is in that custom list
            (folder) are returned. Combine with filter=None for "all in folder".
          - `query`: free-text search by fan name/username — captured curl
            from /my/chats/?q=… uses `query=` (NOT `search=`, which OF rejects
            with "Invalid filter" on this endpoint).

        All defaults match the captured DevTools curls (skip_users=all,
        order=recent). offset+limit are inclusive — i.e. offset=10&limit=10
        gives the 11th through 20th chat.

        `skip_users="all"` (the OF UI default) strips the heavy relationship
        flags (`subscribedOn`/`subscribedBy`) from each `withUser` — fine for the
        inbox list, but it's exactly what left the chat-scrape unable to classify
        peer-creators (source stuck as 'onlyfans'). Pass `skip_users=None` to omit
        the param and get the full `withUser` so callers can classify the source.
        """
        params: dict[str, Any] = {
            "limit": limit, "order": order, "offset": offset,
        }
        if skip_users is not None:
            params["skip_users"] = skip_users
        if filter:
            params["filter"] = filter
        if list_id is not None:
            params["list_id"] = list_id
        if query:
            params["query"] = query
        return self.get_json(f"{API_BASE}/chats", params=params)

    def chat_folders(self, *, limit: int = 10, offset: int = 0,
                     filter: str = "can_pin_chat") -> dict:
        """GET /api2/v2/lists?isChat=true&format=infinite — fan-list 'folders'
        the user has created. The OF chat sidebar pins them as filter chips.
        `filter='can_pin_chat'` returns lists eligible for pinning."""
        params: dict[str, Any] = {
            "filter": filter, "isChat": "true", "skip_users": "all",
            "format": "infinite", "limit": limit, "offset": offset,
        }
        return self.get_json(f"{API_BASE}/lists", params=params)

    def set_list_pinned_to_chat(self, list_id: int | str, pinned: bool) -> dict:
        """PATCH /api2/v2/lists/{id} — pin/unpin a fan list as a chat folder.
        Captured DevTools: `{"isPinnedToChat": true|false}`."""
        return self.request_json("PATCH", f"{API_BASE}/lists/{list_id}",
                                  json_body={"isPinnedToChat": pinned})

    def get_messages(self, chat_id: str | int, *, limit: int = 10,
                     order: str = "desc", before_id: str | int | None = None,
                     offset: int | None = None) -> dict:
        """GET /api2/v2/chats/{chat_id}/messages — single page of history.

        Probed live (read-only): OF IGNORES `limit` and returns FEWER than asked
        (desc 50->34, 100->71; asc 50 was honored). NEVER assume the page size —
        always loop on `hasMore`.

        `before_id` maps to the `id` param, which ALWAYS means "older than id"
        (an upper bound) in BOTH orders — it only pages BACKWARD. So
        `order=desc` + `before_id=<oldest seen>` is the correct older-cursor.

        `offset` pages from a fixed position; pair it with `order=asc` to walk
        FORWARD from the convo START (`offset=0` returns the true first
        messages, bump it for more)."""
        params: dict[str, Any] = {"limit": limit, "order": order, "skip_users": "all"}
        if before_id is not None:
            params["id"] = before_id
        if offset is not None:
            params["offset"] = offset
        return self.get_json(f"{API_BASE}/chats/{chat_id}/messages", params=params)

    def iter_messages(self, chat_id: str | int, *, page_size: int = 10,
                      delay_s: float = 0.3, max_pages: int | None = None,
                      from_id: int | None = None):
        """Generator yielding each page of messages (newest first) until hasMore=False.

        Mirrors the Fast Fetch mode from the cracked extension: pages with
        `&id=<last_message_id>` as the older-cursor, a 300ms inter-page delay
        to avoid rate-limiting, deduplication on message id.

        `from_id` lets the caller resume paginating older — pass the oldest
        message id you already have, and the generator yields strictly older
        pages. Useful for "load older" buttons in a UI: load 50 msgs, user
        scrolls/clicks, fetch the next 50 from where we stopped.

        Yields dicts: {"page": int, "messages": [...], "hasMore": bool}."""
        seen: set = set()
        cursor: int | None = from_id
        page_num = 0
        while True:
            page = self.get_messages(chat_id, limit=page_size, order="desc", before_id=cursor)
            page_num += 1
            msgs = page.get("list") or []
            new = [m for m in msgs if m["id"] not in seen]
            for m in new:
                seen.add(m["id"])
            has_more = bool(page.get("hasMore"))
            yield {"page": page_num, "messages": new, "hasMore": has_more}
            if not has_more or not msgs:
                return
            if max_pages and page_num >= max_pages:
                return
            cursor = msgs[-1]["id"]
            if delay_s:
                import time as _t
                _t.sleep(delay_s)

    def get_all_messages(self, chat_id: str | int, *, page_size: int = 10,
                         delay_s: float = 0.3, max_pages: int | None = None,
                         from_id: int | None = None) -> list[dict]:
        """Collect every message in a chat (Fast Fetch). Returns one flat list.
        Pass `from_id` to resume from a specific cursor (older than that id)."""
        out: list[dict] = []
        for page in self.iter_messages(chat_id, page_size=page_size,
                                       delay_s=delay_s, max_pages=max_pages,
                                       from_id=from_id):
            out.extend(page["messages"])
        return out

    # ── Users ───────────────────────────────────────────────────

    def get_user(self, user_id_or_username: str | int) -> dict:
        """GET /api2/v2/users/{id_or_username} — full profile for a single user.
        Accepts a numeric id or a username string (OF resolves both)."""
        return self.get_json(f"{API_BASE}/users/{user_id_or_username}")

    def list_users(self, user_ids: list[str | int], *, view: str = "m") -> dict:
        """GET /api2/v2/users/list?{view}[]=ID1&{view}[]=ID2 — batch user lookup.

        `view` selects the depth of fields returned:
          - "m" (default): chat-list view — matches the `withUser._view: "m"` you
            get from /chats. Slim, just what's needed to render a chat list.
          - "x": extended view — more fields per user (subscribe state, etc.).

        OF accepts up to ~50 ids per call; chunk larger lists yourself.

        Returns a dict keyed by user-id string: `{"117183": {...}, "470702183": {...}}`.
        Use `.values()` if you need a flat iterable."""
        if not user_ids:
            return {}
        if view not in ("m", "x"):
            raise ValueError(f"view must be 'm' or 'x', got {view!r}")
        from urllib.parse import urlencode
        qs = urlencode([(f"{view}[]", str(uid)) for uid in user_ids])
        url = f"{API_BASE}/users/list?{qs}"
        r = self._proxy_retry(
            lambda: self.http.get(url, headers=self._signed_headers(url), timeout=self.timeout_s),
            what="GET /users/list",
        )
        if not r.ok:
            raise OFAPIError(f"{r.status_code} for {r.url}\n{r.text[:500]}", response=r)
        return r.json()

    # ── Dashboard / init / notifications ───────────────────────

    def init(self) -> dict:
        """GET /api2/v2/init — bootstrap data: notifications, counts, feature flags.
        OF's web app calls this on every page load. Cheap, useful for dashboards."""
        return self.get_json(f"{API_BASE}/init")

    def notifications(self, *, limit: int = 10, offset: int = 0,
                      type: str | None = None) -> dict:
        """GET /api2/v2/users/notifications — alerts feed.
        Note: prefix is /users/notifications, NOT /notifications (my earlier
        guess was wrong; captured via XHR audit). Working `type` filters use the
        PAST-TENSE singular: 'subscribed', 'tipped', 'commented', 'mentioned'
        (verified live 2026-06). Plural/gerund forms like 'subscribes'/
        'subscriptions' 400. Untyped returns ALL types and can be flooded by
        moderation events (`deactivated_media`) — always pass `type` when you
        want a specific feed."""
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if type: params["type"] = type
        return self.get_json(f"{API_BASE}/users/notifications", params=params)

    def notifications_count(self) -> dict:
        """GET /api2/v2/users/notifications/count — badge count per category."""
        return self.get_json(f"{API_BASE}/users/notifications/count")

    def notification_tabs_order(self) -> dict:
        """GET /api2/v2/users/notifications/settings/tabs-order — UI tab order pref."""
        return self.get_json(f"{API_BASE}/users/notifications/settings/tabs-order")

    def my_settings(self) -> dict:
        """GET /api2/v2/users/me/settings — full settings dump."""
        return self.get_json(f"{API_BASE}/users/me/settings")

    def hints(self) -> dict:
        """GET /api2/v2/users/hints — UI hints (banners, onboarding)."""
        return self.get_json(f"{API_BASE}/users/hints")

    def stories_items(self) -> dict:
        """GET /api2/v2/stories/items — items used in stories."""
        return self.get_json(f"{API_BASE}/stories/items")

    def stories_map(self) -> dict:
        """GET /api2/v2/stories/map — story analytics map (geo)."""
        return self.get_json(f"{API_BASE}/stories/map")

    def streams_feed(self) -> dict:
        """GET /api2/v2/streams/feed — live streams feed."""
        return self.get_json(f"{API_BASE}/streams/feed")

    def streams_reminders(self) -> dict:
        """GET /api2/v2/streams/reminder — my stream reminders."""
        return self.get_json(f"{API_BASE}/streams/reminder")

    def payout_requests(self, *, limit: int = 10, offset: int = 0) -> dict:
        """GET /api2/v2/payouts/requests — payout request history (Statements page)."""
        return self.get_json(f"{API_BASE}/payouts/requests",
                             params={"limit": limit, "offset": offset})

    def subscription_counts_all(self) -> dict:
        """GET /api2/v2/subscriptions/count/all — full breakdown (active/expired/blocked/...)."""
        return self.get_json(f"{API_BASE}/subscriptions/count/all")

    def my_promotions(self) -> dict:
        """GET /api2/v2/users/promotions — my outbound promo list (alt to /promotions)."""
        return self.get_json(f"{API_BASE}/users/promotions")

    def vault_media_types(self) -> dict:
        """GET /api2/v2/vault/media/types — allowed media MIME types."""
        return self.get_json(f"{API_BASE}/vault/media/types")

    def vault_media_processing(self) -> dict:
        """GET /api2/v2/vault/media/processing — uploads currently being processed."""
        return self.get_json(f"{API_BASE}/vault/media/processing")

    def posts_on_this_day(self) -> dict:
        """GET /api2/v2/users/posts/on-this-day — memories: my old posts from this date."""
        return self.get_json(f"{API_BASE}/users/posts/on-this-day")

    def labels(self) -> list:
        """GET /api2/v2/labels — fan labels (colored tags)."""
        return self.get_json(f"{API_BASE}/labels")

    # ── User-facing search / lookup ────────────────────────────

    def search_users(self, query: str, *, limit: int = 10) -> list:
        """GET /api2/v2/users?q=... — search by name/username. Returns a list."""
        return self.get_json(f"{API_BASE}/users", params={"q": query, "limit": limit})

    def tagged_friend_users(
        self, *,
        search: str | None = None,
        limit: int = 20,
        offset: int = 0,
        filter: str = "all",
        sort: str = "date:desc",
        skip_users: str = "all",
    ) -> Any:
        """GET /api2/v2/posts/tagged-friend-users — creators eligible to be
        tagged in posts AND chat messages. Captured live 2026-05-23 from
        the OF web chat composer (referer was /my/chats/chat/{id}/), so
        despite the `/posts/` prefix this is the same endpoint OF uses for
        chat @-tags.

        Search is by `name=<query>` (OF rejects `search=`). Response shape
        is `{items, hasMore}` (NOT `{list, hasMore}` like vault endpoints).
        Filter `all` + sort `date:desc` mirror the captured call."""
        params: dict[str, Any] = {
            "limit": int(limit),
            "offset": int(offset),
            "skip_users": skip_users,
            "filter": filter,
            "sort": sort,
        }
        if search:
            params["name"] = search[:100]
        return self.get_json(f"{API_BASE}/posts/tagged-friend-users", params=params)

    def user_posts(self, user_id: str | int, *, limit: int = 10,
                   skip_users: str = "all", type: str | None = None,
                   before_publish_time: str | None = None,
                   order: str = "publish_date_desc",
                   format: str | None = None) -> dict:
        """GET /api2/v2/users/{user_id}/posts — posts owned by a creator.

        `type` filters: 'photo'|'video'|'audio'|'archived' (where allowed).
        `before_publish_time` is OF's cursor for older pages; pass the
        `tailMarker` string from the previous response (e.g.
        '1777499224.000000') to step backwards.
        `format='infinite'` makes OF return tailMarker/headMarker so the
        caller can stitch pages together."""
        params: dict[str, Any] = {"limit": limit, "skip_users": skip_users}
        if type: params["type"] = type
        if order: params["order"] = order
        if format: params["format"] = format
        if before_publish_time: params["beforePublishTime"] = before_publish_time
        return self.get_json(f"{API_BASE}/users/{user_id}/posts", params=params)

    def bookmarked_posts(self, *, limit: int = 10, offset: int = 0) -> list:
        """GET /api2/v2/posts/bookmarks — posts I've bookmarked."""
        return self.get_json(f"{API_BASE}/posts/bookmarks",
                             params={"limit": limit, "offset": offset})

    # ── Subscribers (fans) ─────────────────────────────────────

    def subscribers(self, *, type: str = "active", limit: int = 10, offset: int = 0,
                    sort: str = "-users.last_activity", filter: str | None = None,
                    online: bool | None = None, min_spent: int | None = None,
                    min_tips: int | None = None) -> dict:
        """GET /api2/v2/subscriptions/subscribers — fans subscribed to me.
        `type` in {'active','expired','all'}. `filter` accepts 'attention',
        'recent', 'muted' depending on OF version.

        Object filters (mirror OF web's `filter[...]` query params):
          - `online=True`  → `filter[online]=1`  (only fans currently online)
          - `min_spent=N`  → `filter[total_spent]=N` (fans who spent ≥ N)
          - `min_tips=N`   → `filter[tips]=N`
        VERIFIED LIVE — OF's "Subscribers · online" list (referer
        /my/collections/user-lists/subscribers/active?online=1) hits exactly
        this with `filter[online]=1&format=infinite&more=true`."""
        params: dict[str, Any] = {"type": type, "limit": limit, "offset": offset, "sort": sort}
        if filter:
            params["filter"] = filter
        if online is not None:
            params["filter[online]"] = 1 if online else 0
        if min_spent is not None:
            params["filter[total_spent]"] = int(min_spent)
        if min_tips is not None:
            params["filter[tips]"] = int(min_tips)
        return self.get_json(f"{API_BASE}/subscriptions/subscribers", params=params)

    def iter_online_subscribers(self, *, type: str = "active", page_size: int = 20,
                                max_fans: int = 1000) -> list[dict]:
        """Convenience: page through ALL currently-online subscribers and return
        the flat list of fan objects. OF caps `limit` at 20 for this list, so we
        walk `offset` until `hasMore` is false (or `max_fans` is hit)."""
        out: list[dict] = []
        offset = 0
        page_size = max(1, min(int(page_size), 20))
        while len(out) < max_fans:
            page = self.subscribers(type=type, limit=page_size, offset=offset, online=True)
            rows = page.get("list", page) if isinstance(page, dict) else page
            if not rows:
                break
            out.extend(rows)
            if isinstance(page, dict) and not page.get("hasMore"):
                break
            offset += page_size
        return out[:max_fans]

    def unread_chat_fan_ids(self, *, max_fans: int = 100, page_size: int = 20) -> list[int]:
        """Fan ids of chats with UNREAD messages (fans waiting on a reply),
        newest-first, capped at `max_fans`. Pages `GET /chats?filter=unread`
        (the OF "Unread" inbox tab) and pulls `withUser.id` from each chat.
        Hand the list to `send_mass_message(included_users=...)`."""
        out: list[int] = []
        seen: set[int] = set()
        offset = 0
        page_size = max(1, min(int(page_size), 50))
        while len(out) < max_fans:
            page = self.list_chats(limit=page_size, offset=offset, filter="unread")
            rows = page.get("list", page) if isinstance(page, dict) else page
            if not rows:
                break
            for ch in rows:
                fid = (ch.get("withUser") or {}).get("id")
                if fid and int(fid) not in seen:
                    seen.add(int(fid))
                    out.append(int(fid))
            if isinstance(page, dict) and not page.get("hasMore"):
                break
            offset += page_size
        return out[:max_fans]

    # subscriptions (creators I follow): no clean direct path found on this
    # account. Use /subscribers with type=all (these are subscribed *to me* though,
    # so this is for creator accounts). For "creators I'm subscribed to as a fan"
    # a different OF endpoint exists in some versions but isn't reliable here.

    def subscription_counts(self) -> dict:
        """GET /api2/v2/subscriptions/count — counts of active/expired etc."""
        return self.get_json(f"{API_BASE}/subscriptions/count")

    # ── Posts (own + fan) ──────────────────────────────────────

    def list_posts(self, *, limit: int = 10, offset: int = 0,
                   order: str = "publish_date_desc",
                   skip_users: str = "all") -> dict:
        """GET /api2/v2/posts — my posts."""
        return self.get_json(
            f"{API_BASE}/posts",
            params={"limit": limit, "offset": offset, "order": order, "skip_users": skip_users},
        )

    def get_post(self, post_id: int | str) -> dict:
        """GET /api2/v2/posts/{post_id} — single post detail with media."""
        return self.get_json(f"{API_BASE}/posts/{post_id}")

    def post_comments(self, post_id: int | str, *, limit: int = 20, offset: int = 0,
                      order: str = "publish_date_desc") -> dict:
        """GET /api2/v2/posts/{post_id}/comments — comments on a post.

        Works on YOUR posts. On other creators' posts OF may 404 with
        'User cannot comment' if the post has comments disabled."""
        return self.get_json(
            f"{API_BASE}/posts/{post_id}/comments",
            params={"limit": limit, "offset": offset, "order": order},
        )

    # ── Vault (own media library) ──────────────────────────────

    def vault_media(self, *, limit: int = 24, offset: int = 0,
                    type: str = "all", list_id: int | None = None,
                    sort: str = "desc", field: str = "recent",
                    query: str | None = None) -> dict:
        """GET /api2/v2/vault/media — vault items.

        `type`: 'all'|'photo'|'video'|'gif'|'audio' — **OF uses `?type=`,
        not `?filter=`**. The latter is silently ignored, which we found out
        when filter=video returned photos.
        `list_id`: restrict to a vault list/folder. OF takes this on the
        `list` param (replacing the default 'all'), NOT as a separate
        `list_id` — sending both silently ignores the filter.
        `sort` / `field`: ordering. **`sort=asc` IS honored server-side**
        (verified live: asc → 2018 items, desc → 2026 items) — OF's field name
        is `sort`, values `asc`/`desc`, with `field=recent`.
        `query`: OF-native full-text vault search. The wire param is **`query`**
        (verified live: `query=bikini` narrowed 50→5; `search`/`q`/`text` are
        silently ignored). Empty/None omits it."""
        params: dict[str, Any] = {
            "limit": limit, "offset": offset, "sort": sort, "field": field,
            "list": str(list_id) if list_id is not None else "all",
        }
        if type and type != "all":
            params["type"] = type
        if query and query.strip():
            params["query"] = query.strip()
        return self.get_json(f"{API_BASE}/vault/media", params=params)

    def vault_media_by_id(self, media_id: int) -> dict:
        """GET /api2/v2/vault/media/{id} — a single vault item by its id.

        OF exposes a real by-id read here (verified live): the response is one
        media dict with the SAME shape as an item inside `vault_media()['list']`
        — `files.thumb/squarePreview/preview` for thumbnails, `type`, `duration`,
        `videoSources`, etc. There is NO batch form (`?ids[]=` is silently
        ignored and returns the default page), so callers resolve one id per
        call. A deleted/unknown id raises OFAPIError 404 'Media Not Found'.

        Used to resolve already-saved per-slot brain images (`time_images`,
        which store bare media ids) back to thumbnails — the picker only ever
        returned full media for freshly-picked items, never the saved ids."""
        return self.get_json(f"{API_BASE}/vault/media/{int(media_id)}")

    def vault_lists(self, *, view: str = "main", limit: int = 10, offset: int = 0) -> dict:
        """GET /api2/v2/vault/lists — vault folders / lists.
        The mandatory `view` value is **`main`** (captured from OF's web app
        XHR; every other value returns 400 'Bad view param')."""
        return self.get_json(
            f"{API_BASE}/vault/lists",
            params={"view": view, "limit": limit, "offset": offset},
        )

    # ── Payouts: balance + eligibility ─────────────────────────

    def payout_balances(self) -> dict:
        """GET /api2/v2/payouts/balances — current balance + breakdown."""
        return self.get_json(f"{API_BASE}/payouts/balances")

    def payout_check_receive(self) -> dict:
        """GET /api2/v2/payouts/check-receive — eligibility for next payout."""
        return self.get_json(f"{API_BASE}/payouts/check-receive")

    # ── Settings ───────────────────────────────────────────────

    def settings_chat(self) -> dict:
        """GET /api2/v2/users/settings/chat — default chat / autoresponder settings."""
        return self.get_json(f"{API_BASE}/users/settings/chat")

    def settings_post(self) -> dict:
        """GET /api2/v2/users/settings/post — default post / watermark settings."""
        return self.get_json(f"{API_BASE}/users/settings/post")

    # ── Scheduling (separate API from /messages/queue) ─────────

    def schedules(self, *, publish_date: str | None = None,
                  publish_date_end: str | None = None,
                  time_zone: str = "Europe/Ljubljana",
                  limit: int = 20) -> dict:
        """GET /api2/v2/schedules — calendar view of scheduled chats + posts.
        Dates are ISO yyyy-mm-dd. OF wraps filters as `filter[publishDate]=...`."""
        params: dict[str, Any] = {"limit": limit}
        if publish_date:     params["filter[publishDate]"] = publish_date
        if publish_date_end: params["filter[publishDateEnd]"] = publish_date_end
        params["filter[timeZone]"] = time_zone
        return self.get_json(f"{API_BASE}/schedules", params=params)

    def schedule_counters(self, *, publish_date: str, publish_date_end: str,
                          time_zone: str = "Europe/Ljubljana") -> dict:
        """GET /api2/v2/schedules/counters — counts by day for a date range."""
        return self.get_json(
            f"{API_BASE}/schedules/counters",
            params={
                "filter[publishDate]": publish_date,
                "filter[publishDateEnd]": publish_date_end,
                "filter[timeZone]": time_zone,
            },
        )

    def schedules_later_chat(self, *, limit: int = 10) -> dict:
        """GET /api2/v2/schedules/later/chat — chats scheduled for the future
        (this is what OF's 'Queue' tab actually displays — distinct from
        /messages/queue which is older API)."""
        return self.get_json(f"{API_BASE}/schedules/later/chat", params={"limit": limit})

    def schedules_later_post(self, *, limit: int = 10) -> dict:
        """GET /api2/v2/schedules/later/post — posts scheduled for the future."""
        return self.get_json(f"{API_BASE}/schedules/later/post", params={"limit": limit})

    # ── User lists (fan lists) ─────────────────────────────────

    def get_lists(self, *, limit: int = 20, offset: int = 0) -> dict:
        """GET /api2/v2/lists — my custom fan lists.

        `format=infinite` forces OF to return the `{list, hasMore}` envelope
        instead of a bare array (which it does without the flag on some
        builds). The chat-folder picker also uses this flag — without it,
        the audience picker would silently get an empty response on
        accounts where OF returns the alternate shape."""
        return self.get_json(
            f"{API_BASE}/lists",
            params={"limit": limit, "offset": offset, "format": "infinite"},
        )

    def get_list(self, list_id: int | str) -> dict:
        """GET /api2/v2/lists/{list_id} — list metadata."""
        return self.get_json(f"{API_BASE}/lists/{list_id}")

    def list_users_in(self, list_id: int | str, *, limit: int = 20, offset: int = 0) -> dict:
        """GET /api2/v2/lists/{list_id}/users — fans in a list."""
        return self.get_json(
            f"{API_BASE}/lists/{list_id}/users",
            params={"limit": limit, "offset": offset},
        )

    # ── Money: statements, transactions, payouts ───────────────

    # statements (payout history list): no direct path found that returns 200 here.
    # Likely creator-only and surfaced via the /payouts/stats time-series instead.
    # Use `earning_stats()` for the data the UI actually displays.

    def transactions(self, *, limit: int = 20, offset: int = 0,
                     type: str | None = None,
                     start: str | None = None, end: str | None = None) -> dict:
        """GET /api2/v2/payouts/transactions — every earning event (tip, sub, ppv).
        `type` like 'tip'|'subscribes'|'message'|'post'|None."""
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if type:  params["type"] = type
        if start: params["start"] = start
        if end:   params["end"] = end
        return self.get_json(f"{API_BASE}/payouts/transactions", params=params)

    def earning_stats(self, *, by: str = "day",
                      start: str | None = None, end: str | None = None) -> dict:
        """GET /api2/v2/payouts/stats — time-series totals.
        `by` in {'day','week','month'}."""
        params: dict[str, Any] = {"by": by}
        if start: params["start"] = start
        if end:   params["end"] = end
        return self.get_json(f"{API_BASE}/payouts/stats", params=params)

    # ── Stories / Highlights ───────────────────────────────────

    def my_stories(self) -> dict:
        """GET /api2/v2/users/me/stories — my own stories."""
        return self.get_json(f"{API_BASE}/users/me/stories")

    def user_stories(self, user_id: str | int) -> dict:
        """GET /api2/v2/users/{user_id}/stories — a creator's stories."""
        return self.get_json(f"{API_BASE}/users/{user_id}/stories")

    def user_highlights(self, user_id: str | int) -> dict:
        """GET /api2/v2/users/{user_id}/stories/highlights — a creator's highlights."""
        return self.get_json(f"{API_BASE}/users/{user_id}/stories/highlights")

    def user_photos(self, user_id: str | int, *, limit: int = 10, offset: int = 0) -> list:
        """GET /api2/v2/users/{user_id}/posts/photos — photo posts only."""
        return self.get_json(f"{API_BASE}/users/{user_id}/posts/photos",
                             params={"limit": limit, "offset": offset})

    def user_videos(self, user_id: str | int, *, limit: int = 10, offset: int = 0) -> list:
        """GET /api2/v2/users/{user_id}/posts/videos — video posts only."""
        return self.get_json(f"{API_BASE}/users/{user_id}/posts/videos",
                             params={"limit": limit, "offset": offset})

    def user_labels_on(self, user_id: str | int) -> dict:
        """GET /api2/v2/users/{user_id}/labels — labels applied to this user's
        content (creator-side organization tags). Discovered via captured XHR."""
        return self.get_json(f"{API_BASE}/users/{user_id}/labels")

    def user_social_buttons(self, user_id: str | int) -> list:
        """GET /api2/v2/users/{user_id}/social/buttons — creator's social media
        links (TikTok, Twitter, IG, etc)."""
        return self.get_json(f"{API_BASE}/users/{user_id}/social/buttons")

    def user_links(self, user_id: str | int) -> list:
        """GET /api2/v2/users/{user_id}/links — creator's outbound link list."""
        return self.get_json(f"{API_BASE}/users/{user_id}/links")

    # ── Promotions / trials / tracking links ───────────────────

    def promotions(self) -> dict:
        """GET /api2/v2/promotions — my subscription promotions."""
        return self.get_json(f"{API_BASE}/promotions")

    def trials(self) -> dict:
        """GET /api2/v2/promotions?type=trial — trial-link campaigns (same endpoint
        as promotions, filtered)."""
        return self.get_json(f"{API_BASE}/promotions", params={"type": "trial"})

    # tracking_links: no direct path found on this account. May be hidden under
    # creator-tier features or surfaced through /init only.

    # ── Chat extras: queue (scheduled), single chat ────────────

    def get_chat(self, chat_id: str | int) -> dict:
        """GET /api2/v2/chats/{chat_id} — full chat details (richer than list item)."""
        return self.get_json(f"{API_BASE}/chats/{chat_id}")

    def scheduled_messages(self, *, limit: int = 10, offset: int = 0) -> dict:
        """GET /api2/v2/messages/queue — messages scheduled for the future."""
        return self.get_json(
            f"{API_BASE}/messages/queue",
            params={"limit": limit, "offset": offset},
        )

    def mass_message_history(self, *, start_date: str, end_date: str,
                             limit: int = 50, offset: int = 0) -> dict:
        """GET /api2/v2/users/me/stats/messages/group — past mass-message
        broadcasts in the date range.

        `start_date` / `end_date` are 'YYYY-MM-DD HH:MM:SS' strings (OF's
        engagement stats page uses local time; we pass through whatever
        the caller hands us). Response shape:
          `{hasMore, items: [{id, date, text, giphyId, mediaCount, media,
            sentCount, viewedCount, isCanceled, canUnsend, unsendSeconds,
            template:'group', ...}]}`

        Each `id` is the queue id — can be passed to cancel_scheduled to
        unsend the broadcast from EVERY recipient. OF reports
        `unsendSeconds=1000000` and `canUnsend=true` on mass sends well
        past the per-chat 24h window — mass-message unsend has no edit
        window in practice."""
        return self.get_json(
            f"{API_BASE}/users/me/stats/messages/group",
            params={
                "startDate": start_date,
                "endDate": end_date,
                "limit": limit,
                "offset": offset,
            },
        )

    # ── Messages: write ─────────────────────────────────────────

    def send_message(self, chat_id: str | int, text: str, *,
                     locked_text: bool = False, price: int | float = 0,
                     media_files: list | None = None,
                     previews: list[int] | None = None,
                     is_forward: bool = False,
                     reply_to_message_id: int | None = None,
                     tagged_users: list[int] | None = None,
                     giphy_id: str | None = None) -> dict:
        """POST /api2/v2/chats/{chat_id}/messages — send a message to one fan
        (or another creator, when both subscribe to each other).

        `chat_id` is the *other user's* numeric id.
        `text` may be plain text or HTML; OF web always wraps plain text in
        `<p>…</p>` before posting, and creator-to-creator sends appear to
        REQUIRE this wrapping — pre-wrap here so the relay's behavior
        matches OF web exactly. (Verified live 2026-05-19: bidirectional
        send fails when fields are omitted, succeeds with this shape.)

        `price` > 0 means a PPV (locks media behind purchase); pair with
        `locked_text=True` to lock the text too. `media_files` are vault
        media IDs previously uploaded via /vault/media — leave empty for
        text-only.

        `reply_to_message_id` is OF's native quote-reply: setting it makes
        the receiving side render the original message as a card above
        this one (matches OF web's "↪ Reply" UX exactly).

        Returns the created message object (same shape as items in
        /chats/.../messages.list)."""
        # GIF sends pass an empty text — OF stores the GIF reference under
        # the top-level `giphyId` field, not in `mediaFiles`. Skip the <p>
        # wrap so the empty string survives literally (OF echoes "text":""
        # back). Verified live 2026-05-23 via captured cURL.
        wrapped_text = "" if (giphy_id and not text) else _wrap_text_as_p(text)
        body = {
            "text": wrapped_text,
            "lockedText": locked_text,
            "mediaFiles": media_files or [],
            "price": price,
            # `previews` is plural — list of vault media ids that ride along
            # as UNLOCKED teasers when `price > 0`. Empty list = everything
            # locked. Verified live on OF web 2026-05-23: sending 3 mediaFiles
            # + price=5 + previews=[id1] yields 1 free thumb + 2 locked.
            "previews": previews or [],
            # Release-form consent fields. OF web always sends these as
            # empty arrays; omitting them seems to be why creator-to-
            # creator sends 400 "Cannot send message to this user".
            "rfTag": [],
            "rfGuest": [],
            "rfPartner": [],
            "isForward": is_forward,
        }
        if giphy_id:
            # Sibling of mediaFiles, not nested inside it. OF echoes it on
            # the response object (`"giphyId":"…","mediaCount":0`). One per
            # send — desktop OF doesn't let you attach multiple GIFs.
            body["giphyId"] = str(giphy_id)
        if reply_to_message_id is not None:
            body["replyToMessageId"] = reply_to_message_id
        # `userTags` is OF's "tag other creators in this message" field —
        # numeric user ids, captured from /self/tagged-friend-users. OF
        # rejects unknown / non-friend ids with 400, so the picker UI is
        # the contract: only ids it surfaces will succeed.
        if tagged_users:
            body["userTags"] = [int(u) for u in tagged_users]
        # Diagnostic — confirms OF receives the previews subset we expect.
        # OF silently ignores previews on free messages, so price=0 + a
        # non-empty previews list is also worth flagging.
        log_of.info(
            "send_message chat=%s price=%s mediaFiles=%r previews=%r userTags=%r giphyId=%r",
            chat_id, price, body["mediaFiles"], body["previews"],
            body.get("userTags") or [], body.get("giphyId"),
        )
        return self.post_json(f"{API_BASE}/chats/{chat_id}/messages", json_body=body)

    # ── Chat message-level writes — VERIFIED LIVE ──────────────
    # All four paths are under /messages/{id}/..., NOT /chats/{cid}/messages/{mid}/...
    # as OFAuth's docs imply. Confirmed by running each against chat 117183 on
    # my own test account (see CHANGES.md section "verified writes").

    def unsend_message(self, message_id: int, with_user_id: int | None = None) -> Any:
        """DELETE /api2/v2/messages/{message_id} — unsend a chat message.
        DESTRUCTIVE: only works inside OF's edit window (~24h).

        OF web sends `{"withUserId": <fan_id>}` in the request body. The
        endpoint accepts an empty body too (legacy callsites), but for
        mass-message bubbles the response only includes the `queue` block
        when withUserId is passed — without it the response is
        `{"success": true}` with no way to discover the parent queue id,
        so callers that may want to follow up with an "unsend from all"
        should always pass it."""
        body: dict | None = {"withUserId": int(with_user_id)} if with_user_id is not None else None
        return self.delete_json(f"{API_BASE}/messages/{message_id}", json_body=body)

    def like_message(self, message_id: int) -> Any:
        """POST /api2/v2/messages/{message_id}/like → {success, isLiked: true}.
        VERIFIED LIVE. You can only like the OTHER party's messages (liking
        your own returns 404 'Message not found')."""
        return self.post_json(f"{API_BASE}/messages/{message_id}/like")

    def unlike_message(self, message_id: int) -> Any:
        """DELETE /api2/v2/messages/{message_id}/like → {success, isLiked: false}.
        VERIFIED LIVE."""
        return self.delete_json(f"{API_BASE}/messages/{message_id}/like")

    def pin_message(self, message_id: int, user_id: int | str) -> Any:
        """POST /api2/v2/messages/{message_id}/pin/user/{user_id} → {success: true}.

        OF's web client uses the longer `/pin/user/{user_id}` form which scopes
        the pin to the chat partner. The shorter `/pin` form also returns 200
        but doesn't reliably make the message appear in the chat's pinned list.
        Captured curl 2026-05-20 confirms the long path is what the live UI
        sends."""
        return self.post_json(f"{API_BASE}/messages/{message_id}/pin/user/{user_id}")

    def unpin_message(self, message_id: int, user_id: int | str) -> Any:
        """DELETE /api2/v2/messages/{message_id}/pin/user/{user_id} → {success: true}.
        Mirrors pin_message — same path, DELETE method."""
        return self.delete_json(f"{API_BASE}/messages/{message_id}/pin/user/{user_id}")

    # ── Chat-level actions (VERIFIED LIVE) ─────────────────────
    # Found via the OnlyFansAPI public catalog. The OF endpoints follow a
    # consistent POST-to-do / DELETE-to-undo pattern.

    def mark_chat_read(self, chat_id: str | int) -> Any:
        """POST /api2/v2/chats/{chat_id}/mark-as-read → {success:true}."""
        return self.post_json(f"{API_BASE}/chats/{chat_id}/mark-as-read")

    def mark_chat_unread(self, chat_id: str | int) -> Any:
        """DELETE /api2/v2/chats/{chat_id}/mark-as-read → {success:true}.
        Same path as mark-read but DELETE — OF's symmetric undo convention."""
        return self.delete_json(f"{API_BASE}/chats/{chat_id}/mark-as-read")

    def mark_all_chats_read(self) -> Any:
        """POST /api2/v2/chats/mark-as-read → {success:true}. Marks every chat as read."""
        return self.post_json(f"{API_BASE}/chats/mark-as-read")

    def mute_chat(self, chat_id: str | int) -> Any:
        """POST /api2/v2/chats/{chat_id}/mute."""
        return self.post_json(f"{API_BASE}/chats/{chat_id}/mute")

    def unmute_chat(self, chat_id: str | int) -> Any:
        """DELETE /api2/v2/chats/{chat_id}/mute."""
        return self.delete_json(f"{API_BASE}/chats/{chat_id}/mute")

    def hide_chat(self, chat_id: str | int) -> Any:
        """POST /api2/v2/chats/{chat_id}/hide — removes from the inbox view.
        The chat still exists; the fan can still message you."""
        return self.post_json(f"{API_BASE}/chats/{chat_id}/hide")

    def unhide_chat(self, chat_id: str | int) -> Any:
        """DELETE /api2/v2/chats/{chat_id}/hide."""
        return self.delete_json(f"{API_BASE}/chats/{chat_id}/hide")

    def chat_media(
        self,
        chat_id: str | int,
        *,
        limit: int = 20,
        offset: int = 0,
        last_id: str | int | None = None,
        skip_users: str | None = None,
        opened: int | None = None,
        purchased: int | None = None,
        from_user: str | int | None = None,
    ) -> dict:
        """GET /api2/v2/chats/{chat_id}/media — messages in this chat that
        contain media.

        OF's gallery tabs back onto this one endpoint with different
        query-param combinations:
          • Default — every media-bearing message (free + PPV, sent +
            received). Useful for "show unsold media" surfaces.
          • `purchased=1` + `from_user=<creator_user_id>` → "Purchased"
            tab. ONLY the PPVs the fan actually paid for, with the
            creator's outbound messages only. This is what the OF web
            UI uses at /my/chats/chat/{id}/gallery/purchased and what we
            want for the FanDrawer's "Sales" section.
          • `opened=1` is a DIFFERENT filter (items the fan has viewed,
            includes free messages). Don't confuse with `purchased=1`.
          • `last_id=<previous nextLastId>` is the cursor; `offset` is
            kept for the legacy paginated form OF still accepts.
        """
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if last_id is not None:
            params["last_id"] = last_id
        if skip_users is not None:
            params["skip_users"] = skip_users
        if opened is not None:
            params["opened"] = opened
        if purchased is not None:
            params["purchased"] = purchased
        if from_user is not None:
            params["from_user"] = from_user
        return self.get_json(f"{API_BASE}/chats/{chat_id}/media", params=params)

    def search_chat(self, chat_id: str | int, query: str) -> list:
        """GET /api2/v2/chats/{chat_id}/messages/search?query=… — search the
        full history of one chat. Returns a flat list of matching message
        IDs (newest first), e.g. [8099903898601, 7813771789577]. Empty
        list if no matches. OF's response is just ids — callers fetch
        previews from the regular messages cache or page back if needed.

        Verified live 2026-05-19 against OF web's chat-search dropdown.
        Param is `query`, NOT `text`; OF rejects the latter silently."""
        return self.get_json(f"{API_BASE}/chats/{chat_id}/messages/search",
                             params={"query": query})

    # ── User-level moderation: block + restrict ────────────────
    # block: hides the chat AND blocks them from messaging you
    # restrict: hides their public profile content from you (you still see chat)
    # Both undo with DELETE on the same path.

    def block_user(self, user_id: str | int) -> Any:
        """POST /api2/v2/users/{user_id}/block. VERIFIED LIVE."""
        return self.post_json(f"{API_BASE}/users/{user_id}/block")

    def unblock_user(self, user_id: str | int) -> Any:
        return self.delete_json(f"{API_BASE}/users/{user_id}/block")

    def restrict_user(self, user_id: str | int) -> Any:
        """POST /api2/v2/users/{user_id}/restrict. VERIFIED LIVE."""
        return self.post_json(f"{API_BASE}/users/{user_id}/restrict")

    def unrestrict_user(self, user_id: str | int) -> Any:
        return self.delete_json(f"{API_BASE}/users/{user_id}/restrict")

    # ── Stories feed (for me as a fan) ─────────────────────────

    def stories_feed(self) -> list:
        """GET /api2/v2/stories — all stories from creators I follow."""
        return self.get_json(f"{API_BASE}/stories")

    def my_stories_archive(self, *, limit: int = 20, offset: int = 0) -> dict:
        """GET /api2/v2/stories/archive — my own archived (expired) stories."""
        return self.get_json(f"{API_BASE}/stories/archive",
                             params={"limit": limit, "offset": offset})

    def stories_items(self, story_map: dict) -> dict:
        """GET /api2/v2/stories/items?map[user_id]=story_id&... — fetch story
        items by a (user_id, story_id) map. This is the missing param that
        made our earlier probe 400. Captured live from OF's home page.

        Example: stories_items({18251483: 116351527, 20353972: 116447253})"""
        from urllib.parse import urlencode
        qs = urlencode({f"map[{uid}]": sid for uid, sid in story_map.items()})
        return self.get_json(f"{API_BASE}/stories/items?{qs}")

    # ── Analytics / charts ─────────────────────────────────────
    # All these accept start/end ISO datetime params. The OF UI calls them
    # with `startDate=YYYY-MM-DD HH:MM:SS` (space-separated, URL-encoded).
    # Default range is 30 days.

    def _date_range_params(self, start: str | None, end: str | None) -> dict:
        from datetime import datetime, timedelta
        if not end: end = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if not start:
            start = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
        return {"startDate": start, "endDate": end}

    def earnings_chart(self, *, start: str | None = None, end: str | None = None,
                       with_total: bool = True) -> dict:
        """GET /api2/v2/earnings/chart — earnings time-series for Statistics > Earnings."""
        params = self._date_range_params(start, end)
        if with_total: params["withTotal"] = "true"
        return self.get_json(f"{API_BASE}/earnings/chart", params=params)

    def posts_chart(self, *, start: str | None = None, end: str | None = None,
                    with_total: bool = True) -> dict:
        """GET /api2/v2/posts/chart — post performance time-series."""
        params = self._date_range_params(start, end)
        if with_total: params["withTotal"] = "true"
        return self.get_json(f"{API_BASE}/posts/chart", params=params)

    def posts_top(self, *, start: str | None = None, end: str | None = None,
                  by: str = "purchases", limit: int = 10) -> dict:
        """GET /api2/v2/posts/top?by=purchases — top performing posts.
        `by` in {purchases,likes,comments,views,...}."""
        params = self._date_range_params(start, end)
        params.update({"by": by, "limit": limit})
        return self.get_json(f"{API_BASE}/posts/top", params=params)

    def my_stats_overview(self, *, start: str | None = None, end: str | None = None,
                          by: str = "visitors") -> dict:
        """GET /api2/v2/users/me/stats/overview — visitors/engagement overview."""
        params = self._date_range_params(start, end)
        params["by"] = by
        return self.get_json(f"{API_BASE}/users/me/stats/overview", params=params)

    def my_stats_top_posts(self, *, start: str | None = None, end: str | None = None,
                           limit: int = 10) -> dict:
        """GET /api2/v2/users/me/stats/top/post — my best-performing posts."""
        params = self._date_range_params(start, end)
        params["limit"] = limit
        params["skip_users"] = "all"
        return self.get_json(f"{API_BASE}/users/me/stats/top/post", params=params)

    def my_profile_stats(self, *, start: str | None = None, end: str | None = None,
                         limit: int = 10) -> dict:
        """GET /api2/v2/users/me/profile/stats — visitor source breakdown
        (Statistics > Reach in OF UI)."""
        params = self._date_range_params(start, end)
        params["limit"] = limit
        return self.get_json(f"{API_BASE}/users/me/profile/stats", params=params)

    def my_start_date(self) -> dict:
        """GET /api2/v2/users/me/start-date-model — when I became a creator.
        Used by OF Statistics to range-cap charts."""
        return self.get_json(f"{API_BASE}/users/me/start-date-model")

    def chargebacks_ratio(self, *, start: str | None = None, end: str | None = None) -> dict:
        """GET /api2/v2/payouts/chargebacks/ratio — chargeback-rate risk metric."""
        return self.get_json(f"{API_BASE}/payouts/chargebacks/ratio",
                             params=self._date_range_params(start, end))

    # ── Bookmarks (the OF "Collections > Bookmarks" tab) ───────

    def bookmarks_all(self, *, limit: int = 10, offset: int = 0) -> dict:
        """GET /api2/v2/posts/bookmarks/all/ — paginated bookmarks feed."""
        return self.get_json(f"{API_BASE}/posts/bookmarks/all/",
                             params={"format": "infinite", "limit": limit, "offset": offset, "skip_users": "all"})

    def bookmark_categories(self, *, limit: int = 10, offset: int = 0) -> dict:
        """GET /api2/v2/posts/bookmarks/categories — bookmark folders/tabs."""
        return self.get_json(f"{API_BASE}/posts/bookmarks/categories",
                             params={"limit": limit, "offset": offset})

    # ── Referrals (creator referral program) ───────────────────

    def referrals_balance(self) -> dict:
        """GET /api2/v2/payments/referrals/balance — referral earnings balance."""
        return self.get_json(f"{API_BASE}/payments/referrals/balance")

    def referral_payouts(self, *, limit: int = 10, offset: int = 0) -> dict:
        """GET /api2/v2/payouts/requests/referral — referral payout history."""
        return self.get_json(f"{API_BASE}/payouts/requests/referral",
                             params={"limit": limit, "offset": offset})

    # ── Notification settings ──────────────────────────────────

    def notification_transports(self) -> dict:
        """GET /api2/v2/users/settings/notifications/transports — push/email/SMS
        notification channel settings."""
        return self.get_json(f"{API_BASE}/users/settings/notifications/transports")

    # ── Per-user social / friends ──────────────────────────────

    def user_friends(self, user_id: str | int, *, limit: int = 10, offset: int = 0) -> dict:
        """GET /api2/v2/users/{user_id}/friends — user's friends list."""
        return self.get_json(f"{API_BASE}/users/{user_id}/friends",
                             params={"limit": limit, "offset": offset})

    def user_friends_pinned(self, user_id: str | int) -> dict:
        """GET /api2/v2/users/{user_id}/friends/pinned — pinned friends."""
        return self.get_json(f"{API_BASE}/users/{user_id}/friends/pinned")

    def user_spotify(self, user_id: str | int) -> dict:
        """GET /api2/v2/users/{user_id}/social/spotify — Spotify integration data."""
        return self.get_json(f"{API_BASE}/users/{user_id}/social/spotify")

    # ── GIF picker (OF proxies Giphy under /giphy/proxy/...) ───
    # Discovered live by clicking the chat-compose "Add GIF" icon.

    def gif_trending(self, *, limit: int = 10, offset: int = 0) -> dict:
        """GET /api2/v2/giphy/proxy/gifs/trending — trending GIFs (Giphy via OF)."""
        return self.get_json(f"{API_BASE}/giphy/proxy/gifs/trending",
                             params={"limit": limit, "offset": offset})

    def gif_search(self, query: str, *, limit: int = 10, offset: int = 0) -> dict:
        """GET /api2/v2/giphy/proxy/gifs/search?q=cat — search Giphy via OF's proxy."""
        return self.get_json(f"{API_BASE}/giphy/proxy/gifs/search",
                             params={"q": query, "limit": limit, "offset": offset})

    # ── Message templates (welcome message + saved replies) ────
    # Captured live via service/capture_settings_pages.py — the
    # /my/settings/messaging page fires GET /messages/templates which
    # returns saved replies + the welcome message (template name
    # "reply_on_subscribe"). Each entry has full media + price.

    def message_templates(self, *, template: str | None = None) -> list:
        """GET /api2/v2/messages/templates — saved replies + welcome message.
        Pass `template='reply_on_subscribe'` to filter to the welcome message."""
        params: dict[str, Any] = {}
        if template:
            params["template"] = template
        return self.get_json(f"{API_BASE}/messages/templates",
                             params=params or None)

    def create_template(self, text: str, *, template: str | None = None,
                        media_files: list | None = None,
                        price: float = 0, locked_text: bool = False) -> Any:
        """POST /api2/v2/messages/templates — create a saved reply.
        Pass `template='reply_on_subscribe'` to set/overwrite the welcome message."""
        body: dict[str, Any] = {
            "text": text, "mediaFiles": media_files or [],
            "price": price, "lockedText": locked_text,
        }
        if template:
            body["template"] = template
        return self.post_json(f"{API_BASE}/messages/templates", json_body=body)

    def update_template(self, template_id: str | int, *,
                        text: str | None = None,
                        media_files: list | None = None,
                        price: float | None = None,
                        locked_text: bool | None = None) -> Any:
        """PUT /api2/v2/messages/templates/{id} — edit a saved reply or the
        welcome message."""
        body: dict[str, Any] = {}
        if text is not None:        body["text"] = text
        if media_files is not None: body["mediaFiles"] = media_files
        if price is not None:       body["price"] = price
        if locked_text is not None: body["lockedText"] = locked_text
        return self.put_json(f"{API_BASE}/messages/templates/{template_id}",
                             json_body=body)

    def delete_template(self, template_id: str | int) -> Any:
        """DELETE /api2/v2/messages/templates/{id}."""
        return self.delete_json(f"{API_BASE}/messages/templates/{template_id}")

    # ── Welcome message (reply_on_subscribe) slot ─────────────
    # OF web doesn't address the welcome slot by numeric id — it POSTs/
    # DELETEs directly to /messages/templates/reply_on_subscribe and uses
    # an extended body shape (rfTag/rfGuest/rfPartner/previews/isForward).
    # The numeric-id PUT/DELETE on the generic template endpoint works
    # less reliably; using the slot URL is what the live UI does.
    #
    # Separately, the master "send welcome to new subs" flag lives on
    # /users/me as `replyOnSubscribe` (PATCH). The template stores text
    # + media; the flag controls whether OF actually fires it.

    def upsert_welcome_template(self, text: str, *,
                                media_files: list | None = None,
                                price: float = 0,
                                locked_text: bool = False,
                                previews: list[int] | None = None) -> Any:
        """POST /api2/v2/messages/templates/reply_on_subscribe — create or
        replace the welcome template. OF treats this as an upsert (same id
        returned across calls). Body shape mirrors the live OF web client."""
        body: dict[str, Any] = {
            "text": text if text.startswith("<") else f"<p>{text}</p>",
            "lockedText": bool(locked_text),
            "mediaFiles": media_files or [],
            "price": float(price or 0),
            "previews": [int(p) for p in (previews or [])],
            "rfTag": [],
            "rfGuest": [],
            "rfPartner": [],
            "isForward": False,
        }
        return self.post_json(
            f"{API_BASE}/messages/templates/reply_on_subscribe",
            json_body=body,
        )

    def delete_welcome_template(self) -> Any:
        """DELETE /api2/v2/messages/templates/reply_on_subscribe — clear
        the welcome slot. Leaves `replyOnSubscribe` alone (OF auto-disables
        on next user-settings load if nothing is set, but the flag itself
        is a separate field)."""
        return self.delete_json(
            f"{API_BASE}/messages/templates/reply_on_subscribe",
        )

    def set_reply_on_subscribe(self, enabled: bool) -> Any:
        """PATCH /api2/v2/users/me with `{replyOnSubscribe: bool}` — the
        master switch for whether OF fires the welcome template on new
        subs. Independent from whether the template content exists."""
        return self.request_json(
            "PATCH", f"{API_BASE}/users/me",
            json_body={"replyOnSubscribe": bool(enabled)},
        )

    # ── Subscription bundles ──────────────────────────────────

    def subscription_bundles(self) -> list:
        """GET /api2/v2/subscriptions/bundles — multi-month bundle pricing."""
        return self.get_json(f"{API_BASE}/subscriptions/bundles")

    def create_bundle(self, *, months: int, price: float,
                      discount: int | None = None) -> Any:
        """POST /api2/v2/subscriptions/bundles. `months` ∈ {3,6,12}, `discount` %."""
        body: dict[str, Any] = {"months": months, "price": price}
        if discount is not None: body["discount"] = discount
        return self.post_json(f"{API_BASE}/subscriptions/bundles",
                              json_body=body)

    def delete_bundle(self, bundle_id: int) -> Any:
        """DELETE /api2/v2/subscriptions/bundles/{id}."""
        return self.delete_json(f"{API_BASE}/subscriptions/bundles/{bundle_id}")

    # ── Promotion campaigns (write side) ──────────────────────
    # /promotions returns every promo type (all / expired / trial / offer)
    # in one paginated list. Writes follow OF's standard CRUD shape.

    def create_promo(self, *, price: float, subscribe_counts: int,
                     subscribe_days: int = 0, message: str = "",
                     type: str = "all") -> Any:
        """POST /api2/v2/promotions — create a promo campaign.
        `subscribe_counts` is the max number of claims; `subscribe_days` is
        the duration of the discounted price (0 = lifetime)."""
        return self.post_json(f"{API_BASE}/promotions", json_body={
            "price": price, "subscribeCounts": subscribe_counts,
            "subscribeDays": subscribe_days,
            "message": message, "type": type,
        })

    def delete_promo(self, promo_id: int) -> Any:
        """DELETE /api2/v2/promotions/{id}."""
        return self.delete_json(f"{API_BASE}/promotions/{promo_id}")

    # ── Tracking links (subscription analytics) ───────────────
    # GET /campaigns lists every tracking link with countSubscribers /
    # countTransitions. Captured live from
    # /my/settings/subscription/tracking-links.

    def tracking_links(self) -> list:
        """GET /api2/v2/campaigns — tracking link entries with transition + sub stats."""
        return self.get_json(f"{API_BASE}/campaigns")

    def create_tracking_link(self, *, name: str, code: int | None = None) -> Any:
        """POST /api2/v2/campaigns — register a new tracking link.
        `name` shows in stats; `code` is the URL suffix int. If `code` omitted
        OF auto-picks the next available."""
        body: dict[str, Any] = {"campaignName": name}
        if code is not None:
            body["campaignCode"] = code
        return self.post_json(f"{API_BASE}/campaigns", json_body=body)

    def delete_tracking_link(self, campaign_id: int) -> Any:
        """DELETE /api2/v2/campaigns/{id}."""
        return self.delete_json(f"{API_BASE}/campaigns/{campaign_id}")

    # ── Free-trial links ──────────────────────────────────────
    # GET /trials returns active trial-link entries; each has a public URL,
    # subscribeDays (free duration), subscribeCounts (cap), claimCounts.

    def trial_links(self) -> list:
        """GET /api2/v2/trials — free-trial link list with usage counts."""
        return self.get_json(f"{API_BASE}/trials")

    def create_trial_link(self, *, name: str, subscribe_days: int = 7,
                          subscribe_counts: int = 1,
                          expired_at: str | None = None) -> Any:
        """POST /api2/v2/trials — issue a new free-trial link.
        `subscribe_days` is the free-access duration; `subscribe_counts` caps
        total claims; `expired_at` ISO 8601 limits the link's validity window."""
        body: dict[str, Any] = {
            "trialLinkName": name,
            "subscribeDays": subscribe_days,
            "subscribeCounts": subscribe_counts,
        }
        if expired_at:
            body["expiredAt"] = expired_at
        return self.post_json(f"{API_BASE}/trials", json_body=body)

    def delete_trial_link(self, trial_id: int) -> Any:
        """DELETE /api2/v2/trials/{id}."""
        return self.delete_json(f"{API_BASE}/trials/{trial_id}")

    # ── Profile writes (bio, display name, location, etc.) ────
    # Captured live via PATCH probe: PATCH /users/me accepts {about, name, …}
    # and returns {success:true}. Round-tripped on this account.

    def update_profile(self, **fields) -> Any:
        """PATCH /api2/v2/users/me — update profile fields.

        Verified fields (live): `about` (bio text). Other accepted keys
        likely include: name (display name), location, website, wishlist
        (URL), instagramUsername, twitterUsername, tiktokUsername — same
        names as the corresponding fields in /users/me response.
        """
        if not fields:
            raise ValueError("at least one field required")
        return self.request_json("PATCH", f"{API_BASE}/users/me", json_body=fields)

    # FAN NOTES + CUSTOM NAME: not native OF endpoints.
    # OnlyFansAPI exposes `PUT /fans/{id}/{notes,custom-name}` but these are
    # **their own service features** backed by their database, not proxied to OF.
    # Direct probing confirms no OF /api2/v2 path returns 200 for these.

    # ── Scheduled / mass messaging ─────────────────────────────

    def schedule_message(self, chat_id: str | int, text: str, *,
                         scheduled_date: str, price: int | float = 0,
                         locked_text: bool = False,
                         media_files: list[int] | None = None,
                         previews: list[int] | None = None,
                         tagged_users: list[int] | None = None) -> Any:
        """POST /api2/v2/messages/queue — schedule a message for future delivery.

        VERIFIED LIVE. The right path is `/messages/queue`, NOT
        `/chats/{id}/messages` with a scheduledDate field — the latter accepts
        the field but sends immediately (OF silently ignores it on that path).

        `scheduled_date` is ISO 8601 with timezone offset, e.g.
        '2026-06-01T18:00:00+00:00'. Returns the queue entry:
        `{id, date, isReady, isDone, total, pending, canUnsend, ...}`."""
        body = {
            "text": text,
            "lockedText": locked_text,
            "mediaFiles": media_files or [],
            "price": price,
            "previews": previews or [],
            "scheduledDate": scheduled_date,
            "userIds": [int(chat_id)],
        }
        if tagged_users:
            body["userTags"] = [int(u) for u in tagged_users]
        return self.post_json(f"{API_BASE}/messages/queue", json_body=body)

    def schedule_mass_message(self, *, text: str, scheduled_date: str,
                              user_ids: list[int] | None = None,
                              user_lists: list[str | int] | None = None,
                              excluded_users: list[int] | None = None,
                              excluded_user_lists: list[str | int] | None = None,
                              price: int | float = 0,
                              locked_text: bool = False,
                              media_files: list[int] | None = None,
                              previews: list[int] | None = None,
                              tagged_users: list[int] | None = None,
                              giphy_id: str | None = None,
                              filters: dict | None = None,
                              online_only: bool = False) -> Any:
        """POST /api2/v2/messages/queue — schedule a MASS message (broadcast).
        Set `user_ids` to specific fans OR `user_lists` for built-in/custom lists
        (e.g. ['fans']). `excluded_user_lists` removes whole lists from the
        target set — the wire field is `excludedLists` (VERIFIED LIVE
        2026-06-12: a 1-fan list broadcast with the fan in an excludedLists
        list resolved to total=0; the old `excludeUserLists` name was
        silently ignored and the fan got the message).

        ⚠️ OF has NO per-user exclusion field on this endpoint —
        `excludedUsers` does not exist and was silently dropped for months.
        `excluded_users` here is honored CLIENT-SIDE only: those ids are
        subtracted from `user_ids` before the call. It cannot remove fans
        from a `user_lists`/online audience — sync such ids into an OF
        custom list and pass it via `excluded_user_lists` instead
        (see audiences.ensure_exclude_list).

        `giphy_id`: forwarded as top-level `giphyId` — matches the chat send
        wire shape. OF web includes GIFs on mass-message broadcasts; we
        mirror the field name and let OF accept/reject."""
        excl_ids = {int(u) for u in (excluded_users or [])}
        body: dict = {
            "text": text,
            "lockedText": locked_text,
            "mediaFiles": media_files or [],
            "price": price,
            "previews": previews or [],
            "scheduledDate": scheduled_date,
            "userIds": [int(u) for u in (user_ids or []) if int(u) not in excl_ids],
            "userLists": user_lists or [],
            "excludedLists": excluded_user_lists or [],
        }
        merged_filters = dict(filters) if filters else {}
        if online_only:
            merged_filters["online"] = 1
        if merged_filters:
            body["filters"] = merged_filters
        if tagged_users:
            body["userTags"] = [int(u) for u in tagged_users]
        if giphy_id:
            body["giphyId"] = str(giphy_id)
        return self.post_json(f"{API_BASE}/messages/queue", json_body=body)

    def cancel_scheduled(self, queue_id: int) -> Any:
        """DELETE /api2/v2/messages/queue/{queue_id} — cancel a scheduled send."""
        return self.delete_json(f"{API_BASE}/messages/queue/{queue_id}")

    def send_mass_message(self, text: str, *,
                          user_lists: list[str | int] | None = None,
                          included_users: list[int] | None = None,
                          excluded_users: list[int] | None = None,
                          excluded_user_lists: list[str | int] | None = None,
                          price: int | float = 0,
                          locked_text: bool = False,
                          media_files: list[int] | None = None,
                          previews: list[int] | None = None,
                          scheduled_date: str | None = None,
                          tagged_users: list[int] | None = None,
                          giphy_id: str | None = None,
                          filters: dict | None = None,
                          online_only: bool = False) -> Any:
        """POST /api2/v2/messages/queue — broadcast to many fans at once.

        VERIFIED LIVE. Same endpoint as schedule_message — without scheduledDate
        it fires immediately. The old `/chats/messages` path is **404** on OF;
        I had it wrong. The right one is /messages/queue with `userLists`
        and/or `userIds` (note: NOT `includedUsers`) in the body.

        Targeting (combine as needed):
          - `user_lists`: built-in names ('fans', 'recent') or custom list ids
          - `included_users`: explicit fan ids
          - `excluded_users`: CLIENT-SIDE only — subtracted from
            `included_users` before the call. OF has NO per-user exclusion
            field on this endpoint (`excludedUsers` doesn't exist; VERIFIED
            LIVE 2026-06-12 — it was silently dropped and excluded fans got
            the blast). To exclude ids from a list/online audience, sync them
            into an OF custom list and pass `excluded_user_lists`
            (see audiences.ensure_exclude_list).
          - `excluded_user_lists`: custom list ids excluded server-side — the
            wire field is `excludedLists` (VERIFIED LIVE 2026-06-12; the old
            `excludeUserLists` name was silently ignored).
          - `online_only=True` (or `filters={'online': 1}`): native
            online-fan targeting — OF resolves the audience to fans currently
            online at send time. VERIFIED LIVE: OF web's "online" broadcast
            (referer /my/chats/send?list=users&online=1) posts exactly
            `{"filters":{"online":1},"userLists":["fans"]}`. Combine with
            `user_lists=['fans']` to blast every online fan.
        `scheduled_date`: ISO 8601 with offset — omit for immediate send."""
        merged_filters = dict(filters) if filters else {}
        if online_only:
            merged_filters["online"] = 1
        excl_ids = {int(u) for u in (excluded_users or [])}
        body: dict = {
            "text": text,
            "lockedText": locked_text,
            "mediaFiles": media_files or [],
            "price": price,
            "previews": previews or [],
            "userLists": user_lists or [],
            "userIds": [int(u) for u in (included_users or []) if int(u) not in excl_ids],
            "excludedLists": excluded_user_lists or [],
        }
        if merged_filters:
            body["filters"] = merged_filters
        if scheduled_date:
            body["scheduledDate"] = scheduled_date
        if tagged_users:
            body["userTags"] = [int(u) for u in tagged_users]
        if giphy_id:
            # Same wire-shape as the chat send: top-level `giphyId`, sibling
            # of `mediaFiles`. OF web includes GIFs on broadcasts; mirror the
            # field so the relay forwards rather than dropping silently.
            body["giphyId"] = str(giphy_id)
        return self.post_json(f"{API_BASE}/messages/queue", json_body=body)

    # ── Posts: write ───────────────────────────────────────────

    def create_post(self, text: str, *,
                    media_files: list[int] | None = None,
                    price: int | float = 0,
                    previews: list[int] | None = None,
                    posted_at: str | None = None,
                    fund_raising_target: float | None = None,
                    voting_due_date: str | None = None,
                    expire_period: int | None = None,
                    tagged_users: list[int] | None = None,
                    giphy_id: str | None = None) -> Any:
        """POST /api2/v2/posts — create a feed post.
        `posted_at` ISO for scheduling. `expire_period` in days for paid posts.
        `previews` = media ids (⊆ media_files) shown FREE as the teaser on a PAID
        post; the wire field is `preview` (singular) — CONFIRMED live 2026-06-22:
        a 21-media $60 post stored exactly the 5 passed ids in its `preview` array,
        the other 16 paywalled.
        `giphy_id` rides along as top-level `giphyId`, mirroring chat sends."""
        body: dict = {
            "text": text,
            "mediaFiles": media_files or [],
            "price": price,
        }
        if previews:            body["preview"] = [int(p) for p in previews]
        if posted_at:           body["postedAt"] = posted_at
        if fund_raising_target: body["fundRaisingTarget"] = fund_raising_target
        if voting_due_date:     body["votingDueDate"] = voting_due_date
        if expire_period is not None: body["expirePeriod"] = expire_period
        if tagged_users:        body["userTags"] = [int(u) for u in tagged_users]
        if giphy_id:            body["giphyId"] = str(giphy_id)
        return self.post_json(f"{API_BASE}/posts", json_body=body)

    def edit_post(self, post_id: int, *,
                  text: str | None = None,
                  price: float | None = None,
                  media_files: list[int] | None = None) -> Any:
        """PUT /api2/v2/posts/{post_id}."""
        body: dict = {}
        if text is not None:        body["text"] = text
        if price is not None:       body["price"] = price
        if media_files is not None: body["mediaFiles"] = media_files
        return self.put_json(f"{API_BASE}/posts/{post_id}", json_body=body)

    def delete_post(self, post_id: int) -> Any:
        """DELETE /api2/v2/posts/{post_id}."""
        return self.delete_json(f"{API_BASE}/posts/{post_id}")

    def like_post(self, post_id: int) -> Any:
        """POST /api2/v2/posts/{post_id}/favorites/{user_id}.
        `user_id` is OF's own format — pass `self.user_id`."""
        return self.post_json(f"{API_BASE}/posts/{post_id}/favorites/{self.user_id}")

    def unlike_post(self, post_id: int) -> Any:
        """DELETE /api2/v2/posts/{post_id}/favorites/{user_id}."""
        return self.delete_json(f"{API_BASE}/posts/{post_id}/favorites/{self.user_id}")

    def comment_on_post(self, post_id: int, text: str) -> Any:
        """POST /api2/v2/posts/{post_id}/comments."""
        return self.post_json(f"{API_BASE}/posts/{post_id}/comments",
                              json_body={"text": text})

    def pin_post(self, post_id: int) -> Any:
        """POST /api2/v2/posts/{post_id}/pin."""
        return self.post_json(f"{API_BASE}/posts/{post_id}/pin")

    def unpin_post(self, post_id: int) -> Any:
        """DELETE /api2/v2/posts/{post_id}/pin."""
        return self.delete_json(f"{API_BASE}/posts/{post_id}/pin")

    # ── Fan lists: write ───────────────────────────────────────

    def create_list(self, name: str) -> Any:
        """POST /api2/v2/lists — create custom fan list."""
        return self.post_json(f"{API_BASE}/lists", json_body={"name": name})

    def rename_list(self, list_id: str | int, name: str) -> Any:
        """PATCH /api2/v2/lists/{list_id} — OF uses PATCH (not PUT) for list rename.
        VERIFIED LIVE. Returns the full updated list object."""
        return self.request_json("PATCH", f"{API_BASE}/lists/{list_id}", json_body={"name": name})

    def delete_list(self, list_id: int) -> Any:
        """DELETE /api2/v2/lists/{list_id}."""
        return self.delete_json(f"{API_BASE}/lists/{list_id}")

    def add_user_to_list(self, list_id: int, user_id: int) -> Any:
        """POST /api2/v2/lists/{list_id}/users/{user_id}."""
        return self.post_json(f"{API_BASE}/lists/{list_id}/users/{user_id}")

    def remove_user_from_list(self, list_id: int, user_id: int) -> Any:
        """DELETE /api2/v2/lists/{list_id}/users/{user_id}."""
        return self.delete_json(f"{API_BASE}/lists/{list_id}/users/{user_id}")

    # ── Labels (fan tags): write ───────────────────────────────

    def add_label_to_user(self, label_id: int, user_id: int) -> Any:
        """POST /api2/v2/labels/{label_id}/users/{user_id}."""
        return self.post_json(f"{API_BASE}/labels/{label_id}/users/{user_id}")

    def remove_label_from_user(self, label_id: int, user_id: int) -> Any:
        """DELETE /api2/v2/labels/{label_id}/users/{user_id}."""
        return self.delete_json(f"{API_BASE}/labels/{label_id}/users/{user_id}")

    # ── Subscriber tools ───────────────────────────────────────

    # ── Fan note + custom name ─────────────────────────────────
    # FOUND via Playwright UI capture (chat 470702183):
    # `PUT /subscriptions/{user_id}` with `{notice}` OR `{displayName}` body.
    # ONE endpoint, TWO fields — clear in OF UI as "fan notes" + "custom name"
    # but they share the same OF API call. The fields can be sent together or
    # separately. Send empty string to clear.

    def set_fan_note(self, user_id: str | int, note: str) -> Any:
        """PUT /api2/v2/subscriptions/{user_id} {"notice": ...} — the private
        creator-side note on a fan. Pass empty string to clear. VERIFIED LIVE."""
        return self.put_json(f"{API_BASE}/subscriptions/{user_id}",
                             json_body={"notice": note})

    def set_fan_custom_name(self, user_id: str | int, name: str) -> Any:
        """PUT /api2/v2/subscriptions/{user_id} {"displayName": ...} — custom
        nickname shown next to the fan's username. Empty string to clear.
        VERIFIED LIVE."""
        return self.put_json(f"{API_BASE}/subscriptions/{user_id}",
                             json_body={"displayName": name})

    def update_subscription(self, user_id: str | int, **fields) -> Any:
        """PUT /api2/v2/subscriptions/{user_id} — generic version of the above.
        Pass `notice=`, `displayName=`, or any other accepted field together."""
        return self.put_json(f"{API_BASE}/subscriptions/{user_id}", json_body=fields)

    # ── Media upload ───────────────────────────────────────────
    # Captured live via Playwright on chat 3266586 (incogniton_fingerprint_test.png
    # resized to 407x1920). Three steps:
    #
    #   1) GET  /api2/v2/vault/media/hash?h={md5}&size={bytes}  → 404 = new,
    #          200 = exists (returns the matching media id, skipping re-upload).
    #   2) POST /api2/v2/upload/signed/create  { key, parts, contentType, secure }
    #          → { putUrl, getUrl }   — both presigned for `of2transcoder.s3-accelerate.amazonaws.com`
    #   3) PUT  {putUrl} with raw file bytes → 200, empty body + S3 etag
    #
    # The `key` field is built CLIENT-SIDE and includes the future vault media
    # id as a path segment, e.g.:
    #   upload/<uuid4>/<12-digit-numeric-id>/<filename>
    # OF picks the numeric id by `Math.floor(Math.random() * 1e12)` (looked at
    # bundle). After the S3 PUT, OF's transcoder auto-creates the vault record
    # asynchronously — no explicit finalize XHR. To use the new media in a
    # message: send `mediaFiles=[<that-numeric-id>]` in /chats/{id}/messages.
    #
    # NB: requires the `curl_cffi.requests` session (self.http) for the OF
    # calls (TLS impersonation) but the S3 PUT works with stock requests too —
    # we use self.http for consistency.

    def vault_media_lookup_hash(self, md5_hex: str, size: int) -> dict | None:
        """GET /api2/v2/vault/media/hash — dedupe check before upload.
        Returns the existing vault media dict if found, None on 404."""
        url = f"{API_BASE}/vault/media/hash"
        params = {"h": md5_hex, "size": size}
        r = self.get(url, params=params)
        if r.status_code == 404:
            return None
        if not r.ok:
            raise OFAPIError(f"{r.status_code} for {r.url}\n{r.text[:500]}", response=r)
        return r.json()

    def request_signed_upload(self, *, key: str, content_type: str,
                              parts: int = 1, secure: bool = False) -> dict:
        """POST /api2/v2/upload/signed/create → returns {putUrl, getUrl}.
        `key` is OF's path-style id: `upload/<uuid>/<media_id>/<filename>`.
        The media id portion is the future vault-media id."""
        return self.post_json(
            f"{API_BASE}/upload/signed/create",
            json_body={
                "key": key,
                "parts": parts,
                "contentType": content_type,
                "secure": secure,
            },
        )

    # KNOWN LIMITATION (2026-05-17):
    # The three-step upload below succeeds end-to-end (S3 PUT returns 200,
    # bytes are stored), but using the returned `media_id` as
    # `mediaFiles=[id]` in a subsequent /chats/{id}/messages call returns
    # 400 "Something wrong with attached media, please try to upload it
    # again". This indicates OF binds the upload to additional state we
    # haven't yet captured — likely either (a) a server-side claim XHR
    # between PUT and send, (b) a WebSocket "media-ready" event whose payload
    # gives the real vault id, or (c) some cookie/CSRF state OF sets during
    # the original /upload/signed/create. Needs a follow-up Playwright
    # capture that walks the full Send-with-fresh-upload path with WebSocket
    # frames recorded too. For now upload_media() is useful for: vault
    # browsing of dedupe-hash lookups, capturing the S3 protocol, and as a
    # building block for the eventual full flow.

    def convert_register(self, *, s3_etag: str, s3_location: str, s3_key: str,
                         s3_bucket: str, filename: str, secure: bool = False,
                         watermark_text: str | None = None,
                         watermark_position: str = "bottom_right",
                         upload_args: dict | None = None) -> dict:
        """POST https://convert.onlyfans.com/file/upload (the metadata-claim
        step OF JS performs after a successful S3 PUT, on dedupe miss).

        Sends FormData with `file[ETag/Location/Key/Bucket/name/secure]` +
        the preset fields from /users/me.upload.geoUploadArgs, plus an
        optional watermark. Response carries `{processId, host, sourceUrl,
        extra, files, ...}` — those fields go verbatim into the next
        /chats/{id}/messages or /posts body's `mediaFiles[].`

        Important: this endpoint runs with `withCredentials: false` in OF's
        JS — no cookies. It does require Origin: https://onlyfans.com and a
        Referer; we send both via curl_cffi's chrome-impersonation defaults
        plus an explicit Origin.
        """
        try:
            from curl_cffi import CurlMime
        except ImportError as e:
            raise RuntimeError("curl_cffi.CurlMime missing — pip install curl_cffi") from e

        args = upload_args or {}
        mp = CurlMime()
        # Preset / control fields (booleans become "true"/"false" strings —
        # FormData semantics, OF's JS does Object.entries(formdata) so we
        # match by stringifying truthy → "true").
        for k, v in (args.items() if args else ()):
            if isinstance(v, dict):
                for sub_k, sub_v in v.items():
                    mp.addpart(name=f"{k}[{sub_k}]", data=str(sub_v))
            elif isinstance(v, bool):
                mp.addpart(name=k, data="true" if v else "false")
            elif v is not None:
                mp.addpart(name=k, data=str(v))

        # The file reference — points at the S3 object we just PUT.
        mp.addpart(name="file[ETag]", data=f'"{s3_etag}"')
        mp.addpart(name="file[Location]", data=s3_location)
        mp.addpart(name="file[Key]", data=s3_key)
        mp.addpart(name="file[Bucket]", data=s3_bucket)
        mp.addpart(name="file[name]", data=filename)
        mp.addpart(name="file[secure]", data="true" if secure else "false")

        # Optional watermark (OF JS appends only if user opts in)
        if watermark_text:
            mp.addpart(name="watermark[text]", data=watermark_text)
            mp.addpart(name="watermark[position]", data=watermark_position)

        url = "https://convert.onlyfans.com/file/upload"
        r = self._proxy_retry(lambda: self.http.post(
            url, multipart=mp,
            headers={
                "Origin": "https://onlyfans.com",
                "Referer": "https://onlyfans.com/",
                "Accept": "application/json, text/plain, */*",
                "Sec-Fetch-Site": "same-site",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Dest": "empty",
                "User-Agent": self.user_agent,
            },
            timeout=max(self.timeout_s, 60),
        ), what="convert-upload")
        if not r.ok:
            raise OFAPIError(f"convert register failed: {r.status_code} {r.text[:400]}", response=r)
        return r.json()

    def upload_media(self, file_path: str | Path, *,
                     content_type: str | None = None,
                     check_dedupe: bool = True,
                     watermark_text: str | None = None) -> dict:
        """Upload a single file and register it with OF. Returns either:

        Dedupe-hit shape (file already in vault):
          {ready: true, vault_id: <int>, deduped: true, send_with: [<vault_id>], ...}

        Fresh-claim shape (file new, claim registered):
          {ready: true, vault_id: null, deduped: false,
           send_with: [{processId, host, name, ...}],  # use this in mediaFiles
           process_id, host, source_url, ...}

        On any failure: ready=false plus a `note` explaining what to do.

        Full flow:
          1. md5 + size → GET /vault/media/hash (dedupe). Hit → done with `vault_id`.
          2. Miss → POST /upload/signed/create (returns presigned S3 putUrl).
          3. PUT bytes to S3.
          4. POST https://convert.onlyfans.com/file/upload with FormData
             pointing at the S3 object (NO bytes — just etag/Location/Key/
             Bucket/name + preset fields from /users/me.upload.geoUploadArgs).
             Response contains {processId, host, sourceUrl, extra, ...} —
             these go verbatim into mediaFiles in subsequent send/post calls.

        Caller must pass `mediaFiles=result["send_with"]` straight to
        send_message() / create_post() / etc. Don't try to interpret it.
        """
        import hashlib, mimetypes, time, uuid as _uuid, random
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(str(path))
        data = path.read_bytes()
        md5_hex = hashlib.md5(data).hexdigest()
        size = len(data)
        ct = content_type or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        filename = path.name

        # Step 1: dedupe — if OF already has this file's bytes, we're done.
        if check_dedupe:
            existing = self.vault_media_lookup_hash(md5_hex, size)
            if existing:
                vault_id = existing.get("id") or existing.get("mediaId")
                return {
                    "vault_id": vault_id,
                    "ready": True,
                    "send_with": [vault_id],
                    "upload_key": None,
                    "etag": md5_hex,
                    "size": size,
                    "filename": filename,
                    "deduped": True,
                    "existing": existing,
                    "note": f"dedupe hit — vault_id {vault_id}",
                }

        # Step 2: request presigned PUT URL.
        # OF's JS builds the key as `upload/<uuid4>/<Math.random()*Date.now()>/<filename>`.
        random_id = int(random.random() * (time.time() * 1000))
        key = f"upload/{_uuid.uuid4()}/{random_id}/{filename}"
        signed = self.request_signed_upload(key=key, content_type=ct, parts=1, secure=False)
        put_url = signed["putUrl"]

        # Step 3: PUT bytes to S3 (presigned — no auth needed beyond URL).
        # Through _proxy_retry like every other egress call: a CONNECT-403 blip
        # fails before any body is sent, and the PUT targets a fixed presigned
        # key, so retrying the same upload is idempotent.
        put_r = self._proxy_retry(lambda: self.http.put(
            put_url, data=data,
            headers={"Content-Type": ct},
            timeout=max(self.timeout_s, 60),
        ), what="s3-upload")
        if not put_r.ok:
            raise OFAPIError(f"S3 PUT failed: {put_r.status_code} {put_r.text[:300]}", response=put_r)
        etag = put_r.headers.get("etag", "").strip('"') or md5_hex

        # Step 4: POST metadata to convert.onlyfans.com/file/upload to
        # register a claim. Response gives processId/host/extra which go
        # straight into the next send/post body's mediaFiles entry.
        me = self.me()
        upload_args = (me.get("upload") or {}).get("geoUploadArgs") or {
            "preset": "of_beta",
            "needThumbs": True,
            "additional": {"user": self.user_id},
        }
        # Strip the s3-accelerate hostname out of putUrl for `file[Location]`
        # — OF's JS uses the full presigned URL (we have signed.putUrl).
        # `file[Bucket]` is parsed from the host. Same defaults as the JS.
        from urllib.parse import urlparse
        s3_host = urlparse(put_url).netloc          # "of2transcoder.s3-accelerate.amazonaws.com"
        s3_bucket = s3_host.split(".")[0]           # "of2transcoder"
        # OF's JS strips the query string from Location in the form
        s3_location = put_url.split("?")[0]

        try:
            claim = self.convert_register(
                s3_etag=etag, s3_location=s3_location,
                s3_key=key, s3_bucket=s3_bucket,
                filename=filename, secure=False,
                watermark_text=watermark_text,
                upload_args=upload_args,
            )
        except OFAPIError as e:
            # Convert step failed — bytes are in S3 but no claim registered.
            return {
                "vault_id": None,
                "ready": False,
                "send_with": None,
                "upload_key": key,
                "etag": etag,
                "size": size,
                "filename": filename,
                "deduped": False,
                "existing": None,
                "note": f"bytes in S3 but claim failed: {e}",
            }

        # Build the mediaFiles entry the same way OF's JS does:
        # `{processId, host, thumbId, name, extra}`  (thumbId optional)
        send_with = {
            "processId": claim.get("processId"),
            "host": claim.get("host"),
            "name": filename,
        }
        if "extra" in claim:
            send_with["extra"] = claim["extra"]
        # thumbId optional — fill if present
        if claim.get("thumbs"):
            send_with["thumbId"] = (claim["thumbs"][0] or {}).get("id")

        return {
            "vault_id": None,           # OF assigns after a successful Send
            "ready": True,
            "send_with": [send_with],
            "upload_key": key,
            "etag": etag,
            "size": size,
            "filename": filename,
            "deduped": False,
            "claim": claim,             # full converter response (for debugging)
            "note": f"claim registered — processId {send_with.get('processId')}",
        }

    # ── Stories ────────────────────────────────────────────────
    # Captured live (HAR: "pick image from vault and upload it as story").
    # KEY FINDING: /stories does NOT take a vault media id. Even when the UI
    # picks an existing vault image, OF re-runs the FULL fresh-upload pipeline
    # (signed/create → S3 PUT → convert.onlyfans.com/file/upload) and posts
    # the convert claim — NOT the vault id. So "post from vault/CDN" really
    # means: fetch the image bytes, push them through upload_media(), then
    # hand the resulting `send_with` (`[{processId, host, name, extra}]`) to
    # create_story(). Observed wire body:
    #   POST /api2/v2/stories
    #   {"mediaFiles":[{processId,name,host,extra}],
    #    "rfTag":[],"rfGuest":[],"rfPartner":[]}

    # Overlay geometry — the EXACT values the OF web editor emitted (HAR
    # "storiewith added text question and tag2.har"). We replay them verbatim
    # so each overlay lands center-ish, "as is by default", just like the
    # editor produced. Coordinates are in the 192 x 347 canvas space.
    _STORY_CANVAS = {"canvasWidth": 192, "canvasHeight": 347}
    _STORY_TEXT_PRESET = {
        "left": 19.58615311, "top": 40.55326425,
        "scale": 1.7993021042613935, "angle": 0,
        "color": "#ffffff", "fontSize": "19.88px", "fontWeight": "400",
        "fontFamily": "Roboto", "textAlign": "left", "bgColor": "#00000000",
        "type": "text",
        "textWidth": 117.31449719784288, "textHeight": 65.56034611398962,
        "zIndex": 2,
    }
    _STORY_MENTION_PRESET = {
        "left": 9.5119356, "top": 29.47480311,
        "scale": 1.7993021042613935, "angle": 0,
        "color": "#0091ea", "fontSize": "19.88px", "fontWeight": "500",
        "fontFamily": "Roboto", "textAlign": "left", "bgColor": "#ffffff",
        "type": "mention",
        "textWidth": 138.00647139684892, "textHeight": 192.78683533678753,
        "zIndex": 3,
    }
    _STORY_QUESTION_PRESET = {
        "left": 20.60537168, "top": 38.39384914, "angle": 0,
        "width": 115.19999999613947, "height": 71.70088386428068,
        "color": "#FFFFFF", "zIndex": 4,
    }

    def create_story(self, media_files: list, *,
                     caption: str | None = None,
                     mention: str | None = None,
                     question: str | None = None) -> Any:
        """POST /api2/v2/stories — publish a story from already-prepared media,
        optionally with a text caption, an @mention tag, and a question sticker.

        `media_files` is the `mediaFiles` array verbatim — pass the
        `send_with` list returned by upload_media() (fresh-claim shape
        `[{processId, host, name, extra}]`). Bare vault ids are NOT accepted
        by this endpoint (unlike posts/messages), so always feed it a convert
        claim. rf* arrays mirror posts/creator-DMs and must be present.

        Overlays (`caption`/`mention`/`question`) reuse the captured editor
        geometry (see the *_PRESET dicts). When any overlay is present we also
        send `canvasWidth/Height` and a `mediaEditorFiles` entry per media
        (keyed by processId), exactly as OF's JS does."""
        body: dict = {
            "mediaFiles": media_files,
            "rfTag": [],
            "rfGuest": [],
            "rfPartner": [],
        }

        # texts is an index-keyed map ("0","1",...). Caption goes first (z2),
        # mention second (z3) — same ordering OF's editor emitted.
        texts: dict = {}
        if caption:
            texts[str(len(texts))] = {**self._STORY_TEXT_PRESET, "text": caption}
        if mention:
            handle = mention if mention.startswith("@") else f"@{mention}"
            texts[str(len(texts))] = {**self._STORY_MENTION_PRESET, "text": handle}

        has_overlay = bool(texts) or bool(question)
        if has_overlay:
            body.update(self._STORY_CANVAS)
        if question:
            body["question"] = {**self._STORY_QUESTION_PRESET, "text": question}
        if texts:
            body["texts"] = texts
        if has_overlay:
            editor: dict = {}
            for mf in media_files:
                pid = mf.get("processId") if isinstance(mf, dict) else None
                if pid:
                    editor[pid] = {"baseFile": {}, "stickers": []}
            if editor:
                body["mediaEditorFiles"] = editor

        return self.post_json(f"{API_BASE}/stories", json_body=body)

    def delete_story(self, story_id: int) -> Any:
        """DELETE /api2/v2/stories/{story_id} — remove one of my stories.
        Story dicts carry `canDelete: true` for ones I own. Used by the
        unsend_messages story cleanup leg + the auto_stories automation."""
        return self.delete_json(f"{API_BASE}/stories/{story_id}")

    def post_story(self, file_path: str | Path, *,
                   content_type: str | None = None,
                   watermark_text: str | None = None,
                   caption: str | None = None,
                   mention: str | None = None,
                   question: str | None = None) -> Any:
        """Upload a local file and publish it as a story in one call.

        Forces the fresh-upload path (check_dedupe=False): /stories only
        accepts a convert claim, and a dedupe hit returns a bare vault id
        that stories rejects."""
        up = self.upload_media(
            file_path, content_type=content_type,
            check_dedupe=False, watermark_text=watermark_text,
        )
        if not up.get("ready") or not up.get("send_with"):
            raise OFAPIError(f"story media not ready: {up.get('note')}")
        return self.create_story(
            up["send_with"], caption=caption, mention=mention, question=question,
        )

    def post_story_from_url(self, media_url: str, *,
                            content_type: str | None = None,
                            watermark_text: str | None = None,
                            caption: str | None = None,
                            mention: str | None = None,
                            question: str | None = None) -> Any:
        """Download an image (vault CDN url or any image url) and publish it
        as a story. This is the "post from vault / from CDN" path: a vault
        item's `files.full.url` (or a thumbs/cdn url) is fetched, re-uploaded
        through OF's convert pipeline, then posted."""
        import tempfile, os, mimetypes
        r = self._proxy_retry(
            lambda: self.http.get(media_url, timeout=max(self.timeout_s, 60)),
            what="cdn-download",
        )
        if not r.ok:
            raise OFAPIError(f"download failed: {r.status_code} for {media_url[:120]}")
        data = r.content or b""
        if not data:
            raise OFAPIError(f"download empty for {media_url[:120]}")
        ct = content_type or r.headers.get("content-type", "").split(";")[0].strip() \
            or "image/jpeg"
        suffix = mimetypes.guess_extension(ct) or ".jpg"
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                tmp.write(data)
                tmp_path = tmp.name
            return self.post_story(
                tmp_path, content_type=ct, watermark_text=watermark_text,
                caption=caption, mention=mention, question=question,
            )
        finally:
            if tmp_path:
                try: os.unlink(tmp_path)
                except OSError: pass

    # ── Tips ───────────────────────────────────────────────────

    def send_tip(self, user_id: int, amount: float, *, message: str = "") -> Any:
        """POST /api2/v2/tips — send a tip to a creator.
        Beware: this MOVES MONEY. Code path exists; deliberately not live-tested."""
        return self.post_json(f"{API_BASE}/tips",
                              json_body={"userId": user_id, "amount": amount, "message": message})
